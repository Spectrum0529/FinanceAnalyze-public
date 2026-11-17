"""Owned checkpoint freeze/status/staging; import has no runtime effects."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

from .jobs import JobService,parse_run
from .ta_native import require_real_context
from .ta_resume_plan import make_resume_plan,encode,digest,reject,pin,exact,eligible_terminal,resumable_budget,NATIVE_ERROR_CODES

MAXIMUM=64*1024**2


def directories(path):
    path=Path(path)
    if not path.is_absolute() or ".."in path.parts:reject("TA_RESUME_DIRECTORY_INVALID")
    for item in reversed((path,*path.parents)):
        info=item.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:reject("TA_RESUME_DIRECTORY_ALIAS")
    return path


def file_info(path):
    path=Path(path);directories(path.parent);info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or getattr(info,"st_file_attributes",0)&0x400
            or not 0<=info.st_size<=MAXIMUM):reject("TA_RESUME_FILE_ALIAS_OR_LIMIT")
    return info


def identity(info):return (info.st_dev,info.st_ino,info.st_nlink,info.st_size,info.st_mtime_ns)


def read_file(path):
    path=Path(path);before=file_info(path)
    with path.open("rb") as stream:
        if identity(os.fstat(stream.fileno()))!=identity(before):reject("TA_RESUME_FILE_CHANGED")
        raw=stream.read(MAXIMUM+1)
        if identity(os.fstat(stream.fileno()))!=identity(before):reject("TA_RESUME_FILE_CHANGED")
    if len(raw)>MAXIMUM or identity(file_info(path))!=identity(before):reject("TA_RESUME_FILE_CHANGED")
    return raw


def file_pin(path):
    raw=read_file(path);return {"path":str(Path(path)),"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}


def write_new(path,raw):
    path=Path(path);directories(path.parent)
    if len(raw)>MAXIMUM:reject("TA_RESUME_FILE_LIMIT")
    with path.open("xb") as stream:
        opened=os.fstat(stream.fileno());current=file_info(path)
        if (opened.st_dev,opened.st_ino,opened.st_nlink)!=(current.st_dev,current.st_ino,1):reject("TA_RESUME_FILE_CHANGED")
        stream.write(raw);stream.flush()
    if read_file(path)!=raw:reject("TA_RESUME_WRITE_UNPROVEN")


@dataclass(frozen=True)
class TAResumeStage:
    plan: object
    receipt: tuple

    @property
    def native_resume(self):
        return {"binding_hash":self.plan.binding_hash,"checkpoint_manifest_hash":self.plan.checkpoint_manifest_hash,"consumed":self.plan.consumed}


@dataclass(frozen=True)
class TAResumeStore:
    service: object
    attempt_base: Path
    workflow: object
    source_identity: str
    worker_lock_sha256: str
    profile: str="ta-product-native-server-1"

    def __post_init__(self):
        if type(self.service)is not JobService or not Path(self.attempt_base).is_absolute():reject("TA_RESUME_STORE_BINDING_INVALID")
        pin(self.source_identity);pin(self.worker_lock_sha256)
        if self.profile not in {"ta-product-native-server-1","ta-native-offline-one-two-r1"}:reject("TA_RESUME_STORE_BINDING_INVALID")

    def root(self,owner,run):
        if (type(owner)is not str or not owner or type(run)is not str or not run or len(run)>128 or not run[0].isalnum()
                or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in run)):
            reject("TA_RESUME_NAMESPACE_INVALID")
        return Path(self.attempt_base)/hashlib.sha256(owner.encode()).hexdigest()/run

    def row(self,run_id,owner):
        with closing(self.service.database.connect()) as con:
            row=con.execute("SELECT j.*,r.spec_json,r.parent_run_id FROM jobs j JOIN runs r USING(run_id) WHERE j.run_id=?",(run_id,)).fetchone()
            if row is None or row["owner_id"]!=owner:reject("TA_RESUME_OWNER_OR_SOURCE_NOT_FOUND")
            events=con.execute("SELECT kind,metadata_json FROM job_events WHERE run_id=? ORDER BY event_id",(run_id,)).fetchall()
        return dict(row),parse_run(row["spec_json"]),[(item["kind"],item["metadata_json"]) for item in events]

    def core(self,row,*,active=False):
        return {key:row[key] for key in ("owner_id","job_id","run_id","fencing_token","status","parent_run_id","lease_owner","cancel_requested")}|{
            "lease_until":None if row["lease_until"]is None else str(row["lease_until"]),"lease_current":active,"result_present":row["result_json"]is not None}

    def runtime_identity(self,row,spec):
        definition=self.service.registry.get(spec.handler)
        if (self.service.registry.current_code_hash(definition.handler)!=spec.code_hash or definition.code_hash!=spec.code_hash
                or self.service.dependency_hash!=spec.dependency_hash):reject("TA_RESUME_CURRENT_CODE_OR_DEPENDENCY_CHANGED")
        return {"config_sha256":digest(json.loads(row["parameters_json"])),"code_sha256":spec.code_hash,"dependency_sha256":spec.dependency_hash,
            "source_manifest_sha256":self.source_identity,"worker_lock_sha256":self.worker_lock_sha256}

    def outcome(self,root):
        raw=read_file(root/"parent-outcome.json");value=json.loads(raw)
        if (value.get("source_identity")!=self.source_identity or value.get("profile",{}).get("profile")!=self.profile
                or value.get("admitted")is not True or value.get("child_reaped")is not True or value.get("cleanup_failed")is not False
                or value.get("core_committed")is not False or value.get("checkpoint_ack_confirmed")is not False
                or type(value.get("exit_code"))is not int or value["exit_code"]==0):reject("TA_RESUME_CLOSED_OUTCOME_UNPROVEN")
        job=value.get("held_job_final",{})
        if (job.get("kernel_final_empty")is not True or job.get("active_processes")!=0 or job.get("pids")!=[]
                or type(job.get("total_processes_started"))is not int or job["total_processes_started"]!=1):reject("TA_RESUME_CLOSED_OUTCOME_UNPROVEN")
        return value,hashlib.sha256(raw).hexdigest()

    def verify_files(self,root,rows):
        for row in rows:
            info=file_pin(root/row["relative"])
            if info["bytes"]!=row["bytes"] or info["sha256"]!=row["sha256"]:reject("TA_RESUME_CHECKPOINT_BYTES_CHANGED")

    def failure_cause(self,root):
        raw=read_file(root/"evidence/child-failure.json");record=json.loads(raw)
        if (record.get("scope")!="PRODUCT_SERVER_PROFILE" or record.get("checkpoint_action")!="RETAIN"
                or record.get("native_success_claimed")is not False or record.get("failure_evidence_unproven")is not False
                or type(record.get("error_code"))is not str or record["error_code"]not in NATIVE_ERROR_CODES):
            reject("TA_RESUME_TERMINAL_CAUSE_UNPROVEN")
        return record["error_code"],hashlib.sha256(raw).hexdigest()

    def capture_closed(self,session):
        # Called by the owned supervisor after its close/reap/outcome write.
        # Core may still be RUNNING; this freezes bytes, not a terminal claim.
        context=session.context;require_real_context(context)
        if context.service is not self.service or session.binding.source_identity!=self.source_identity:reject("TA_RESUME_CONTEXT_INVALID")
        root=directories(self.root(context.run.owner_id,context.run.run_id))
        if Path(session.attempt)!=root:reject("TA_RESUME_NAMESPACE_INVALID")
        outcome,outcome_hash=self.outcome(root)
        terminal_code,failure_hash=self.failure_cause(root)
        failure=session.failure_record
        if type(failure)is not dict or type(failure.get("consumed"))is not dict or type(failure.get("trace"))is not list:
            reject("TA_RESUME_ACTUAL_FAILURE_BUDGET_MISSING")
        binding=self.workflow.binding(session.admission["binding"])
        if any(binding[key]!=value for key,value in (("owner_id",context.run.owner_id),("run_id",context.run.run_id),("job_id",context.job_id),("fencing_token",context.fencing_token))):reject("TA_RESUME_CONTEXT_INVALID")
        spent=self.workflow.consumed(failure["consumed"],binding["options"])
        if len(failure["trace"])>4096:reject("TA_RESUME_FAILURE_TRACE_LIMIT")
        row,spec,_=self.row(context.run.run_id,context.run.owner_id)
        if row["job_id"]!=context.job_id or row["fencing_token"]!=context.fencing_token:reject("TA_RESUME_CONTEXT_INVALID")
        rows=[];allowed={binding["ticker"]+".db"+suffix for suffix in ("","-wal","-shm","-journal")}
        for item in outcome.get("checkpoint_files",[]):
            relative=Path(item["path"])
            if relative.is_absolute() or relative.parent!=Path("cache/checkpoints") or relative.name not in allowed:reject("TA_RESUME_CHECKPOINT_FILE_INVALID")
            rows.append({"relative":relative.as_posix(),"bytes":item["bytes"],"sha256":item["sha256"]})
        checkpoint={"state":"RETAINED_WRITERS_CLOSED","files":rows}
        record={"schema":"TA_CLOSED_CHECKPOINT_BYTES_2","binding":binding,"identity":self.runtime_identity(row,spec),"checkpoint":checkpoint,
            "consumed":spent,"failure_trace_sha256":digest(failure["trace"]),"outcome_sha256":outcome_hash,"terminal_code":terminal_code,"failure_sha256":failure_hash}
        # Validate fixed file schema now. Core terminal state is re-read later;
        # this closed-byte record creates no job or resume permission.
        source=self.source_record(record,row)
        self.validate_checkpoint_shape(source)
        self.verify_files(root,rows)
        write_new(root/"retained-checkpoint.json",encode(record))
        self.verify_files(root,rows)
        return file_pin(root/"retained-checkpoint.json")

    def validate_checkpoint_shape(self,source):
        binding=self.workflow.binding(source["binding"]);files=source["checkpoint"]["files"]
        name="cache/checkpoints/"+binding["ticker"]+".db";allowed={name+suffix for suffix in ("","-wal","-shm","-journal")}
        names=set();total=0
        if type(files)is not list or not 1<=len(files)<=4:reject("TA_RESUME_CHECKPOINT_FILE_INVALID")
        for row in files:
            exact(row,{"relative","bytes","sha256"});pin(row["sha256"])
            if (row["relative"]not in allowed or row["relative"]in names or type(row["bytes"])is not int or not 0<row["bytes"]<=MAXIMUM):reject("TA_RESUME_CHECKPOINT_FILE_INVALID")
            names.add(row["relative"]);total+=row["bytes"]
        if name not in names or total>MAXIMUM:reject("TA_RESUME_CHECKPOINT_FILE_INVALID")

    def source_record(self,record,row):
        return {"schema":"TA_OWNED_RETAINED_CHECKPOINT_METADATA_2","binding":record["binding"],"identity":record["identity"],"core":self.core(row),
            "process":{"child_reaped":True,"kernel_final_empty":True,"writers_closed":True,"core_committed":False,"checkpoint_ack_confirmed":False,"outcome_sha256":record["outcome_sha256"],"failure_sha256":record["failure_sha256"],"failure_trace_sha256":record["failure_trace_sha256"]},
            "checkpoint":record["checkpoint"],"consumed":record["consumed"],"terminal_code":record["terminal_code"]}

    def source(self,owner,run):
        row,spec,events=self.row(run,owner)
        if (row["status"]not in {"FAILED","CANCELLED"} or row["result_json"]is not None or row["lease_owner"]is not None or row["lease_until"]is not None
                or not events or events[-1][0]!=row["status"] or any(kind=="SUCCEEDED" for kind,_ in events)):
            reject("TA_RESUME_SOURCE_NOT_TERMINAL_RETAINED")
        root=directories(self.root(owner,run));raw=read_file(root/"retained-checkpoint.json");record=json.loads(raw)
        exact(record,{"schema","binding","identity","checkpoint","consumed","failure_trace_sha256","outcome_sha256","terminal_code","failure_sha256"})
        if record["schema"]!="TA_CLOSED_CHECKPOINT_BYTES_2":reject("TA_RESUME_METADATA_SCHEMA_INVALID")
        pin(record["failure_trace_sha256"])
        binding=self.workflow.binding(record["binding"])
        if (any(binding[key]!=row[key] for key in ("owner_id","job_id","run_id","fencing_token")) or record["identity"]!=self.runtime_identity(row,spec)):
            reject("TA_RESUME_CORE_BINDING_INVALID")
        outcome,outcome_hash=self.outcome(root)
        if record["outcome_sha256"]!=outcome_hash:reject("TA_RESUME_CLOSED_OUTCOME_UNPROVEN")
        terminal_code,failure_hash=self.failure_cause(root)
        if terminal_code!=record["terminal_code"] or failure_hash!=record["failure_sha256"]:reject("TA_RESUME_TERMINAL_CAUSE_UNPROVEN")
        eligible_terminal(terminal_code)
        source=self.source_record(record,row);self.validate_checkpoint_shape(source)
        resumable_budget(source["consumed"],binding["options"],self.workflow);self.verify_files(root,source["checkpoint"]["files"])
        return source,spec,root,hashlib.sha256(raw).hexdigest()

    def status(self,principal,job):
        if type(job)is not dict or getattr(principal,"user_id",None)!=job.get("owner_id"):return {"available":False,"reason_code":"CHECKPOINT_OWNER_MISMATCH"}
        try:
            source,_,_,_=self.source(principal.user_id,job["run_id"])
            if source["core"]["job_id"]!=job["job_id"]:reject("TA_RESUME_CORE_BINDING_INVALID")
            if source["consumed"]["wall_millis"]>=source["binding"]["options"]["wall_seconds"]*1000:reject("TA_RESUME_WALL_BUDGET_EXHAUSTED")
            if "provider_input_tokens" in source["binding"]["options"]:
                with closing(self.service.database.connect()) as con:
                    if con.execute("SELECT 1 FROM runs WHERE owner_id=? AND parent_run_id=? LIMIT 1",
                                   (principal.user_id,job["run_id"])).fetchone():
                        return {"available":False,"reason_code":"TA_PROVIDER_CHECKPOINT_ALREADY_CONTINUED"}
            return {"available":True,"reason_code":"VERIFIED_RETAINED_CHECKPOINT"}
        except (OSError,ValueError,KeyError,TypeError):return {"available":False,"reason_code":"NO_VERIFIED_NATIVE_CHECKPOINT"}

    def stage(self,context,current_binding):
        require_real_context(context)
        if context.service is not self.service or not context.run.parent_run_id:reject("TA_RESUME_CONTEXT_INVALID")
        context.require_active();owner=context.run.owner_id
        source,spec,source_root,source_file_hash=self.source(owner,context.run.parent_run_id)
        row,current_spec,_=self.row(context.run.run_id,owner)
        if (current_spec!=context.run or row["job_id"]!=context.job_id or row["fencing_token"]!=context.fencing_token or row["lease_owner"]!=context.worker_id):reject("TA_RESUME_CONTEXT_INVALID")
        for field in ("strategy","config","data","model","model_reason","seed","mode","handler"):
            if getattr(spec,field)!=getattr(context.run,field):reject("TA_RESUME_RUN_SPEC_CHANGED")
        with closing(self.service.database.connect()) as con:
            if con.execute("SELECT count(*) FROM runs r JOIN jobs j USING(run_id) WHERE r.parent_run_id=? AND j.job_id!=? AND j.status IN ('QUEUED','RUNNING','SUCCEEDED')",(spec.run_id,context.job_id)).fetchone()[0]:reject("TA_RESUME_OTHER_CHILD_ACTIVE_OR_SUCCEEDED")
            if "provider_input_tokens" in current_binding["options"]:
                first=con.execute("SELECT run_id FROM runs WHERE owner_id=? AND parent_run_id=? ORDER BY rowid LIMIT 1",
                                  (owner,spec.run_id)).fetchone()
                if first is None or first["run_id"]!=context.run.run_id:
                    reject("TA_PROVIDER_CHECKPOINT_ALREADY_CONTINUED")
        target={"binding":current_binding,"identity":self.runtime_identity(row,current_spec),"core":self.core(row,active=True)}
        plan=make_resume_plan(source,target,principal_owner_id=owner,workflow=self.workflow)
        target_root=directories(self.root(owner,context.run.run_id));directories(target_root/"evidence")
        write_new(target_root/"evidence/resume-stage-start.json",encode({"schema":"TA_RESUME_STAGE_START_1","source_manifest_sha256":plan.checkpoint_manifest_hash,
            "source_run_id":plan.source_run_id,"target_run_id":plan.target_run_id,"target_fencing_token":plan.target_fencing_token}))
        try:
            cache=target_root/"cache"
            try:cache.lstat()
            except FileNotFoundError:cache.mkdir()
            directories(cache);destination=cache/"checkpoints";destination.mkdir(exist_ok=False);directories(destination)
            for item in plan.files:
                context.require_active();raw=read_file(source_root/item["relative"])
                if len(raw)!=item["bytes"] or hashlib.sha256(raw).hexdigest()!=item["sha256"]:reject("TA_RESUME_CHECKPOINT_BYTES_CHANGED")
                write_new(target_root/item["relative"],raw)
            self.verify_files(source_root,plan.files);self.verify_files(target_root,plan.files)
            fresh,_,_,fresh_file_hash=self.source(owner,context.run.parent_run_id)
            if fresh!=source or fresh_file_hash!=source_file_hash:reject("TA_RESUME_SOURCE_CHANGED_DURING_STAGE")
            context.require_active()
            receipt={"schema":"TA_RESUME_STAGE_COMPLETE_1","source_job_id":plan.source_job_id,"source_run_id":plan.source_run_id,
                "owner_namespace_sha256":plan.owner_namespace_sha256,
                "target_job_id":plan.target_job_id,"target_run_id":plan.target_run_id,"target_fencing_token":plan.target_fencing_token,
                "source_manifest_sha256":plan.checkpoint_manifest_hash,"closed_record_sha256":source_file_hash,"binding_hash":plan.binding_hash,"files":plan.files,
                "source_rechecked_unchanged":True,"target_bytes_rechecked":True,"current_real_core_lease_checked":True}
            path=target_root/"evidence/resume-stage-complete.json";write_new(path,encode(receipt));info=file_pin(path)
            return TAResumeStage(plan,tuple(info.items()))
        except BaseException:
            write_new(target_root/"evidence/resume-stage-failure.json",encode({"status":"FAILED_RETAIN_SOURCE_AND_PARTIAL_TARGET","source_unchanged_required":True,"automatic_retry":False}))
            raise

    def verify_staged(self,context,stage):
        require_real_context(context);context.require_active()
        if type(stage)is not TAResumeStage or context.service is not self.service:reject("TA_RESUME_STAGE_INVALID")
        plan=stage.plan
        if (context.job_id!=plan.target_job_id or context.run.run_id!=plan.target_run_id or context.fencing_token!=plan.target_fencing_token
                or context.run.parent_run_id!=plan.source_run_id):reject("TA_RESUME_STAGE_INVALID")
        source,_,_,_=self.source(context.run.owner_id,plan.source_run_id)
        if digest(source)!=plan.checkpoint_manifest_hash:reject("TA_RESUME_SOURCE_CHANGED_DURING_STAGE")
        info=dict(stage.receipt);path=self.root(context.run.owner_id,context.run.run_id)/"evidence/resume-stage-complete.json"
        if info!=file_pin(path):reject("TA_RESUME_STAGE_INVALID")
        self.verify_files(self.root(context.run.owner_id,context.run.run_id),plan.files)
        return stage.native_resume
