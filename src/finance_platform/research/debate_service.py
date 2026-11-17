"""TA research jobs/results on the existing platform services and native host."""
from .payload_storage import task_binding
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
import hashlib
import json
from finance_platform.core.contracts import VersionRef,RunMode,payload,fingerprint
from finance_platform.core.auth import AccountDelegation
from finance_platform.core.security import safe_json,now_text
from .data import ResearchError
from . import tradingagents_workflow as native
from . import native_results
from .debate_tools import DebateInputBridge,result_refs

FAMILY="tradingagents-native-report-1"
REQUEST_FIELDS={"data_reference","ticker","trade_date","as_of","evidence_results","options"}
EXTERNAL_LIMITS={"max_model_calls":32,"max_input_tokens":2097152,
                 "max_output_tokens":262144,"max_wall_seconds":120}


def require_external(principal):
    if not isinstance(principal,AccountDelegation) or getattr(principal,"mcp_scope",None)!="external":
        raise ResearchError("TA_EXTERNAL_SCOPE_REQUIRED")


def external_profile(value):
    if type(value)is not dict or set(value)!={"profile_id","profile_sha256"}:
        raise ResearchError("TA_EXACT_PROFILE_REQUIRED")
    native.binding_id(value["profile_id"]);native.pin(value["profile_sha256"])
    if not value["profile_id"].startswith("team-") or len(value["profile_id"])<=5:
        raise ResearchError("TA_EXTERNAL_PROFILE_NOT_REGISTERED")
    return {"mode":"SERVER_PROFILE",**value}


def provider_approval(profile,request,bundle,options,approval):
    if (type(approval)is not dict
            or set(approval)!={"approved_profile","approved_limits","approve_source_transfer"}
            or external_profile(approval["approved_profile"])!=profile
            or approval["approve_source_transfer"] is not True):
        raise ResearchError("TA_EXTERNAL_EXECUTION_APPROVAL_REQUIRED")
    limits=approval["approved_limits"]
    if type(limits)is not dict or limits.keys()!=EXTERNAL_LIMITS.keys() or any(
            type(limits[key])is not int or not 1<=limits[key]<=maximum
            for key,maximum in EXTERNAL_LIMITS.items()):
        raise ResearchError("TA_EXTERNAL_BUDGET_INVALID")
    options=native.options(options)
    if (limits["max_wall_seconds"]>options["wall_seconds"] or limits["max_model_calls"]>options["model_calls"]
            or limits["max_output_tokens"]<native.PROVIDER_MIN_OUTPUT_TOKENS):
        raise ResearchError("TA_EXTERNAL_BUDGET_INVALID")
    binding={**deepcopy(approval),"input_hash":native_results.digest(bundle),
        "request_hash":native.digest(request),
        "options":{**options,"wall_seconds":limits["max_wall_seconds"],"model_calls":limits["max_model_calls"],
                   "provider_input_tokens":limits["max_input_tokens"],"provider_output_tokens":limits["max_output_tokens"]}}
    return {**binding,"execution_sha256":native.digest(binding)}


def approved_fields(value):
    return {key:value[key] for key in ("approved_profile","approved_limits","approve_source_transfer")}


@dataclass(frozen=True)
class _VerifiedLauncher:
    """Call-local metadata; native operations still invoke the original host.

    Never installed in runtime or retained across requests. Core prepare /
    execute / stage / admission retain their own current-binding checks.
    """
    original:object
    worker_lock_bytes:bytes
    source_manifest_sha256:str
    _release:dict
    supports_agent_teams:bool

    @property
    def release(self):return deepcopy(self._release)

    def __getattr__(self,name):return getattr(self.original,name)


def scope_projection(bundle,execution_scope):
    """Keep model execution and source-data meaning as independent fields."""
    return {"execution_scope":execution_scope,"data_scope":bundle["scope"],
        "evidence_scopes":sorted({item["scope"] for item in bundle["evidence_results"]}),
        "data_binding":{key:bundle[key] for key in ("scope","data_reference","snapshot_content_hash","selected_view_hash","as_of","price_fields","availability")},
        "tool_references":[{"kind":"authorized_input","scope":bundle["scope"],"data_reference":bundle["data_reference"],"selected_view_hash":bundle["selected_view_hash"]},
            *[{key:item[key] for key in ("kind","run_id","scope","result_hash","source")} for item in bundle["evidence_results"]]]}


