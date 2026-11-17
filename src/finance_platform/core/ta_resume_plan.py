"""Pure resume metadata guard; never authorizes files, core jobs or native runs.

Trusted host must supply freshly read actual core/process/retention facts and
verify/stage every sealed checkpoint byte separately. API bodies are not facts.
No filesystem/database/model/Graph imports or effectful runtime binding here.
"""
from dataclasses import dataclass
import hashlib
import json
import re


class ResumePlanError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def reject(code):raise ResumePlanError(code)


def encode(value):return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()


def digest(value):return hashlib.sha256(encode(value)).hexdigest()


def exact(value,keys):
    if type(value)is not dict or set(value)!=set(keys):reject("TA_RESUME_METADATA_SCHEMA_INVALID")


def pin(value):
    if type(value)is not str or re.fullmatch("[0-9a-f]{64}",value)is None:reject("TA_RESUME_METADATA_PIN_INVALID")


@dataclass(frozen=True)
class TAResumeMetadataPlan:
    source_job_id: str
    source_run_id: str
    target_job_id: str
    target_run_id: str
    target_fencing_token: int
    owner_namespace_sha256: str
    binding_hash: str
    checkpoint_manifest_hash: str
    consumed_json: bytes
    files_json: bytes
    metadata_only: bool=True

    @property
    def consumed(self):return json.loads(self.consumed_json)

    @property
    def files(self):return json.loads(self.files_json)


NATIVE_ERROR_CODES=frozenset(('JOB_CANCELLED', 'TA_ACTUAL_MODEL_TRACE_REQUIRED', 'TA_ACTUAL_NATIVE_CONTROLS_REQUIRED', 'TA_ATTEMPT_ALIAS_REJECTED', 'TA_BACKTEST_BRIDGE_UNAVAILABLE', 'TA_BINDING_HASH_INVALID', 'TA_BINDING_ID_INVALID', 'TA_BINDING_INVALID', 'TA_BUDGET_STATE_INVALID', 'TA_CALL_BUDGET_EXCEEDED', 'TA_CALL_ID_INVALID', 'TA_CHECKPOINT_INTERRUPTED', 'TA_CHILD_INPUT_BINDING_MISMATCH', 'TA_COMPLETE_IMPORT_PROOF_REQUIRED', 'TA_DATA_ENTRY_BUDGET_EXCEEDED', 'TA_DATA_OWNER_OR_KIND_INVALID', 'TA_DATA_REFERENCE_INVALID', 'TA_EVIDENCE_ENGINE_MISMATCH', 'TA_EVIDENCE_REFERENCES_INVALID', 'TA_EVIDENCE_SCOPE_INVALID', 'TA_EVIDENCE_SOURCE_AUTHORIZATION_REQUIRED', 'TA_EVIDENCE_SOURCE_CHANGED', 'TA_EVIDENCE_SUMMARY_TOO_LARGE', 'TA_EVIDENCE_SYMBOL_MISMATCH', 'TA_FAILURE_EVIDENCE_UNPROVEN', 'TA_FAILURE_TRACE_INVALID', 'TA_FENCE_INVALID', 'TA_FIXTURE_BINDING_MISMATCH', 'TA_FUTURE_EVIDENCE_REJECTED', 'TA_FUTURE_INPUT_REJECTED', 'TA_FUTURE_TOOL_REQUEST_REJECTED', 'TA_GUARDED_PORTS_REQUIRED', 'TA_INPUT_BUNDLE_TOO_LARGE', 'TA_INPUT_ROWS_UNAVAILABLE', 'TA_INPUT_SYMBOL_UNAVAILABLE', 'TA_MODEL_BACKEND_UNCONFIGURED', 'TA_MODEL_PROFILE_BINDING_MISMATCH', 'TA_MODEL_PROFILE_INVALID', 'TA_NATIVE_ERROR_UNCLASSIFIED', 'TA_NATIVE_FINAL_STATE_MISSING', 'TA_NATIVE_REQUEST_INVALID', 'TA_NATIVE_SOURCE_BINDING_MISMATCH', 'TA_NATIVE_TOOL_BINDING_MISMATCH', 'TA_OPTIONS_BINDING_MISMATCH', 'TA_OPTIONS_INVALID', 'TA_OPTIONS_NOT_NORMALIZED', 'TA_OUTPUT_BUDGET_EXCEEDED', 'TA_OUTPUT_TYPE_INVALID', 'TA_OWNED_ATTEMPT_REQUIRED', 'TA_PORT_BINDING_MISMATCH', 'TA_PUBLISH_ACK_INVALID', 'TA_RECURSION_LIMIT_INVALID', 'TA_REPORT_SCOPE_INVALID', 'TA_REQUESTED_RESUME_CHECKPOINT_MISSING', 'TA_RESULT_TOO_LARGE', 'TA_RESUME_BINDING_MISMATCH', 'TA_RESUME_MANIFEST_INVALID', 'TA_SETTLEMENT_DATA_NOT_AUTHORIZED', 'TA_SYMBOL_INVALID', 'TA_TEMPORAL_BINDING_INVALID', 'TA_TOOL_SYMBOL_MISMATCH', 'TA_TOOL_WINDOW_INVALID', 'TA_WALL_BUDGET_EXCEEDED'))
BLOCKED_TERMINAL_CODES=frozenset(('TA_CALL_BUDGET_EXCEEDED', 'TA_FAILURE_EVIDENCE_UNPROVEN', 'TA_NATIVE_ERROR_UNCLASSIFIED', 'TA_OUTPUT_BUDGET_EXCEEDED', 'TA_WALL_BUDGET_EXCEEDED'))


