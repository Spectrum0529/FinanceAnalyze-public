"""Thin lifecycle/budget binding for the actual official TradingAgentsGraph.

No Graph/agent/LLM implementation lives here. Native execution requires complete
runtime import evidence, reviewed containment and a server-registered test port.
The real model backend is intentionally UNCONFIGURED in this first candidate.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from pathlib import Path
import hashlib
import json
import re
import threading
import time

ENGINE={"name":"TradingAgents","version":"0.7.0","commit":"e8f5503e9b86d5c2710b0c76b1f686703968c9df"}
WHEEL_SHA256="44ff72729b717a2f4098f75e5b96d6339b017f073e0f77e0b397bc51d5762dec"
WORKER_LOCK_SHA256="35cfc278c965e120749a60e8f7197c7a00c2e7105392130078e93326484e4950"
IMPORTS=("tradingagents","tradingagents.llm_clients","tradingagents.graph.setup",
    "tradingagents.graph.trading_graph","langgraph.checkpoint.sqlite","langchain_core.language_models.chat_models")
PROVIDER_TOKEN_LIMITS={"provider_input_tokens":2097152,"provider_output_tokens":262144}
PROVIDER_MIN_OUTPUT_TOKENS=16


class DebateBoundaryError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def fail(code):raise DebateBoundaryError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def pin(value):
    if type(value)is not str or re.fullmatch(r"[0-9a-f]{64}",value)is None:fail("TA_BINDING_HASH_INVALID")
    return value


def binding_id(value):
    if type(value)is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",value)is None:fail("TA_BINDING_ID_INVALID")
    return value


def reference(value):
    if type(value)is not dict or set(value)!={"resource_id","version","content_hash"}:fail("TA_DATA_REFERENCE_INVALID")
    for key in ("resource_id","version"):
        if type(value[key])is not str or not 1<=len(value[key])<=128:fail("TA_DATA_REFERENCE_INVALID")
    pin(value["content_hash"])


def options(raw):
    defaults={"debate_rounds":1,"risk_rounds":1,"tool_rounds":1,"recursion_limit":64,
        "model_calls":32,"tool_calls":16,"wall_seconds":120,"output_chars":160000}
    if type(raw)is not dict or set(raw)-set(defaults)-set(PROVIDER_TOKEN_LIMITS):fail("TA_OPTIONS_INVALID")
    if set(raw)&PROVIDER_TOKEN_LIMITS.keys() and (
            not PROVIDER_TOKEN_LIMITS.keys()<=raw.keys()
            or any(type(raw[key])is not int or not 1<=raw[key]<=maximum
                   for key,maximum in PROVIDER_TOKEN_LIMITS.items())):
        fail("TA_OPTIONS_INVALID")
    value={**defaults,**raw}
    for name,minimum,maximum in (("debate_rounds",1,2),("risk_rounds",1,2),("tool_rounds",1,2),
        ("recursion_limit",16,64),("model_calls",1,32),("tool_calls",1,16),("wall_seconds",1,120),("output_chars",1,160000)):
        if type(value[name])is not int or not minimum<=value[name]<=maximum:fail("TA_OPTIONS_INVALID")
    if 2*value["tool_rounds"]+2>=value["recursion_limit"]:fail("TA_RECURSION_LIMIT_INVALID")
    return value


def binding(value):
    required={"owner_id","logical_task_id","run_id","job_id","fencing_token","ticker","trade_date","as_of",
        "data_reference","snapshot_content_hash","selected_view_hash","model_profile","tools_manifest_hash","options"}
    if type(value)is not dict or set(value)!=required:fail("TA_BINDING_INVALID")
    for key in ("owner_id","logical_task_id","run_id","job_id"):
        if type(value[key])is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",value[key])is None:fail("TA_BINDING_ID_INVALID")
    if type(value["fencing_token"])is not int or value["fencing_token"]<1:fail("TA_FENCE_INVALID")
    if type(value["ticker"])is not str or re.fullmatch(r"[A-Z][A-Z0-9.-]{0,17}",value["ticker"])is None:fail("TA_SYMBOL_INVALID")
    try:
        for key in ("trade_date","as_of"):
            if type(value[key])is not str or date.fromisoformat(value[key]).isoformat()!=value[key]:raise ValueError
        if value["trade_date"]>date.today().isoformat() or value["as_of"]>value["trade_date"]:raise ValueError
    except (ValueError,TypeError):fail("TA_TEMPORAL_BINDING_INVALID")
    reference(value["data_reference"])
    for key in ("snapshot_content_hash","selected_view_hash","tools_manifest_hash"):pin(value[key])
    model=value["model_profile"]
    if type(model)is not dict or model.get("mode") not in {"UNCONFIGURED","OFFLINE_FIXTURE","SERVER_PROFILE"}:fail("TA_MODEL_PROFILE_INVALID")
    if model["mode"]=="UNCONFIGURED":
        if set(model)!={"mode"}:fail("TA_MODEL_PROFILE_INVALID")
    elif model["mode"]=="OFFLINE_FIXTURE":
        if set(model)!={"mode","fixture_id","fixture_sha256"} or type(model["fixture_id"])is not str:fail("TA_MODEL_PROFILE_INVALID")
        pin(model["fixture_sha256"])
    else:
        if set(model)!={"mode","profile_id","profile_sha256"}:fail("TA_MODEL_PROFILE_INVALID")
        binding_id(model["profile_id"]);pin(model["profile_sha256"])
    if value["options"]!=options(value["options"]):fail("TA_OPTIONS_NOT_NORMALIZED")
    return value


def resume_key(value):
    binding(value)
    # Retry gets a fresh run/job/fence and must stage only this task's verified
    # checkpoint into its new namespace. These execution IDs do not change the
    # logical data/model/tool contract; all the other inputs do.
    return digest({"engine":ENGINE,**{key:entry for key,entry in value.items()
        if key not in {"run_id","job_id","fencing_token"}}})


def verify_resume(previous,current):
    if type(previous)is not dict or set(previous)!={"binding_hash","checkpoint_manifest_hash","consumed"}:fail("TA_RESUME_MANIFEST_INVALID")
    if previous["binding_hash"]!=resume_key(current):fail("TA_RESUME_BINDING_MISMATCH")
    pin(previous["checkpoint_manifest_hash"])
    return consumed(previous["consumed"],current["options"])


def budget_limits(limits):
    maxima={"model":limits["model_calls"],"tool":limits["tool_calls"],
            "chars":limits["output_chars"],"wall_millis":limits["wall_seconds"]*1000}
    if "provider_input_tokens" in limits:
        maxima.update(provider_calls=limits["model_calls"],
                      **{key:limits[key] for key in PROVIDER_TOKEN_LIMITS})
    return maxima


def consumed(value,limits):
    maxima=budget_limits(limits)
    if type(value)is not dict or set(value)!=set(maxima):fail("TA_BUDGET_STATE_INVALID")
    for key,maximum in maxima.items():
        if type(value[key])is not int or not 0<=value[key]<=maximum:fail("TA_BUDGET_STATE_INVALID")
    return dict(value)


class Budget:
    """Atomic reservation before native model/tool calls; no provider fallback."""
    def __init__(self,limits,*,active,initial=None,clock=time.monotonic):
        self.limits=options(limits);self.active=active;self.clock=clock;self.started=clock()
        self.lock=threading.Lock();self.used=consumed(initial if initial is not None else
            dict.fromkeys(budget_limits(self.limits),0),self.limits)
        self.seen=set();self.violation=None

    def _check(self):
        self.active()
        if self.violation:fail(self.violation)
        if self.used["wall_millis"]+int(max(0,self.clock()-self.started)*1000)>=self.limits["wall_seconds"]*1000:
            self.violation="TA_WALL_BUDGET_EXCEEDED";fail(self.violation)

    def reserve(self,kind,call_id):
        if kind not in {"model","tool"} or not isinstance(call_id,(str,int)):fail("TA_CALL_ID_INVALID")
        with self.lock:
            self._check();key=(kind,str(call_id))
            if key in self.seen:return
            if self.used[kind]>=self.limits[kind+"_calls"]:
                self.violation="TA_CALL_BUDGET_EXCEEDED";fail(self.violation)
            self.used[kind]+=1;self.seen.add(key)

    def output(self,text):
        if type(text)is not str:fail("TA_OUTPUT_TYPE_INVALID")
        with self.lock:
            self._check()
            if self.used["chars"]+len(text)>self.limits["output_chars"]:
                self.violation="TA_OUTPUT_BUDGET_EXCEEDED";fail(self.violation)
            self.used["chars"]+=len(text)

    def provider_spent(self, counters):
        """Mirror only monotonic totals received from the private parent."""
        with self.lock:
            allowed={key:maximum for key,maximum in budget_limits(self.limits).items()
                     if key.startswith("provider_")}
            if not allowed or type(counters)is not dict or counters.keys()!=allowed.keys() or any(
                    type(counters[key])is not int or not self.used[key]<=counters[key]<=maximum
                    for key,maximum in allowed.items()):
                fail("TA_PROVIDER_BUDGET_STATE_INVALID")
            self.used.update(counters)

    def snapshot(self):
        with self.lock:
            self._check()
            return {**self.used,"wall_millis":self.used["wall_millis"]+int(max(0,self.clock()-self.started)*1000)}

    def consumed_for_retry(self):
        """Preserve spent counters even when active/wall checks already fail."""
        with self.lock:
            return {**self.used,"wall_millis":min(self.limits["wall_seconds"]*1000,
                self.used["wall_millis"]+int(max(0,self.clock()-self.started)*1000))}


def require_native_release(value,proof):
    binding(value)
    if (type(proof)is not dict or proof.get("complete_imports")!=list(IMPORTS)
            or proof.get("commit")!=ENGINE["commit"] or proof.get("wheel_sha256")!=WHEEL_SHA256
            or proof.get("worker_lock_sha256")!=WORKER_LOCK_SHA256):fail("TA_COMPLETE_IMPORT_PROOF_REQUIRED")
    for key in ("import_receipt_sha256","security_receipt_sha256","source_manifest_sha256"):
        pin(proof.get(key))
    model=value["model_profile"]
    if model["mode"]=="UNCONFIGURED":fail("TA_MODEL_BACKEND_UNCONFIGURED")
    if model["mode"]=="OFFLINE_FIXTURE" and proof.get("fixture_sha256")!=model["fixture_sha256"]:fail("TA_FIXTURE_BINDING_MISMATCH")
    if model["mode"]=="SERVER_PROFILE" and proof.get("model_profile_sha256")!=model["profile_sha256"]:fail("TA_MODEL_PROFILE_BINDING_MISMATCH")


def budget_callback(budget):
    # Lazy import only after complete native import/review admission.
    from langchain_core.callbacks import BaseCallbackHandler
    class NativeBudgetCallback(BaseCallbackHandler):
        raise_error=True
        run_inline=True
        def on_chat_model_start(self,serialized,messages,*,run_id,**kwargs):budget.reserve("model",str(run_id))
        def on_llm_start(self,serialized,prompts,*,run_id,**kwargs):budget.reserve("model",str(run_id))
        def on_tool_start(self,serialized,input_str,*,run_id,**kwargs):budget.reserve("tool",str(run_id))
        def on_llm_end(self,response,**kwargs):
            for generations in response.generations:
                for generation in generations:
                    text=generation.text
                    # LangChain TextAccessor is a str subclass. Detach text only;
                    # nontext values still fail the strict Budget.output check.
                    budget.output(str.__str__(text) if isinstance(text,str) else text)
                    for call in getattr(getattr(generation,"message",None),"tool_calls",()):
                        budget.output(json.dumps(call,ensure_ascii=False,allow_nan=False))
        def on_tool_end(self,output,**kwargs):
            content=getattr(output,"content",output)
            budget.output(content if type(content)is str else json.dumps(content,allow_nan=False))
    return NativeBudgetCallback()


class StagedNativeReport:
    def __init__(self,result,graph,value,config,*,active):
        self.result=result;self.graph=graph;self.value=value;self.config=config;self.active=active
        self.result_hash=digest(result);self.confirmed=False

    def confirm_publication(self,receipt):
        """Only the trusted parent's post-JobService.finish acknowledgement.

        Same admitted child/session, no public HTTP clear-checkpoint operation.
        The executor authenticates the pipe/session nonce and DB success receipt.
        """
        expected={"status":"SUCCEEDED","owner_id":self.value["owner_id"],"run_id":self.value["run_id"],
            "job_id":self.value["job_id"],"fencing_token":self.value["fencing_token"],"result_hash":self.result_hash}
        if receipt!=expected or self.confirmed:fail("TA_PUBLISH_ACK_INVALID")
        from tradingagents.dataflows.config import run_config
        with run_config(self.config):
            self.graph.clear_checkpoint_on_success(self.value["ticker"],self.value["trade_date"],"stock",None)
        self.confirmed=True


def checked_checkpoint_input(graph,initial_state,*,resume_requested,thread_id):
    """Never turn a requested restore into upstream's fresh-run fallback."""
    native_input=graph.checkpoint_input(initial_state)
    if resume_requested and (initial_state is None or type(thread_id)is not str or not thread_id or native_input is not None):
        fail("TA_REQUESTED_RESUME_CHECKPOINT_MISSING")
    return native_input


