"""One admitted Qlib process. Native mode is not released by this candidate.

Reads bounded stdin only after FA013 assigns Windows Job and active admission.
There is no process launcher/scheduler here and no imported platform runtime.
"""
from pathlib import Path
from importlib import util
from contextlib import contextmanager
import argparse
import errno
import hashlib
import json
import math
import os
import socket
import sys
from urllib.parse import urlparse, unquote

sys.dont_write_bytecode=True
spec=util.spec_from_file_location("_native_qlib_contract",Path(__file__).with_name("qlib_workflow.py"))
contract=util.module_from_spec(spec);spec.loader.exec_module(contract)



# One task per child; success path has fixed assignments only, no sampling thread.
_NATIVE_STAGE="ENTRY"
_FAILURE_TYPES={"PermissionError","FileNotFoundError","FileExistsError","ImportError","ModuleNotFoundError","TypeError","ValueError","RuntimeError","OSError","QlibContractError","KeyError","AttributeError","AssertionError"}
def _failure_packet(error):
    import re
    code=getattr(error,"code",None)
    if type(code)is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}",code)is None:code="QLIB_NATIVE_FAILED"
    result={"status":"FAILED","code":code,"stage":_NATIVE_STAGE,
        "error_type":type(error).__name__ if type(error).__name__ in _FAILURE_TYPES else "OtherException","public_frames":[]}
    entry=Path(__file__).absolute()
    # Match only fixed application guard reasons, never arbitrary exception text.
    reason=error.args[0] if len(error.args)==1 and type(error.args[0])is str else None
    result["fixed_reason"]=reason if reason in {"QLIB_EXTERNAL_EFFECT_DENIED","QLIB_WRITE_DENIED","QLIB_READ_DENIED"} else None
    tb=error.__traceback__;visited=0
    while tb is not None and visited<24 and len(result["public_frames"])<12:
        visited+=1;path=Path(tb.tb_frame.f_code.co_filename);alias=None
        if path.is_absolute():
            path=path.absolute()
            if path==entry:alias="ENTRY"
            elif path==entry.with_name("qlib_workflow.py"):alias="CONTRACT"
        function=tb.tb_frame.f_code.co_name
        if alias is not None and len(alias)<=180 and re.fullmatch(r"[A-Za-z_<>][A-Za-z0-9_<>]{0,95}",function):
            result["public_frames"].append({"source":alias,"function":function,"line":tb.tb_lineno})
        tb=tb.tb_next
    return result

