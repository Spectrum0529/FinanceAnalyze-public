"""Exact project TA worker/product sources; no reports-runtime schema alias."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
from .ta_native import FilePin
from .ta_identity import verify_source
from .ta_protocol import checked,encoded,digest
from .ta_product import fail,LIMITS

IMPORTS=("tradingagents","tradingagents.llm_clients","tradingagents.graph.setup","tradingagents.graph.trading_graph","langgraph.checkpoint.sqlite","langchain_core.language_models.chat_models")
NAMES=frozenset({"worker_binding","runtime","worker_python","manifest","lock","worker_spec","identity_probe","probe_result","probe_request","probe_stdout","probe_stderr",
    "venv_config","base_python","base_dll","wheel","import_receipt","bootstrap","identity","protocol","guard","child","base_controls","native_entry","workflow","model_port","sources","security_receipt","coordinator_scope"})


def matches(row,pin):
    return (type(row)is dict and set(row)=={"path","bytes","sha256"} and type(row["path"])is str and Path(row["path"]).is_absolute()
            and Path(row["path"]).absolute()==pin.path and type(row["bytes"])is int and row["bytes"]==pin.bytes and row["sha256"]==pin.sha256)


@dataclass(frozen=True)
class ProductBinding:
    pins: tuple
    source_identity: str
    attempt_base: Path
    model_profile_sha256: str
    project_root: Path

    def __post_init__(self):
        if (type(self.pins)is not tuple or len(self.pins)!=len(NAMES) or {key for key,_ in self.pins}!=NAMES
                or any(type(pin)is not FilePin for _,pin in self.pins) or not Path(self.attempt_base).is_absolute()
                or not Path(self.project_root).is_absolute()
                or any(type(value)is not str or len(value)!=64 or any(char not in "0123456789abcdef" for char in value) for value in (self.source_identity,self.model_profile_sha256))):
            fail("TA_PRODUCT_BINDING_INVALID")

    @property
    def files(self):return dict(self.pins)

    def verify(self):
        files=self.files
        for pin in files.values():pin.read_verified()
        worker=checked(files["worker_binding"].read_verified());runtime=checked(files["runtime"].read_verified())
        completion=checked(files["probe_result"].read_verified());request=checked(files["probe_request"].read_verified())
        prefix=files["worker_python"].path.parents[1];root=prefix.parent;site=prefix/"Lib/site-packages"
        if (worker.get("schema")!="FA010_MEASURED_PROJECT_WORKER_BINDING_EVIDENCE_2" or worker.get("worker")!="tradingagents"
                or root!=Path(self.project_root).absolute()/"workers/tradingagents"
                or root.name!="tradingagents" or root.parent.name!="workers" or prefix!=root/".venv"
                or worker.get("worker_root")!=str(root) or worker.get("site_root")!=str(site)
                or worker.get("reports_venv_fallback_allowed") is not False or worker.get("native_workflow_activated") is not False
                or runtime.get("schema")!="FINANCE_ANALYZE_WORKER_ACTUAL_RUNTIME_IDENTITY_1" or runtime.get("worker_kind")!="tradingagents"
                or runtime.get("version")!=[3,12,14] or runtime.get("platform")!="win32" or runtime.get("pointer_bits")!=64
                or runtime.get("worker_root")!=str(root) or runtime.get("venv_prefix")!=str(prefix) or runtime.get("site_root")!=str(site)
                or runtime.get("base_prefix")!=str(files["base_python"].path.parent) or runtime.get("reports_venv_fallback_allowed") is not False
                or files["base_dll"].path!=files["base_python"].path.parent/"python312.dll"):
            fail("TA_PRODUCT_PROJECT_WORKER_REQUIRED")
        for key,worker_key in (("worker_python","python"),("manifest","manifest"),("lock","lock"),("worker_spec","worker_spec"),
            ("identity_probe","identity_probe"),("base_python","base_python"),("base_dll","actual_loaded_python_DLL"),("venv_config","venv_config")):
            if not matches(worker.get(worker_key),files[key]) or not matches(runtime.get(worker_key),files[key]):fail("TA_PRODUCT_WORKER_PIN_MISMATCH")
        if not matches(worker.get("identity"),files["runtime"]):fail("TA_PRODUCT_WORKER_PIN_MISMATCH")
        if (files["worker_python"].path!=prefix/"Scripts/python.exe" or files["manifest"].path!=root/"pyproject.toml"
                or files["lock"].path!=root/"uv.lock" or files["identity_probe"].path!=root/"inspect_runtime.py"
                or files["venv_config"].path!=prefix/"pyvenv.cfg" or files["worker_spec"].path!=root/"worker_spec.json"):
            fail("TA_PRODUCT_WORKER_PIN_MISMATCH")
        if (completion.get("schema")!="FA010_TA_INSTALL_REPAIR_STAGE_ACTUAL_2" or completion.get("stage")!="probe"
                or type(completion.get("exit_code"))is not int or completion["exit_code"]!=0 or completion.get("timeout") is not False
                or completion.get("native_workflow_activated") is not False or completion.get("automatic_retry") is not False
                or type(completion.get("elapsed_seconds"))not in (int,float) or isinstance(completion.get("elapsed_seconds"),bool)
                or not 0<=completion["elapsed_seconds"]<=30 or request.get("timeout_seconds")!=30
                or completion.get("new_run_id")!=worker.get("new_run_id") or request.get("new_run_id")!=worker.get("new_run_id")
                or request.get("schema")!="FA010_TA_INSTALL_REPAIR_STAGE_REQUEST_2" or request.get("stage")!="probe"
                or request.get("cwd")!=str(root) or request.get("command")!=[str(files["worker_python"].path),"-I","-B","-X","utf8",str(files["identity_probe"].path)]
                or any(not matches(completion.get(field),files[key]) for field,key in (("request","probe_request"),("stdout","probe_stdout"),("stderr","probe_stderr")))
                or files["probe_stderr"].read_verified()!=b"" or encoded(checked(files["probe_stdout"].read_verified()))!=encoded(runtime)):
            fail("TA_PRODUCT_PROBE_COMPLETION_UNPROVEN")
        spec=checked(files["worker_spec"].read_verified())
        if (spec.get("worker_kind")!="tradingagents" or spec.get("reports_venv_fallback_allowed") is not False
                or spec.get("expected_installed_distribution_versions")!=runtime.get("installed_distribution_versions")
                or spec.get("primary_distribution_source_files")!=runtime.get("primary_distribution_source_files")
                or any(spec.get("control_file_sha256",{}).get(files[key].path.name)!=files[key].sha256 for key in ("manifest","lock","identity_probe"))):
            fail("TA_PRODUCT_WORKER_SPEC_MISMATCH")
        sources=checked(files["sources"].read_verified())
        if (sources.get("schema")!="FA013_TA_PRODUCT_SOURCES_1" or type(sources.get("rows"))is not list
                or not sources["rows"] or type(sources.get("guard_sources"))is not list
                or type(sources.get("package_roots"))is not dict):fail("TA_PRODUCT_SOURCE_BINDING_MISMATCH")
        if digest({key:sources[key] for key in ("rows","guard_sources","package_roots")})!=self.source_identity:fail("TA_PRODUCT_SOURCE_BINDING_MISMATCH")
        indexed={}
        for row in sources["rows"]:
            path=verify_source(row,str(site))
            if str(path)in indexed:fail("TA_PRODUCT_SOURCE_BINDING_MISMATCH")
            indexed[str(path)]=row
        for row in sources["guard_sources"]:
            if indexed.get(str(Path(row["path"]).absolute()))!=row:fail("TA_PRODUCT_SOURCE_BINDING_MISMATCH")
        for key in ("bootstrap","identity","protocol","guard","child","base_controls","native_entry","workflow","model_port"):
            row=indexed.get(str(files[key].path))
            if row is None or any(row.get(field)!=files[key].metadata()[field] for field in ("path","bytes","sha256")):
                fail("TA_PRODUCT_SOURCE_BINDING_MISMATCH")
        packages=sources["package_roots"]
        if (set(packages)!={"finance_platform","finance_platform.core","finance_platform.research","predictor","predictor.research"}
                or packages["finance_platform.research"]!=[str(files["native_entry"].path.parent)]):
            fail("TA_PRODUCT_PACKAGE_ROOTS_INVALID")
        for name,paths in packages.items():
            if type(paths)is not list or not paths or len(paths)>4:fail("TA_PRODUCT_PACKAGE_ROOTS_INVALID")
            for path in paths:
                if type(path)is not str or not Path(path).is_absolute() or not any(Path(row["path"]).parent==Path(path) for row in indexed.values()):
                    fail("TA_PRODUCT_PACKAGE_ROOTS_INVALID")
        if (files["native_entry"].path.name!="debate_native.py" or files["workflow"].path!=files["native_entry"].path.parent/"tradingagents_workflow.py"
                or files["model_port"].path.name!="server_model_ports.py"):
            fail("TA_PRODUCT_FIXED_ENTRY_REQUIRED")
        imports=checked(files["import_receipt"].read_verified())
        if (imports.get("worker_binding_sha256")!=files["worker_binding"].sha256 or imports.get("complete_imports")!=list(IMPORTS)
                or imports.get("decision")!="PASS_PROJECT_SIX_IMPORTS"):
            fail("TA_PRODUCT_CURRENT_PROJECT_IMPORT_PROOF_REQUIRED")
        return runtime,worker,sources

    @property
    def release(self):
        return {"complete_imports":list(IMPORTS),"commit":"e8f5503e9b86d5c2710b0c76b1f686703968c9df",
            "wheel_sha256":self.files["wheel"].sha256,"worker_lock_sha256":self.files["lock"].sha256,
            "import_receipt_sha256":self.files["import_receipt"].sha256,"security_receipt_sha256":self.files["security_receipt"].sha256,
            "source_manifest_sha256":self.source_identity,"model_profile_sha256":self.model_profile_sha256}

    def review(self,context,case,attempt):
        self.verify()
        if case!="SERVER_PROFILE" or Path(attempt).absolute()!=self.attempt(context):fail("TA_PRODUCT_SCOPE_MISMATCH")
        expected={"source_identity":self.source_identity,"profile":LIMITS.profile,"model_profile_sha256":self.model_profile_sha256,
            "native_entrypoint":"finance_platform.research.debate_native:execute_native_request"}
        security=checked(self.files["security_receipt"].read_verified());scope=checked(self.files["coordinator_scope"].read_verified())
        if (security.get("decision")!="GO" or security.get("reviewer")!="FA012-R03" or security.get("binding")!=expected or scope.get("decision")!="RELEASE"
                or scope.get("coordinator")!="FA001-R03"
                or scope.get("binding")!=expected or scope.get("security_receipt_sha256")!=self.files["security_receipt"].sha256):
            fail("TA_PRODUCT_CURRENT_RELEASE_REQUIRED")

    def attempt(self,context):
        run=context.run.run_id
        if type(run)is not str or not run or not run[0].isalnum() or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in run):
            fail("TA_PRODUCT_RUN_NAMESPACE_INVALID")
        return Path(self.attempt_base).absolute()/hashlib.sha256(context.run.owner_id.encode()).hexdigest()/run


@dataclass(frozen=True)
class PersonalTeamBinding(ProductBinding):
    """Current batch source-bound host, not a fabricated per-case review GO."""
    def review(self,context,case,attempt):
        from .auth import Principal
        from .ta_native import require_real_context
        require_real_context(context)
        self.verify()
        if case!="SERVER_PROFILE" or Path(attempt).absolute()!=self.attempt(context):
            fail("TA_PRODUCT_SCOPE_MISMATCH")
        authority=checked(self.files['coordinator_scope'].read_verified())
        coordinator=authority.get('coordinator_thread_id')
        if (type(coordinator)is not str or not coordinator.isascii() or authority.get('schema')!='FA001_PERSONAL_MULTI_MODEL_BATCH_1'
                or authority.get('status')!='AUTHORIZED_ACTIVE'
                or hashlib.sha256(coordinator.encode('utf-8')).hexdigest()!='67d4a361ba98b4f5e30fee978a4a0d4047429c1c71f8d0c46c7239d8dea11070'
                or authority.get('local_personal_use',{}).get('requested') is not True):
            fail('TA_CURRENT_HUMAN_BATCH_AUTHORITY_REQUIRED')
        principal=Principal(context.run.owner_id,context.run.owner_id,'researcher')
        if context.run.model is None:fail('TA_TEAM_PROFILE_BINDING_REQUIRED')
        value=context.service.versions.get(principal,context.run.model)
        if type(value)is not dict or set(value) not in ({'profile','team_snapshot'},
                {'profile','team_snapshot','provider_approval'}):fail('TA_TEAM_PROFILE_BINDING_REQUIRED')
        snapshot=value['team_snapshot'];profile=value['profile']
        if (snapshot.get('owner_id')!=context.run.owner_id or snapshot.get('sha256')!=self.model_profile_sha256
                or digest({key:snapshot[key] for key in ('configuration','resolved_roles')})!=snapshot.get('sha256')
                or profile!={'mode':'SERVER_PROFILE','profile_id':'team-'+snapshot['snapshot_id'],'profile_sha256':snapshot['sha256']}):
            fail('TA_TEAM_PROFILE_BINDING_REQUIRED')
        if set(snapshot.get('resolved_roles',{}))!={'market_analyst','social_analyst','news_analyst','fundamentals_analyst','bull_researcher','bear_researcher','research_manager','trader','risky_analyst','safe_analyst','neutral_analyst','risk_manager'}:
            fail('TA_TEAM_PROFILE_BINDING_REQUIRED')
        from .ta_team_admission import approved_team
        approved_team(snapshot,profile,value.get("provider_approval"))