def run_native(value,raw_options,attempt_root,*,proof,ports,active,resume=None):
    """Actual public upstream checkpoint/stream lifecycle; never synthesize state.

    ports.scope is an explicitly reviewed test-file model/tool port. It patches
    only native model factory/data endpoints; GraphSetup/roles/loops are official.
    Missing real model backend or six-import evidence stops before any import.
    """
    require_native_release(value,proof)
    if options(raw_options)!=value["options"]:fail("TA_OPTIONS_BINDING_MISMATCH")
    model=value["model_profile"]
    if ((model["mode"]=="OFFLINE_FIXTURE" and getattr(ports,"fixture_sha256",None)!=model["fixture_sha256"])
            or (model["mode"]=="SERVER_PROFILE" and getattr(ports,"profile_sha256",None)!=model["profile_sha256"])
            or getattr(ports,"tools_manifest_hash",None)!=value["tools_manifest_hash"]):fail("TA_PORT_BINDING_MISMATCH")
    if not callable(getattr(ports,"assert_no_external_attempts",None)) or not callable(getattr(ports,"authorize_binding",None)):
        fail("TA_GUARDED_PORTS_REQUIRED")
    ports.authorize_binding(value)
    initial=verify_resume(resume,value) if resume is not None else None
    budget=Budget(value["options"],active=active,initial=initial)
    ports.budget=budget  # Actual spent state remains accessible on early failure.
    active()
    import tradingagents.graph.trading_graph as native_module
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.dataflows.config import run_config
    from tradingagents.agents.rating import run_rating
    unresolved=Path(attempt_root).absolute()
    for parent in (unresolved,*unresolved.parents):
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(),"st_file_attributes",0)&0x400):
            fail("TA_ATTEMPT_ALIAS_REJECTED")
    root=unresolved.resolve()
    if not root.is_dir():fail("TA_OWNED_ATTEMPT_REQUIRED")
    config=deepcopy(DEFAULT_CONFIG)
    config.update({"data_cache_dir":str(root/"cache"),"results_dir":str(root/"results"),
        "memory_log_path":str(root/"memory/trading_memory.md"),"checkpoint_enabled":True,
        "max_debate_rounds":value["options"]["debate_rounds"],"max_risk_discuss_rounds":value["options"]["risk_rounds"],
        "max_tool_rounds":value["options"]["tool_rounds"],"max_recur_limit":value["options"]["recursion_limit"],
        "llm_max_retries":0,"output_language":"Chinese"})
    if model["mode"]=="OFFLINE_FIXTURE":
        config.update(quick_think_llm="OFFLINE_FIXTURE_QUICK",deep_think_llm="OFFLINE_FIXTURE_DEEP")
    # A SERVER_PROFILE is configured only by the trusted model port's scope;
    # no credentials/backend URL/model configuration comes from an API body.
    callback=budget_callback(budget)
    with ports.scope(native_module,config,value,budget),run_config(config):
        graph=native_module.TradingAgentsGraph(selected_analysts=("market","social","news","fundamentals"),
            debug=False,config=config,callbacks=[callback])
        current={}
        with graph.checkpoint_scope(value["ticker"],value["trade_date"],"stock",None) as thread_id:
            initial_state=graph.create_run_state(value["ticker"],value["trade_date"],"stock",None)
            # Constructor callbacks cover model factories. Graph invocation
            # callbacks also cover the real ToolNode and native node trace.
            callbacks=[callback,*getattr(ports,"native_callbacks",())]
            args=graph.propagator.get_graph_args(callbacks=callbacks)
            args.setdefault("config",{})["max_concurrency"]=1
            if thread_id is not None:args.setdefault("config",{}).setdefault("configurable",{})["thread_id"]=thread_id
            native_input=checked_checkpoint_input(graph,initial_state,resume_requested=resume is not None,thread_id=thread_id)
            # Nested event streaming queues a blocking waiter in the same
            # one-worker pool. Consume the official top-level state stream.
            # Never enable eager/messages/custom streaming in this entry.
            if graph.graph.stream_eager is not False:fail("TA_NATIVE_EAGER_STREAM_UNSUPPORTED")
            args["stream_mode"]="values"
            stream=graph.graph.stream(native_input,subgraphs=False,**args)
            try:
                for state in stream:
                    active();budget.snapshot()
                    if state is not None:current.update(state)
            finally:stream.close()
        active();budget.snapshot();ports.assert_no_external_attempts()
        if not current or "final_trade_decision" not in current:fail("TA_NATIVE_FINAL_STATE_MISSING")
        graph.record_decision(value["ticker"],value["trade_date"],current)
        ports.assert_no_external_attempts()
        # Use only actual native state fields. Messages/custom Python objects are
        # not dumped with default=str and no fabricated manager result is added.
        state={key:entry for key,entry in current.items() if key!="messages"}
        json.dumps(state,allow_nan=False)
        report_path=graph.save_reports(current,value["ticker"],save_path=root/"reports",html=False)
        if not Path(report_path).resolve().is_relative_to(root):fail("TA_REPORT_SCOPE_INVALID")
        result={"family":"tradingagents-native-report-1","engine":dict(ENGINE),"scope":"OFFLINE_FIXTURE" if model["mode"]=="OFFLINE_FIXTURE" else "DEVELOPMENT_RESEARCH",
            **{key:value[key] for key in ("owner_id","logical_task_id","run_id","job_id","fencing_token","data_reference",
                "snapshot_content_hash","selected_view_hash","as_of","model_profile","tools_manifest_hash")},
            "native_state":state,"native_rating":run_rating(current),"native_settings":graph.run_settings(),
            "budget":budget.snapshot(),"binding_hash":resume_key(value),"reports_path":str(Path(report_path).relative_to(root)),
            "checkpoint_state":"RETAINED_UNTIL_CORE_JOB_SUCCEEDED_ACK","publication_gate":"CORE_JOB_SUCCEEDED_REQUIRED"}
        active()
        ports.assert_no_external_attempts()
        if hasattr(ports,"used"):result["tool_access_audit"]=list(ports.used)
        if hasattr(ports,"data_scope"):
            result["data_scope"]=ports.data_scope
            result["evidence_scopes"]=list(getattr(ports,"evidence_scopes",()))
        if len(json.dumps(result,allow_nan=False).encode())>2*1024**2:fail("TA_RESULT_TOO_LARGE")
        # Deliberately do not call propagate()/clear_checkpoint_on_success here.
        return StagedNativeReport(result,graph,value,config,active=active)
