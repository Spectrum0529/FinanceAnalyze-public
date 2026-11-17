"""Admin-only operations router, composed by the core app."""
from fastapi import APIRouter, Request, Query
from pydantic import BaseModel, ConfigDict

from .service import OperationsService, require_admin
from .pagination import MAX_OFFSET


class NotificationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def create_router(runtime):
    router = APIRouter(tags=["operations"])
    service = OperationsService(runtime)

    def admin(request, write=False):
        principal = runtime.require_principal(request, write=write)
        require_admin(principal)
        return principal

    @router.get("/operations/health")
    def health(request: Request):
        admin(request)
        return service.health()

    @router.get("/operations/alerts")
    def alerts(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        return service.list_alerts(admin(request), limit, offset)

    @router.post("/operations/alerts/evaluate")
    def evaluate(request: Request):
        principal = admin(request, True)
        rows = service.evaluate_alerts()
        runtime.audit(principal, "operations.alert_evaluate", details={"created": len(rows)})
        return {"items": rows}

    @router.get("/operations/notifications")
    def notification_status(request: Request):
        return service.notification_status(admin(request))

    @router.get("/operations/notifications/deliveries")
    def notification_deliveries(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        return service.list_deliveries(admin(request), limit, offset)

    @router.post("/operations/notifications/test")
    def notification_test(body: NotificationBody, request: Request):
        return service.test_notification(admin(request, True))

    @router.post("/operations/alerts/{alert_id}/notify")
    def notify_alert(alert_id: str, body: NotificationBody, request: Request):
        return service.notify_local(admin(request, True), alert_id)

    @router.post("/operations/diagnostics")
    def diagnostics(request: Request):
        return service.diagnostics(admin(request, True))

    @router.get("/operations/backups")
    def backups(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        admin(request)
        return service.backups.list(limit=limit, offset=offset)

    @router.post("/operations/backups")
    def backup(request: Request):
        principal = admin(request, True)
        return runtime.jobs.submit(principal, "operations.backup", {}, idempotency_key=None)

    @router.post("/operations/backups/{backup_id}/verify")
    def verify(backup_id: str, request: Request):
        principal = admin(request, True)
        manifest = service.backups.verify(backup_id)
        runtime.audit(principal, "operations.backup_verify", backup_id)
        return {"backup_id": backup_id, "validation": manifest["validation"]}

    @router.post("/operations/backups/{backup_id}/restore")
    def restore(backup_id: str, request: Request):
        principal = admin(request, True)
        service.backups._target(service.backups.base, backup_id)
        return runtime.jobs.submit(principal, "operations.restore", {"backup_id": backup_id})

    return router
