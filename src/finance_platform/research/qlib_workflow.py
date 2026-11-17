"""Allowlisted native Qlib task configuration; no training/model framework.

This module is stdlib-only so the independent Qlib worker can load it without
importing the platform provider/runtime dependency tree.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePosixPath
import hashlib
import json
import math
import re

ENGINE = {"name":"Qlib","version":"0.9.8","commit":"54355232463878d2eebb91fe0ee5fa7fa1f5976c",
          "workflow":"alpha158-lgb-us-1","license":"MIT"}
WORKER_BINDING_SHA256 = "8cb1d6b130986c14b51752e3b771c99301c720190788660e9f1162761b8d122e"
WORKER_LOCK_SHA256 = "cfbf1f7e96a463af947dd5239b17a50f927e7897842ad6b896e06baa04079ce6"
FAMILY = "qlib-workflow-result-1"
LABEL = "Ref($close, -2)/Ref($close, -1) - 1"
LABEL_HORIZON = 2
BASIC_PROFILE = "basic-close-open-volume-lgb-1"
ALPHA_PROFILE = ENGINE["workflow"]
BASIC_FEATURES = (("$close", "$open", "$volume"), ("CLOSE", "OPEN", "VOLUME"))

def engine_for(options):
    return {**ENGINE,"workflow":options.get("feature_profile",ALPHA_PROFILE)}

def feature_protocol(profile):
    if profile!=BASIC_PROFILE:fail("QLIB_PROFILE_NOT_VERIFIED")
    return {"id":BASIC_PROFILE,"version":1,"features":[list(v) for v in BASIC_FEATURES],
        "label":LABEL,"label_horizon_sessions":LABEL_HORIZON,"frequency":"day",
        "handler":"DataHandlerLP","loader":"QlibDataLoader","model":"LGBModel",
        "records":["SignalRecord"],"portfolio_analysis":False,"scope":"SYNTHETIC_ONLY"}

FIXTURE_ID = "FA014-QLIB-US-OFFLINE-V2"
SYMBOLS = tuple("SYNTH"+suffix for suffix in "ABCDEFGH")
BENCHMARK = "SYNTHBM"


class QlibContractError(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


def fail(code):
    raise QlibContractError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def hex_digest(value):
    if type(value)is not str or re.fullmatch("[0-9a-f]{64}",value)is None:fail("QLIB_HASH_INVALID")
    return value


def identifier(value):
    if type(value)is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",value)is None:fail("QLIB_ID_INVALID")
    return value


def reference(value):
    if type(value)is not dict or set(value)!={"resource_id","version","content_hash"}:fail("QLIB_REFERENCE_INVALID")
    for key in ("resource_id","version"):
        if type(value[key])is not str or not 1<=len(value[key])<=128 or any(ord(c)<32 for c in value[key]):fail("QLIB_REFERENCE_INVALID")
    hex_digest(value["content_hash"])
    return dict(value)


def validate_input(value):
    """Worker semantic envelope supplied by the FA014-authorized export adapter.

    It is not a second exporter/fixture format. Actual exporter manifests and
    native dump receipts must be mapped by the trusted data-owner resolver.
    """
    required={"fixture_id","scope","calendar","instruments","benchmark","fields","logical_data_hash",
              "snapshot_content_hash","selected_view_hash","native_manifest_hash","native_dump_succeeded",
              "export_storage_dtype","factor_unit_contract","feature_max_lead_sessions","provider_files"}
    if type(value)is not dict or set(value)!=required:fail("QLIB_EXPORT_CONTRACT_INVALID")
    if (value["fixture_id"]!=FIXTURE_ID or value["scope"]!="SYNTHETIC"
            or value["instruments"]!=list(SYMBOLS) or value["benchmark"]!=BENCHMARK
            or value["export_storage_dtype"]!="float32"
            or value["factor_unit_contract"]!="FA014_SYNTHETIC_SPLIT_NORMALIZED_FACTOR_VOLUME_V1"
            or type(value["feature_max_lead_sessions"])is not int or value["feature_max_lead_sessions"]!=0):
        fail("QLIB_EXPORT_SEMANTICS_UNSUPPORTED")
    # Alpha158 uses VWAP by default. Do not manufacture it from OHLC. The actual
    # FA014 contract must explicitly supply it or agree a native configuration
    # delta before this default workflow can be released.
    if (type(value["fields"])is not list or any(type(field)is not str for field in value["fields"])
            or set(value["fields"])!={"open","high","low","close","volume","factor","vwap"}):
        fail("QLIB_ALPHA158_VWAP_BINDING_REQUIRED")
    hex_digest(value["logical_data_hash"])
    for field in ("snapshot_content_hash","selected_view_hash","native_manifest_hash"):
        if value[field] is not None:hex_digest(value[field])
    if value["native_dump_succeeded"] is True:hex_digest(value["native_manifest_hash"])
    calendar=value["calendar"]
    if type(calendar)is not list or len(calendar)!=420 or any(type(day)is not str for day in calendar):fail("QLIB_CALENDAR_INVALID")
    if calendar!=sorted(set(calendar)):fail("QLIB_CALENDAR_INVALID")
    try:
        if any(type(day)is not str or date.fromisoformat(day).isoformat()!=day for day in calendar):raise ValueError
    except (TypeError,ValueError):fail("QLIB_CALENDAR_INVALID")
    if type(value["native_dump_succeeded"])is not bool:fail("QLIB_EXPORT_CONTRACT_INVALID")
    files=value["provider_files"]
    if type(files)is not list or len(files)>256:fail("QLIB_PROVIDER_MANIFEST_INVALID")
    names=[];total=0
    for item in files:
        if type(item)is not dict or set(item)!={"path","sha256","bytes"}:fail("QLIB_PROVIDER_MANIFEST_INVALID")
        name=safe_relative(item["path"])
        valid=name in {"calendars/day.txt","instruments/all.txt"}
        parts=PurePosixPath(name).parts
        if len(parts)==3 and parts[0]=="features" and parts[1] in {s.lower() for s in (*SYMBOLS,BENCHMARK)}:
            valid=parts[2] in {field+".day.bin" for field in value["fields"]}
        if not valid or type(item["bytes"])is not int or not 0<item["bytes"]<=16*1024**2:fail("QLIB_PROVIDER_MANIFEST_INVALID")
        names.append(name);total+=item["bytes"];hex_digest(item["sha256"])
    if len(set(names))!=len(names) or total>64*1024**2:fail("QLIB_PROVIDER_MANIFEST_INVALID")
    required_files={"calendars/day.txt","instruments/all.txt"}|{
        f"features/{symbol.lower()}/{field}.day.bin" for symbol in (*SYMBOLS,BENCHMARK) for field in value["fields"]}
    if value["native_dump_succeeded"] and set(names)!=required_files:
        fail("QLIB_PROVIDER_MANIFEST_INCOMPLETE")
    return value


def bind_prepared_manifest(manifest):
    """Bind the actual FA014 public r02 descriptor, without reading CSV/Parquet.

    A prepared descriptor can configure a workflow, but cannot authorize a
    provider_uri or training. Completed native output will be bound separately
    by the data owner's reviewed authorization/staging adapter.
    """
    if (type(manifest)is not dict or manifest.get("schema")!="FA014_QLIB_SYNTHETIC_FIXTURE_BINDING_2"
            or manifest.get("state")!="INPUT_PREPARED_NATIVE_DUMP_NOT_EXECUTED"
            or manifest.get("fixture_id")!=FIXTURE_ID or manifest.get("origin")!="SYNTHETIC_ONLY_NOT_PROVIDER_DATA"
            or manifest.get("provider_uri") is not None or manifest.get("seed")!=14027):
        fail("QLIB_PREPARED_INPUT_BINDING_INVALID")
    semantics=manifest.get("semantics",{})
    if (semantics.get("price_policy")!="SYNTHETIC_QLIB_NORMALIZED_OHLCV_V1"
            or semantics.get("normalization")!="raw_price*A/100; factor=A/100; volume=raw_shares/factor"
            or semantics.get("split")!={"index":210,"ratio":"2","symbol":"SYNTHA"}):
        fail("QLIB_EXPORT_SEMANTICS_UNSUPPORTED")
    csv_files=manifest.get("CSV_files")
    if (type(csv_files)is not list or {item.get("symbol") for item in csv_files}!=set((*SYMBOLS,BENCHMARK))
            or sum(item.get("rows",0) for item in csv_files)!=3779):fail("QLIB_PREPARED_INPUT_BINDING_INVALID")
    for item in csv_files:
        if item["rows"]!=(419 if item["symbol"]=="SYNTHC" else 420):fail("QLIB_PREPARED_INPUT_BINDING_INVALID")
        hex_digest(item["sha256"]);hex_digest(item["logical_rows_hash"])
    data={"fixture_id":FIXTURE_ID,"scope":"SYNTHETIC","calendar":list(manifest["calendar"]),
        "instruments":list(manifest["universe"]),"benchmark":manifest["benchmark"],
        "fields":[name for name in manifest["columns"] if name not in {"symbol","date"}],
        "logical_data_hash":manifest["logical_data_hash"],
        # The source explicitly has no provider HistoryStore snapshot/view refs.
        # Do not alias the logical, CSV, Parquet or future bin hashes to them.
        "snapshot_content_hash":None,"selected_view_hash":None,"native_manifest_hash":None,
        "native_dump_succeeded":False,"export_storage_dtype":"float32",
        "factor_unit_contract":"FA014_SYNTHETIC_SPLIT_NORMALIZED_FACTOR_VOLUME_V1",
        "feature_max_lead_sessions":0,"provider_files":[]}
    validate_input(data)
    splits=manifest["split_indices"];days=data["calendar"]
    if (splits.get("index_convention")!="0-based half-open" or splits.get("label_horizon_sessions")!=2
            or splits.get("train")!=[60,280] or splits.get("valid")!=[280,340]
            or splits.get("test_predictions")!=[340,400]
            or splits.get("effective_train_calendar_indices")!=[60,278]
            or splits.get("effective_valid_calendar_indices")!=[280,338]):fail("QLIB_SPLIT_INVALID")
    options={"train":[days[60],days[277]],"train_cutoff":days[279],
        "valid":[days[280],days[337]],"valid_cutoff":days[339],"test":[days[340],days[399]]}
    return {"data":data,"workflow":workflow_options(options,data),"owner_id":manifest["owner_id"],
        "export_id":manifest["export_id"],"input_run_id":manifest["input_run_id"],
        "manifest_ref":manifest["manifest_ref"],"provider_uri":None,
        "authority":"PREPARED_PUBLIC_DESCRIPTOR_ONLY_NOT_EXECUTION_GRANT"}


def safe_relative(value):
    if (type(value)is not str or "\\" in value or ":" in value or "\x00" in value
            or PurePosixPath(value).is_absolute() or any(p in {".","..",""} for p in value.split("/"))):
        fail("QLIB_PATH_INVALID")
    return value


def owned_path(root, relative):
    unresolved=Path(root).absolute()
    for parent in (unresolved,*unresolved.parents):
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(),"st_file_attributes",0)&0x400):
            fail("QLIB_PATH_ALIAS_REJECTED")
    root=unresolved.resolve();relative=safe_relative(relative)
    target=root.joinpath(*PurePosixPath(relative).parts)
    current=root
    for part in PurePosixPath(relative).parts:
        current=current/part
        if current.exists() and (current.is_symlink() or getattr(current.lstat(),"st_file_attributes",0)&0x400):
            fail("QLIB_PATH_ALIAS_REJECTED")
    if not target.resolve().is_relative_to(root):fail("QLIB_PATH_INVALID")
    return target


def workflow_options(raw, data):
    validate_input(data)
    required={"train","valid","test","train_cutoff","valid_cutoff"}
    defaults={"feature_profile":ALPHA_PROFILE,"seed":14027,"num_boost_round":20,"topk":3,"n_drop":1,"initial_cash":"1000000",
              "open_cost":"0.0005","close_cost":"0.0005","min_cost":"1"}
    if type(raw)is not dict or not required<=set(raw) or set(raw)-required-set(defaults):fail("QLIB_OPTIONS_INVALID")
    options={**defaults,**raw}
    if options["feature_profile"] not in (ALPHA_PROFILE,BASIC_PROFILE):fail("QLIB_PROFILE_UNSUPPORTED")
    if options["feature_profile"]==BASIC_PROFILE and "num_boost_round" not in raw:options["num_boost_round"]=5
    calendar=data["calendar"];indices={day:i for i,day in enumerate(calendar)}
    pairs={}
    for role in ("train","valid","test"):
        pair=options[role]
        if type(pair)is not list or len(pair)!=2 or any(day not in indices for day in pair):fail("QLIB_SPLIT_INVALID")
        a,b=(indices[day] for day in pair)
        if a>b:fail("QLIB_SPLIT_INVALID")
        pairs[role]=(a,b)
    if any(options[key] not in indices for key in ("train_cutoff","valid_cutoff")):fail("QLIB_SPLIT_INVALID")
    train,valid,test=(pairs[role] for role in ("train","valid","test"))
    tc,vc=(indices[options[key]] for key in ("train_cutoff","valid_cutoff"))
    if not (train[0]>=60 and train[1]+LABEL_HORIZON<=tc<valid[0]
            and valid[1]+LABEL_HORIZON<=vc<test[0] and test[1]+LABEL_HORIZON<len(calendar)):
        fail("QLIB_LABEL_MATURITY_OR_SPLIT_INVALID")
    for key,minimum,maximum in (("seed",0,2**31-1),("num_boost_round",1,32),("topk",1,8),("n_drop",0,8)):
        if type(options[key])is not int or not minimum<=options[key]<=maximum:fail("QLIB_OPTIONS_INVALID")
    if options["n_drop"]>options["topk"]:fail("QLIB_OPTIONS_INVALID")
    for key,minimum,maximum in (("initial_cash",1000,10000000),("open_cost",0,.01),("close_cost",0,.01),("min_cost",0,1000)):
        try:
            if type(options[key])is not str:raise ValueError
            number=Decimal(options[key])
            if not number.is_finite() or not Decimal(str(minimum))<=number<=Decimal(str(maximum)):raise ValueError
        except (ValueError,ArithmeticError):fail("QLIB_OPTIONS_INVALID")
    return options


def _legacy_alpha_task(raw, data):
    options=workflow_options(raw,data)
    handler={"class":"Alpha158","module_path":"qlib.contrib.data.handler","kwargs":{
        "start_time":data["calendar"][0],"end_time":data["calendar"][-1],
        "instruments":list(data["instruments"]),"fit_start_time":options["train"][0],"fit_end_time":options["train"][1],
        "label":[LABEL],"infer_processors":[{"class":"ProcessInf"},{"class":"ZScoreNorm","kwargs":{"fields_group":"feature"}},
            {"class":"Fillna","kwargs":{"fields_group":"feature"}}],
        "learn_processors":[{"class":"DropnaLabel"},{"class":"CSZScoreNorm","kwargs":{"fields_group":"label"}}]}}
    seed=options["seed"]
    return {"model":{"class":"LGBModel","module_path":"qlib.contrib.model.gbdt","kwargs":{
                "loss":"mse","num_boost_round":options["num_boost_round"],"early_stopping_rounds":5,
                "learning_rate":.05,"max_depth":3,"num_leaves":7,"min_data_in_leaf":8,
                "seed":seed,"feature_fraction_seed":seed,"bagging_seed":seed,"data_random_seed":seed,
                "num_threads":1,"deterministic":True,"force_col_wise":True}},
        "dataset":{"class":"DatasetH","module_path":"qlib.data.dataset","kwargs":{
            "handler":handler,"segments":{role:list(options[role]) for role in ("train","valid","test")}}},
        "record":[{"class":"SignalRecord","module_path":"qlib.workflow.record_temp","kwargs":{"model":"<MODEL>","dataset":"<DATASET>"}},
            {"class":"SigAnaRecord","module_path":"qlib.workflow.record_temp","kwargs":{"ana_long_short":False,"ann_scaler":252}},
            {"class":"PortAnaRecord","module_path":"qlib.workflow.record_temp","kwargs":{"config":{
                "strategy":{"class":"TopkDropoutStrategy","module_path":"qlib.contrib.strategy",
                    "kwargs":{"signal":"<PRED>","topk":options["topk"],"n_drop":options["n_drop"]}},
                "backtest":{"start_time":data["calendar"][data["calendar"].index(options["test"][0])+1],
                    "end_time":data["calendar"][data["calendar"].index(options["test"][1])+1],
                    "account":float(options["initial_cash"]),"benchmark":data["benchmark"],"exchange_kwargs":{
                        "limit_threshold":None,"trade_unit":1,"deal_price":"$open","impact_cost":0,"volume_threshold":None,
                        **{key:float(options[key]) for key in ("open_cost","close_cost","min_cost")}}}}}}]}


def build_task(raw, data):
    options=workflow_options(raw,data)
    task=_legacy_alpha_task(options,data)
    if options["feature_profile"]!=BASIC_PROFILE:return task
    old=task["dataset"]["kwargs"]["handler"]["kwargs"]
    task["dataset"]["kwargs"]["handler"]={"class":"DataHandlerLP","module_path":"qlib.data.dataset.handler","kwargs":{
        "start_time":old["start_time"],"end_time":old["end_time"],"instruments":old["instruments"],
        "infer_processors":[{"class":"ProcessInf"},{"class":"ZScoreNorm","kwargs":{"fields_group":"feature",
            "fit_start_time":old["fit_start_time"],"fit_end_time":old["fit_end_time"]}},{"class":"Fillna","kwargs":{"fields_group":"feature"}}],
        "learn_processors":old["learn_processors"],"data_loader":{"class":"QlibDataLoader","module_path":"qlib.data.dataset.loader",
            "kwargs":{"freq":"day","config":{"feature":[list(v) for v in BASIC_FEATURES],"label":[old["label"],["LABEL0"]]}}}}}
    task["record"]=task["record"][:1]
    return task


def validate_request(value, *, native=False):
    required={"schema_version","owner_id","run_id","job_id","fencing_token","export_reference","source_reference",
              "data","workflow","worker_binding_sha256","source_manifest_sha256","release"}
    if type(value)is not dict or set(value)!=required or value["schema_version"]!="qlib-worker-request-1":fail("QLIB_REQUEST_INVALID")
    for key in ("owner_id","run_id","job_id"):identifier(value[key])
    if type(value["fencing_token"])is not int or value["fencing_token"]<1:fail("QLIB_FENCE_INVALID")
    for key in ("export_reference","source_reference"):reference(value[key])
    if value["worker_binding_sha256"]!=WORKER_BINDING_SHA256:fail("QLIB_WORKER_BINDING_MISMATCH")
    hex_digest(value["source_manifest_sha256"])
    task=build_task(value["workflow"],value["data"])
    if native:
        if value["workflow"].get("feature_profile",ALPHA_PROFILE)!=BASIC_PROFILE:fail("QLIB_ALPHA158_UNVERIFIED")
        if value["data"]["native_dump_succeeded"] is not True:fail("QLIB_NATIVE_DUMP_NOT_READY")
        approval=value["release"]
        if type(approval)is not dict or set(approval)!={"security_receipt_sha256","financial_receipt_sha256","source_manifest_sha256","native_manifest_hash"}:
            fail("QLIB_EXECUTION_RELEASE_REQUIRED")
        for pin in approval.values():hex_digest(pin)
        if (approval["source_manifest_sha256"]!=value["source_manifest_sha256"]
                or approval["native_manifest_hash"]!=value["data"]["native_manifest_hash"]):fail("QLIB_RELEASE_BINDING_MISMATCH")
    return task


def verify_provider(attempt_root, data):
    """Verify supplied completed export only; no exporter/bin encoder here."""
    if data["native_dump_succeeded"] is not True:fail("QLIB_NATIVE_DUMP_NOT_READY")
    provider=owned_path(attempt_root,"provider")
    expected={item["path"]:item for item in data["provider_files"]}
    observed={path.relative_to(provider).as_posix() for path in provider.rglob("*") if path.is_file()}
    if observed!=set(expected):fail("QLIB_PROVIDER_MANIFEST_MISMATCH")
    for name,item in expected.items():
        path=owned_path(provider,name)
        if path.stat().st_size!=item["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest()!=item["sha256"]:
            fail("QLIB_PROVIDER_MANIFEST_MISMATCH")
    if (provider/"calendars/day.txt").read_text(encoding="utf-8").splitlines()!=data["calendar"]:fail("QLIB_CALENDAR_BINDING_MISMATCH")
    return provider


def validate_effect_counts(value):
    for field in ("network_attempts","process_attempts"):
        if type(value.get(field))is not int or value[field]!=0:fail("QLIB_EXTERNAL_EFFECT_COUNTER_INVALID")


def validate_numeric_result(value):
    def number(item):
        if item is not None and (type(item)not in (int,float) or not math.isfinite(item)):fail("QLIB_RESULT_NUMBER_INVALID")
    if type(value.get("fencing_token"))is not int or type(value.get("metrics"))is not dict or type(value.get("portfolio_report"))is not list:
        fail("QLIB_RESULT_NUMBER_INVALID")
    for item in value["metrics"].values():number(item)
    for row in value["portfolio_report"]:
        if type(row)is not dict or type(row.get("datetime"))is not str:fail("QLIB_RESULT_NUMBER_INVALID")
        for key,item in row.items():
            if key!="datetime":number(item)
    for row in value.get("predictions",[]):
        if type(row)is not dict:fail("QLIB_RESULT_NUMBER_INVALID")
        score=row.get("score")
        if type(score)not in (int,float) or not math.isfinite(score):fail("QLIB_RESULT_NUMBER_INVALID")
    for row in value.get("positions",[]):
        if type(row)is not dict:fail("QLIB_RESULT_NUMBER_INVALID")
        for field in ("cash","value"):
            item=row.get(field)
            if type(item)not in (int,float) or not math.isfinite(item):fail("QLIB_RESULT_NUMBER_INVALID")
        if type(row.get("amounts"))is not dict:fail("QLIB_RESULT_NUMBER_INVALID")
        for item in row["amounts"].values():
            if type(item)not in (int,float) or not math.isfinite(item) or item<0:fail("QLIB_RESULT_NUMBER_INVALID")


def validate_result(value, request):
    """Only primitive task-owned metadata crosses back to the platform."""
    if type(value)is not dict or value.get("family")!=FAMILY or value.get("engine")!=engine_for(request["workflow"]):fail("QLIB_RESULT_INVALID")
    allowed={"family","schema_version","engine","owner_id","run_id","job_id","fencing_token","export_reference",
        "source_reference","worker_binding_sha256","source_manifest_sha256","scope","workflow_hash","native_manifest_hash",
        "recorder","metrics","predictions","portfolio_report","positions","effective_sample_audit","model_semantic_hash",
        "native_artifact_root","native_artifacts","network_attempts","process_attempts","undefined_metrics","control","publication_gate","fit_audit"}
    if set(value)!=allowed:fail("QLIB_RESULT_INVALID")
    for key in ("owner_id","run_id","job_id","fencing_token","export_reference","source_reference","worker_binding_sha256","source_manifest_sha256"):
        if value.get(key)!=request[key]:fail("QLIB_RESULT_BINDING_MISMATCH")
    if value.get("workflow_hash")!=digest(build_task(request["workflow"],request["data"])):fail("QLIB_RESULT_BINDING_MISMATCH")
    if request["workflow"].get("feature_profile")==BASIC_PROFILE and (value.get("portfolio_report")!=[] or value.get("positions")!=[]):
        fail("QLIB_BASIC_PROFILE_PORTFOLIO_NOT_SUPPORTED")
    validate_effect_counts(value)
    if value.get("native_manifest_hash")!=request["data"]["native_manifest_hash"]:
        fail("QLIB_RESULT_BINDING_MISMATCH")
    if value.get("scope")!="SYNTHETIC" or value.get("publication_gate")!="CORE_JOB_SUCCEEDED_REQUIRED":fail("QLIB_RESULT_INVALID")
    recorder=value.get("recorder")
    if type(recorder)is not dict or set(recorder)!={"id","experiment_id","uri_scope"}:fail("QLIB_RESULT_INVALID")
    identifier(recorder["id"]);identifier(recorder["experiment_id"])
    if recorder["uri_scope"]!="OWNER_RUN_LOCAL":fail("QLIB_RESULT_INVALID")
    if not safe_relative(value.get("native_artifact_root","")).startswith("recorders/"):fail("QLIB_ARTIFACT_SCOPE_INVALID")
    artifacts=value.get("native_artifacts")
    if type(artifacts)is not list or not 0<len(artifacts)<=4096:fail("QLIB_ARTIFACTS_INVALID")
    for artifact in artifacts:
        if type(artifact)is not dict or set(artifact)!={"path","sha256","bytes"}:fail("QLIB_ARTIFACTS_INVALID")
        safe_relative(artifact["path"]);hex_digest(artifact["sha256"])
        if type(artifact["bytes"])is not int or not 0<=artifact["bytes"]<=64*1024**2:fail("QLIB_ARTIFACTS_INVALID")
    predictions=value.get("predictions")
    if type(predictions)is not list or not 0<len(predictions)<=8000:fail("QLIB_PREDICTIONS_INVALID")
    seen=set();start,end=request["workflow"]["test"]
    for row in predictions:
        if type(row)is not dict or set(row)!={"datetime","instrument","score"}:fail("QLIB_PREDICTIONS_INVALID")
        key=(row["datetime"],row["instrument"])
        if key in seen or row["instrument"] not in SYMBOLS or not start<=row["datetime"]<=end:fail("QLIB_PREDICTIONS_INVALID")
        seen.add(key)
        if type(row["score"])not in (int,float) or not math.isfinite(row["score"]):fail("QLIB_PREDICTIONS_INVALID")
    options=workflow_options(request["workflow"],request["data"])
    audit=value.get("effective_sample_audit")
    if type(audit)is not dict or set(audit)!={"train","valid","test"}:fail("QLIB_EFFECTIVE_SAMPLE_AUDIT_REQUIRED")
    for role,info in audit.items():
        if type(info)is not dict or set(info)!={"rows","nonmissing_label_rows","indices","native_frame_hash"}:fail("QLIB_EFFECTIVE_SAMPLE_AUDIT_REQUIRED")
        if (type(info["rows"])is not int or not 0<info["rows"]<=8000 or type(info["indices"])is not list
                or len(info["indices"])!=info["rows"] or type(info["nonmissing_label_rows"])is not int
                or not 0<=info["nonmissing_label_rows"]<=info["rows"]):fail("QLIB_EFFECTIVE_SAMPLE_AUDIT_REQUIRED")
        hex_digest(info["native_frame_hash"])
        for day,symbol in info["indices"]:
            if symbol not in SYMBOLS or not options[role][0]<=day<=options[role][1]:fail("QLIB_EFFECTIVE_SAMPLE_INDEX_INVALID")
    hex_digest(value.get("model_semantic_hash"))
    fit=value.get("fit_audit")
    if (type(fit)is not dict or set(fit)!={"model_class","best_iteration","current_iteration","processor_fit_window","processors","rehydration"}
            or fit["model_class"]!="qlib.contrib.model.gbdt.LGBModel" or fit["processor_fit_window"]!=options["train"]
            or fit["rehydration"]!="OFFICIAL_DATAHANDLER_IT_LS_WITHOUT_REFIT"
            or type(fit["best_iteration"])is not int or type(fit["current_iteration"])is not int
            or not 0<=fit["best_iteration"]<=fit["current_iteration"]<=options["num_boost_round"]):fail("QLIB_FIT_AUDIT_INVALID")
    if type(fit["processors"])is not list or len(fit["processors"])!=5:fail("QLIB_FIT_AUDIT_INVALID")
    expected_processors=[("infer_processors","ProcessInf"),("infer_processors","ZScoreNorm"),("infer_processors","Fillna"),
        ("learn_processors","DropnaLabel"),("learn_processors","CSZScoreNorm")]
    for processor,expected in zip(fit["processors"],expected_processors):
        if type(processor)is not dict or set(processor)!={"role","class","native_state_sha256"}:fail("QLIB_FIT_AUDIT_INVALID")
        if (processor["role"],processor["class"])!=(expected[0],"qlib.data.dataset.processor."+expected[1]):fail("QLIB_FIT_AUDIT_INVALID")
        hex_digest(processor["native_state_sha256"])
    for position in value.get("positions",[]):
        for field in ("cash","value"):
            if type(position.get(field))not in (int,float) or not math.isfinite(position[field]):fail("QLIB_POSITION_VALUE_INVALID")
        for symbol,amount in position.get("amounts",{}).items():
            if symbol not in SYMBOLS or type(amount)not in (int,float) or not math.isfinite(amount) or amount<0:fail("QLIB_POSITION_VALUE_INVALID")
    # Numeric bools are not native metric/portfolio values, even though Python
    # bool compares equal to 0/1. Input Decimal fingerprint remains separate.
    validate_numeric_result(value)
    # Reject unknown/non-JSON objects, NaN and overlarge primitive payloads.
    if len(json.dumps(value,allow_nan=False).encode())>2*1024**2:fail("QLIB_RESULT_TOO_LARGE")
    return value
