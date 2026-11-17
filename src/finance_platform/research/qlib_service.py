"""Platform-owned VersionRefs/jobs/results around one official Qlib workflow.

Data authorization/staging is the FA014 adapter; process control is the FA013
adapter. Neither can be supplied by an API body. Missing adapters fail closed.
"""
from .payload_storage import task_binding
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import re

from finance_platform.core.contracts import VersionRef, RunMode, payload, fingerprint
from finance_platform.core.security import safe_json, now_text
from .data import ResearchError
from . import qlib_workflow as workflow
from . import native_results
from finance_platform.core.errors import PlatformError

TEMPLATE_ID="qlib-us-basic-close-open-volume-lgb-1"
LEGACY_TEMPLATE_ID="qlib-us-alpha158-lgb-1"


def validate_parameters(value):
    if type(value)is not dict or set(value)!={"export_reference","workflow","strategy_reference"}:
        raise ResearchError("QLIB_JOB_PARAMETERS_INVALID")
    for key in ("export_reference","strategy_reference"):workflow.reference(value[key])
    if type(value["workflow"])is not dict:raise ResearchError("QLIB_OPTIONS_INVALID")


class QlibResearchService:
    def __init__(self,research):
        self.research=research
        self.runtime=research.runtime
        self.database=research.database

    def templates(self,principal):
        self.runtime.require_owner(principal,principal.user_id)
        return {"items":[{"id":TEMPLATE_ID,"name":"Qlib 三列基础合成训练",
            "engine":workflow.engine_for({"feature_profile":workflow.BASIC_PROFILE}),
            "feature_profile":workflow.BASIC_PROFILE,"feature_protocol":workflow.feature_protocol(workflow.BASIC_PROFILE),
            "pipeline":["QlibDataLoader","DataHandlerLP","DatasetH","LGBModel","SignalRecord"],
            "scope":"SYNTHETIC_ONLY","input_profile_id":workflow.FIXTURE_ID,
            "configuration_source":"Select an authorized export; submit its explicit feature_profile and workflow dates",
            "coverage_status":"NOT_REAL_MARKET_COVERAGE_OR_RESEARCH_APPROVAL"},
            {"id":LEGACY_TEMPLATE_ID,"name":"Alpha158（未验证）","engine":workflow.ENGINE,
             "feature_profile":workflow.ALPHA_PROFILE,"available":False,"reason_code":"QLIB_ALPHA158_UNVERIFIED",
             "scope":"SYNTHETIC_ONLY","input_profile_id":workflow.FIXTURE_ID}]}


    def exports(self,principal,limit=25,offset=0):
        self.runtime.require_owner(principal,principal.user_id)
        self.runtime.jobs._paging(limit,offset)
        if limit>50:raise ResearchError("QLIB_EXPORT_PAGE_LIMIT_INVALID")
        adapter=getattr(self.runtime,"qlib_export_authorizer",None)
        if adapter is None or not callable(getattr(adapter,"authorize",None)):
            return {"items":[],"limit":limit,"offset":offset,"next_offset":None,"reason_code":"QLIB_AUTHORIZED_EXPORT_RESOLVER_REQUIRED"}
        # Only real registered owner exports; preserve all per-export gates.
        if not callable(getattr(adapter,"list_ready",None)):
            return {"items":[],"limit":limit,"offset":offset,"next_offset":None,
                "reason_code":"QLIB_AUTHORIZED_EXPORT_CATALOG_REQUIRED"}
        page=adapter.list_ready(principal,limit=limit,offset=offset)
        rows=page["items"]
        items=[]
        for row in rows:
            reference=row["export_reference"]
            try:grant=self.research.inputs.validate_qlib(principal,reference,require_native=False)
            except (ValueError,PlatformError):continue
            data=grant["data"];days=data["calendar"]
            code=None;options=None
            try:
                if len(days)<420:raise ResearchError("QLIB_TEMPLATE_CALENDAR_UNAVAILABLE")
                options=workflow.workflow_options({"feature_profile":workflow.BASIC_PROFILE,"num_boost_round":5,
                    "train":[days[60],days[277]],"train_cutoff":days[279],
                    "valid":[days[280],days[337]],"valid_cutoff":days[339],"test":[days[340],days[399]]},data)
                launcher=self._launcher(principal,grant)
                parameters={"export_reference":reference,"workflow":options}
                request=self._request(principal.user_id,"catalog-preview","catalog-preview",1,parameters,grant,launcher)
                workflow.validate_request(request,native=True)
                if not callable(getattr(adapter,"stage",None)):raise ResearchError("QLIB_EXPORT_STAGER_REQUIRED")
            except (ValueError,PlatformError) as error:code=getattr(error,"code","QLIB_EXPORT_NOT_READY")
            items.append({"export_reference":reference,"template_id":TEMPLATE_ID,"scope":data["scope"],
                "coverage":{"first":days[0],"last":days[-1],"sessions":len(days),"instruments":data["instruments"],
                    "basis":"AUTHORIZED_CALENDAR_METADATA_NOT_CERTIFIED_REAL_MARKET_COVERAGE"},
                "native_dump_succeeded":data["native_dump_succeeded"],"ready":code is None,"reason_code":code,
                "workflow":options,"native_manifest_hash":data["native_manifest_hash"]})
        return {"items":items,"limit":limit,"offset":offset,"next_offset":page["next_offset"],
            "total_registered":page["total_registered"],"unavailable_on_page":page["unavailable_on_page"],
            "reason_code":None if items else "QLIB_NO_AUTHORIZED_EXPORTS_IN_PAGE"}

    def capabilities(self,principal):
        catalog=self.exports(principal)
        code=catalog["reason_code"]
        try:self._deployment();worker_ready=True
        except (ValueError,PlatformError) as error:worker_ready=False;code=getattr(error,"code","QLIB_CONTROLLED_EXECUTOR_REQUIRED")
        ready=any(item["ready"] for item in catalog["items"])
        if not ready and code is None:code="QLIB_NO_AUTHORIZED_READY_EXPORTS"
        return {"engine":workflow.engine_for({"feature_profile":workflow.BASIC_PROFILE}),"worker_ready":worker_ready,"can_submit":ready,"reason_code":code,
            "templates_url":"/api/v1/research/qlib/templates","exports_url":"/api/v1/research/qlib/exports",
            "scope":"SYNTHETIC_ONLY_NOT_MARKET_OR_FORMAL_RESEARCH_APPROVAL","TA_model_configuration_required":False,
            "input_preparation":self._input_status(principal),
            "readiness_scope":"This bounded owner export page; individual selected export always revalidated on submit"}

    @staticmethod
    def _check_deployment(value):
        if getattr(value,"worker_binding_sha256",None)!=workflow.WORKER_BINDING_SHA256:
            raise ResearchError("QLIB_WORKER_BINDING_MISMATCH")
        workflow.hex_digest(getattr(value,"source_manifest_sha256",None))
        if (type(getattr(value,"worker_lock_bytes",None))is not bytes
                or hashlib.sha256(value.worker_lock_bytes).hexdigest()!=workflow.WORKER_LOCK_SHA256):
            raise ResearchError("QLIB_WORKER_LOCK_MISMATCH")

    def _deployment(self):
        factory=getattr(self.runtime,"qlib_native_launcher_factory",None)
        value=factory if factory is not None else getattr(self.runtime,"qlib_native_launcher",None)
        if factory is not None:
            if not callable(getattr(factory,"for_export",None)):raise ResearchError("QLIB_LAUNCHER_FACTORY_REQUIRED")
        elif (value is None or not callable(getattr(value,"execute",None))
                or not callable(getattr(value,"prepare_attempt",None))):
            raise ResearchError("QLIB_CONTROLLED_EXECUTOR_REQUIRED")
        self._check_deployment(value)
        return value,factory

    def _launcher(self,principal,grant):
        if (type(grant)is not dict or set(grant)!={"owner_id","export_reference","source_reference","data"}
                or grant["owner_id"]!=principal.user_id):raise ResearchError("QLIB_EXPORT_AUTHORIZATION_INVALID")
        workflow.reference(grant["export_reference"]);workflow.reference(grant["source_reference"])
        workflow.validate_input(grant["data"])
        if grant["data"]["native_dump_succeeded"] is not True:raise ResearchError("QLIB_NATIVE_DUMP_NOT_READY")
        deployment,factory=self._deployment()
        if factory is None:
            launcher=deployment  # Restricted old single-export mode only.
        else:
            supplied=deepcopy(grant)
            launcher=factory.for_export(principal,grant=supplied,purpose="development_readonly")
            if supplied!=grant:raise ResearchError("QLIB_FACTORY_MUTATED_GRANT")
            try:
                from finance_platform.core.qlib_native import QlibNativeLauncher
            except ImportError:
                raise ResearchError("QLIB_IMMUTABLE_LAUNCHER_REQUIRED") from None
            if (type(launcher)is not QlibNativeLauncher
                    or not getattr(getattr(type(launcher),"__dataclass_params__",None),"frozen",False)):
                raise ResearchError("QLIB_IMMUTABLE_LAUNCHER_REQUIRED")
            self._check_deployment(launcher)
            if launcher.source_manifest_sha256!=deployment.source_manifest_sha256:
                raise ResearchError("QLIB_LAUNCHER_SOURCE_MISMATCH")
            if launcher.binding.native_manifest_hash!=grant["data"]["native_manifest_hash"]:
                raise ResearchError("QLIB_EXPORT_LAUNCHER_BINDING_MISMATCH")
        if (type(getattr(launcher,"release",None))is not dict
                or launcher.release.get("native_manifest_hash")!=grant["data"]["native_manifest_hash"]):
            raise ResearchError("QLIB_EXPORT_LAUNCHER_BINDING_MISMATCH")
        return launcher

    def _input_ports(self):
        value=getattr(self.runtime,"qlib_input_preparation",None)
        if value is None or not all(callable(getattr(value,name,None)) for name in ("submit_prepare","submit_register")):
            raise ResearchError("QLIB_INPUT_PREPARATION_REQUIRED")
        self.runtime.handlers.get("data.qlib-prepare");self.runtime.handlers.get("data.qlib-register")
        return value

    def _input_status(self,principal):
        code=None;can_queue=False
        try:
            ports=self._input_ports()
            read=getattr(ports,"list_capabilities",None)
            if not callable(read):raise ResearchError("QLIB_INPUT_CAPABILITIES_REQUIRED")
            response=read(principal)
            if type(response)is not dict or type(response.get("profiles"))is not list or not 1<=len(response["profiles"])<=16:
                raise ResearchError("QLIB_INPUT_CAPABILITIES_INVALID")
            if any(type(item)is not dict for item in response["profiles"]):raise ResearchError("QLIB_INPUT_CAPABILITIES_INVALID")
            matches=[item for item in response["profiles"] if item.get("profile_id")==workflow.FIXTURE_ID]
            if len(matches)!=1:raise ResearchError("QLIB_INPUT_CAPABILITIES_INVALID")
            profile=matches[0];reason=profile.get("blocked_reason")
            if (profile.get("scope")!="SYNTHETIC" or profile.get("purpose")!="development_readonly"
                    or profile.get("supported")is not True or type(profile.get("can_prepare"))is not bool
                    or profile.get("market_data_claim")is not False or profile.get("formal_research_approved")is not False
                    or "blocked_reason" not in profile
                    or reason is not None and (type(reason)is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}",reason)is None)
                    or profile["can_prepare"] and reason is not None):
                raise ResearchError("QLIB_INPUT_CAPABILITIES_INVALID")
            can_queue=profile["can_prepare"]
            if not can_queue:code=reason or "QLIB_INPUT_PREPARATION_UNAVAILABLE"
        except (ValueError,PlatformError,TypeError,KeyError,AttributeError,OSError) as error:
            code=getattr(error,"code","QLIB_INPUT_CAPABILITIES_INVALID")
            if type(code)is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}",code)is None:code="QLIB_INPUT_CAPABILITIES_INVALID"
        return {"profile_id":workflow.FIXTURE_ID,"scope":"SYNTHETIC_ONLY","arbitrary_history_supported":False,
            "can_queue":can_queue,"reason_code":code,"prepare_url":"/api/v1/research/qlib/inputs/prepare",
            "registration_rule":"Separate POST after the source prepare Job SUCCEEDED; no native_id/receipt input"}

    def _input_job(self,principal,value,handler):
        if type(value)is not dict or type(value.get("job_id"))is not str:raise ResearchError("QLIB_INPUT_JOB_BINDING_INVALID")
        job=self.runtime.jobs.get(principal,value["job_id"])
        if type(job)is not dict or job.get("owner_id")!=principal.user_id or job.get("handler")!=handler:
            raise ResearchError("QLIB_INPUT_JOB_BINDING_INVALID")
        return job

    def prepare_input(self,principal,profile_id,*,idempotency_key=None):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        if profile_id!=workflow.FIXTURE_ID:raise ResearchError("QLIB_INPUT_PROFILE_UNSUPPORTED")
        ports=self._input_ports()
        return self._input_job(principal,ports.submit_prepare(principal,profile_id=profile_id,
            idempotency_key=idempotency_key),"data.qlib-prepare")

    def register_input(self,principal,source_job_id,*,idempotency_key=None):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        workflow.identifier(source_job_id);ports=self._input_ports()
        source=self.runtime.jobs.get(principal,source_job_id)
        if source["owner_id"]!=principal.user_id or source["handler"]!="data.qlib-prepare":
            raise ResearchError("QLIB_INPUT_SOURCE_JOB_INVALID")
        if source["status"]!="SUCCEEDED":raise ResearchError("QLIB_INPUT_SOURCE_JOB_NOT_SUCCEEDED")
        return self._input_job(principal,ports.submit_register(principal,source_job_id=source_job_id,
            idempotency_key=idempotency_key),"data.qlib-register")

    def submit(self,principal,export_reference,options,*,idempotency_key=None):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        authorization=self.research.inputs.validate_qlib(principal,export_reference)
        normalized=workflow.workflow_options(options,authorization["data"])
        launcher=self._launcher(principal,authorization)
        preview={"export_reference":export_reference,"workflow":normalized}
        workflow.validate_request(self._request(principal.user_id,"pending-run","pending-job",1,preview,authorization,launcher),native=True)
        declaration={"engine":workflow.engine_for(normalized),"workflow":normalized}
        key="qlib-preset:"+hashlib.sha256(principal.user_id.encode()).hexdigest()[:24]+":"+workflow.digest(normalized)[:16]
        strategy=self.runtime.versions.freeze(principal,key,"1","strategy",declaration)
        return self.runtime.jobs.submit(principal,"research.qlib",{
            "export_reference":workflow.reference(export_reference),"workflow":normalized,"strategy_reference":payload(strategy)},
            mode=RunMode.BACKTEST,idempotency_key=idempotency_key)

    def resolve(self,principal,parameters):
        validate_parameters(parameters)
        authorization=self.research.inputs.validate_qlib(principal,parameters["export_reference"])
        options=workflow.workflow_options(parameters["workflow"],authorization["data"])
        if options!=parameters["workflow"]:raise ResearchError("QLIB_CONFIG_NOT_NORMALIZED")
        declaration=self.runtime.versions.get(principal,VersionRef(**parameters["strategy_reference"]))
        if declaration!={"engine":workflow.engine_for(options),"workflow":options}:raise ResearchError("QLIB_STRATEGY_BINDING_MISMATCH")
        launcher=self._launcher(principal,authorization)
        # Validate release before accepting a raw /jobs submission as well.
        request=self._request(principal.user_id,"pending-run","pending-job",1,parameters,authorization,launcher)
        workflow.validate_request(request,native=True)
        return {"data":parameters["export_reference"],"strategy":parameters["strategy_reference"],
                "seed":options["seed"],"model_reason":"New native Qlib LGBModel; no existing LinearPipeline artifact"}

    @staticmethod
    def _request(owner,run_id,job_id,fence,parameters,authorization,launcher):
        return {"schema_version":"qlib-worker-request-1","owner_id":owner,"run_id":run_id,"job_id":job_id,
            "fencing_token":fence,"export_reference":parameters["export_reference"],
            "source_reference":authorization["source_reference"],"data":authorization["data"],
            "workflow":parameters["workflow"],"worker_binding_sha256":launcher.worker_binding_sha256,
            "source_manifest_sha256":launcher.source_manifest_sha256,"release":dict(launcher.release)}

    def execute(self,context,parameters):
        validate_parameters(parameters)
        principal=self.research.principal(context.run.owner_id)
        self.resolve(principal,parameters)
        authorization=self.research.inputs.validate_qlib(principal,parameters["export_reference"])
        if (payload(context.run.data)!=parameters["export_reference"]
                or payload(context.run.strategy)!=parameters["strategy_reference"]
                or context.run.model is not None
                or self.runtime.versions.get(principal,context.run.config)!=parameters):
            raise ResearchError("QLIB_RUN_BINDING_MISMATCH")
        context.require_active();launcher=self._launcher(principal,authorization)
        request=self._request(principal.user_id,context.run.run_id,context.job_id,context.fencing_token,parameters,authorization,launcher)
        workflow.validate_request(request,native=True)
        root=self.runtime.root/".local/platform/native_research"
        namespace=hashlib.sha256(principal.user_id.encode()).hexdigest()
        attempt=workflow.owned_path(root,namespace+"/"+workflow.identifier(context.run.run_id))
        attempt.mkdir(parents=True,exist_ok=False)
        launcher.prepare_attempt(context,attempt)
        paths=sorted(Path(__file__).parent.glob("*.py"))
        self.research.archive.begin(context.run.run_id,context.run,storage_binding=task_binding(context),
            data={"scope":"SYNTHETIC","export_reference":parameters["export_reference"],"input":authorization["data"]},
            configuration={"parameters":parameters,"worker_binding_sha256":workflow.WORKER_BINDING_SHA256},
            code_paths=paths,dependency_bytes=launcher.worker_lock_bytes)
        try:
            # The data owner stages its completed dump with AccessPolicy/current
            # source/window/rights authorization, never arbitrary client paths.
            exporter=self.runtime.qlib_export_authorizer
            if not callable(getattr(exporter,"stage",None)):raise ResearchError("QLIB_EXPORT_STAGER_REQUIRED")
            stage=exporter.stage(principal,parameters["export_reference"],attempt/"provider",context=context)
            expected_stage={"state":"STAGED","export_reference":parameters["export_reference"],
                "native_manifest_hash":authorization["data"]["native_manifest_hash"],"file_count":len(authorization["data"]["provider_files"])}
            if type(stage)is not dict or type(stage.get("file_count"))is not int or stage!=expected_stage:
                raise ResearchError("QLIB_STAGE_BINDING_MISMATCH")
            workflow.verify_provider(attempt,authorization["data"])
            if self.research.inputs.validate_qlib(principal,parameters["export_reference"])!=authorization:
                raise ResearchError("QLIB_INPUT_AUTHORIZATION_CHANGED")
            context.require_active()
            result=launcher.execute(context,request,attempt)
            workflow.validate_result(result,request)
            artifact_root=workflow.owned_path(attempt,result["native_artifact_root"])
            if not artifact_root.is_relative_to(attempt/"recorders"):raise ResearchError("QLIB_ARTIFACT_SCOPE_INVALID")
            for artifact in result["native_artifacts"]:
                path=workflow.owned_path(artifact_root,artifact["path"])
                if path.stat().st_size!=artifact["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest()!=artifact["sha256"]:
                    raise ResearchError("QLIB_NATIVE_ARTIFACT_CHANGED")
            from finance_platform.core.qlib_artifact_storage import QlibArtifactArchive
            durable_artifacts=QlibArtifactArchive(self.runtime)
            promoted_artifacts=durable_artifacts.prepare(context,attempt,result)
            result={**result,"lineage":payload(context.run),"publication_gate":"CORE_JOB_SUCCEEDED_REQUIRED"}
            encoded=native_results.encode(result)
            context.require_active()
            stored_report=native_results.prepare_stored_result(self.research,context,result)
            with self.database.transaction() as con:
                context.assert_fenced(con)
                active_principal=self.research.principal(principal.user_id)
                self.runtime.require_owner(active_principal,principal.user_id,write=True)
                # Readonly reauthorization inside the same writer boundary; a
                # data resolver must never acquire/export/mutate at this point.
                current=self.research.inputs.validate_qlib(active_principal,parameters["export_reference"])
                if current!=authorization:raise ResearchError("QLIB_INPUT_AUTHORIZATION_CHANGED")
                native_results.insert_result(self.research,context,result,sample_hash=authorization["data"]["logical_data_hash"],
                    strategy_hash=fingerprint(self.runtime.versions.get(principal,context.run.strategy)),connection=con,stored_encoded=stored_report)
                durable_artifacts.record(context,promoted_artifacts,connection=con)
            # Archive describes completed native calculation. It is never the
            # publication authority: result reads require the core job SUCCEEDED.
            self.research.archive.finish(context.run.run_id,result)
            return {"run_id":context.run.run_id,"family":workflow.FAMILY,
                "recorder":result["recorder"],"result_url":"/api/v1/research/results/"+context.run.run_id}
        except Exception as error:
            self.research.archive.fail(context.run.run_id,getattr(error,"code","QLIB_NATIVE_FAILED"))
            raise