def normalize(value):
    if type(value)is not dict or set(value)!=REQUEST_FIELDS:raise ResearchError("TA_REQUEST_FIELDS_INVALID")
    native.reference(value["data_reference"]);result_refs(value["evidence_results"])
    for key in ("trade_date","as_of"):
        if type(value[key])is not str or date.fromisoformat(value[key]).isoformat()!=value[key]:raise ResearchError("TA_DATE_INVALID")
    if value["as_of"]>value["trade_date"] or value["trade_date"]>date.today().isoformat():raise ResearchError("TA_DATE_INVALID")
    if type(value["ticker"])is not str or native.re.fullmatch(r"[A-Z][A-Z0-9.-]{0,17}",value["ticker"])is None:
        raise ResearchError("TA_SYMBOL_INVALID")
    if type(value["options"])is not dict or set(value["options"])-{"debate_rounds","risk_rounds","tool_rounds"}:
        raise ResearchError("TA_OPTIONS_INVALID")
    options=native.options(value["options"])
    return {**value,"options":{key:options[key] for key in ("debate_rounds","risk_rounds","tool_rounds")}}


def validate_parameters(value):
    common={"request","strategy_reference","model_reference"}
    if type(value)is not dict or set(value) not in (common,common|{"agent_team_snapshot_id"},
            common|{"agent_team_snapshot_id","provider_approval"}):
        raise ResearchError("TA_JOB_PARAMETERS_INVALID")
    if "agent_team_snapshot_id" in value and (type(value['agent_team_snapshot_id'])is not str or not value['agent_team_snapshot_id']):raise ResearchError('TA_TEAM_SNAPSHOT_REQUIRED')
    if "provider_approval" in value:
        approval=value["provider_approval"]
        if type(approval)is not dict or set(approval)!={"approved_profile","approved_limits","approve_source_transfer",
                "input_hash","request_hash","options","execution_sha256"}:
            raise ResearchError("TA_EXTERNAL_EXECUTION_APPROVAL_REQUIRED")
        native.pin(approval["execution_sha256"])
    if normalize(value["request"])!=value["request"]:raise ResearchError("TA_JOB_PARAMETERS_NOT_NORMALIZED")
    for key in ("strategy_reference","model_reference"):native.reference(value[key])