class OptionalIPv6Refusal:
    def __init__(self,root,sources,*,is_root):
        if type(is_root) is not bool:raise RuntimeError('OPTIONAL_PROBE_ROLE_INVALID')
        if type(sources) is not dict or set(sources)!={'socket','connection'}:raise RuntimeError('OPTIONAL_PROBE_SOURCE_REQUIRED')
        self.sources=sources;self.keys={k:os.path.normcase(os.path.abspath(v['path'])) for k,v in sources.items()}
        self.refusals=0;self.returns=0;self.records=0;self.bytes=0;self.installed=False;self.invalid=False
        self.pid=os.getpid();self.role='root' if is_root else 'official_pool_helper'
        self.stream=(root/('probe-refusal-'+str(self.pid)+'.jsonl')).open('xb',buffering=0)
        if not self._hashes_match():raise RuntimeError('OPTIONAL_PROBE_SOURCE_CHANGED')
    def _hashes_match(self):
        for entry in self.sources.values():
            raw=Path(entry['path']).read_bytes()  # Pinned runtime code, never data.
            if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:return False
        return True
    def _frame(self,frame,source,name,first,line=None):
        return (frame is not None and frame.f_code.co_name==name and frame.f_code.co_firstlineno==first
            and (line is None or frame.f_lineno==line)
            and os.path.normcase(os.path.abspath(frame.f_code.co_filename))==self.keys[source])
    def matches(self,values,caller):
        return (len(values)==4 and all(type(v) is int for v in values[1:]) and tuple(values[1:])==(23,1,0)
            and self._frame(caller,'socket','__init__',221,233)
            and self._frame(caller.f_back,'connection','_has_ipv6',114,126)
            and self._frame(caller.f_back.f_back,'connection','<module>',1,137))
    def _write(self,value):
        value={'pid':self.pid,'role':self.role,**value}
        raw=(json.dumps(value,separators=(',',':'))+'\n').encode()
        if self.records>=3 or self.bytes+len(raw)>8192:raise RuntimeError('OPTIONAL_PROBE_PROOF_BOUND')
        if self.stream.write(raw)!=len(raw):raise RuntimeError('OPTIONAL_PROBE_PROOF_WRITE_FAILED')
        self.records+=1;self.bytes+=len(raw)
    def refuse(self,values,caller):
        # Returning delegates every mismatch/repeat to the original audit
        # refusal + DENIED monitor, including unexpected socket constructors.
        if self.refusals or not self.matches(values,caller) or not self._hashes_match():return
        self._write({'event':'OPTIONAL_IPV6_CONSTRUCTOR_REFUSED','attempt':1,'family':23,'type':1,'proto':0,
            'source_sha256':self.sources['connection']['sha256'],'constructor_line':126,'module_call_line':137,
            'OS_constructor_permission':False,'refusal':'OSError(EACCES)_BEFORE_ORIGINAL_CONSTRUCTOR'})
        self.refusals=1
        raise OSError(errno.EACCES,'QLIB_OPTIONAL_IPV6_CAPABILITY_REFUSED_BEFORE_CONSTRUCTOR')
    def hook(self,event,values):
        if event=='socket.__new__':self.refuse(values,sys._getframe(1))
    def profile(self,frame,event,result):
        if event!='return' or frame.f_code.co_name!='_has_ipv6' or not self._frame(frame,'connection','_has_ipv6',114):return
        if self.refusals!=1 or self.returns or result is not False:
            self.invalid=True;self._write({'event':'UNEXPECTED_OPTIONAL_PROBE_RETURN','proof':False})
            raise RuntimeError('OPTIONAL_PROBE_NATIVE_FALLBACK_UNPROVEN')
        self._write({'event':'ORIGINAL_OPTIONAL_PROBE_RETURN','returned_false':True,'return_count':1})
        self.returns=1
    def install_profile(self):
        if sys.getprofile() is not None:raise RuntimeError('OPTIONAL_PROBE_PROFILE_ALREADY_SET')
        sys.setprofile(self.profile);self.installed=True
    def verify_after_import(self):
        if not self.installed or sys.getprofile()!=self.profile or self.invalid or (self.refusals,self.returns)!=(1,1):
            raise RuntimeError('OPTIONAL_PROBE_NATIVE_FALLBACK_UNPROVEN')
        module=sys.modules.get('urllib3.util.connection')
        if module is None or getattr(module,'HAS_IPV6',None) is not False:raise RuntimeError('OPTIONAL_PROBE_MODULE_RESULT_UNPROVEN')
        return {'expected_optional_ipv6_constructor_refusals':1,'original_HAS_IPV6':False}
    def close(self):
        try:
            if self.installed:
                if sys.getprofile()!=self.profile:raise RuntimeError('OPTIONAL_PROBE_PROFILE_CHANGED')
                sys.setprofile(None);self.installed=False
        finally:self.stream.close()


@contextmanager
def optional_ipv6_refusal(attempt):
    # Reuse r56's exact negative constructor probe and native False proof.
    # Paths are fixed runtime code, not configurable read permissions.
    sources={
        "socket":{"path":str(Path(sys.base_prefix)/"Lib/socket.py"),"bytes":37815,
            "sha256":"7d4d4c66e6f4bcc961ab462c4f08002ca97def8713a4be1c7373bdbd970a5274"},
        "connection":{"path":str(Path(sys.prefix)/"Lib/site-packages/urllib3/util/connection.py"),"bytes":4444,
            "sha256":"2633bbdb69731e5ccb5cf4e4afd65605d86c7979cc5633126f50c92d5ad74a74"}}
    refusal=OptionalIPv6Refusal(Path(attempt),sources,is_root=True)
    try:
        refusal.install_profile()
        yield refusal
    finally:refusal.close()


def finite(value):
    value=float(value)
    return value if math.isfinite(value) else None


def audit_effect_counts(denials):
    network={"socket.__new__","socket.connect","socket.getaddrinfo","socket.bind"}
    process={"subprocess.Popen","os.system","os.posix_spawn"}
    io={"write_outside_owned_artifacts","read_outside_admitted_roots",
        "mkdir_outside_owned_attempt","mutation_outside_owned_artifacts"}
    if type(denials)is not list or any(type(event)is not str or event not in network|process|io for event in denials):
        contract.fail("QLIB_AUDIT_EVENT_INVALID")
    return {"network_attempts":sum(event in network for event in denials),
            "process_attempts":sum(event in process for event in denials)}


