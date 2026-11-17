"""Fixed product Graph entry. No initial fixture case dispatch or fake result."""
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType


NATIVE_ERROR_CODES=frozenset(('JOB_CANCELLED', 'TA_ACTUAL_MODEL_TRACE_REQUIRED', 'TA_ACTUAL_NATIVE_CONTROLS_REQUIRED', 'TA_ATTEMPT_ALIAS_REJECTED', 'TA_BACKTEST_BRIDGE_UNAVAILABLE', 'TA_BINDING_HASH_INVALID', 'TA_BINDING_ID_INVALID', 'TA_BINDING_INVALID', 'TA_BUDGET_STATE_INVALID', 'TA_CALL_BUDGET_EXCEEDED', 'TA_CALL_ID_INVALID', 'TA_CHECKPOINT_INTERRUPTED', 'TA_CHILD_INPUT_BINDING_MISMATCH', 'TA_COMPLETE_IMPORT_PROOF_REQUIRED', 'TA_DATA_ENTRY_BUDGET_EXCEEDED', 'TA_DATA_OWNER_OR_KIND_INVALID', 'TA_DATA_REFERENCE_INVALID', 'TA_EVIDENCE_ENGINE_MISMATCH', 'TA_EVIDENCE_REFERENCES_INVALID', 'TA_EVIDENCE_SCOPE_INVALID', 'TA_EVIDENCE_SOURCE_AUTHORIZATION_REQUIRED', 'TA_EVIDENCE_SOURCE_CHANGED', 'TA_EVIDENCE_SUMMARY_TOO_LARGE', 'TA_EVIDENCE_SYMBOL_MISMATCH', 'TA_FAILURE_EVIDENCE_UNPROVEN', 'TA_FAILURE_TRACE_INVALID', 'TA_FENCE_INVALID', 'TA_FIXTURE_BINDING_MISMATCH', 'TA_FUTURE_EVIDENCE_REJECTED', 'TA_FUTURE_INPUT_REJECTED', 'TA_FUTURE_TOOL_REQUEST_REJECTED', 'TA_GUARDED_PORTS_REQUIRED', 'TA_INPUT_BUNDLE_TOO_LARGE', 'TA_INPUT_ROWS_UNAVAILABLE', 'TA_INPUT_SYMBOL_UNAVAILABLE', 'TA_MODEL_BACKEND_UNCONFIGURED', 'TA_MODEL_PROFILE_BINDING_MISMATCH', 'TA_MODEL_PROFILE_INVALID', 'TA_NATIVE_ERROR_UNCLASSIFIED', 'TA_NATIVE_FINAL_STATE_MISSING', 'TA_NATIVE_REQUEST_INVALID', 'TA_NATIVE_SOURCE_BINDING_MISMATCH', 'TA_NATIVE_TOOL_BINDING_MISMATCH', 'TA_OPTIONS_BINDING_MISMATCH', 'TA_OPTIONS_INVALID', 'TA_OPTIONS_NOT_NORMALIZED', 'TA_OUTPUT_BUDGET_EXCEEDED', 'TA_OUTPUT_TYPE_INVALID', 'TA_OWNED_ATTEMPT_REQUIRED', 'TA_PORT_BINDING_MISMATCH', 'TA_PUBLISH_ACK_INVALID', 'TA_RECURSION_LIMIT_INVALID', 'TA_REPORT_SCOPE_INVALID', 'TA_REQUESTED_RESUME_CHECKPOINT_MISSING', 'TA_RESULT_TOO_LARGE', 'TA_RESUME_BINDING_MISMATCH', 'TA_RESUME_MANIFEST_INVALID', 'TA_SETTLEMENT_DATA_NOT_AUTHORIZED', 'TA_SYMBOL_INVALID', 'TA_TEMPORAL_BINDING_INVALID', 'TA_TOOL_SYMBOL_MISMATCH', 'TA_TOOL_WINDOW_INVALID', 'TA_WALL_BUDGET_EXCEEDED'))
NATIVE_ERROR_CODES |= frozenset(("TA_PROVIDER_USAGE_UNKNOWN","TA_PROVIDER_INPUT_COUNT_UNAVAILABLE",
    "TA_PROVIDER_INPUT_BUDGET_EXCEEDED","TA_PROVIDER_CALL_BUDGET_EXCEEDED","TA_PROVIDER_TOKEN_BUDGET_EXHAUSTED",
    "TA_PROVIDER_TARGET_CHANGED","TA_PROVIDER_REQUEST_INVALID","TA_PROVIDER_RESPONSE_INVALID","TA_PROVIDER_BUDGET_STATE_INVALID"))
