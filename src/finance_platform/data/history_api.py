"""Small authenticated saved-history API; routes never acquire data directly."""
from contextlib import closing
from finance_platform.core.contracts import RunMode, identifier
from finance_platform.core.errors import PlatformError
from finance_platform.data.contracts import DataError, validate_paging
from .history_gateway import HistoryGateway
from .history_jobs import validate_download
from .market_lineage import history_metadata
from .market_management import approval


def mount_history_routes(router, runtime, service):
    from fastapi import Request
    from fastapi.responses import JSONResponse
    gateway = HistoryGateway(service, None)
    def actor(request, write=False):
        principal = runtime.require_principal(request, write=write)
        runtime.require_owner(principal, principal.user_id, write=write)
        return principal
    def call(fn, *args, **kwargs):
        try: return fn(*args, **kwargs)
        except DataError as e: raise PlatformError(e.code, 404 if e.code == "RESOURCE_NOT_FOUND" else 422) from None
        except PlatformError: raise
        except (KeyError, TypeError, ValueError): raise PlatformError("INVALID_INPUT", 422) from None
    def submitted(principal, handler, params, key):
        job = runtime.jobs.submit(principal, handler, params, mode=RunMode.BACKTEST, idempotency_key=key)
        return JSONResponse({"job_id": job["job_id"], "run_id": job["run_id"], "status": job["status"], "operation": handler}, status_code=202)

    @router.post("/history/search")
    def search(request: Request, body: dict):
        principal = actor(request, True)
        if set(body)-{"query", "idempotency_key"} or "query" not in body: raise PlatformError("INVALID_INPUT", 422)
        return submitted(principal, "data.history_search", {"query": body["query"]}, body.get("idempotency_key"))

    @router.post("/history/download")
    def download(request: Request, body: dict):
        principal = actor(request, True)
        params = {k: v for k, v in body.items() if k != "idempotency_key"}
        call(validate_download, params)
        if params.get("history_id") is not None:
            ref = call(gateway.freeze_reference, principal.user_id, params["history_id"], params.get("history_version"))
            params["history_version"] = ref["version"]
        return submitted(principal, "data.history_download", params, body.get("idempotency_key"))

    @router.get("/history")
    def histories(request: Request, state: str = "active", limit: int = 50, offset: int = 0):
        return call(gateway.list, actor(request).user_id, limit, offset, state=state)

    @router.get("/history/submissions/{idempotency_key}")
    def submission(request: Request, idempotency_key: str):
        principal = actor(request)
        call(identifier, idempotency_key)
        with closing(runtime.database.connect()) as con:
            row = con.execute("SELECT j.job_id,j.run_id,j.status,j.handler FROM job_keys k JOIN jobs j ON j.job_id=k.job_id "
                              "WHERE k.owner_id=? AND k.idempotency_key=? AND j.handler IN ('data.history_download','data.history_search')", (principal.user_id, idempotency_key)).fetchone()
        if row is None: raise PlatformError("RESOURCE_NOT_FOUND", 404)
        return {"job_id": row["job_id"], "run_id": row["run_id"], "status": row["status"], "operation": row["handler"]}

    @router.get("/history/{history_id}")
    def detail(request: Request, history_id: str, version: int | None = None, include_deleted: bool = False):
        return call(gateway.summary, actor(request).user_id, history_id, version, include_deleted=include_deleted)

    @router.get("/history/{history_id}/rows")
    def rows(request: Request, history_id: str, version: int, limit: int = 100, offset: int = 0):
        owner = actor(request).user_id
        call(validate_paging, limit, offset)
        if limit > 100: raise PlatformError("PAGINATION_INVALID", 422)
        summary = call(gateway.summary, owner, history_id, version)
        value = call(gateway.store.view, owner, history_id, version, summary["requested_range"]["start"], summary["requested_range"]["end"])
        return {"history_id": history_id, "version": version, "content_hash": value["content_hash"], "items": value["rows"][offset:offset+limit], "total": len(value["rows"]), "limit": limit, "offset": offset}

    @router.post("/history/{history_id}/lifecycle")
    def lifecycle(request: Request, history_id: str, body: dict):
        principal = actor(request, True)
        if set(body)-{"action", "name", "expected_revision", "idempotency_key"} or not {"action", "expected_revision", "idempotency_key"} <= set(body): raise PlatformError("INVALID_INPUT", 422)
        guard = approval(principal)
        def check(con):
            guard(con)
            value = gateway.store._version(con, principal.user_id, history_id, None, allow_deleted=True)
            try:
                history_metadata(value)
            except DataError:
                raise DataError("RESOURCE_NOT_FOUND") from None
        return call(gateway.lifecycle, principal.user_id, history_id, **body,
                    publication_guard=check if guard is not None else None)
