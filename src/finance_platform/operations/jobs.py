"""Backup/restore task handlers; restore never replaces the active database."""
from contextlib import closing
from .service import OperationsService
from .service import require_admin
from finance_platform.core.errors import PlatformError


def register_handlers(registry, runtime):
    service = OperationsService(runtime)

    def authorize(context):
        # Generic job submission must not bypass the operations router's admin gate.
        with closing(runtime.database.connect()) as con:
            row = con.execute("SELECT role,enabled FROM users WHERE user_id=?", (context.run.owner_id,)).fetchone()
        if not row or row["role"] != "admin" or not row["enabled"]:
            raise PlatformError("FORBIDDEN", 403)

    def resolve(principal, parameters):
        # Synchronous resolver makes direct /jobs and scheduler submission admin-only too.
        require_admin(principal)
        return {}

    def validate_backup(parameters):
        if parameters != {}:
            raise PlatformError("INVALID_INPUT")

    def validate_restore(parameters):
        if type(parameters) is not dict or set(parameters) != {"backup_id"}:
            raise PlatformError("INVALID_INPUT")
        service.backups._target(service.backups.base, parameters["backup_id"])

    def backup(context, parameters):
        authorize(context)
        context.require_active()
        result = service.backups.create()
        context.require_active()
        # The public job result exposes bounded summary; account replay stays private.
        return {"backup_id": result["backup_id"], "manifest_sha256": result["manifest_sha256"], "bytes": result["bytes"]}

    def restore(context, parameters):
        authorize(context)
        context.require_active()
        result = service.backups.restore(parameters["backup_id"])
        context.require_active()
        return {"restore_id": result["restore_id"], "backup_id": result["backup_id"],
                "session_restore": result["session_restore"], "trading_resume": result["trading_resume"]}

    registry.register("operations.backup", backup, validate=validate_backup, recoverable=False, resolve_versions=resolve)
    registry.register("operations.restore", restore, validate=validate_restore, recoverable=False, resolve_versions=resolve)