BLOCKED_TERMINAL_CODES=frozenset(('TA_CALL_BUDGET_EXCEEDED', 'TA_FAILURE_EVIDENCE_UNPROVEN', 'TA_NATIVE_ERROR_UNCLASSIFIED', 'TA_OUTPUT_BUDGET_EXCEEDED', 'TA_WALL_BUDGET_EXCEEDED'))

def safe_native_failure(error):
    known_types={"RuntimeError","ValueError","TypeError","ImportError","ModuleNotFoundError","OSError","PermissionError","FileNotFoundError","DebateBoundaryError","ResearchError","PlatformError","InjectedCheckpointInterruption"}
    name=type(error).__name__
    try:code=getattr(error,"code",None)
    except BaseException:code=None
    if name=="InjectedCheckpointInterruption" and code is None:code="TA_CHECKPOINT_INTERRUPTED"
    if type(code)is not str or code not in NATIVE_ERROR_CODES:code="TA_NATIVE_ERROR_UNCLASSIFIED"
    try:notes=getattr(error,"__notes__",())
    except BaseException:notes=()
    unproven=type(notes)in (list,tuple) and any(type(note)is str and note=="TA_FAILURE_EVIDENCE_UNPROVEN" for note in notes)
    return {"exception_type":name if name in known_types else "OTHER_EXCEPTION_REDACTED","error_code":code,
        "scope":"PRODUCT_SERVER_PROFILE","checkpoint_action":"RETAIN","native_success_claimed":False,"failure_evidence_unproven":bool(unproven)}

