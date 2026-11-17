"""Independent product-session adapter; initial fixture host is never aliased.

Default model/backend is unconfigured. This module makes no model, process,
database or application call on import. Trusted composition supplies exact
project-worker/source/model/security bindings; callers supply no file paths.
"""
from contextlib import closing
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import threading
import time

from .errors import PlatformError
from .ta_native import (StagedTAResult, _TASession, committed_receipt, require_real_context,
                        FilePin, _protect, _regular, load_pure, _disk_budget)
from .ta_protocol import checked, encoded, digest
from .ta_process import LIMITS as INITIAL_LIMITS, TAOwnedJob as InitialOwnedJob

ENTRYPOINT="finance_platform.research.debate_native:execute_native_request"
LIMITS=replace(INITIAL_LIMITS,profile="ta-product-native-server-1")
FAMILY="tradingagents-native-report-1"
REQUEST_FIELDS={"binding","input_bundle","parent_run_id","source_manifest_sha256","release","native_entrypoint"}


def fail(code):raise PlatformError(code)


class ProductOwnedJob(InitialOwnedJob):
    """Deliberately retain the same numeric bounds with a distinct profile id."""
    def snapshot(self):
        return {**super().snapshot(),"profile":LIMITS.profile}


def _saved_projection(context,saved_hash):
    # research_results is immutable in the existing research migrations. Read
    # the exact new owner/job/run row, never an arbitrary path or old backup.
    with closing(context.service.database.connect()) as con:
        row=con.execute("SELECT owner_id,job_id,run_id,result_json,result_hash FROM research_results WHERE run_id=?",(context.run.run_id,)).fetchone()
    if (row is None or row["owner_id"]!=context.run.owner_id or row["job_id"]!=context.job_id
            or row["run_id"]!=context.run.run_id or row["result_hash"]!=saved_hash):
        fail("TA_SAVED_RESULT_BINDING_MISMATCH")
    from finance_platform.research.payload_storage import hydrate
    public=hydrate(context.service, row["owner_id"], "reports", row["result_json"])
    if (type(public)is not dict or public.get("family")!=FAMILY or public.get("run_id")!=context.run.run_id
            or public.get("job_id")!=context.job_id or digest(public)!=saved_hash):
        fail("TA_SAVED_RESULT_HASH_MISMATCH")
    return public


@dataclass(frozen=True)
class ProductNativeHandle:
    _session: object
    _raw: bytes

    @property
    def result(self):return checked(self._raw)


def _product_admission(request,attempt,resume_stage):
    resume=None if resume_stage is None else resume_stage.native_resume
    receipt_hash=None if resume_stage is None else dict(resume_stage.receipt)["sha256"]
    return {"case":"SERVER_PROFILE","binding":request["binding"],"request":request,"attempt_root":str(attempt),
            "resume":resume,"resume_stage_sha256":receipt_hash}