def eligible_terminal(code):
    if type(code)is not str or code not in NATIVE_ERROR_CODES or code in BLOCKED_TERMINAL_CODES:
        reject("TA_RESUME_TERMINAL_CAUSE_NOT_ELIGIBLE")


def resumable_budget(value,options,workflow):
    spent=workflow.consumed(value,options)
    for key,limit in (("model",options["model_calls"]),("tool",options["tool_calls"]),("chars",options["output_chars"]),("wall_millis",options["wall_seconds"]*1000)):
        if spent[key]>=limit:reject("TA_RESUME_BUDGET_EXHAUSTED")
    if "provider_input_tokens" in options and (spent["provider_calls"]>=options["model_calls"]
            or spent["provider_input_tokens"]>=options["provider_input_tokens"]
            or options["provider_output_tokens"]-spent["provider_output_tokens"]<workflow.PROVIDER_MIN_OUTPUT_TOKENS):
        reject("TA_RESUME_BUDGET_EXHAUSTED")
    return spent


def make_resume_plan(source,target,*,principal_owner_id,workflow):
    """Validate immutable metadata only; no path stage or native resume token.

    The existing workflow supplies binding/resume-key/consumed validation. Its
    source must be pinned by composition; OFFLINE_FIXTURE and SERVER_PROFILE
    stay distinct. A successful return is not checkpoint availability or GO.
    """
    exact(source,{"schema","binding","identity","core","process","checkpoint","consumed","terminal_code"})
    exact(target,{"binding","identity","core"})
    if source["schema"]!="TA_OWNED_RETAINED_CHECKPOINT_METADATA_2":reject("TA_RESUME_METADATA_SCHEMA_INVALID")
    eligible_terminal(source["terminal_code"])
    previous=workflow.binding(source["binding"]);current=workflow.binding(target["binding"])
    if (type(principal_owner_id)is not str or previous["owner_id"]!=principal_owner_id or current["owner_id"]!=principal_owner_id):
        reject("TA_RESUME_OWNER_MISMATCH")
    if (previous["run_id"]==current["run_id"] or previous["job_id"]==current["job_id"]
            or previous["model_profile"].get("mode")=="UNCONFIGURED" or current["model_profile"].get("mode")=="UNCONFIGURED"):
        reject("TA_RESUME_FRESH_CONFIGURED_EXECUTION_REQUIRED")
    keys={"config_sha256","code_sha256","dependency_sha256","source_manifest_sha256","worker_lock_sha256"}
    for identity in (source["identity"],target["identity"]):
        exact(identity,keys)
        for value in identity.values():pin(value)
    if source["identity"]!=target["identity"]:reject("TA_RESUME_IDENTITY_CHANGED")
    binding_hash=workflow.resume_key(previous)
    if binding_hash!=workflow.resume_key(current):reject("TA_RESUME_LOGICAL_BINDING_CHANGED")
    fields={"owner_id","job_id","run_id","fencing_token","status","parent_run_id","lease_owner","lease_until",
        "lease_current","cancel_requested","result_present"}
    for facts,binding in ((source["core"],previous),(target["core"],current)):
        exact(facts,fields)
        if (any(facts[key]!=binding[key] for key in ("owner_id","job_id","run_id","fencing_token"))
                or type(facts["fencing_token"])is not int or type(facts["cancel_requested"])is not int
                or facts["cancel_requested"]not in (0,1) or type(facts["lease_current"])is not bool or facts["result_present"]is not False):
            reject("TA_RESUME_CORE_BINDING_INVALID")
    old=source["core"];new=target["core"]
    if (old["status"]not in {"FAILED","CANCELLED"} or old["lease_owner"]is not None or old["lease_until"]is not None or old["lease_current"]is not False):
        reject("TA_RESUME_SOURCE_NOT_TERMINAL_RETAINED")
    if (new["status"]!="RUNNING" or new["parent_run_id"]!=previous["run_id"] or new["lease_current"]is not True
            or type(new["lease_owner"])is not str or not new["lease_owner"] or type(new["lease_until"])is not str or not new["lease_until"]
            or new["cancel_requested"]!=0):
        reject("TA_RESUME_TARGET_NOT_ACTIVE_CHILD")
    process=source["process"]
    exact(process,{"child_reaped","kernel_final_empty","writers_closed","core_committed","checkpoint_ack_confirmed","outcome_sha256","failure_sha256","failure_trace_sha256"})
    if (any(process[key]is not True for key in ("child_reaped","kernel_final_empty","writers_closed"))
            or process["core_committed"]is not False or process["checkpoint_ack_confirmed"]is not False):
        reject("TA_RESUME_WRITERS_OR_PUBLICATION_UNPROVEN")
    for key in ("outcome_sha256","failure_sha256","failure_trace_sha256"):pin(process[key])
    checkpoint=source["checkpoint"];exact(checkpoint,{"state","files"})
    if checkpoint["state"]!="RETAINED_WRITERS_CLOSED" or type(checkpoint["files"])is not list or not 1<=len(checkpoint["files"])<=4:
        reject("TA_RESUME_CHECKPOINT_NOT_RETAINED")
    database="cache/checkpoints/"+previous["ticker"]+".db"
    allowed={database+suffix for suffix in ("","-wal","-shm","-journal")};names=set();total=0
    for row in checkpoint["files"]:
        exact(row,{"relative","bytes","sha256"})
        if (type(row["relative"])is not str or row["relative"]not in allowed or row["relative"]in names
                or type(row["bytes"])is not int or not 0<row["bytes"]<=64*1024**2):reject("TA_RESUME_CHECKPOINT_FILE_INVALID")
        pin(row["sha256"]);names.add(row["relative"]);total+=row["bytes"]
    if database not in names or total>64*1024**2:reject("TA_RESUME_CHECKPOINT_FILE_INVALID")
    spent=resumable_budget(source["consumed"],current["options"],workflow)
    # The manifest digest binds all declared source facts and file hashes. It
    # does not claim those hashes were measured or files safely copied here.
    return TAResumeMetadataPlan(previous["job_id"],previous["run_id"],current["job_id"],current["run_id"],current["fencing_token"],
        hashlib.sha256(principal_owner_id.encode()).hexdigest(),binding_hash,digest(source),encode(spent),
        encode(sorted(checkpoint["files"],key=lambda row:row["relative"])))
