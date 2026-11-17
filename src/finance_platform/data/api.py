"""Authenticated data router; root runtime owns sessions, CSRF and audit."""
import base64
import binascii
from contextlib import closing
import json
from uuid import uuid4

from predictor.access import AccessPolicy
from finance_platform.core.errors import PlatformError
from finance_platform.core.contracts import RunMode, payload
from finance_platform.data.contracts import DataError, validate_paging
from .provider_sdk.service import ProviderDataService as DataService
from finance_platform.data.jobs import connector_for
from finance_platform.data.collection import connection_test, Collector


def create_router(runtime):
    from fastapi import APIRouter, Request
    from fastapi.responses import Response
    router = APIRouter(prefix="/data", tags=["数据中心"])
    service = DataService(runtime.database, AccessPolicy(runtime.root))

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError as exc:
            raise PlatformError(exc.code, 404 if exc.code == "RESOURCE_NOT_FOUND" else 422) from None
        except (TypeError, KeyError, ValueError):
            raise PlatformError("INVALID_INPUT", 422) from None

    def actor(request, write=False):
        p = runtime.require_principal(request, write=write)
        runtime.require_owner(p, p.user_id, write=write)
        return p

    def raw_bytes(body):
        data = body.get("content_base64")
        if not isinstance(data, str) or len(data) > 23 * 1024 * 1024:
            raise PlatformError("INVALID_INPUT", 422)
        try:
            return base64.b64decode(data, validate=True)
        except binascii.Error:
            raise PlatformError("INVALID_INPUT", 422) from None

    @router.get("/sources")
    def sources(request: Request, limit: int = 50, offset: int = 0):
        return call(service.list_sources, actor(request).user_id, limit, offset)

    @router.get("/catalog")
    def catalog(request: Request):
        actor(request)
        from .catalog import builtin_catalog
        return {"items": builtin_catalog(), "formal_research_approved": False}

    @router.post("/sources")
    def create_source(request: Request, body: dict):
        p = actor(request, True)
        result = call(service.register_source, p.user_id, body)
        runtime.audit(p, "data.source_registered", result["source_id"], {"version": result["version"]})
        return result

    @router.get("/sources/{source_id}")
    def source(request: Request, source_id: str, version: int | None = None):
        p=actor(request)
        if version is not None and version<1:raise PlatformError("INVALID_INPUT",422)
        return call(service.source,p.user_id,source_id,version)

    @router.post("/datasets")
    def create_dataset(request: Request, body: dict):
        p = actor(request, True)
        result = call(service.create_dataset, p.user_id, body.get("source_id"), scope=body.get("scope"),
                      identities=body.get("identities", []), title=body.get("title", "Development dataset"), issuer_ids=body.get("issuer_ids", []))
        runtime.audit(p, "data.dataset_created", result["dataset_id"])
        return result

    @router.get("/datasets")
    def datasets(request: Request, limit: int = 50, offset: int = 0):
        p = actor(request)
        call(validate_paging, limit, offset)
        with closing(runtime.database.connect()) as conn:
            total = conn.execute("SELECT count(*) FROM data_datasets WHERE owner_id=?", (p.user_id,)).fetchone()[0]
            rows = conn.execute("SELECT dataset_id,body,content_hash FROM data_datasets WHERE owner_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (p.user_id, limit, offset)).fetchall()
        return {"items": [{"dataset_id": r[0], **json.loads(r[1]), "content_hash": r[2]} for r in rows], "total": total, "limit": limit, "offset": offset}

    @router.get("/datasets/{dataset_id}")
    def dataset(request: Request, dataset_id: str):
        return call(service.dataset, actor(request).user_id, dataset_id)

    @router.get("/datasets/{dataset_id}/coverage")
    def coverage(request: Request, dataset_id: str):
        return call(service.coverage, actor(request).user_id, dataset_id)

    @router.post("/datasets/{dataset_id}/preview")
    def preview(request: Request, dataset_id: str, body: dict):
        p = actor(request, True)
        result = call(service.preview, p.user_id, dataset_id, raw_bytes(body), body.get("format"), body.get("mapping"))
        # Preview is bounded in response size; normalized rows never replace error reports.
        return {**result, "records": result["records"][:100], "preview_limit": 100}

    @router.post("/datasets/{dataset_id}/import")
    def import_data(request: Request, dataset_id: str, body: dict):
        p = actor(request, True)
        result = call(service.import_bytes, p.user_id, dataset_id, raw_bytes(body), body.get("format"), body.get("mapping"))
        runtime.audit(p, "data.imported", dataset_id, {"appended": result["appended"], "raw_hash": result["raw"]["body_hash"]})
        return result

    @router.post("/datasets/{dataset_id}/snapshots")
    def snapshot(request: Request, dataset_id: str, body: dict):
        p = actor(request, True)
        result = call(service.snapshot, p.user_id, dataset_id, as_of=body.get("as_of"), instrument_ids=body.get("instrument_ids"),
                      expected_keys=body.get("expected_keys", []), max_delay_seconds=body.get("max_delay_seconds"), expected_bars=body.get("expected_bars", []), effective_at=body.get("effective_at"), required_kinds=body.get("required_kinds"))
        runtime.audit(p, "data.snapshot_frozen", result["snapshot_id"], {"content_hash": result["content_hash"], "quality_allowed": result["quality"]["allowed"]})
        return result

    @router.post("/datasets/{dataset_id}/events")
    def events(request: Request, dataset_id: str, body: dict):
        p = actor(request, True)
        result = call(service.import_events_bytes, p.user_id, dataset_id, raw_bytes(body))
        runtime.audit(p, "data.events_imported", dataset_id, {"appended": result["appended"], "raw_hash": result["raw"]["body_hash"]})
        return result

    @router.get("/snapshots")
    def snapshots(request: Request, limit: int = 50, offset: int = 0):
        p = actor(request)
        call(validate_paging, limit, offset)
        with closing(runtime.database.connect()) as conn:
            total = conn.execute("SELECT count(*) FROM data_snapshots WHERE owner_id=?", (p.user_id,)).fetchone()[0]
            rows = conn.execute("SELECT snapshot_id,body,content_hash FROM data_snapshots WHERE owner_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (p.user_id, limit, offset)).fetchall()
        return {"items": [{"snapshot_id": r[0], **{k: v for k, v in json.loads(r[1]).items() if k != "records"}, "content_hash": r[2]} for r in rows], "total": total, "limit": limit, "offset": offset}

    @router.get("/snapshots/{snapshot_id}")
    def get_snapshot(request: Request, snapshot_id: str):
        return call(service.get_snapshot, actor(request).user_id, snapshot_id, require_quality=False)

    @router.get("/snapshots/{snapshot_id}/export")
    def export(request: Request, snapshot_id: str, format: str = "csv"):
        p = actor(request)
        raw = call(service.export, p.user_id, snapshot_id, format)
        runtime.audit(p, "data.exported", snapshot_id, {"format": format, "bytes": len(raw)})
        return Response(raw, media_type="text/csv" if format == "csv" else "application/vnd.apache.parquet",
                        headers={"Content-Disposition": 'attachment; filename="snapshot.' + format + '"'})

    @router.post("/datasets/{dataset_id}/connection-test")
    def test_connection(request: Request, dataset_id: str, body: dict | None = None):
        p = actor(request, True)
        d = call(service.dataset, p.user_id, dataset_id)
        s = call(service.source, p.user_id, d["source_id"], d["source_version"])
        if s["provider"] in {"yfinance","akshare"}:
            resolved=call(service.resolve_call,p.user_id,dataset_id,body or {},probe=True)
            result=call(runtime.jobs.submit,p,"data.connection_test",{"dataset_id":dataset_id,"parameters":body or {},"collection_id":None,"max_pages":1,"resolved_call":resolved},mode=RunMode.BACKTEST)
            from fastapi.responses import JSONResponse
            return JSONResponse({**result,"job_type":"data.connection_test","data_family":"provider-observation-1"},status_code=202)
        result = call(connection_test, call(connector_for, s), d, s["license"]["license_id"])
        runtime.audit(p, "data.connection_test", dataset_id, {"requests": result["requests"], "configuration": result["configuration"]})
        return result

    @router.post("/datasets/{dataset_id}/collect")
    def collect(request: Request, dataset_id: str, body: dict):
        p = actor(request, True)
        d = call(service.dataset, p.user_id, dataset_id)
        s = call(service.source, p.user_id, d["source_id"], d["source_version"])
        if s["provider"] in {"yfinance", "akshare"}:
            call(service.resolve_call,p.user_id,dataset_id,body.get("parameters"))
            if body.get("max_pages",1)!=1:raise PlatformError("SDK_UNIT_LIMIT",422)
            if set(body)-{"parameters","collection_id","max_pages","idempotency_key"}:raise PlatformError("INVALID_INPUT",422)
            parameters={"dataset_id":dataset_id,"parameters":body.get("parameters"),"collection_id":body.get("collection_id"),"max_pages":1}
            return call(runtime.jobs.submit,p,"data.collect",parameters,mode=RunMode.BACKTEST,idempotency_key=body.get("idempotency_key"))
        connector = call(connector_for, s)
        call(connector.initial, body.get("parameters"))
        parameters = {"dataset_id": dataset_id, "parameters": body.get("parameters"),
                      "collection_id": body.get("collection_id"), "max_pages": body.get("max_pages", 100)}
        return call(runtime.jobs.submit, p, "data.collect", parameters, mode=RunMode.BACKTEST, idempotency_key=body.get("idempotency_key"))

    @router.get("/collections/{collection_id}")
    def collection(request: Request, collection_id: str):
        p = actor(request)
        pages = Collector(service).pages(p.user_id, collection_id)
        with closing(runtime.database.connect()) as conn:
            events = [json.loads(r[0]) for r in conn.execute("SELECT body FROM data_collection_events WHERE collection_id=? AND owner_id=? ORDER BY event_id", (collection_id, p.user_id))]
        return {"collection_id": collection_id, "pages": pages, "events": events}

    @router.post("/datasets/{dataset_id}/stream")
    def stream(request: Request, dataset_id: str, body: dict):
        p = actor(request, True)
        d = call(service.dataset, p.user_id, dataset_id)
        s = call(service.source, p.user_id, d["source_id"], d["source_version"])
        if s["provider"] != "alpaca":
            raise PlatformError("UNSUPPORTED_CAPABILITY", 422)
        return call(runtime.jobs.submit, p, "data.stream", {"dataset_id": dataset_id, "symbols": body.get("symbols"), "max_frames": body.get("max_frames", 100)},
                    mode=RunMode.BACKTEST, idempotency_key=body.get("idempotency_key"))

    @router.get("/schedules")
    def schedules(request: Request, limit: int = 50, offset: int = 0):
        from .provider_sdk.schedules import CollectionSchedules
        return call(CollectionSchedules(service, runtime).list, actor(request), limit, offset)

    @router.post("/schedules")
    def create_schedule(request: Request, body: dict):
        from .provider_sdk.schedules import CollectionSchedules
        p = actor(request, True)
        result = call(CollectionSchedules(service, runtime).create, p, dataset_id=body.get("dataset_id"), start_at=body.get("start_at"),
                      interval_seconds=body.get("interval_seconds"), parameters=body.get("parameters"), max_pages=body.get("max_pages", 100))
        runtime.audit(p, "data.schedule_created", result["schedule_id"])
        return result

    @router.post("/schedules/{schedule_id}/enabled")
    def enabled(request: Request, schedule_id: str, body: dict):
        from .provider_sdk.schedules import CollectionSchedules
        p = actor(request, True)
        result = call(CollectionSchedules(service, runtime).set_enabled, p, schedule_id, body.get("enabled"))
        runtime.audit(p, "data.schedule_enabled", schedule_id, {"enabled": not result["paused"]})
        return result

    @router.get("/datasets/{dataset_id}/observations")
    def observations(request: Request,dataset_id: str,limit: int=50,offset: int=0):
        return call(service.observations,actor(request).user_id,dataset_id,limit,offset)

    @router.get("/observations/{result_id}")
    def observation(request: Request,result_id: str,limit: int=50,offset: int=0):
        return call(service.observation,actor(request).user_id,result_id,limit,offset)

    @router.get("/observations/{result_id}/export")
    def observation_export(request: Request,result_id: str,format: str="json"):
        p=actor(request)
        raw=call(service.export_observation,p.user_id,result_id,format)
        runtime.audit(p,"data.observation_exported",result_id,{"format":format,"bytes":len(raw)})
        return Response(raw,media_type="application/json" if format=="json" else "text/csv",
            headers={"Content-Disposition":'attachment; filename="observation.'+format+'"'})

    from .history_api import mount_history_routes
    mount_history_routes(router, runtime, service)
    return router