def run(packet,channel,identity,helpers,guard):
    # This fixed source contains the audited controls only; its initial run()
    # is never invoked. Product binding/runtime/entry differ explicitly.
    base=helpers.load(packet["pins"]["base_controls"],"fa013_product_base_controls")
    request=packet["admission"]["request"]
    bound=request["binding"]
    if request["native_entrypoint"]!="finance_platform.research.debate_native:execute_native_request" or bound["model_profile"]["mode"]!="SERVER_PROFILE":
        raise RuntimeError("TA_PRODUCT_FIXED_SERVER_ENTRY_REQUIRED")

    class ProductControls(base.Controls):
        def _active_exchange(self,kind):
            value=self.channel.exchange(kind,{"admission_hash":self.helpers.digest(self.admission)})
            if type(value)is dict and set(value)=={"active"} and value["active"]is True:return
            if (type(value)is dict and set(value)=={"active","reason_code"}
                    and value["active"]is False and value["reason_code"]=="JOB_CANCELLED"):
                # ACTIVE/VERIFY are first used by the native request below,
                # after its exact package roots have been installed. Keep the
                # pre-guard, site-only bootstrap free of application imports.
                from finance_platform.core.errors import PlatformError
                raise PlatformError("JOB_CANCELLED",409)
            self.stop("host.active","PRODUCT_ACTIVE_REPLY_INVALID")

        def require_active(self):
            self._active_exchange("ACTIVE")

        def verify_current_binding(self,value):
            if value!=request:self.stop("host.binding","PRODUCT_REQUEST_CHANGED")
            self._active_exchange("VERIFY")

        def invoke_provider(self,value):
            approval=request["input_bundle"].get("provider_approval")
            if not self.native_phase or approval is None:
                self.stop("host.model","TA_EXTERNAL_EXECUTION_APPROVAL_REQUIRED")
            return self.channel.exchange("PROVIDER_MODEL",{"execution_sha256":approval["execution_sha256"],
                                                           "request":value})

        def allow_sqlite(self,values,frame):
            ticker=bound["ticker"]
            if (type(ticker)is not str or not ticker or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for char in ticker)):
                self.stop("sqlite3.connect","INVALID_PRODUCT_TICKER")
            expected=Path(self.packet["attempt_root"])/"cache/checkpoints"/(ticker+".db")
            if (not self.native_phase or type(values)is not tuple or len(values)!=1 or type(values[0])is not str
                    or Path(values[0]).absolute()!=expected or frame.f_globals.get("__name__")!="tradingagents.graph.checkpointer"
                    or frame.f_code.co_name not in {"get_checkpointer","clear_checkpoint"}):
                self.stop("sqlite3.connect","NON_OWNED_PRODUCT_CHECKPOINT_DENIED")
            self.source(frame,module="tradingagents.graph.checkpointer")
            for item in (expected,*expected.parents):
                if item.exists() and (item.is_symlink() or getattr(item.lstat(),"st_file_attributes",0)&0x400 or (item.is_file() and item.lstat().st_nlink!=1)):
                    self.stop("sqlite3.connect","PRODUCT_CHECKPOINT_ALIAS_DENIED")
            self.sqlite_connects+=1
            if self.sqlite_connects>8:self.stop("sqlite3.connect","PRODUCT_CHECKPOINT_OPEN_BUDGET")

        def allow_team_runnable(self,frame,instance):
            from finance_platform.core.ta_team_admission import runnable
            runnable(frame,instance,self.packet,self.sources,self.helpers)

        def allow_product_model(self,frame,instance):
            if bound['model_profile']['profile_id'].startswith('team-'):
                from finance_platform.core.ta_team_admission import model
                model(instance,self.packet,self.sources,self.helpers)
                return
            cls=type(instance)
            code=getattr(getattr(cls,"_generate",None),"__code__",None)
            source=self.packet["pins"]["model_port"]
            if (cls.__module__!="finance_platform_server_model_ports" or cls.__name__!="RegisteredServerChatModel"
                    or code is None or Path(code.co_filename).absolute()!=Path(source["path"]).absolute()):
                self.stop("profile.model","NON_REGISTERED_PRODUCT_CHAT_MODEL")
            self.helpers.verify_pin(source)

    controls=ProductControls(packet,channel,identity,helpers)
    controls.snapshot=guard.install(packet,controls)
    try:
        _bootstrap_phase=getattr(controls,'_phase_diag',None)
        for _bootstrap_id,name in enumerate(("tradingagents","tradingagents.llm_clients","tradingagents.graph.setup","tradingagents.graph.trading_graph","langgraph.checkpoint.sqlite","langchain_core.language_models.chat_models"),1):
            if _bootstrap_phase is not None:_bootstrap_phase.emit('BOOTSTRAP_%d_ENTER'%_bootstrap_id)
            module=importlib.import_module(name)
            if not Path(module.__file__).resolve().is_relative_to(Path(packet["site_root"]).resolve()):
                controls.stop("host.import","PRODUCT_IMPORT_OUTSIDE_EXACT_PROJECT_SITE")
            if _bootstrap_phase is not None:_bootstrap_phase.emit('BOOTSTRAP_%d_RETURN'%_bootstrap_id)
        if _bootstrap_phase is not None:_bootstrap_phase.emit('BOOTSTRAP_COMPLETE')
        controls.assert_no_unapproved_effects()
        sources=json.loads(Path(packet["pins"]["sources"]["path"]).read_bytes())
        for name,paths in sources["package_roots"].items():
            if name not in {"finance_platform","finance_platform.core","finance_platform.research","predictor","predictor.research"}:
                controls.stop("host.source","PRODUCT_PACKAGE_ROOT_INVALID")
            module=ModuleType(name);module.__path__=[str(Path(path).absolute()) for path in paths];sys.modules[name]=module
        # Current measured source graph must bind the fixed research package.
        research=sys.modules["finance_platform.research"]
        if research.__path__!=[str(Path(packet["pins"]["native_entry"]["path"]).parent.absolute())]:
            controls.stop("host.source","PRODUCT_RESEARCH_PACKAGE_ROOT_CHANGED")
        model=helpers.load(packet["pins"]["model_port"],"finance_platform_server_model_ports")
        controls.native_phase=True
        factory=getattr(model,"create_server_model_ports",None)
        if not callable(factory):controls.stop("host.model","SERVER_MODEL_PORT_UNCONFIGURED")
        ports=factory(bound["model_profile"])
        if getattr(ports,"profile_sha256",None)!=bound["model_profile"]["profile_sha256"]:
            controls.stop("host.model","SERVER_MODEL_PROFILE_MISMATCH")
        if bound['model_profile']['profile_id'].startswith('team-'):
            sink=getattr(ports,'configure_observation_sink',None)
            if not callable(sink):controls.stop('host.model','TEAM_OBSERVATION_SINK_REQUIRED')
            def completed_observation(row):
                value=channel.exchange('MODEL_OBSERVATION',{'observation':row})
                if type(value)is not dict or set(value)!={'recorded'} or type(value['recorded'])is not bool:
                    raise RuntimeError('TA_OBSERVATION_ACK_INVALID')
            sink(completed_observation)
        helpers.verify_pin(packet["pins"]["native_entry"])
        native=importlib.import_module("finance_platform.research.debate_native")
        if Path(native.__file__).resolve()!=Path(packet["pins"]["native_entry"]["path"]).resolve():
            controls.stop("host.source","PRODUCT_NATIVE_ENTRY_CHANGED")
        resume=packet["admission"]["resume"]
        receipt_hash=packet["admission"]["resume_stage_sha256"]
        if request["parent_run_id"] is None:
            if resume is not None or receipt_hash is not None:controls.stop("host.resume","UNEXPECTED_RESUME_METADATA")
        else:
            if type(resume)is not dict or type(receipt_hash)is not str:controls.stop("host.resume","VERIFIED_RESUME_REQUIRED")
            receipt_path=Path(packet["attempt_root"])/"evidence/resume-stage-complete.json"
            raw=receipt_path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=receipt_hash:controls.stop("host.resume","STAGED_RECEIPT_CHANGED")
            receipt=json.loads(raw)
            if (receipt.get("schema")!="TA_RESUME_STAGE_COMPLETE_1" or receipt.get("source_run_id")!=request["parent_run_id"]
                    or receipt.get("target_job_id")!=bound["job_id"] or receipt.get("target_run_id")!=bound["run_id"]
                    or receipt.get("target_fencing_token")!=bound["fencing_token"]
                    or receipt.get("owner_namespace_sha256")!=hashlib.sha256(bound["owner_id"].encode()).hexdigest()
                    or receipt.get("source_manifest_sha256")!=resume.get("checkpoint_manifest_hash")
                    or receipt.get("binding_hash")!=resume.get("binding_hash") or native.native.resume_key(bound)!=resume.get("binding_hash")
                    or any(receipt.get(key)is not True for key in ("source_rechecked_unchanged","target_bytes_rechecked","current_real_core_lease_checked"))):
                controls.stop("host.resume","STAGED_RECEIPT_BINDING_INVALID")
            native.native.verify_resume(resume,bound)
            files=receipt.get("files")
            if type(files)is not list or not 1<=len(files)<=4:controls.stop("host.resume","CHECKPOINT_FAMILY_INVALID")
            expected="cache/checkpoints/"+bound["ticker"]+".db"
            allowed={expected+suffix for suffix in ("","-wal","-shm","-journal")};seen=set()
            for row in files:
                if type(row)is not dict or set(row)!={"relative","bytes","sha256"} or row["relative"]not in allowed or row["relative"]in seen:
                    controls.stop("host.resume","CHECKPOINT_FAMILY_INVALID")
                seen.add(row["relative"])
                helpers.verify_pin({"path":str(Path(packet["attempt_root"])/row["relative"]),"bytes":row["bytes"],"sha256":row["sha256"]})
            if expected not in seen:controls.stop("host.resume","CHECKPOINT_FAMILY_INVALID")
        staged=native.execute_native_request(request,packet["attempt_root"],model_ports=ports,controls=controls,resume=resume)
        # Trace is supplied by actual server callbacks if configured, not by an
        # initial test model. State/hash comes only from the native staged body.
        trace_method=getattr(ports,"trace_primitive",None)
        if not callable(trace_method):controls.stop("host.trace","ACTUAL_PRODUCT_NATIVE_TRACE_REQUIRED")
        trace=trace_method()
        receipt=channel.exchange("STAGED",{"result":staged.result,"trace":trace,
            "state_hash":helpers.digest(staged.result["native_state"]),"effects":controls.assert_no_unapproved_effects()})
        staged.confirm_publication(receipt)
        controls.assert_no_unapproved_effects()
        channel.exchange("CLEANED",{"result_hash":staged.result_hash,"checkpoint_confirmed":staged.confirmed})
    except BaseException as error:
        with (Path(packet["attempt_root"])/"evidence/child-failure.json").open("x",encoding="utf-8") as stream:
            json.dump(safe_native_failure(error),stream)
        raise