class ProductSession(_TASession):
    """Retains native result while typed transport carries compact core JSON."""
    def __init__(self,binding,context,request,fixtures,marker,*,resume_store=None,resume_stage=None,provider_store=None):
        admission=_product_admission(request,marker["attempt_root"],resume_stage)
        super().__init__(binding,context,admission,fixtures,marker)
        self.resume_store=resume_store
        self.failure_record=None
        self.core_result=None
        self.native_result_hash=None
        self.saved_result_hash=None
        self.typed_returned=False
        self.provider_session=None
        if provider_store is not None:
            from .ta_external_model import ProviderSession
            self.provider_session=ProviderSession(context,request,
                None if resume_stage is None else resume_stage.plan.consumed,provider_store)

    def _environment(self):
        environment=super()._environment()
        return _bind_local_appdata_environment(environment,self.attempt,self.binding.attempt_base,self.context.run.owner_id,self.context.run.run_id)

    def wait_handle(self):
        self.staged.wait(LIMITS.wall_seconds+15)
        if self.result is None or self.failure is not None:
            self.abort();fail(self.failure or "TA_PRODUCT_NO_NATIVE_RESULT")
        return ProductNativeHandle(self,encoded(self.result))

    def _validate_staged(self,value):
        if type(value)is not dict or set(value)!={"result","trace","state_hash","effects"}:
            fail("TA_PRODUCT_NATIVE_RESULT_INVALID")
        result=value["result"];bound=self.admission["binding"]
        if (type(result)is not dict or len(encoded(result))>LIMITS.result_bytes or result.get("family")!=FAMILY
                or result.get("scope")!="DEVELOPMENT_RESEARCH" or result.get("model_profile",{}).get("mode")!="SERVER_PROFILE"
                or type(result.get("fencing_token"))is not int
                or result.get("publication_gate")!="CORE_JOB_SUCCEEDED_REQUIRED"
                or result.get("checkpoint_state")!="RETAINED_UNTIL_CORE_JOB_SUCCEEDED_ACK"
                or any(result.get(key)!=bound[key] for key in ("owner_id","logical_task_id","run_id","job_id","fencing_token",
                    "data_reference","snapshot_content_hash","selected_view_hash","model_profile","tools_manifest_hash","as_of"))
                or digest(result.get("native_state"))!=value["state_hash"]
                or type(value["trace"])is not list or not value["trace"] or len(value["trace"])>LIMITS.trace_rows):
            fail("TA_PRODUCT_NATIVE_RESULT_BINDING_MISMATCH")
        effects=value["effects"]
        if (type(effects)is not dict or any(type(effects.get(key))is not int for key in ("unexpected","expected_ipv6","expected_windows_refusals"))
                or effects.get("unexpected")!=0 or effects.get("expected_ipv6")!=1 or effects.get("expected_windows_refusals") not in (0,3)):
            fail("TA_PRODUCT_EFFECTS_UNPROVEN")
        from .ta_team_admission import fallback_accounting
        if not fallback_accounting([None]*effects['expected_windows_refusals'],effects.get('windows_fallback')):
            fail("TA_PRODUCT_EFFECTS_UNPROVEN")
        if (type(effects.get("threads_started"))is not int or not 0<=effects["threads_started"]<=128
                or type(effects.get("sqlite_connects"))is not int or not 1<=effects["sqlite_connects"]<=8):
            fail("TA_PRODUCT_EFFECTS_UNPROVEN")
        for key,maximum in (("model",32),("tool",16),("chars",160000),("wall_millis",120000)):
            if type(result.get("budget",{}).get(key))is not int or not 0<=result["budget"][key]<=maximum:
                fail("TA_PRODUCT_BUDGET_INVALID")
        self._provider_counts(result["budget"])
        from .model_usage import retain_trace
        retain_trace(self.context,value['trace'],source='STAGED')
        return result

    def _provider_counts(self,spent):
        if self.provider_session is None:
            return
        from finance_platform.research.tradingagents_workflow import consumed
        options=self.admission["binding"]["options"]
        counters=consumed(spent,options)
        if any(counters[key]!=value for key,value in self.provider_session.meter.spent.items()):
            fail("TA_PROVIDER_BUDGET_STATE_INVALID")

    def stage(self,context,handle,core_result,native_result_hash,saved_result_hash):
        require_real_context(context)
        with self.lock:
            if (context is not self.context or type(handle)is not ProductNativeHandle or handle._session is not self
                    or self.typed_returned or self.failure is not None or self.cancelled.is_set() or self.result is None
                    or handle.result!=self.result or digest(self.result)!=native_result_hash):
                fail("TA_PRODUCT_STAGE_BINDING_MISMATCH")
            context.require_active()
            expected={"family":FAMILY,"run_id":context.run.run_id,"result_url":"/api/v1/research/results/"+context.run.run_id,
                      "native_result_hash":native_result_hash,"saved_result_hash":saved_result_hash}
            if type(core_result)is not dict or core_result!=expected:
                fail("TA_PRODUCT_COMPACT_RESULT_MISMATCH")
            _saved_projection(context,saved_result_hash)
            self.core_result=checked(encoded(core_result))
            self.native_result_hash=native_result_hash
            self.saved_result_hash=saved_result_hash
            self.typed_returned=True
            return StagedTAResult(self,core_result)

    def finish_core(self,context,result):
        require_real_context(context)
        with self.lock:
            if (context is not self.context or not self.typed_returned or self.receipt is not None
                    or self.failure is not None or self.cancelled.is_set() or result!=self.core_result
                    or digest(self.result)!=self.native_result_hash
                    or result.get("native_result_hash")!=self.native_result_hash
                    or result.get("saved_result_hash")!=self.saved_result_hash):
                fail("TA_PRODUCT_CORE_RESULT_MISMATCH")
            context.require_active()
            _saved_projection(context,self.saved_result_hash)
            context.service.finish(context.job_id,context.worker_id,context.fencing_token,result)
            self.committed=True
            core_receipt=committed_receipt(context,result)
            _saved_projection(context,self.saved_result_hash)
            # The actual core commit/event proves compact JSON. Native ACK has
            # its own retained hash; neither public nor compact hash substitutes.
            self.receipt={**core_receipt,"result_hash":self.native_result_hash}
        if not self.done.wait(LIMITS.wall_seconds+15) or self.failure is not None or not self.acknowledged:
            self.abort();fail("TA_CORE_SUCCEEDED_CHECKPOINT_ACK_UNPROVEN")
        return dict(self.receipt)

    # _drive is defined concretely in ta_product_driver.ProductSessionDriver,
    # using the owned native lifecycle, fixed product packet and product child.


