"""Trusted installed module composition; worker and API share one registry."""
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

from .auth import AuthService, Principal, require_owner
from .contracts import RunMode
from .database import Database
from .errors import PlatformError
from .jobs import HandlerRegistry, JobService
from .schedules import SchedulerService
from .acceptance import AcceptanceDeclarations

MODULES = ("data", "strategies", "research", "trading", "operations")
HOSTED_DISABLED_HANDLERS = frozenset({"operations.backup", "operations.restore"})


@dataclass
class RuntimeContext:
    root: Path
    database: Database
    auth: AuthService
    jobs: JobService
    handlers: HandlerRegistry
    allowed_origins: tuple[str, ...] = ()
    modules: dict = field(default_factory=dict)
    routers: list = field(default_factory=list)
    scheduler: SchedulerService | None = None
    # Trusted server composition only; default remains unavailable.
    qlib_native_launcher: object | None = None
    qlib_reviewed_catalog: object | None = None
    qlib_export_authorizer: object | None = None
    qlib_server_material: object | None = None
    qlib_native_launcher_factory: object | None = None
    qlib_input_preparation: object | None = None
    qlib_dump_deployment: object | None = None
    tradingagents_native_launcher: object | None = None
    tradingagents_models: object | None = None
    access_mode: str = "accounts"
    hosted_accounts: bool = False
    agent_teams: object | None = None
    storage: object | None = None

    def require_principal(self, request, *, write=False):
        delegated = request.scope.get("state", {}).get("mcp_principal")
        if delegated is not None:
            if write and delegated.role != "researcher":
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            return delegated
        token = request.cookies.get("finance_session")
        if write:
            if request.headers.get("origin") not in self.allowed_origins:
                raise PlatformError("ORIGIN_REJECTED", 403)
            return self.auth.require_csrf(token, request.headers.get("x-csrf-token"))
        return self.auth.session(token)[0]

    def require_owner(self, principal, owner_id, *, write=False):
        return require_owner(principal, owner_id, write=write)

    def audit(self, principal, action, resource_id=None, details=None):
        return self.database.audit(principal.user_id if principal else None, action, resource_id, details)

    def module_capabilities(self):
        # Refresh actual byte/environment bindings on reads, never elevate from
        # discovery or mutate the underlying assembly facts/operational gates.
        return AcceptanceDeclarations(self.root).project(self.modules)

    @property
    def versions(self):
        return self.jobs.versions

    def list_audit(self, principal, *, limit=50, offset=0):
        if principal.role != "admin":
            raise PlatformError("FORBIDDEN", 403)
        self.jobs._paging(limit, offset)
        with closing(self.database.connect()) as con:
            total = con.execute("SELECT count(*) FROM audit_events").fetchone()[0]
            rows = con.execute("SELECT * FROM audit_events ORDER BY event_id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"items": [{"event_id": r["event_id"], "actor_id": r["actor_id"], "action": r["action"],
                           "resource_id": r["resource_id"], "recorded_at": r["recorded_at"], "details": json.loads(r["details_json"])} for r in rows],
                "total": total, "limit": limit, "offset": offset}


def _validate_synthetic(parameters):
    if set(parameters) - {"steps", "fail_at", "delay_ms"}:
        raise PlatformError("INVALID_INPUT", 422)
    steps = parameters.get("steps", 5)
    delay = parameters.get("delay_ms", 0)
    fail_at = parameters.get("fail_at")
    if type(steps) is not int or not 1 <= steps <= 1000 or type(delay) is not int or not 0 <= delay <= 100:
        raise PlatformError("INVALID_INPUT", 422)
    if fail_at is not None and (type(fail_at) is not int or not 0 <= fail_at < steps):
        raise PlatformError("INVALID_INPUT", 422)


