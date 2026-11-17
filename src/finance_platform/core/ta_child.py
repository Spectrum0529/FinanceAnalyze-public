"""Trusted initial-case child controls. Native Graph is called only by run()."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import threading


class Controls:
    def __init__(self, packet, channel, identity, helpers):
        self.packet, self.channel, self.identity, self.helpers = packet, channel, identity, helpers
        self.admission = packet["admission"]
        self.thread_starts = self.sqlite_connects = 0
        self.stop = None
        self.snapshot = None
        self.approved_thread = None
        self.native_phase = False
        self.dormant_executor = None
        self.dormant_initialized = False
        self.dormant_submissions = 0
        self.background_import_executor = None
        self.background_initialized = False

    def install_sources(self, packet):
        rows = json.loads(Path(packet["pins"]["sources"]["path"]).read_bytes())["guard_sources"]
        self.sources = {row["path"]: row for row in rows}
        for row in rows:
            self.helpers.verify_source(row,packet["site_root"])

    def source(self, frame, *, module=None):
        path = str(Path(frame.f_code.co_filename).absolute())
        row = self.sources.get(path)
        if row is None or (module is not None and frame.f_globals.get("__name__") != module):
            self.stop("host.source", "UNREVIEWED_EXECUTION_SOURCE")
        _phase = getattr(self, "_phase_diag", None)
        _phase_token = _phase.source_enter(frame) if self.native_phase and _phase is not None else None
        self.helpers.verify_source(row,self.packet["site_root"])
        if _phase_token is not None:
            _phase.source_return(frame, _phase_token)
        return row

    def verify_runtime_identity(self, worker, worker_sha, lock_sha):
        pins = self.packet["pins"]
        return (Path(sys.executable).resolve() == Path(pins["base_python"]["path"]).resolve()
            and Path(self.identity["actual_image"]).resolve() == Path(pins["base_python"]["path"]).resolve()
            and Path(self.identity["actual_python_dll"]).resolve() == Path(pins["base_dll"]["path"]).resolve()
            and self.identity["actual_self_in_held_job"] is True
            and Path(worker).resolve() == Path(self.packet["venv_prefix"]).resolve()
            and worker_sha == pins["worker_python"]["sha256"] and lock_sha == pins["lock"]["sha256"]
            and all(self.helpers.verify_pin(pins[key]) for key in ("worker_python", "lock", "base_python", "base_dll")))

    def verify_current_binding(self, admission):
        if admission != self.admission or admission["case"] not in {"one_round", "two_round"}:
            self.stop("host.binding", "UNRELEASED_CASE_OR_ADMISSION_CHANGED")
        self.channel.exchange("VERIFY", {"admission_hash":self.helpers.digest(admission)})

    def require_active(self):
        self.channel.exchange("ACTIVE", {"admission_hash":self.helpers.digest(self.admission)})

    def assert_no_unapproved_effects(self):
        return self.snapshot()

    def cancel_current(self, job_id, fencing_token):
        # Initial one/two-round release does not admit the running_cancel case.
        self.stop("host.case", "RUNNING_CANCEL_CASE_NOT_RELEASED")

    def preserve_case_failure(self, case, trace, consumed):
        if case != self.admission["case"] or len(trace) > 4096:
            self.stop("host.failure", "FAILURE_SCOPE_INVALID")
        self.channel.exchange("FAILURE", {"case":case, "trace":trace, "consumed":consumed,
                                         "checkpoint_action":"RETAIN_AFTER_NATIVE_WRITERS_CLOSE"})

    def allow_sqlite(self, values, frame):
        expected = Path(self.packet["attempt_root"])/"cache/checkpoints/SYNTHA.db"
        # The only audit allowance is the actual upstream source's two literal
        # connect calls. No URI, :memory:, arbitrary DB or application core DB.
        if (not self.native_phase or type(values) is not tuple or len(values) != 1 or type(values[0]) is not str
                or Path(values[0]).absolute() != expected
                or frame.f_globals.get("__name__") != "tradingagents.graph.checkpointer"
                or frame.f_code.co_name not in {"get_checkpointer", "clear_checkpoint"}):
            self.stop("sqlite3.connect", "NON_OWNED_NATIVE_CHECKPOINT_DENIED")
        self.source(frame, module="tradingagents.graph.checkpointer")
        for item in (expected, *expected.parents):
            if item.exists() and (item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0)&0x400
                    or (item.is_file() and item.lstat().st_nlink != 1)):
                self.stop("sqlite3.connect", "CHECKPOINT_ALIAS_DENIED")
        self.sqlite_connects += 1
        if self.sqlite_connects > 8:
            self.stop("sqlite3.connect", "NATIVE_CHECKPOINT_OPEN_BUDGET")

    def allow_graph(self, frame, instance):
        allowed = {"tradingagents.graph.trading_graph": {"TradingAgentsGraph"},
            "tradingagents.graph.setup": {"GraphSetup"}, "tradingagents.graph.propagation": {"Propagator"},
            "tradingagents.graph.conditional_logic": {"ConditionalLogic"},
            "langgraph.graph.state": {"StateGraph", "CompiledStateGraph"},
            "langgraph.graph.graph": {"Graph", "CompiledGraph"}}
        cls = type(instance)
        row = self.source(frame)
        if not self.native_phase or cls.__name__ not in allowed.get(cls.__module__, set()) or row["role"] != "graph":
            self.stop("profile.constructor", "NON_EXACT_OFFICIAL_GRAPH_CONSTRUCTOR")

    def allow_executor(self, frame):
        row = self.source(frame, module="concurrent.futures.thread")
        module = sys.modules.get("concurrent.futures.thread")
        base = Path(self.packet["pins"]["base_python"]["path"]).parent
        if (module is None or frame.f_globals is not module.__dict__
                or Path(row["path"]) != base/"Lib/concurrent/futures/thread.py"
                or row.get("role") != "thread"
                or row["sha256"] != "fe940a20adc4d3e66a8ea42a4c7e6c16957b011e8ada50c4e7a7bddbcbbffa0d"):
            self.stop("profile.thread", "EXACT_EXECUTOR_CONSTRUCTOR_SOURCE_REQUIRED")
        pool, work = module.ThreadPoolExecutor, module._WorkItem
        instance = frame.f_locals.get("self")
        # _WorkItem stores one queued call. It does not construct an executor
        # and has no max_workers argument. Its exact class/code is still bound.
        if (frame.f_code is work.__init__.__code__ and type(instance) is work
                and frame.f_code.co_qualname == "_WorkItem.__init__"
                and frame.f_code.co_firstlineno == 48):
            return
        if (frame.f_code is not pool.__init__.__code__
                or frame.f_code.co_qualname != "ThreadPoolExecutor.__init__"
                or frame.f_code.co_firstlineno != 127
                or pool not in type(instance).__mro__):
            self.stop("profile.thread", "EXACT_EXECUTOR_CONSTRUCTOR_REQUIRED")
        if self._exact_background_import(frame):
            if self.background_import_executor is not None:
                self.stop("profile.thread", "REPEATED_BACKGROUND_IMPORT_EXECUTOR")
            self.background_import_executor=frame.f_locals["self"]
            self._executor_evidence("expected_background_executor_before_init.r06.json",{
                "classification":"EXACT_LANGSMITH_BACKGROUND_MODULE_CONSTRUCTOR_ONLY",
                "phase":"IMPORT","observed_frozen_max_workers":32,"constructor_body_completed":False,
                "caller_module":"langsmith._internal._background_thread","caller_first_line":1,"caller_line":37,
                "cpu_count":"Original stdlib function origin checked, never replaced or called by controls",
                "native_executor_workers":1,"background_activity_permitted":False})
            return
        if self._exact_dormant_import(frame):
            if self.dormant_executor is not None:
                self.stop("profile.thread", "REPEATED_DORMANT_IMPORT_EXECUTOR")
            self.dormant_executor = frame.f_locals["self"]
            self._executor_evidence("expected_dormant_executor_before_init.r05.json",{
                "classification":"EXACT_LANGSMITH_EXPECT_IMPORT_CONSTRUCTOR_ONLY",
                "phase":"IMPORT","max_workers":3,"constructor_body_completed":False,
                "native_executor_workers":1,"background_activity_permitted":False,
                "frames":[{"module":"concurrent.futures.thread","function":"__init__","first_line":127},
                    {"module":"langsmith._expect","function":"__init__","first_line":259,"line":261},
                    {"module":"langsmith._expect","function":"<module>","first_line":1,"line":458}]})
            return
        if not self.native_phase or frame.f_locals.get("max_workers") != 1:
            self._executor_denial_evidence(frame)
            self.stop("profile.thread", "EXECUTOR_MUST_USE_NATIVE_MAX_CONCURRENCY_ONE")

    def _exact_background_import(self, frame):
        if self.native_phase:
            return False
        values=frame.f_locals
        cls=type(values.get("self"))
        caller=frame.f_back
        base=Path(self.packet["pins"]["base_python"]["path"]).parent
        row=self.source(frame,module="concurrent.futures.thread")
        if (Path(row["path"])!=base/"Lib/concurrent/futures/thread.py"
                or row["sha256"]!="fe940a20adc4d3e66a8ea42a4c7e6c16957b011e8ada50c4e7a7bddbcbbffa0d"
                or frame.f_code.co_name!="__init__" or frame.f_code.co_firstlineno!=127
                or type(values.get("max_workers")) is not int or values["max_workers"]!=32
                or values.get("thread_name_prefix")!="" or values.get("initializer") is not None
                or type(values.get("initargs")) is not tuple or values["initargs"]!=()
                or cls is not frame.f_globals.get("ThreadPoolExecutor")
                or caller is None or caller.f_globals.get("__name__")!="langsmith._internal._background_thread"
                or caller.f_code.co_name!="<module>" or caller.f_code.co_firstlineno!=1 or caller.f_lineno!=37):
            return False
        expected=Path(self.packet["site_root"])/"langsmith/_internal/_background_thread.py"
        source=self.source(caller,module="langsmith._internal._background_thread")
        cpu=caller.f_globals.get("cpu_count")
        code=getattr(getattr(cpu,"__func__",None),"__code__",None)
        cpu_path=str(base/"Lib/multiprocessing/context.py")
        cpu_source=self.sources.get(cpu_path)
        if (Path(source["path"])!=expected or source["role"]!="import-idle-executor"
                or source["sha256"]!="1f26de2c29ebbfc838d4f3686523aca1e3867770d4ff4f6706c04bf85c13ca44"
                or getattr(caller.f_globals.get("cf"),"ThreadPoolExecutor",None) is not cls
                or getattr(cpu,"__module__",None)!="multiprocessing.context" or getattr(cpu,"__name__",None)!="cpu_count"
                or code is None or str(Path(code.co_filename).absolute())!=cpu_path or code.co_firstlineno!=41
                or cpu_source is None or cpu_source["sha256"]!="63f15d3169f4453eb80af4ce673a193a0670941206673ab5192ea16a17e8419f"):
            return False
        self.helpers.verify_source(cpu_source,self.packet["site_root"])
        return True

    def _executor_evidence(self, name, value):
        try:
            path=Path(self.packet["attempt_root"])/"evidence"/name
            with path.open("x",encoding="utf-8") as stream:
                json.dump(value,stream,sort_keys=True,allow_nan=False)
        except BaseException:
            self.stop("profile.thread", "EXECUTOR_EVIDENCE_WRITE_FAILED")

    def _exact_dormant_import(self, frame):
        """A fixed dormant object, not a max_workers3 execution allowance."""
        if self.native_phase:
            return False
        std_row=self.source(frame,module="concurrent.futures.thread")
        base=Path(self.packet["pins"]["base_python"]["path"]).parent
        if (Path(std_row["path"])!=base/"Lib/concurrent/futures/thread.py"
                or std_row["sha256"]!="fe940a20adc4d3e66a8ea42a4c7e6c16957b011e8ada50c4e7a7bddbcbbffa0d"):
            return False
        values=frame.f_locals
        instance=values.get("self")
        cls=type(instance)
        caller=frame.f_back
        module_frame=None if caller is None else caller.f_back
        if (frame.f_code.co_name!="__init__" or frame.f_code.co_firstlineno!=127
                or type(values.get("max_workers")) is not int or values["max_workers"]!=3
                or values.get("thread_name_prefix")!="" or values.get("initializer") is not None
                or type(values.get("initargs")) is not tuple or values["initargs"]!=()
                or cls.__name__!="ContextThreadPoolExecutor" or cls.__module__!="langsmith.utils"
                or frame.f_globals.get("ThreadPoolExecutor") not in cls.__mro__
                or caller is None or module_frame is None):
            return False
        for current,function,first,line in ((caller,"__init__",259,261),(module_frame,"<module>",1,458)):
            if (current.f_globals.get("__name__")!="langsmith._expect"
                    or current.f_code.co_name!=function or current.f_code.co_firstlineno!=first
                    or current.f_lineno!=line):
                return False
            row=self.source(current,module="langsmith._expect")
            if (row["role"]!="import-idle-executor"
                    or Path(row["path"])!=Path(self.packet["site_root"])/"langsmith/_expect.py"
                    or row["sha256"]!="6aea345829d91a1c66429f8fc9012545570585b0c9a7941cbabb3af2e5d4cae3"):
                return False
        utils=caller.f_globals.get("ls_utils")
        expect_class=caller.f_globals.get("_Expect")
        utils_path=str(Path(self.packet["site_root"])/"langsmith/utils.py")
        utils_row=self.sources.get(utils_path)
        if (getattr(utils,"ContextThreadPoolExecutor",None) is not cls
                or expect_class is None or type(caller.f_locals.get("self")) is not expect_class
                or expect_class.__name__!="_Expect" or expect_class.__module__!="langsmith._expect"
                or caller.f_locals.get("client") is not None
                or module_frame.f_globals.get("_Expect") is not expect_class
                or utils_row is None or utils_row["role"]!="import-idle-executor"
                or utils_row["sha256"]!="1850e29cf11ae6b6989d57e0f2ce2ee1b9957fcd0b9fa495a04243ff7be446d2"):
            return False
        self.helpers.verify_source(utils_row,self.packet["site_root"])
        return True

    def executor_return(self, frame, returned):
        instance=frame.f_locals.get("self")
        background=instance is self.background_import_executor and instance is not None
        if (instance is not self.dormant_executor and not background) or instance is None:
            return
        self.source(frame,module="concurrent.futures.thread")
        initialized=self.background_initialized if background else self.dormant_initialized
        maximum=32 if background else 3
        if (self.native_phase or initialized or returned is not None
                or getattr(instance,"_max_workers",None)!=maximum or getattr(instance,"_threads",None)!=set()
                or getattr(instance,"_initializer",None) is not None or getattr(instance,"_initargs",None)!=()
                or getattr(instance,"_shutdown",None) is not False):
            self.stop("profile.thread", "DORMANT_EXECUTOR_POSTCONDITION_FAILED")
        if background:self.background_initialized=True
        else:self.dormant_initialized=True
        self._executor_evidence("expected_background_executor_after_init.r06.json" if background else "expected_dormant_executor_after_init.r05.json",{
            "classification":"EXACT_DORMANT_CONSTRUCTOR_COMPLETED_NO_WORKERS",
            "constructor_body_completed":True,"max_workers":maximum,"worker_set_size":0,
            "submissions":0,"native_executor_workers":1,"background_activity_permitted":False})

    def block_dormant_activity(self, frame):
        instance=frame.f_locals.get("self")
        held=instance is not None and (instance is self.dormant_executor or instance is self.background_import_executor)
        if (held
                and frame.f_code.co_name in {"submit","map","_adjust_thread_count"}):
            self.dormant_submissions+=1
            self.stop("profile.thread", "DORMANT_IMPORT_EXECUTOR_ACTIVITY_DENIED")

    def dormant_snapshot(self):
        count=0
        for instance,initialized in ((self.dormant_executor,self.dormant_initialized),(self.background_import_executor,self.background_initialized)):
            if instance is None:continue
            count+=1
            if not initialized or self.dormant_submissions or getattr(instance,"_threads",None)!=set():
                self.stop("profile.thread", "DORMANT_EXECUTOR_EFFECTS_UNPROVEN")
        return {"expected_dormant_executor_constructors":count,"dormant_executor_submissions":0}

    def _executor_denial_evidence(self, frame):
        # Record only source-allowlisted locations and fixed argument classes;
        # no arbitrary argv, env, locals, prefix text or unallowlisted paths.
        frames=[];omitted=0;current=frame
        for _ in range(32):
            if current is None:
                break
            row=self.sources.get(str(Path(current.f_code.co_filename).absolute()))
            if row is None or len(frames)>=12:
                omitted+=1
            else:
                self.helpers.verify_source(row,self.packet["site_root"])
                frames.append({"source_sha256":row["sha256"],"role":row["role"],
                    "function":current.f_code.co_name if current.f_code.co_name in {"__init__","<module>"} else "OTHER_REDACTED",
                    "first_line":current.f_code.co_firstlineno,"line":current.f_lineno})
            current=current.f_back
        value=frame.f_locals.get("max_workers")
        self._executor_evidence("executor_constructor_refusal.r05.json",{
            "phase":"NATIVE" if self.native_phase else "IMPORT",
            "max_workers_class":value if type(value) is int and 0<=value<=128 else "NONE" if value is None else "OTHER_REDACTED",
            "source_bound_frames":frames,"omitted_frames":omitted,"constructor_admitted":False})

    def allow_thread(self, frame):
        instance = frame.f_locals.get("self")
        caller = frame.f_back
        self.source(frame, module="threading")
        if (not self.native_phase or caller is None or caller.f_code.co_name != "_adjust_thread_count"
                or caller.f_globals.get("__name__") != "concurrent.futures.thread"):
            self.stop("profile.thread", "NON_NATIVE_EXECUTOR_THREAD_START")
        self.source(caller, module="concurrent.futures.thread")
        target = getattr(instance, "_target", None)
        code = getattr(target, "__code__", None)
        if (getattr(target, "__module__", "") != "concurrent.futures.thread"
                or getattr(target, "__name__", "") != "_worker" or code is None
                or str(Path(code.co_filename).absolute()) not in self.sources):
            self.stop("profile.thread", "UNAPPROVED_THREAD_TARGET")
        # Require an exact reviewed LangGraph/LangChain caller in this stack;
        # arbitrary application code cannot gain an executor allowance.
        cursor, trusted = caller, False
        for _ in range(32):
            if cursor is None:
                break
            row = self.sources.get(str(Path(cursor.f_code.co_filename).absolute()))
            if row is not None and row["role"] == "executor":
                self.source(cursor)
                trusted = True
                break
            cursor = cursor.f_back
        self.thread_starts += 1
        if (not trusted or self.thread_starts > 128
                or sum(thread.is_alive() and thread is not threading.main_thread() for thread in threading.enumerate()) >= 8):
            self.stop("profile.thread", "FINITE_NATIVE_THREAD_BUDGET_OR_SOURCE")
        self.approved_thread = id(instance)

    def allow_low_level_thread(self, frame):
        self.source(frame, module="threading")
        if frame.f_code.co_name != "start" or id(frame.f_locals.get("self")) != self.approved_thread:
            self.stop("profile.thread", "LOW_LEVEL_THREAD_WITHOUT_APPROVED_EXECUTOR")
        self.approved_thread = None


def run(packet, channel, identity, helpers, guard):
    controls = Controls(packet, channel, identity, helpers)
    controls.snapshot = guard.install(packet, controls)
    admission = packet["admission"]
    try:
        # Exactly the original six bootstrap imports, protected by r07's
        # compatible refusals, constructor denial and synthetic dotenv guards.
        modules = ("tradingagents", "tradingagents.llm_clients", "tradingagents.graph.setup",
                   "tradingagents.graph.trading_graph", "langgraph.checkpoint.sqlite",
                   "langchain_core.language_models.chat_models")
        for name in modules:
            module = importlib.import_module(name)
            if not Path(module.__file__).resolve().is_relative_to(Path(packet["site_root"]).resolve()):
                controls.stop("host.import", "IMPORT_OUTSIDE_EXACT_TA_SITE")
        controls.assert_no_unapproved_effects()
        source = helpers.verify_pin(packet["pins"]["payload"])
        spec = importlib.util.spec_from_file_location("fa015_ta_native_payload", source)
        payload = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = payload
        spec.loader.exec_module(payload)
        # This is the real FA015 object; it stays in this process, with the real
        # graph/checkpointer, while parent commits the core result transaction.
        controls.native_phase = True
        outcome = payload.execute_case(admission, controls)
        receipt = channel.exchange("STAGED", {"result":outcome.staged.result, "trace":outcome.trace,
            "state_hash":outcome.actual_state_hash, "effects":controls.assert_no_unapproved_effects()})
        outcome.staged.confirm_publication(receipt)
        controls.assert_no_unapproved_effects()
        channel.exchange("CLEANED", {"result_hash":outcome.staged.result_hash,
                                     "checkpoint_confirmed":outcome.staged.confirmed})
    except BaseException as error:
        # No native exception text, prompt or local variables in public output.
        evidence = {"exception_type":type(error).__name__, "case":admission["case"],
                    "checkpoint_action":"RETAIN", "native_graph_success_claimed":False}
        with (Path(packet["attempt_root"])/"evidence/child-failure.json").open("x", encoding="utf-8") as stream:
            json.dump(evidence, stream)
        raise