@dataclass(frozen=True)
class TANativeProductLauncher:
    binding: object=None
    resume_store: object=None
    provider_store_factory: object=None

    def _binding(self):
        from .ta_product_binding import ProductBinding
        from .ta_product_binding import PersonalTeamBinding
        if type(self.binding) not in (ProductBinding,PersonalTeamBinding):fail("TA_MODEL_BACKEND_UNCONFIGURED")
        self.binding.verify()
        return self.binding

    def _store(self,binding,context=None):
        from .ta_resume_store import TAResumeStore
        store=self.resume_store
        if (type(store)is not TAResumeStore or store.source_identity!=binding.source_identity
                or Path(store.attempt_base)!=Path(binding.attempt_base) or store.worker_lock_sha256!=binding.files["lock"].sha256
                or store.profile!=LIMITS.profile or Path(store.workflow.__file__).absolute()!=binding.files["workflow"].path
                or (context is not None and store.service is not context.service)):
            fail("TA_RESUME_STORE_BINDING_REQUIRED")
        binding.files["workflow"].read_verified()
        return store

    def describe_verified(self):
        """Fresh request-local metadata after one complete binding check.

        This description is not an execution admission. Native execution and
        source/owner/fence review retain their own complete checks.
        """
        binding = self._binding()
        return {
            "release": binding.release,
            "worker_lock_bytes": binding.files["lock"].read_verified(),
            "source_manifest_sha256": binding.source_identity,
            "supports_agent_teams": False,
        }

    @property
    def worker_lock_bytes(self):return self._binding().files["lock"].read_verified()
    @property
    def source_manifest_sha256(self):return self._binding().source_identity
    @property
    def release(self):return self._binding().release

    def prepare_attempt(self,context,attempt):
        require_real_context(context);context.require_active();binding=self._binding()
        root=binding.attempt(context)
        if Path(attempt).absolute()!=root or not root.is_dir() or any(root.iterdir()):fail("TA_PRODUCT_FRESH_ATTEMPT_REQUIRED")
        _regular(root.parent,root.name,directory=True);_protect(root)
        for name in ("evidence","home/AppData/Roaming","home/AppData/Local","tmp"):(root/name).mkdir(parents=True,exist_ok=True)
        fixtures=[]
        for name in (".env",".env.enterprise"):
            path=root/name
            with path.open("xb"):pass
            info=path.lstat();fixtures.append({"filename":name,"path":str(path),"bytes":0,"sha256":hashlib.sha256(b"").hexdigest(),"st_dev":info.st_dev,"st_ino":info.st_ino,"st_nlink":info.st_nlink})
        with (root/"product-prepared.json").open("xb") as stream:stream.write(encoded({"owner_id":context.run.owner_id,"run_id":context.run.run_id,
            "job_id":context.job_id,"fencing_token":context.fencing_token,"source_identity":binding.source_identity,"fixtures":fixtures}))

    def execute(self,context,request,attempt):
        require_real_context(context);context.require_active();binding=self._binding()
        root=binding.attempt(context)
        if Path(attempt).absolute()!=root or type(request)is not dict or set(request)!=REQUEST_FIELDS or request["native_entrypoint"]!=ENTRYPOINT:
            fail("TA_PRODUCT_REQUEST_INVALID")
        bound=request["binding"]
        if (bound.get("model_profile",{}).get("mode")!="SERVER_PROFILE" or bound["model_profile"].get("profile_sha256")!=binding.model_profile_sha256
                or any(bound.get(key)!=value for key,value in (("owner_id",context.run.owner_id),("run_id",context.run.run_id),("job_id",context.job_id),("fencing_token",context.fencing_token)))
                or digest(request["input_bundle"])!=bound.get("tools_manifest_hash") or request["release"]!=binding.release
                or request["source_manifest_sha256"]!=binding.source_identity
                or request["parent_run_id"]!=context.run.parent_run_id):
            fail("TA_PRODUCT_REQUEST_BINDING_MISMATCH")
        approval=request["input_bundle"].get("provider_approval")
        if approval is not None:
            from .auth import Principal
            principal=Principal(context.run.owner_id,context.run.owner_id,"researcher")
            frozen=context.service.versions.get(principal,context.run.model)
            if (not callable(self.provider_store_factory) or frozen.get("provider_approval")!=approval
                    or frozen.get("team_snapshot")!=request["input_bundle"].get("team_snapshot")
                    or approval.get("options")!=bound["options"]):
                fail("TA_EXTERNAL_EXECUTION_BINDING_CHANGED")
        elif self.provider_store_factory is not None or "provider_input_tokens" in bound["options"]:
            fail("TA_EXTERNAL_EXECUTION_BINDING_CHANGED")
        marker=checked((root/"product-prepared.json").read_bytes())
        if any(marker.get(key)!=value for key,value in (("owner_id",context.run.owner_id),("run_id",context.run.run_id),("job_id",context.job_id),("fencing_token",context.fencing_token),("source_identity",binding.source_identity))):
            fail("TA_PRODUCT_PREPARED_MARKER_MISMATCH")
        binding.review(context,"SERVER_PROFILE",root)
        store=None if self.resume_store is None else self._store(binding,context)
        stage=None
        if context.run.parent_run_id is not None:
            if store is None:fail("TA_VERIFIED_RETRY_CHECKPOINT_REQUIRED")
            stage=store.stage(context,bound)
            store.verify_staged(context,stage)
        admission=_product_admission(request,root,stage)
        session_marker={"attempt_root":str(root),"admission_hash":digest(admission),"source_identity":binding.source_identity}
        with (root/"owner-marker.json").open("xb") as stream:stream.write(encoded(session_marker))
        from .ta_product_driver import ProductSessionDriver
        session=ProductSessionDriver(binding,context,request,marker["fixtures"],session_marker,
            resume_store=store,resume_stage=stage,
            **({} if approval is None else {"provider_store":self.provider_store_factory()}))
        session.start()
        return session.wait_handle()

    def stage_for_core_success(self,context,handle,*,core_result,native_result_hash,saved_result_hash):
        from .ta_product_driver import ProductSessionDriver
        binding=self._binding()
        if type(handle)is not ProductNativeHandle or type(handle._session)is not ProductSessionDriver or handle._session.binding is not self.binding:
            fail("TA_PRODUCT_RETAINED_HANDLE_REQUIRED")
        binding.review(context,"SERVER_PROFILE",handle._session.attempt)
        return handle._session.stage(context,handle,core_result,native_result_hash,saved_result_hash)

    def discard_staged(self,context,handle):
        require_real_context(context)
        from .ta_product_driver import ProductSessionDriver
        if type(handle)is not ProductNativeHandle or type(handle._session)is not ProductSessionDriver or handle._session.context is not context or handle._session.binding is not self.binding:
            fail("TA_PRODUCT_RETAINED_HANDLE_REQUIRED")
        handle._session.abort()

    def checkpoint_status(self,principal,job):
        if self.resume_store is None:
            return {"available":False,"reason_code":"NO_VERIFIED_NATIVE_CHECKPOINT"}
        try:
            binding=self._binding()
            return self._store(binding).status(principal,job)
        except PlatformError:
            return {"available":False,"reason_code":"NO_VERIFIED_NATIVE_CHECKPOINT"}