def _synthetic_check(context, parameters):
    import time
    total = 0
    steps = parameters.get("steps", 5)
    for step in range(steps):
        context.require_active()
        if step == parameters.get("fail_at"):
            raise PlatformError("HANDLER_FAILED")
        if parameters.get("delay_ms"):
            time.sleep(parameters["delay_ms"] / 1000)
        total += step
        context.checkpoint(min(99, (step + 1) * 99 // steps), {"completed_steps": step + 1})
    return {"scope": "SYNTHETIC", "steps": steps, "sum": total, "market_data_used": False}


def _present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError as error:
        if error.name == name or name.startswith(error.name + "."):
            return False
        raise


def load_modules(runtime):
    for module_name in MODULES:
        package_name = "finance_platform." + module_name
        if not _present(package_name):
            runtime.modules[module_name] = {"implementation": "NOT_STARTED", "router": False, "handlers": False}
            continue
        package = importlib.import_module(package_name)
        # Explicit initialization is for trusted installed platform modules only.
        initialized = False
        if callable(getattr(package, "initialize", None)):
            package.initialize(runtime)
            initialized = True
        jobs_name = package_name + ".jobs"
        registered = False
        if _present(jobs_name):
            module = importlib.import_module(jobs_name)
            if callable(getattr(module, "register_handlers", None)):
                module.register_handlers(runtime.handlers, runtime)
                registered = True
        elif callable(getattr(package, "register_handlers", None)):
            package.register_handlers(runtime.handlers, runtime)
            registered = True
        api_name = package_name + ".api"
        routed = False
        if _present(api_name):
            module = importlib.import_module(api_name)
            factory = getattr(module, "create_router", None)
            if not callable(factory):
                raise PlatformError("HANDLER_UNAVAILABLE", 503)
            runtime.routers.append(factory(runtime))
            routed = True
        runtime.modules[module_name] = {"implementation": "IN_PROGRESS",
                                        "router": routed, "handlers": registered, "initialized": initialized,
                                        "verification": "NOT_RUN"}


def build_runtime(root: Path, database_path: Path | None = None, *, allowed_origins=(), include_modules=True, qlib_native_launcher=None,
                  qlib_reviewed_catalog=None, tradingagents_native_launcher=None, tradingagents_models=None,
                  qlib_project_binding=None, qlib_server_material=None,
                  qlib_native_launcher_factory=None, qlib_input_preparation=None, qlib_dump_deployment=None,
                  access_mode="accounts", team_catalogue=None, tradingagents_team_descriptor=None,model_capability_registry=None, auth_mfa_key=None, hosted_accounts=False):
    from .local_personal import validate_mode, PersonalAuthService
    validate_mode(access_mode, allowed_origins)
    if type(hosted_accounts) is not bool or hosted_accounts and access_mode != "accounts":
        raise PlatformError("INVALID_INPUT", 422)
    root = Path(root).resolve()
    destination = (root / Path(database_path)).resolve() if database_path else root / ".local/platform/platform.sqlite3"
    if not destination.is_relative_to(root) or any(destination.is_relative_to((root / p).resolve()) for p in
            ("planning", "samples/public", "data", ".local/data", ".local/prospective", ".local/secrets", ".local/audit")):
        raise PlatformError("INVALID_INPUT")
    database = Database(destination)
    database.migrate()
    registry = HandlerRegistry(root, disabled=HOSTED_DISABLED_HANDLERS if hosted_accounts else ())
    lock = root / "uv.lock"
    # Missing dependency lock is an installation error, not a fabricated digest.
    if not lock.is_file():
        raise PlatformError("INVALID_INPUT")
    jobs = JobService(database, registry, dependency_hash=hashlib.sha256(lock.read_bytes()).hexdigest())
    auth = (PersonalAuthService(database) if access_mode == "local-personal"
            else AuthService(database, mfa_key=auth_mfa_key))
    runtime = RuntimeContext(root, database, auth, jobs, registry, tuple(allowed_origins))
    # API and worker construct the same DB-backed registry; no process-local
    # cache may decide where a later submitted job writes durable content.
    from predictor.access import AccessPolicy
    from .storage_locations import StorageService
    runtime.storage = StorageService(database, AccessPolicy(root))
    jobs.storage = runtime.storage
    jobs.versions.attach_storage(runtime.storage)
    runtime.access_mode = access_mode
    runtime.hosted_accounts = hosted_accounts
    from .team_profiles import TeamProfileService
    from .model_capabilities import ModelCapabilities
    from .model_library import ModelProfileService
    if model_capability_registry is not None and type(model_capability_registry)is not ModelCapabilities:
        raise PlatformError('INVALID_INPUT',422)
    runtime.model_capabilities=model_capability_registry or ModelCapabilities(team_catalogue)
    def credential_store():
        from finance_platform.operations.provider_credentials import ProviderCredentialStore
        return ProviderCredentialStore(root,target_resolver=runtime.model_capabilities.resolve_target)
    runtime.provider_credential_store_factory=credential_store
    runtime.model_profiles=ModelProfileService(database,capabilities=runtime.model_capabilities,
        credential_metadata=lambda owner,reference:credential_store().binding_for_reference(owner,reference),
        target_freezer=lambda owner,provider,ident:credential_store().freeze_target(owner,provider,ident),
        reference_validator=lambda owner,reference,*,target:credential_store().validate_reference_target(owner,reference,target=target))
    runtime.agent_teams = TeamProfileService(database, catalogue=team_catalogue,model_profiles=runtime.model_profiles,capabilities=runtime.model_capabilities)
    if qlib_project_binding is not None:
        if qlib_native_launcher is not None:
            raise PlatformError('QLIB_AMBIGUOUS_SERVER_BINDING')
        from .qlib_composition import project_qlib_launcher
        qlib_native_launcher = project_qlib_launcher(runtime, qlib_project_binding)
    runtime.qlib_native_launcher = qlib_native_launcher
    runtime.qlib_reviewed_catalog = qlib_reviewed_catalog
    if qlib_server_material is not None:
        from .server_composition import QlibServerMaterial
        if type(qlib_server_material) is not QlibServerMaterial or qlib_server_material.root != root:
            raise PlatformError('QLIB_SERVER_MATERIAL_INVALID')
    runtime.qlib_server_material = qlib_server_material
    if qlib_server_material is not None and qlib_native_launcher_factory is None:
        from .qlib_composition import ExportQlibLauncherFactory
        qlib_native_launcher_factory = ExportQlibLauncherFactory(runtime, qlib_server_material)
    if qlib_native_launcher_factory is not None and (qlib_native_launcher is not None or qlib_project_binding is not None):
        raise PlatformError('QLIB_AMBIGUOUS_SERVER_BINDING')
    runtime.qlib_native_launcher_factory = qlib_native_launcher_factory
    runtime.qlib_input_preparation = qlib_input_preparation
    runtime.qlib_dump_deployment = qlib_dump_deployment
    if tradingagents_team_descriptor is not None:
        if tradingagents_native_launcher is not None:
            raise PlatformError("TA_AMBIGUOUS_SERVER_BINDING")
        from .ta_team_deployment import load_personal_team_launcher
        tradingagents_native_launcher = load_personal_team_launcher(runtime,tradingagents_team_descriptor)
    if tradingagents_native_launcher is None and include_modules:
        from .ta_product import TANativeProductLauncher
        tradingagents_native_launcher = TANativeProductLauncher()
    runtime.tradingagents_native_launcher = tradingagents_native_launcher
    runtime.tradingagents_models = tradingagents_models
    runtime.scheduler = SchedulerService(database, jobs)
    registry.register("synthetic.check", _synthetic_check, validate=_validate_synthetic, recoverable=True)
    if include_modules:
        from .computation_cache import ComputationCache
        from .computation_cache_api import create_router as create_cache_router
        from .model_connection_test import install as install_model_connection_test

        runtime.computation_cache = ComputationCache(database)
        runtime.routers.append(create_cache_router(runtime))
        install_model_connection_test(runtime, credential_store)
        load_modules(runtime)
        from .ux_composition import install_workflows
        install_workflows(runtime, runtime.provider_credential_store_factory())
    return runtime
