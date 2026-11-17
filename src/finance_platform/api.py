"""Versioned authenticated control plane. Jobs execute in a separate worker."""
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
from typing import Literal
import uuid

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from .core.contracts import RunMode
from .core.errors import PlatformError
from .core.runtime import build_runtime
from .core.overview import OverviewService
from .core.pagination import MAX_LIMIT, MAX_OFFSET
from .core.pending_writes import KEY_CLOSED, install_guards as install_pending_write_guards, recover as recover_write
from .core.mcp_request_context import request_approval

log = logging.getLogger("finance_platform.api")
DEFAULT_ORIGINS = ("http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class JobBody(StrictModel):
    handler: str = Field(min_length=1, max_length=128)
    parameters: dict = Field(default_factory=dict)
    mode: str = "BACKTEST"
    idempotency_key: str | None = Field(default=None, max_length=128)
    versions: None = None

class RetryBody(StrictModel):
    idempotency_key:str=Field(min_length=1,max_length=128)


class JobOperationCapability(StrictModel):
    available: bool
    reason_code: str | None


class JobCapabilitiesResponse(StrictModel):
    job_id: str
    status: str
    cancel: JobOperationCapability
    retry: JobOperationCapability
    resume: JobOperationCapability


class PendingWriteBody(StrictModel):
    method: Literal["POST", "PUT", "DELETE"]
    path: str = Field(min_length=2, max_length=320)
    idempotency_key: str = Field(min_length=1, max_length=128)


class ClosePendingWriteBody(PendingWriteBody):
    confirm: bool


class PendingWriteResource(StrictModel):
    kind: Literal["job", "connection", "dataset", "model_profile", "upload"]
    id: str
    job_id: str | None = None
    run_id: str | None = None
    owner_id: str | None = None


class PendingWriteReceipt(StrictModel):
    status: Literal["COMMITTED", "UNKNOWN", "CLOSED"]
    resource: PendingWriteResource | None


class ScheduleBody(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    handler: str = Field(min_length=1, max_length=128)
    parameters: dict = Field(default_factory=dict)
    mode: str = "BACKTEST"
    rule: str
    interval_seconds: int | None = None
    start_at: str
    idempotency_key:str|None=Field(default=None,min_length=1,max_length=128)


class TriggerBody(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=128)


class ErrorResponse(StrictModel):
    code: str
    message: str
    request_id: str
    details: dict


class JobResponse(StrictModel):
    job_id: str
    run_id: str
    parent_run_id: str | None
    owner_id: str
    handler: str
    mode: str
    status: str
    progress: int
    cancel_requested: bool
    created_at: str
    updated_at: str
    result: dict | None
    error_code: str | None


class PageResponse(StrictModel):
    items: list[dict]
    total: int
    limit: int = Field(ge=1, le=MAX_LIMIT)
    offset: int = Field(ge=0, le=MAX_OFFSET)


class JobPageResponse(PageResponse):
    items: list[JobResponse]


class DataSummaryResponse(StrictModel):
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    sources: int | None
    datasets: int | None
    snapshots: int | None
    quality_allowed_snapshots: int | None
    quality_blocked_snapshots: int | None
    quality_unknown_snapshots: int | None
    quality_state: Literal["BLOCKED", "UNKNOWN", "CLEAR", "NO_SNAPSHOTS", "UNAVAILABLE"]


class StrategySummaryResponse(StrictModel):
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    instances: int | None
    running: int | None
    by_status: dict[str, int] | None


class AccountSummaryResponse(StrictModel):
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    total: int | None
    by_mode: dict[str, int] | None
    frozen: int | None
    emergency_stopped: int | None
    unknown_orders: int | None
    sending_orders: int | None
    accounts_with_uncertain_orders: int | None


class OverviewSummaryResponse(StrictModel):
    scope: Literal["ALL_OWNERS", "OWNED"]
    generated_at: str
    data: DataSummaryResponse
    strategies: StrategySummaryResponse
    accounts: AccountSummaryResponse


class OverviewResponse(StrictModel):
    jobs: dict[str, int]
    recent_jobs: JobPageResponse
    database: dict
    modules: dict
    mode: str
    original_research: str
    live_trading_enabled: bool
    summary: OverviewSummaryResponse


class SecurityMiddleware:
    """Bounded request body, exact Host/Origin, safe correlation and no input logs."""
    def __init__(self, app, *, allowed_origins, access_mode="accounts", hosted_config=None, mcp_access=None):
        self.app, self.origins = app, set(allowed_origins)
        self.hosts = {urlsplit(origin).netloc.lower() for origin in self.origins}
        self.access_mode, self.hosted_config = access_mode, hosted_config
        self.mcp_access = mcp_access

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket" and (self.hosted_config is not None or self.access_mode == "local-personal"):
            await send({"type": "websocket.close", "code": 4403})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        header_pairs = scope["headers"]
        hosts = [v.decode("latin-1").lower() for k, v in header_pairs if k.lower() == b"host"]
        origins = [v.decode("latin-1") for k, v in header_pairs if k.lower() == b"origin"]
        duplicate_csrf = sum(key.lower() == b"x-csrf-token" for key, _ in header_pairs) > 1
        try:
            if duplicate_csrf:
                raise PlatformError("CSRF_REJECTED", 403)
            if self.access_mode == "local-personal":
                from .core.local_personal import require_local_scope
                require_local_scope(scope, self.origins)
            if len(hosts) != 1 or hosts[0] not in self.hosts or len(origins) > 1:
                raise PlatformError("ORIGIN_REJECTED", 403)
            if self.hosted_config is not None:
                try:
                    self.hosted_config.validate_request(
                        scheme=scope.get("scheme"), client=(scope.get("client") or (None,))[0],
                        method=scope["method"], path=scope["path"], headers=header_pairs)
                except PermissionError:
                    raise PlatformError("FORBIDDEN", 403) from None
                except (ValueError, TypeError):
                    raise PlatformError("ORIGIN_REJECTED", 403) from None
            if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
                if len(origins) != 1 or origins[0] not in self.origins:
                    raise PlatformError("ORIGIN_REJECTED", 403)
                if urlsplit(origins[0]).netloc.lower() != hosts[0] or urlsplit(origins[0]).scheme != scope.get("scheme"):
                    raise PlatformError("ORIGIN_REJECTED", 403)
            # Auth inputs are short and must remain bounded before JSON processing.
            max_body = 8192 if scope["path"].startswith("/api/v1/auth/") else 2 * 1024 * 1024
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > max_body:
                    raise PlatformError("INVALID_INPUT", 413)
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            if scope["path"].startswith("/api/v1/operations/mcp-connection/"):
                from .core.mcp_access import signed_path
                if signed_path(scope):
                    if self.mcp_access is None:
                        raise PlatformError("FORBIDDEN", 403)
                    from starlette.concurrency import run_in_threadpool
                    scope = await run_in_threadpool(self.mcp_access.authorize, scope, bytes(body))
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()
            async def correlated(message):
                if message["type"] == "http.response.start":
                    message.setdefault("headers", []).extend([(b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store"),
                        (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer")])
                    log.info(json.dumps({"event": "http_response", "request_id": request_id, "method": scope["method"], "status": message["status"]}))
                await send(message)
            with request_approval(scope):
                await self.app(scope, replay, correlated)
        except PlatformError as error:
            await JSONResponse(error.response(request_id), status_code=error.status, headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})(scope, receive, send)


def create_app(root: Path, database_path: Path | None = None, *, allowed_origins=DEFAULT_ORIGINS,
               include_modules=True, secure_cookies=False, qlib_native_launcher=None,
               qlib_reviewed_catalog=None, tradingagents_native_launcher=None, tradingagents_models=None,
               qlib_project_binding=None, qlib_server_material=None,
               qlib_native_launcher_factory=None, qlib_input_preparation=None, qlib_dump_deployment=None,
               access_mode="accounts", team_catalogue=None, tradingagents_team_descriptor=None,model_capability_registry=None,
               hosted_accounts=False, auth_mfa_key=None):
    hosted_config = None
    try:
        parsed = [urlsplit(value) for value in allowed_origins]
        if not parsed or any(p.scheme not in {"http", "https"} or not p.hostname or p.path
                             or p.username is not None or p.query or p.fragment or p.netloc != p.netloc.lower()
                             or p.port is not None and not 1 <= p.port <= 65535 for p in parsed):
            raise ValueError
        local = all(p.hostname in {"127.0.0.1", "localhost"} for p in parsed)
        if hosted_accounts:
            if len(parsed) != 1 or not secure_cookies or access_mode != "accounts":
                raise ValueError
            from .core.hosted_config import HostedConfig
            hosted_config = HostedConfig(allowed_origins[0])
        elif not local:
            raise ValueError
    except (ValueError, TypeError):
        raise PlatformError("INVALID_INPUT", 422) from None
    runtime = build_runtime(root, database_path, allowed_origins=allowed_origins, include_modules=include_modules,
        qlib_native_launcher=qlib_native_launcher, qlib_reviewed_catalog=qlib_reviewed_catalog,
        tradingagents_native_launcher=tradingagents_native_launcher, tradingagents_models=tradingagents_models,
        qlib_project_binding=qlib_project_binding, qlib_server_material=qlib_server_material,
        qlib_native_launcher_factory=qlib_native_launcher_factory, qlib_input_preparation=qlib_input_preparation,
        qlib_dump_deployment=qlib_dump_deployment, access_mode=access_mode, team_catalogue=team_catalogue, tradingagents_team_descriptor=tradingagents_team_descriptor,model_capability_registry=model_capability_registry, auth_mfa_key=auth_mfa_key, hosted_accounts=hosted_accounts)
    if hosted_config is not None and not runtime.auth.initialized():
        raise PlatformError("SETUP_REQUIRED", 503)
    install_pending_write_guards(runtime.database)
    errors = {code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 413, 422, 429, 500, 503)}
    app = FastAPI(title="FinanceAnalyze platform", version="1.0.0", openapi_url=None if hosted_accounts else "/api/v1/openapi.json",
                  docs_url=None if hosted_accounts else "/api/v1/docs", redoc_url=None,
                  responses=errors)
    app.state.runtime = runtime
    mcp_grants = mcp_access = None
    if include_modules and runtime.modules.get("operations", {}).get("router") and access_mode == "accounts" and not hosted_accounts:
        from .core.mcp_access import McpAccess
        from .core.mcp_grants import McpGrants
        from .core.mcp_proof import installation_identity
        mcp_grants = McpGrants(runtime.auth, installation_identity(runtime.root))
        mcp_access = McpAccess(mcp_grants, runtime.allowed_origins, runtime=runtime)
    app.add_middleware(CORSMiddleware, allow_origins=list(allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "X-CSRF-Token"],
                       expose_headers=["X-Request-ID"])
    app.add_middleware(SecurityMiddleware, allowed_origins=allowed_origins, access_mode=access_mode,
                       hosted_config=hosted_config, mcp_access=mcp_access)

    def request_id(request):
        return getattr(request.state, "request_id", str(uuid.uuid4()))

    @app.exception_handler(PlatformError)
    async def platform_error(request: Request, error: PlatformError):
        return JSONResponse(error.response(request_id(request)), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return JSONResponse(PlatformError("INVALID_INPUT", 422).response(request_id(request)), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error):
        code = "NOT_FOUND" if error.status_code == 404 else "INVALID_INPUT"
        return JSONResponse(PlatformError(code, error.status_code).response(request_id(request)), status_code=error.status_code)

    @app.exception_handler(Exception)
    async def unknown_error(request: Request, error):
        if isinstance(error, sqlite3.IntegrityError) and str(error) == KEY_CLOSED:
            return JSONResponse(PlatformError(KEY_CLOSED, 409).response(request_id(request)), status_code=409)
        # No exception text, traceback, URL, request body, cookie or credential logs.
        code = "DATABASE_UNAVAILABLE" if isinstance(error, sqlite3.Error) else "INTERNAL_ERROR"
        log.error(json.dumps({"event": "request_failed", "request_id": request_id(request), "code": code}))
        status = 503 if code == "DATABASE_UNAVAILABLE" else 500
        return JSONResponse(PlatformError(code, status).response(request_id(request)), status_code=status,
                            headers={"X-Request-ID": request_id(request)})

    @app.get("/api/v1/health/live")
    def live():
        return {"status": "alive", "live_trading_enabled": False}

    @app.get("/api/v1/health/ready")
    def ready():
        health = runtime.database.health()
        if health["database"] != "ok":
            raise PlatformError("DATABASE_UNAVAILABLE", 503)
        return {"status": "ready", **health}

    from .core.account_api import create_router as create_accounts_router
    app.include_router(create_accounts_router(runtime, secure_cookies=secure_cookies, hosted=hosted_accounts),
                       prefix="/api/v1")

    @app.get("/api/v1/jobs", response_model=JobPageResponse)
    def jobs(request: Request, limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        return runtime.jobs.list(runtime.require_principal(request), limit=limit, offset=offset)

    @app.post("/api/v1/jobs", response_model=JobResponse, status_code=201)
    def submit(body: JobBody, request: Request):
        return runtime.jobs.submit(runtime.require_principal(request, write=True), body.handler, body.parameters, mode=body.mode,
                                   idempotency_key=body.idempotency_key, versions=body.versions)

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
    def job(job_id: str, request: Request):
        return runtime.jobs.get(runtime.require_principal(request), job_id)

    def job_authorization(request, operation):
        if getattr(request.state, "mcp_principal", None) is None:
            return None
        authorize = getattr(request.state, "mcp_authorize_job", None)
        if callable(authorize):
            return authorize
        if operation == "cancel":
            from .core.mcp_access import require_portfolio_job
            return require_portfolio_job
        raise PlatformError("MCP_SCOPE_REQUIRED", 403)

    def checkpoint_status(principal, job):
        verifier = getattr(runtime.tradingagents_native_launcher, "checkpoint_status", None)
        return verifier(principal, job) if callable(verifier) else None

    @app.get("/api/v1/jobs/{job_id}/capabilities", response_model=JobCapabilitiesResponse)
    def job_capabilities(job_id: str, request: Request):
        principal = runtime.require_principal(request)
        return runtime.jobs.operation_capabilities(principal, job_id, checkpoint_status=checkpoint_status,
                                                  authorize_job=job_authorization(request, "capabilities"))

    @app.get("/api/v1/jobs/{job_id}/events", response_model=PageResponse)
    def events(job_id: str, request: Request, limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        return runtime.jobs.events(runtime.require_principal(request), job_id, limit=limit, offset=offset)

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobResponse)
    def cancel(job_id: str, request: Request):
        principal = runtime.require_principal(request, write=True)
        return runtime.jobs.cancel(principal, job_id, authorize_job=job_authorization(request, "cancel"))

    @app.post("/api/v1/jobs/{job_id}/retry", response_model=JobResponse, status_code=201)
    def retry(job_id: str, request: Request, body: RetryBody | None = None):
        principal = runtime.require_principal(request, write=True)
        return runtime.jobs.retry(principal, job_id, idempotency_key=body.idempotency_key if body else None,
                                  authorize_job=job_authorization(request, "retry"), checkpoint_status=checkpoint_status)

    @app.post("/api/v1/jobs/{job_id}/resume", response_model=JobResponse, status_code=201)
    def resume(job_id: str, body: RetryBody, request: Request):
        principal = runtime.require_principal(request, write=True)
        return runtime.jobs.retry(principal, job_id, idempotency_key=body.idempotency_key, intent="resume",
                                  authorize_job=job_authorization(request, "resume"), checkpoint_status=checkpoint_status)

    @app.post("/api/v1/operations/pending-write/lookup", response_model=PendingWriteReceipt,
              response_model_exclude_unset=True)
    def pending_write(body: PendingWriteBody, request: Request):
        principal = runtime.require_principal(request, write=True)
        return recover_write(runtime.database, principal, body.method, body.path, body.idempotency_key)

    @app.post("/api/v1/operations/pending-write/close", response_model=PendingWriteReceipt,
              response_model_exclude_unset=True)
    def close_pending_write(body: ClosePendingWriteBody, request: Request):
        principal = runtime.require_principal(request, write=True)
        if not body.confirm:
            raise PlatformError("INVALID_INPUT", 422)
        return recover_write(runtime.database, principal, body.method, body.path, body.idempotency_key, close=True)

    @app.get("/api/v1/capabilities")
    def capabilities(request: Request):
        runtime.require_principal(request)
        return {"contract_version": "platform-1", "modes": ["BACKTEST", "LOCAL_SIMULATION", "BROKER_PAPER"], "live_trading_enabled": False,
                "handlers": runtime.handlers.list(), "modules": runtime.module_capabilities(), "original_research": "BLOCKED_DATA_UNVERIFIED"}

    @app.get("/api/v1/schedules", response_model=PageResponse)
    def schedules(request: Request, limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        return runtime.scheduler.list(runtime.require_principal(request), limit=limit, offset=offset)

    @app.post("/api/v1/schedules", status_code=201)
    def add_schedule(body: ScheduleBody, request: Request):
        return runtime.scheduler.create(runtime.require_principal(request, write=True), body.model_dump(exclude={'idempotency_key'}),idempotency_key=body.idempotency_key)

    @app.get("/api/v1/schedules/{schedule_id}")
    def schedule(schedule_id: str, request: Request):
        return runtime.scheduler.get(runtime.require_principal(request), schedule_id)

    @app.put("/api/v1/schedules/{schedule_id}")
    def update_schedule(schedule_id: str, body: ScheduleBody, request: Request):
        return runtime.scheduler.update(runtime.require_principal(request, write=True), schedule_id, body.model_dump(exclude={'idempotency_key'}),idempotency_key=body.idempotency_key)

    @app.delete("/api/v1/schedules/{schedule_id}")
    def archive_schedule(schedule_id: str, request: Request):
        return runtime.scheduler.archive(runtime.require_principal(request, write=True), schedule_id)

    @app.post("/api/v1/schedules/{schedule_id}/pause")
    def pause_schedule(schedule_id: str, request: Request):
        return runtime.scheduler.set_paused(runtime.require_principal(request, write=True), schedule_id, True)

    @app.post("/api/v1/schedules/{schedule_id}/resume")
    def resume_schedule(schedule_id: str, request: Request):
        return runtime.scheduler.set_paused(runtime.require_principal(request, write=True), schedule_id, False)

    @app.post("/api/v1/schedules/{schedule_id}/trigger")
    def trigger_schedule(schedule_id: str, body: TriggerBody, request: Request):
        return runtime.scheduler.trigger(runtime.require_principal(request, write=True), schedule_id, idempotency_key=body.idempotency_key)

    @app.get("/api/v1/schedules/{schedule_id}/history", response_model=PageResponse)
    def schedule_history(schedule_id: str, request: Request, limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        return runtime.scheduler.history(runtime.require_principal(request), schedule_id, limit=limit, offset=offset)

    @app.get("/api/v1/audit", response_model=PageResponse)
    def audit(request: Request, limit: int = Query(50, ge=1, le=MAX_LIMIT), offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        return runtime.list_audit(runtime.require_principal(request), limit=limit, offset=offset)

    @app.get("/api/v1/overview", response_model=OverviewResponse)
    def overview(request: Request):
        principal = runtime.require_principal(request)
        predicate, args = ("1=1", ()) if principal.role == "admin" else ("owner_id=?", (principal.user_id,))
        with closing(runtime.database.connect()) as con:
            counts = {r[0]: r[1] for r in con.execute(f"SELECT status,count(*) FROM jobs WHERE {predicate} GROUP BY status", args)}
        return {"jobs": counts, "recent_jobs": runtime.jobs.list(principal, limit=10), "database": runtime.database.health(),
                "modules": runtime.module_capabilities(), "mode": "LOCAL_SIMULATION", "original_research": "BLOCKED_DATA_UNVERIFIED", "live_trading_enabled": False,
                "summary": OverviewService(runtime).summary(principal)}

    from .core.team_profiles_api import create_router as create_team_router
    from .core.storage_locations_api import create_router as create_storage_router
    app.include_router(create_storage_router(runtime), prefix="/api/v1")
    app.include_router(create_team_router(runtime), prefix="/api/v1")
    from .core.model_library_api import create_router as create_model_library_router
    app.include_router(create_model_library_router(runtime),prefix='/api/v1')
    from .core.result_index import create_router as create_results_router
    research_results=daily_results=task_results=None
    if include_modules and runtime.modules.get('research',{}).get('router'):
        from .research.service import ResearchService
        from .research.daily_service import DailyBacktestService
        research_results=getattr(runtime,'research_result_service',None) or ResearchService(runtime)
        daily_results=getattr(runtime,'daily_result_service',None) or DailyBacktestService(runtime)
        try:
            from .research.ux_service import ResearchTaskService
        except ModuleNotFoundError as error:
            if error.name!='finance_platform.research.ux_service':raise
        else:
            task_results=getattr(runtime,'research_task_service',None) or ResearchTaskService(runtime)
    app.include_router(create_results_router(runtime,research_service=research_results,daily_service=daily_results,task_service=task_results),prefix='/api/v1')
    if mcp_access is not None:
        from .core.mcp_research import create_router as create_mcp_research_router
        app.include_router(create_mcp_research_router(runtime, research_service=research_results), prefix="/api/v1")
    for router in runtime.routers:
        app.include_router(router, prefix="/api/v1")
    if include_modules and runtime.modules.get('operations',{}).get('router'):
        from .operations.provider_credentials import ProviderCredentialStore
        from .operations.provider_credentials_api import create_router as create_credential_router
        from .operations.mcp_connection import create_router as create_mcp_connection_router
        credential_store=runtime.provider_credential_store_factory()
        app.include_router(create_credential_router(runtime,credential_store),prefix='/api/v1')
        app.include_router(create_mcp_connection_router(runtime, mcp_grants),prefix='/api/v1')
    # Optional built production UI is same-origin. Missing build is an explicit
    # deployment gap; API remains usable. No source directory or secrets served.
    web_dist = (runtime.root / "web/dist").resolve()
    if web_dist.is_relative_to(runtime.root / "web") and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="console")
    return app