"""Pure owned-directory binding for platformdirs' existing Windows override."""

def _owned_local_appdata(attempt,attempt_base,owner_id,run_id):
    import stat
    root=Path(attempt);base=Path(attempt_base)
    if (not root.is_absolute() or not base.is_absolute() or ".."in root.parts or ".."in base.parts
            or type(owner_id)is not str or not owner_id or type(run_id)is not str or not run_id or len(run_id)>128
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in run_id)
            or not run_id[0].isalnum()):
        fail("TA_LOCAL_APPDATA_BINDING_INVALID")
    if root!=base/hashlib.sha256(owner_id.encode()).hexdigest()/run_id:
        fail("TA_LOCAL_APPDATA_OWNER_RUN_MISMATCH")
    target=root/"home/AppData/Local"
    # lstat each lexical component before descending. Never resolve/follow an
    # alias, consult Path.home/environment/registry, or call a known-folder API.
    components=list(reversed((root,*root.parents)))+[root/"home",root/"home/AppData",target,root/"evidence"]
    for item in components:
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400 or not stat.S_ISDIR(info.st_mode):
            fail("TA_LOCAL_APPDATA_ALIAS_OR_NON_DIRECTORY")
    return target


def _bind_local_appdata_environment(environment,attempt,attempt_base,owner_id,run_id):
    target=_owned_local_appdata(attempt,attempt_base,owner_id,run_id)
    name="WIN_PD_OVERRIDE_LOCAL_APPDATA"
    if (type(environment)is not dict or name in environment or environment.get("LOCALAPPDATA")!=str(target)
            or environment.get("HOME")!=str(Path(attempt)/"home") or environment.get("USERPROFILE")!=str(Path(attempt)/"home")):
        fail("TA_LOCAL_APPDATA_ENVIRONMENT_MISMATCH")
    result={**environment,name:str(target)}
    evidence={"schema":"FA013_TA_OWNED_LOCAL_APPDATA_OVERRIDE_8","variable":name,"value_relative":"home/AppData/Local",
        "absolute":True,"owner_run_bound":True,"all_directory_components_lstat_checked":True,"reparse_or_symlink_allowed":False,
        "host_environment_or_profile_or_registry_queried":False,"upstream_function_replaced":False}
    with (Path(attempt)/"evidence/local_appdata_override.r08.json").open("xb") as stream:
        stream.write(json.dumps(evidence,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode())
    return result