def private_artifacts(root):
    rows=[];total=0
    files=[]
    for current,dirs,names in os.walk(root,followlinks=False):
        for name in dirs:contract.owned_path(root,(Path(current)/name).relative_to(root).as_posix())
        files.extend(Path(current)/name for name in names)
    for path in sorted(files):
        relative=path.relative_to(root).as_posix()
        path=contract.owned_path(root,relative);size=path.stat().st_size
        total+=size
        if total>64*1024**2 or len(rows)>=4096:contract.fail("QLIB_ARTIFACT_BUDGET_EXCEEDED")
        rows.append({"path":relative,"bytes":size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
    return rows


@contextmanager
def deny_external_effects(attempt,*,optional_probe=None):
    """Process-local audit supplements the parent's Windows Job containment."""
    root=Path(attempt).resolve();denials=[]
    read_roots=(root,Path(sys.prefix).resolve(),Path(sys.base_prefix).resolve(),Path(__file__).resolve().parent)
    # Input provider is read-only. Native artifacts/caches/HOME must stay here.
    def audit(event,args):
        if event=="socket.__new__" and optional_probe is not None:
            optional_probe.refuse(args,sys._getframe(1))
        # Exact expected probe raises before reaching this denial; every other
        # constructor is denied, including repeats and source/argument drift.
        if event in {"socket.__new__","socket.connect","socket.getaddrinfo","socket.bind","subprocess.Popen","os.system","os.posix_spawn"}:
            denials.append(event);raise PermissionError("QLIB_EXTERNAL_EFFECT_DENIED")
        if event=="open" and not isinstance(args[0],int):
            path,mode,flags=args
            writes=(type(mode)is str and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)
            if writes:
                # dill's import-time file-type probes open exactly os.devnull.
                # Windows NUL is a discard device, never an artifact directory.
                if os.name=="nt" and type(path)is str and path.casefold()==os.devnull.casefold():
                    return
                target=Path(path).resolve()
                if not target.is_relative_to(root) or target.is_relative_to(root/"provider"):
                    denials.append("write_outside_owned_artifacts");raise PermissionError("QLIB_WRITE_DENIED")
            elif str(path).upper()!="NUL" and not any(Path(path).resolve().is_relative_to(base) for base in read_roots):
                denials.append("read_outside_admitted_roots");raise PermissionError("QLIB_READ_DENIED")
        if event=="os.mkdir" and not Path(args[0]).resolve().is_relative_to(root):
            denials.append("mkdir_outside_owned_attempt");raise PermissionError("QLIB_WRITE_DENIED")
        if event in {"os.remove","os.rmdir","os.rename"}:
            for name in args[:2] if event=="os.rename" else args[:1]:
                if type(name)in (str,bytes):
                    target=Path(os.fsdecode(name)).resolve()
                    if not target.is_relative_to(root) or target.is_relative_to(root/"provider"):
                        denials.append("mutation_outside_owned_artifacts");raise PermissionError("QLIB_WRITE_DENIED")
    sys.addaudithook(audit)
    yield denials
    # Audit hooks are intentionally not removed; this process exits after one task.


def artifact_root(recorder,attempt):
    uri=urlparse(recorder.get_artifact_uri())
    if uri.scheme!="file" or uri.netloc not in {"","localhost"}:contract.fail("QLIB_RECORDER_URI_INVALID")
    path=unquote(uri.path)
    if os.name=="nt" and len(path)>2 and path[0]=="/" and path[2]==":":path=path[1:]
    target=Path(path).resolve()
    if not target.is_relative_to(Path(attempt).resolve()/"recorders"):contract.fail("QLIB_RECORDER_URI_INVALID")
    return target


def verify_optional_git_hook_sources(recorder_class,context_registry,git_context_class):
    sources=(
        (recorder_class._log_uncommitted_code,"f7ad3d9929c636d07f53b8ed02ff8bfbb64b3b7a0c3b482bb1a2fe390335eb35"),
        (context_registry.resolve_tags,"219cb7c4911259bb0d437537a310695a033342a6706483ad4ccbbe97f03d16e9"),
        (git_context_class.in_context,"b998aa92e73f414222ca70ee59747cecf9c372079ad04a29c18d0fd58db6dd3c"))
    site=Path(sys.prefix).resolve()/"Lib/site-packages"
    for function,expected in sources:
        source=Path(function.__code__.co_filename).resolve()
        if not source.is_relative_to(site) or hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
            contract.fail("QLIB_OPTIONAL_GIT_SOURCE_CHANGED")


@contextmanager
def without_optional_git_capture(recorder_class,context_registry,git_context_class):
    """One-child scope: Qlib repository capture + only MLflow GitRunContext.

    Uses the installed registry's native ignore mechanism. Other providers,
    supplied tags and pre-existing ignore classes retain their original path.
    No GitPython import/cache warmup, global setting or installed file edit.
    """
    from unittest.mock import patch
    original=context_registry.resolve_tags
    def resolve_without_git(tags=None,ignore=None):
        excluded=[] if ignore is None else list(ignore)
        if git_context_class not in excluded:excluded.append(git_context_class)
        return original(tags,ignore=excluded)
    with patch.object(recorder_class,"_log_uncommitted_code",lambda self:None), \
            patch.object(context_registry,"resolve_tags",resolve_without_git):
        yield


def native_fit_audit(model,dataset,request):
    """Read actual native fitted state; never recalculate/replace fit values."""
    def primitive(value):
        if hasattr(value,"tolist"):return primitive(value.tolist())
        if type(value)in (list,tuple):return [primitive(item) for item in value]
        if type(value)is float and not math.isfinite(value):return {"native_nonfinite":repr(value)}
        if value is None or type(value)in (str,bool,int,float):return value
        contract.fail("QLIB_FIT_AUDIT_STATE_UNSUPPORTED")
    processors=[]
    for role in ("infer_processors","learn_processors"):
        for processor in getattr(dataset.handler,role):
            cls=type(processor);name=cls.__module__+"."+cls.__name__
            if cls.__module__!="qlib.data.dataset.processor" or cls.__name__ not in {"ProcessInf","ZScoreNorm","Fillna","DropnaLabel","CSZScoreNorm"}:
                contract.fail("QLIB_FIT_AUDIT_PROCESSOR_UNSUPPORTED")
            state={"class":name}
            if cls.__name__=="ZScoreNorm":
                for key in ("mean_train","std_train","ignore","cols"):
                    state[key]=primitive(getattr(processor,key))
                start,end=(str(getattr(processor,key))[:10] for key in ("fit_start_time","fit_end_time"))
                if [start,end]!=request["workflow"]["train"]:contract.fail("QLIB_PROCESSOR_FIT_WINDOW_CHANGED")
                state.update(fit_start=start,fit_end=end)
            processors.append({"role":role,"class":name,"native_state_sha256":contract.digest(state)})
    booster=model.model
    best=booster.best_iteration;current=booster.current_iteration()
    if type(best)is not int or type(current)is not int or not 0<=best<=current<=request["workflow"]["num_boost_round"]:
        contract.fail("QLIB_NATIVE_ITERATION_AUDIT_INVALID")
    return {"model_class":type(model).__module__+"."+type(model).__name__,"best_iteration":best,
        "current_iteration":current,"processor_fit_window":request["workflow"]["train"],
        "processors":processors,"rehydration":"OFFICIAL_DATAHANDLER_IT_LS_WITHOUT_REFIT"}


def native_run(request,attempt,*,control=False):
    global _NATIVE_STAGE
    _NATIVE_STAGE="VALIDATE_REQUEST"
    task=contract.validate_request(request,native=True)
    workflow_hash=contract.digest(task)  # Upstream fills placeholders with live objects.
    basic=request["workflow"].get("feature_profile")==contract.BASIC_PROFILE
    if basic and control:contract.fail("QLIB_BASIC_QRUN_CONTROL_UNSUPPORTED")
    _NATIVE_STAGE="VERIFY_PROVIDER"
    provider=contract.verify_provider(attempt,request["data"])
    # The parent supplies a minimal environment; do not let upstream log user
    # _QLIB_* values or reuse user/global MLflow/HOME locations.
    if any(key.startswith("_QLIB_") for key in os.environ):contract.fail("QLIB_ENVIRONMENT_REJECTED")
    attempt=Path(attempt).resolve()
    _NATIVE_STAGE="CREATE_OWNED_DIRS"
    for name in ("home","tmp","cache","recorders"):(attempt/name).mkdir(exist_ok=False)
    for key in ("HOME","USERPROFILE"):os.environ[key]=str(attempt/"home")
    for key in ("TEMP","TMP"):os.environ[key]=str(attempt/"tmp")
    os.environ["MLFLOW_DISABLE_TELEMETRY"]="true"
    os.environ["MLFLOW_ENABLE_ASYNC_LOGGING"]="false"
    with optional_ipv6_refusal(attempt) as refusal,deny_external_effects(attempt,optional_probe=refusal) as denials:
        _NATIVE_STAGE="IMPORT_NATIVE"
        import qlib
        from qlib.constant import REG_US
        from qlib.model.trainer import task_train
        from qlib.workflow import R
        from qlib.workflow.recorder import MLflowRecorder
        from mlflow.tracking.context import registry as context_registry
        from mlflow.tracking.context.git_context import GitRunContext
        refusal.verify_after_import()
        refusal.close()  # No profiling during training; hook still denies repeats.
        if qlib.__version__!="0.9.8":contract.fail("QLIB_UPSTREAM_VERSION_MISMATCH")
        init={"provider_uri":str(provider),"region":REG_US,"kernels":1,"joblib_backend":"threading",
            "expression_cache":None,"dataset_cache":None,"auto_mount":False,
            "local_cache_path":str(attempt/"cache"),
            "exp_manager":{"class":"MLflowExpManager","module_path":"qlib.workflow.expm",
                "kwargs":{"uri":(attempt/"recorders").as_uri(),"default_exp_name":"owned-offline"}}}
        experiment="owner-"+hashlib.sha256(request["owner_id"].encode()).hexdigest()[:24]
        config={"qlib_init":init,"task":task,"experiment_name":experiment}
        # Exact source has no configuration switch for optional repository capture.
        # Suppress only Git capture in this one process; no model/record algorithm
        # is replaced and installed upstream files are never edited.
        _NATIVE_STAGE="VERIFY_OPTIONAL_GIT_SOURCES"
        verify_optional_git_hook_sources(MLflowRecorder,context_registry,GitRunContext)
        with without_optional_git_capture(MLflowRecorder,context_registry,GitRunContext):
            if control:
                from qlib.cli.run import workflow
                path=attempt/"native_qrun_config.json"
                path.write_text(json.dumps(config,allow_nan=False),encoding="utf-8")
                workflow(str(path),experiment_name=experiment)
                rec=R.get_recorder(experiment_name=experiment)
            else:
                _NATIVE_STAGE="QLIB_INIT"
                qlib.init(**init)
                _NATIVE_STAGE="OFFICIAL_TASK_TRAIN"
                rec=task_train(task,experiment_name=experiment,recorder_name=request["run_id"])
        _NATIVE_STAGE="RECORDER_ARTIFACTS"
        owned=artifact_root(rec,attempt)
        before={row["path"]:row for row in private_artifacts(owned)}
        def current_object(name,*,trusted_native=False):
            # Only same-run official outputs, already hashed in owned local store.
            # No uploaded/old pickle, global trusted mode or parent-side unpickle.
            if name not in before:contract.fail("QLIB_NATIVE_ARTIFACT_MISSING")
            path=contract.owned_path(owned,name)
            if hashlib.sha256(path.read_bytes()).hexdigest()!=before[name]["sha256"]:contract.fail("QLIB_NATIVE_ARTIFACT_CHANGED")
            return rec.load_object(name,trusted=True) if trusted_native else rec.load_object(name)
        _NATIVE_STAGE="PREDICTION_LOAD"
        pred=current_object("pred.pkl")
        if basic:
            # Official task_train has filled SignalRecord's placeholders. Reuse
            # those fresh live objects; no executable pickle rehydration needed.
            filled=task["record"][0]["kwargs"]
            model,dataset=filled["model"],filled["dataset"]
            report,positions=None,{}
        else:
            report=current_object("portfolio_analysis/report_normal_1day.pkl")
            positions=current_object("portfolio_analysis/positions_normal_1day.pkl",trusted_native=True)
            model=current_object("params.pkl",trusted_native=True)
            dataset=current_object("dataset",trusted_native=True)
        _NATIVE_STAGE="FITTED_STATE_AUDIT"
        fit_audit=native_fit_audit(model,dataset,request)
        # Saved DatasetH is dump_all=False. Rehydrate through its official method
        # at the identical provider/config for an explicit effective-index audit.
        from qlib.data.dataset.handler import DataHandlerLP
        _NATIVE_STAGE="EFFECTIVE_SAMPLE_REHYDRATION"
        dataset.setup_data(handler_kwargs={"init_type":DataHandlerLP.IT_LS})
        if native_fit_audit(model,dataset,request)!=fit_audit:contract.fail("QLIB_FITTED_STATE_CHANGED_DURING_AUDIT")
        audit={}
        for role in ("train","valid","test"):
            frame=dataset.prepare(role,col_set=["feature","label"],data_key="learn" if role!="test" else "infer")
            label_rows=int((~frame["label"].isna().any(axis=1)).sum())
            indices=[]
            for index in frame.index:
                keys=dict(zip(frame.index.names,index))
                indices.append([str(keys["datetime"].date()),str(keys["instrument"])])
            audit[role]={"rows":len(frame),"nonmissing_label_rows":label_rows,
                "indices":indices,
                "native_frame_hash":hashlib.sha256(frame.to_json(date_format="iso",double_precision=15).encode()).hexdigest()}
        _NATIVE_STAGE="NORMALIZE_RESULT"
        if set(pred.index.names)!={"datetime","instrument"}:contract.fail("QLIB_PREDICTION_INDEX_INVALID")
        predictions=[]
        for index,row in pred.iterrows():
            keys=dict(zip(pred.index.names,index))
            predictions.append({"datetime":str(keys["datetime"].date()),"instrument":str(keys["instrument"]),"score":float(row.iloc[0])})
        portfolio=[{"datetime":str(index.date()),**{str(key):finite(value) for key,value in row.items()}}
                   for index,row in report.iterrows()] if report is not None else []
        holdings=[{"datetime":str(day.date()),"cash":finite(position.get_cash()),
            "value":finite(position.calculate_value()),
            "amounts":{stock:finite(position.get_stock_amount(stock)) for stock in position.get_stock_list()}}
            for day,position in positions.items()]
        _NATIVE_STAGE="EFFECT_AND_RESULT_VALIDATION"
        if denials:contract.fail("QLIB_EXTERNAL_EFFECT_ATTEMPTED")
        result={"family":contract.FAMILY,"schema_version":contract.FAMILY,"engine":contract.engine_for(request["workflow"]),
            **{key:request[key] for key in ("owner_id","run_id","job_id","fencing_token","export_reference",
                "source_reference","worker_binding_sha256","source_manifest_sha256")},
            "scope":"SYNTHETIC","workflow_hash":workflow_hash,
            "native_manifest_hash":request["data"]["native_manifest_hash"],
            "recorder":{"id":rec.info["id"],"experiment_id":rec.experiment_id,"uri_scope":"OWNER_RUN_LOCAL"},
            "metrics":{key:finite(value) for key,value in rec.list_metrics().items()},
            "predictions":predictions,"portfolio_report":portfolio,"positions":holdings,
            "effective_sample_audit":audit,"model_semantic_hash":hashlib.sha256(model.model.model_to_string().encode()).hexdigest(),
            "fit_audit":fit_audit,
            "native_artifact_root":owned.relative_to(attempt).as_posix(),
            "native_artifacts":private_artifacts(owned),**audit_effect_counts(denials),
            "undefined_metrics":"Non-finite native values are null; inspect native artifact/valid sample count. No values imputed.",
            "control":"OFFICIAL_QRUN" if control else "OFFICIAL_TASK_TRAIN",
            "publication_gate":"CORE_JOB_SUCCEEDED_REQUIRED"}
        return contract.validate_result(result,request)


def main(*,force_qrun_control=False):
    global _NATIVE_STAGE
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=("validate-config","native","qrun-control"),default="validate-config")
    parser.add_argument("--attempt-root")
    args=parser.parse_args()
    # Parent sends this only after Job assignment and current active admission.
    raw=sys.stdin.buffer.read(524289)
    if len(raw)>524288:contract.fail("QLIB_REQUEST_TOO_LARGE")
    request=json.loads(raw)
    if args.mode=="validate-config":
        task=contract.validate_request(request)
        print(json.dumps({"status":"CONFIG_VALIDATED_ONLY","workflow_hash":contract.digest(task),
                          "native_import_or_execution":False}))
        return
    if not args.attempt_root:contract.fail("QLIB_ATTEMPT_ROOT_REQUIRED")
    result=native_run(request,args.attempt_root,control=force_qrun_control or args.mode=="qrun-control")
    _NATIVE_STAGE="SERIALIZE_RESULT"
    target=contract.owned_path(args.attempt_root,"result.json")
    with target.open("x",encoding="utf-8") as output:json.dump(result,output,allow_nan=False)
    print(json.dumps({"status":"NATIVE_RESULT_STAGED_NOT_PUBLISHED","run_id":request["run_id"]}))


if __name__=="__main__":
    try:main()
    except Exception as error:
        # Native raw exceptions/logs stay in bounded private executor evidence.
        print(json.dumps(_failure_packet(error)))
        raise SystemExit(1)