class DebateResearchService:
    def __init__(self,research):
        self.research=research;self.runtime=research.runtime;self.database=research.database
        self.inputs=DebateInputBridge(research)

    def _profile(self,principal):
        registry=getattr(self.runtime,"tradingagents_models",None)
        if registry is None or not callable(getattr(registry,"product_profile",None)):
            raise ResearchError("TA_MODEL_BACKEND_UNCONFIGURED")
        value=registry.product_profile(principal)
        # Offline fixtures have no public submit capability, including direct
        # generic /jobs submission/retry through this handler's resolver.
        if type(value)is not dict or value.get("mode")!="SERVER_PROFILE":raise ResearchError("TA_MODEL_BACKEND_UNCONFIGURED")
        if set(value)!={"mode","profile_id","profile_sha256"}:raise ResearchError("TA_MODEL_PROFILE_INVALID")
        native.binding_id(value["profile_id"]);native.pin(value["profile_sha256"])
        return value

    @staticmethod
    def _verified_launcher(launcher):
        methods=("prepare_attempt","execute","stage_for_core_success","discard_staged")
        if launcher is None or not all(callable(getattr(launcher,key,None)) for key in methods):raise ResearchError("TA_CONTROLLED_NATIVE_HOST_REQUIRED")
        describe=getattr(launcher,'describe_verified',None)
        if callable(describe):
            description=describe()
            if type(description)is not dict or set(description)!={'release','worker_lock_bytes','source_manifest_sha256','supports_agent_teams'}:
                raise ResearchError('TA_VERIFIED_DESCRIPTION_INVALID')
            proof=description.get('release');lock=description.get('worker_lock_bytes')
            source=description.get('source_manifest_sha256');teams=description.get('supports_agent_teams')
            if type(teams)is not bool:raise ResearchError('TA_VERIFIED_DESCRIPTION_INVALID')
        else:
            if describe is not None:raise ResearchError('TA_VERIFIED_DESCRIPTION_INVALID')
            # Old hosts: one read per property, never fallback after a failed
            # descriptor. The metadata is local to this call, not a cache.
            proof=getattr(launcher,'release',None);lock=getattr(launcher,'worker_lock_bytes',None)
            source=getattr(launcher,'source_manifest_sha256',None)
            teams=getattr(launcher,'supports_agent_teams',False)is True
        if type(proof)is not dict:raise ResearchError("TA_CURRENT_EXECUTION_RELEASE_REQUIRED")
        if type(lock)is not bytes or hashlib.sha256(lock).hexdigest()!=native.WORKER_LOCK_SHA256:
            raise ResearchError("TA_WORKER_LOCK_MISMATCH")
        native.pin(source)
        if (proof.get("source_manifest_sha256")!=source
                or proof.get("complete_imports")!=list(native.IMPORTS) or proof.get("commit")!=native.ENGINE["commit"]
                or proof.get("wheel_sha256")!=native.WHEEL_SHA256 or proof.get("worker_lock_sha256")!=native.WORKER_LOCK_SHA256):
            raise ResearchError("TA_CURRENT_EXECUTION_RELEASE_REQUIRED")
        for key in ("import_receipt_sha256","security_receipt_sha256"):native.pin(proof.get(key))
        return _VerifiedLauncher(launcher,lock,source,deepcopy(proof),teams)

    def _launcher(self, principal):
        launcher = self._verified_launcher(getattr(self.runtime, 'tradingagents_native_launcher', None))
        accepts_owner = getattr(launcher, 'accepts_team_owner', None)
        if launcher.supports_agent_teams and callable(accepts_owner) and not accepts_owner(principal):
            return replace(launcher, supports_agent_teams=False)
        return launcher

    def capabilities(self,principal):
        self.runtime.require_owner(principal,principal.user_id)
        team_ready=False;host=None;host_error=None
        try:
            host=self._launcher(principal)
            team_ready=getattr(host,'supports_agent_teams',False)is True and getattr(self.runtime,'agent_teams',None)is not None
        except ValueError as error:host_error=error
        try:
            self._profile(principal)
            if host_error is not None:raise host_error
            status,ready,code,message="CONFIGURED",True,None,"模型与原生执行边界已由服务端绑定。"
        except ValueError as error:
            code=getattr(error,"code","TA_CURRENT_EXECUTION_RELEASE_REQUIRED")
            status="UNCONFIGURED" if code=="TA_MODEL_BACKEND_UNCONFIGURED" else "CONFIGURED_EXECUTION_UNAVAILABLE"
            ready,message=False,"尚未配置真实模型后端，暂不能发起分析。" if status=="UNCONFIGURED" else "原生执行边界尚未就绪。"
        return {"engine":"TradingAgents 0.7.0","model_status":"OFFLINE_TEAM_CONFIGURED" if team_ready and not ready else status,"can_submit":ready or team_ready,"reason_code":None if team_ready else code,
            "message":"离线团队已就绪，提交时选择团队快照。" if team_ready and not ready else message,"offline_fixture_is_product_backend":False,
            'agent_teams':{'contract':'finance-agent-teams/v1','snapshot_field':'agent_team_snapshot_id','snapshot_required':True,
                'offline_model':'deterministic-v1','can_submit':team_ready,'reason_code':None if team_ready else 'TA_TEAM_NATIVE_HOST_UNAVAILABLE','real_provider_calls':'NOT_RUN'},
            "limits":{"debate_rounds":[1,2],"risk_rounds":[1,2],"tool_rounds":[1,2]}}

    def _team(self,principal,snapshot_id,*,external=False):
        snapshot=self.runtime.agent_teams.get_snapshot(principal,snapshot_id)
        if snapshot['owner_id']!=principal.user_id or native_results.digest({k:snapshot[k] for k in ('configuration','resolved_roles')})!=snapshot['sha256']:
            raise ResearchError('TA_TEAM_SNAPSHOT_BINDING_CHANGED')
        if not external and any(row['model']['provider']!='offline' for row in snapshot['resolved_roles'].values()):raise ResearchError('TA_REAL_PROVIDER_DISABLED')
        profile={'mode':'SERVER_PROFILE','profile_id':'team-'+snapshot['snapshot_id'],'profile_sha256':snapshot['sha256']}
        return profile,snapshot

    def _authorize_provider_source(self,principal,request,bundle):
        if bundle["scope"]!="SYNTHETIC" or any(item["scope"]!="SYNTHETIC" or item["kind"]!="qlib"
                                              for item in bundle["evidence_results"]):
            raise ResearchError("TA_EXTERNAL_SOURCE_TRANSFER_UNAVAILABLE")
        raw,_,_=self.research.inputs.validate(principal,request["data_reference"])
        origin=raw.get("origin") or {}
        if (origin.get("origin_kind")!="generated" or origin.get("generator")!="platform-synthetic-v1"
                or "export" not in origin.get("use_allowed",())):
            raise ResearchError("TA_EXTERNAL_SOURCE_TRANSFER_UNAVAILABLE")

    def _provider_options(self, request, snapshot):
        return native.options({**request["options"],
            "wall_seconds":snapshot["configuration"]["execution"]["timeout_seconds"]})

    def external_capabilities(self,principal,profile_id=None):
        require_external(principal)
        result={"can_submit":False,"profile":None,"approved_limits":dict(EXTERNAL_LIMITS),
                "profile_source":"Existing Agent Team snapshot, prefix snapshot_id with team-",
                "source_scope":"PLATFORM_GENERATED_SYNTHETIC",
                "provider":"openai","token_meter":"RESPONSES_INPUT_COUNT_AND_OUTPUT_LIMIT",
                "minimum_output_tokens":native.PROVIDER_MIN_OUTPUT_TOKENS}
        if profile_id is None:
            return {**result,"reason_code":"TA_EXTERNAL_TEAM_PROFILE_REQUIRED"}
        try:
            native.binding_id(profile_id)
            if not profile_id.startswith("team-"):
                raise ResearchError("TA_EXTERNAL_PROFILE_NOT_REGISTERED")
            profile,snapshot=self._team(principal,profile_id[5:],external=True)
            self._launcher(principal).validate_external_team(principal,snapshot)
            limits={**EXTERNAL_LIMITS,"max_wall_seconds":snapshot["configuration"]["execution"]["timeout_seconds"]}
            return {**result,"can_submit":True,"profile":{key:profile[key] for key in ("profile_id","profile_sha256")},
                    "approved_limits":limits,"reason_code":None}
        except (AttributeError,ValueError) as error:
            return {**result,"reason_code":getattr(error,"code","TA_PROVIDER_EXACT_METER_UNAVAILABLE")}

    def submit(self,principal,*,data_reference,ticker,trade_date,as_of,evidence_results=None,options=None,
               idempotency_key=None,agent_team_snapshot_id=None,approved_profile=None,approve_execution=None):
        self.runtime.require_owner(principal,principal.user_id,write=True)
        external=approved_profile is not None or approve_execution is not None
        if external:
            require_external(principal)
            if agent_team_snapshot_id is not None or not callable(approve_execution) or not idempotency_key:
                raise ResearchError("TA_EXTERNAL_EXECUTION_APPROVAL_REQUIRED")
            expected_profile=external_profile(approved_profile)
            agent_team_snapshot_id=expected_profile["profile_id"][5:]
        snapshot=None
        if agent_team_snapshot_id is not None:
            if external:
                profile,snapshot=self._team(principal,agent_team_snapshot_id,external=True)
                if expected_profile!=profile:raise ResearchError("TA_EXTERNAL_PROFILE_CHANGED")
            else:
                profile,snapshot=self._team(principal,agent_team_snapshot_id)
            rounds=snapshot['configuration']['rounds']
            team_options={'debate_rounds':rounds['debate'],'risk_rounds':rounds['risk'],'tool_rounds':rounds['tool']}
            if options is not None and (type(options)is not dict or any(key not in team_options or value!=team_options[key] for key,value in options.items())):
                raise ResearchError('TA_TEAM_ROUND_BINDING_MISMATCH')
            options=team_options
        request=normalize({"data_reference":data_reference,"ticker":ticker,"trade_date":trade_date,"as_of":as_of,
            "evidence_results":[] if evidence_results is None else evidence_results,"options":{} if options is None else options})
        if snapshot is None:profile=self._profile(principal)  # No freeze/queue/data effects if absent.
        launcher=self._launcher(principal)
        if snapshot is not None and getattr(launcher,'supports_agent_teams',False)is not True:raise ResearchError('TA_TEAM_NATIVE_HOST_UNAVAILABLE')
        bundle=self.inputs.authorize(principal,request)
        if snapshot is not None:bundle={**bundle,'team_snapshot':snapshot}
        if external:
            self._authorize_provider_source(principal,request,bundle)
            launcher.validate_external_team(principal,snapshot)
            normalized=self._provider_options(request,snapshot)
            approval=provider_approval(profile,request,bundle,normalized,
                approve_execution(deepcopy(profile),deepcopy(bundle),deepcopy(normalized)))
            bundle={**bundle,"provider_approval":approval}
        declaration={"engine":native.ENGINE,"request":request,"input_hash":native_results.digest(bundle),"model_profile":profile}
        owner_key=hashlib.sha256(principal.user_id.encode()).hexdigest()[:24]
        strategy=self.runtime.versions.freeze(principal,"ta-strategy:"+owner_key+":"+fingerprint(declaration),"1","strategy",declaration)
        model_payload=profile if snapshot is None else {'profile':profile,'team_snapshot':snapshot}
        model_version=profile["profile_sha256"][:32]
        if external:
            model_payload={**model_payload,"provider_approval":approval}
            model_version=approval["execution_sha256"][:32]
        model=self.runtime.versions.freeze(principal,"ta-model:"+owner_key+":"+hashlib.sha256(profile["profile_id"].encode()).hexdigest()[:32],model_version,"model",model_payload)
        parameters={"request":request,"strategy_reference":payload(strategy),"model_reference":payload(model)}
        if snapshot is not None:parameters['agent_team_snapshot_id']=snapshot['snapshot_id']
        if external:parameters["provider_approval"]=approval
        job=self.runtime.jobs.submit(principal,"research.debate",parameters,mode=RunMode.BACKTEST,idempotency_key=idempotency_key)
        return {**job,"approved_execution_sha256":approval["execution_sha256"]} if external else job

    def _bindings(self,principal,parameters):
        validate_parameters(parameters);snapshot=None
        approval=parameters.get("provider_approval")
        if approval is not None:profile,snapshot=self._team(principal,parameters['agent_team_snapshot_id'],external=True)
        elif 'agent_team_snapshot_id' in parameters:profile,snapshot=self._team(principal,parameters['agent_team_snapshot_id'])
        else:profile=self._profile(principal)
        launcher=self._launcher(principal)
        if snapshot is not None and getattr(launcher,'supports_agent_teams',False)is not True:raise ResearchError('TA_TEAM_NATIVE_HOST_UNAVAILABLE')
        request=parameters["request"];bundle=self.inputs.authorize(principal,request)
        if snapshot is not None:bundle={**bundle,'team_snapshot':snapshot}
        if approval is not None:
            self._authorize_provider_source(principal,request,bundle)
            launcher.validate_external_team(principal,snapshot)
            if provider_approval(profile,request,bundle,self._provider_options(request,snapshot),approved_fields(approval))!=approval:
                raise ResearchError("TA_EXTERNAL_EXECUTION_BINDING_CHANGED")
            bundle={**bundle,"provider_approval":approval}
        model=self.runtime.versions.get(principal,VersionRef(**parameters["model_reference"]))
        strategy=self.runtime.versions.get(principal,VersionRef(**parameters["strategy_reference"]))
        for key,kind in (("model_reference","model"),("strategy_reference","strategy")):
            reference=parameters[key]
            with closing(self.database.connect()) as con:
                row=con.execute("SELECT owner_id,kind FROM resource_versions WHERE resource_id=? AND version=?",(reference["resource_id"],reference["version"])).fetchone()
            if row is None or row["owner_id"]!=principal.user_id or row["kind"]!=kind:raise ResearchError("TA_VERSION_OWNER_MISMATCH")
        expected_model=profile if snapshot is None else {'profile':profile,'team_snapshot':snapshot}
        if approval is not None:expected_model={**expected_model,"provider_approval":approval}
        if model!=expected_model or strategy!={"engine":native.ENGINE,"request":request,"input_hash":native_results.digest(bundle),"model_profile":profile}:
            raise ResearchError("TA_FROZEN_BINDING_CHANGED")
        return profile,bundle,launcher

    def resolve(self,principal,parameters):
        if type(parameters)is dict and "provider_approval" in parameters:
            require_external(principal)
        self._bindings(principal,parameters)
        return {"data":parameters["request"]["data_reference"],"strategy":parameters["strategy_reference"],
            "model":parameters["model_reference"],"model_reason":None}

    def _job(self,principal,job_id):
        value=self.runtime.jobs.get(principal,job_id)
        if value["owner_id"]!=principal.user_id or value["handler"]!="research.debate":raise ResearchError("TA_JOB_NOT_FOUND")
        return value

    def status(self,principal,job_id):
        job=self._job(principal,job_id)
        resume={"available":False,"reason_code":"NO_VERIFIED_NATIVE_CHECKPOINT"}
        launcher=getattr(self.runtime,"tradingagents_native_launcher",None)
        if callable(getattr(launcher,"checkpoint_status",None)):
            checked=launcher.checkpoint_status(principal,job)
            if type(checked)is not dict or set(checked)!={"available","reason_code"} or type(checked["available"])is not bool:
                raise ResearchError("TA_CHECKPOINT_STATUS_INVALID")
            if type(checked["reason_code"])is not str or native.re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}",checked["reason_code"])is None:
                raise ResearchError("TA_CHECKPOINT_STATUS_INVALID")
            resume=checked
        value={**job,"resume":resume}
        if isinstance(principal,AccountDelegation) and getattr(principal,"mcp_scope",None)=="external":
            with closing(self.database.connect()) as con:
                row=con.execute("SELECT parameters_json,parameters_hash FROM jobs WHERE job_id=? AND owner_id=?",
                                (job_id,principal.user_id)).fetchone()
            if row is not None and hashlib.sha256(row["parameters_json"].encode()).hexdigest()==row["parameters_hash"]:
                approval=json.loads(row["parameters_json"]).get("provider_approval")
                if approval is not None:
                    value["approved_execution_sha256"]=native.pin(approval["execution_sha256"])
        return value

    def cancel(self,principal,job_id):
        self._job(principal,job_id);self.runtime.jobs.cancel(principal,job_id)
        return self.status(principal,job_id)

    def retry(self,principal,job_id):
        self._job(principal,job_id);return self.runtime.jobs.retry(principal,job_id)

    def execute(self,context,parameters):
        principal=self.research.principal(context.run.owner_id)
        profile,bundle,launcher=self._bindings(principal,parameters)
        if (payload(context.run.data)!=parameters["request"]["data_reference"] or payload(context.run.strategy)!=parameters["strategy_reference"]
                or payload(context.run.model)!=parameters["model_reference"] or self.runtime.versions.get(principal,context.run.config)!=parameters):
            raise ResearchError("TA_RUN_BINDING_MISMATCH")
        context.require_active()
        if 'team_snapshot' in bundle:
            factory=getattr(launcher,'for_profile',None)
            if not callable(factory):raise ResearchError('TA_TEAM_PROFILE_FACTORY_REQUIRED')
            launcher=self._verified_launcher(
                factory(principal,profile,bundle['team_snapshot'],provider_approval=bundle["provider_approval"])
                if "provider_approval" in bundle else factory(principal,profile,bundle['team_snapshot']))
        owner_hash=hashlib.sha256(principal.user_id.encode()).hexdigest()
        attempt=self.runtime.root/".local/platform/native_research"/owner_hash/context.run.run_id
        for ancestor in (attempt,*attempt.parents):
            if ancestor.exists() and (ancestor.is_symlink() or getattr(ancestor.lstat(),"st_file_attributes",0)&0x400):raise ResearchError("TA_ATTEMPT_ALIAS_REJECTED")
        attempt.mkdir(parents=True,exist_ok=False);launcher.prepare_attempt(context,attempt)
        request=parameters["request"]
        logical=context.run.run_id;parent=context.run.parent_run_id;seen={logical}
        with closing(self.database.connect()) as con:
            while parent:
                if parent in seen or len(seen)>64:raise ResearchError("TA_RETRY_LINEAGE_INVALID")
                seen.add(parent)
                row=con.execute("SELECT owner_id,parent_run_id FROM runs WHERE run_id=?",(parent,)).fetchone()
                if row is None or row["owner_id"]!=principal.user_id:raise ResearchError("TA_RETRY_LINEAGE_INVALID")
                logical=parent;parent=row["parent_run_id"]
        binding={"owner_id":principal.user_id,"logical_task_id":logical,
            "run_id":context.run.run_id,"job_id":context.job_id,"fencing_token":context.fencing_token,
            **{key:request[key] for key in ("ticker","trade_date","as_of","data_reference")},
            "snapshot_content_hash":bundle["snapshot_content_hash"],"selected_view_hash":bundle["selected_view_hash"],
            "model_profile":profile,"tools_manifest_hash":native_results.digest(bundle),"options":native.options(request["options"])}
        if 'team_snapshot' in bundle:binding['options']['wall_seconds']=bundle['team_snapshot']['configuration']['execution']['timeout_seconds']
        if 'provider_approval' in bundle:binding['options']=dict(bundle["provider_approval"]["options"])
        native.binding(binding)
        native.require_native_release(binding,launcher.release)
        native_request={"binding":binding,"input_bundle":bundle,"parent_run_id":context.run.parent_run_id,
            "source_manifest_sha256":launcher.source_manifest_sha256,"release":dict(launcher.release),
            "native_entrypoint":"finance_platform.research.debate_native:execute_native_request"}
        self.research.archive.begin(context.run.run_id,context.run,storage_binding=task_binding(context),data=bundle,configuration=parameters,
            code_paths=sorted(Path(__file__).parent.glob('*.py')),dependency_bytes=launcher.worker_lock_bytes)
        handle=None
        try:
            handle=launcher.execute(context,native_request,attempt)
            result=self._public_result(handle.result,binding,bundle,context)
            native_hash=native.digest(handle.result);encoded=native_results.encode(result);saved_hash=native_results.digest(json.loads(encoded))
            context.require_active()
            if "provider_approval" in bundle:
                _,current_bundle,_=self._bindings(principal,parameters)
            else:
                current_bundle=self.inputs.authorize(principal,request)
                if 'team_snapshot' in bundle:current_bundle={**current_bundle,'team_snapshot':self._team(principal,parameters['agent_team_snapshot_id'])[1]}
            if current_bundle!=bundle:raise ResearchError("TA_INPUT_AUTHORIZATION_CHANGED")
            stored_report=native_results.prepare_stored_result(self.research,context,result)
            with self.database.transaction() as con:
                context.assert_fenced(con);self.runtime.require_owner(self.research.principal(principal.user_id),principal.user_id,write=True)
                checked_hash=native_results.insert_result(self.research,context,result,sample_hash=bundle["selected_view_hash"],
                    strategy_hash=fingerprint(parameters["strategy_reference"]),connection=con,stored_encoded=stored_report)
                if checked_hash!=saved_hash:raise ResearchError("TA_RESULT_HASH_CHANGED")
            # Existing FA013/top-level worker owns the post-core-finish ACK.
            # The saved row remains invisible until the actual core SUCCEEDED.
            core_result={"family":FAMILY,"run_id":context.run.run_id,"result_url":"/api/v1/research/results/"+context.run.run_id,
                "native_result_hash":native_hash,"saved_result_hash":saved_hash}
            try:
                from finance_platform.core.ta_native import StagedTAResult
            except ImportError:
                raise ResearchError("TA_TYPED_RESULT_TRANSPORT_REQUIRED") from None
            staged=launcher.stage_for_core_success(context,handle,core_result=dict(core_result),
                native_result_hash=native_hash,saved_result_hash=saved_hash)
            if type(staged)is not StagedTAResult:raise ResearchError("TA_TYPED_RESULT_TRANSPORT_REQUIRED")
            if dict(staged)!=core_result:raise ResearchError("TA_STAGED_CORE_RESULT_MISMATCH")
            self.research.archive.finish(context.run.run_id,result)
            return staged
        except BaseException as error:
            if handle is not None:launcher.discard_staged(context,handle)
            self.research.archive.fail(context.run.run_id,getattr(error,"code","TA_NATIVE_FAILED"))
            raise

    @staticmethod
    def _public_result(raw,binding,bundle,context):
        if type(raw)is not dict or raw.get("family")!=FAMILY or raw.get("engine")!=native.ENGINE:
            raise ResearchError("TA_NATIVE_RESULT_INVALID")
        for key in ("owner_id","run_id","job_id","fencing_token","data_reference","snapshot_content_hash","selected_view_hash","model_profile","tools_manifest_hash","as_of"):
            if raw.get(key)!=binding[key]:raise ResearchError("TA_NATIVE_RESULT_BINDING_MISMATCH")
        if raw.get("binding_hash")!=native.resume_key(binding):raise ResearchError("TA_NATIVE_RESULT_BINDING_MISMATCH")
        state=raw.get("native_state")
        required=("final_trade_decision","market_report","sentiment_report","news_report","fundamentals_report")
        if type(state)is not dict or any(type(state.get(key))is not str for key in required):raise ResearchError("TA_NATIVE_REPORT_INVALID")
        debate=state["investment_debate_state"];risk=state["risk_debate_state"]
        if type(debate)is not dict or type(risk)is not dict or type(debate.get("count"))is not int or type(risk.get("count"))is not int:
            raise ResearchError("TA_NATIVE_ROUND_STATE_INVALID")
        if debate["count"]!=2*binding["options"]["debate_rounds"] or risk["count"]!=3*binding["options"]["risk_rounds"]:
            raise ResearchError("TA_NATIVE_ROUND_STATE_INVALID")
        if any(type(debate.get(key))is not str for key in ("bull_history","bear_history")) or type(risk.get("history"))is not str:
            raise ResearchError("TA_NATIVE_REPORT_INVALID")
        expected_scope="OFFLINE_FIXTURE" if binding["model_profile"]["mode"]=="OFFLINE_FIXTURE" else "DEVELOPMENT_RESEARCH"
        if raw.get("scope")!=expected_scope or raw.get("native_rating") not in {"Buy","Overweight","Hold","Underweight","Sell","REVIEW"}:
            raise ResearchError("TA_NATIVE_REPORT_INVALID")
        if raw.get("data_scope")!=bundle["scope"]:raise ResearchError("TA_NATIVE_DATA_SCOPE_MISMATCH")
        native.consumed(raw["budget"],binding["options"])
        if raw.get("publication_gate")!="CORE_JOB_SUCCEEDED_REQUIRED":raise ResearchError("TA_NATIVE_PUBLICATION_GATE_INVALID")
        paid="provider_approval" in bundle
        scopes=scope_projection(bundle,'DEVELOPMENT_RESEARCH' if paid else 'TEST_OFFLINE' if 'team_snapshot' in bundle else raw["scope"])
        provider_calls=("OBSERVED" if raw["budget"].get("provider_calls",0) else "NOT_RUN") if paid else "NOT_RUN"
        model_status="EXTERNAL_APPROVED" if paid else "OFFLINE_TEAM_CONFIGURED" if 'team_snapshot' in bundle else (
            "CONFIGURED" if binding["model_profile"]["mode"]=="SERVER_PROFILE" else "OFFLINE_FIXTURE")
        return {"family":FAMILY,"engine":native.ENGINE,"scope":raw["scope"],**scopes,"run_id":binding["run_id"],"job_id":binding["job_id"],
            "lineage":payload(context.run),"model_status":model_status,
            **({"approved_execution_sha256":bundle["provider_approval"]["execution_sha256"]} if paid else {}),
            'agent_team':None if 'team_snapshot' not in bundle else {'snapshot_id':bundle['team_snapshot']['snapshot_id'],'sha256':bundle['team_snapshot']['sha256'],'real_provider_calls':provider_calls},
            "report":{"conclusion":state["final_trade_decision"],"supporting_argument":debate["bull_history"],
                "opposing_argument":debate["bear_history"],"risk_debate":risk["history"],"rating":raw["native_rating"],
                "analyst_reports":{key:state[key] for key in required[1:]},"rounds":{"research_turns":debate["count"],"risk_turns":risk["count"]},
                "data_scope":scopes["data_scope"],"evidence_scopes":scopes["evidence_scopes"]},
            "tool_access_audit":raw.get("tool_access_audit",[]),"budget":raw["budget"],"publication_gate":"CORE_JOB_SUCCEEDED_REQUIRED"}
