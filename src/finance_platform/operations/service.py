"""Persistent local health, alerts and sanitized support bundles."""
from contextlib import closing
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit

from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import safe_json, now_text
from .backup import BackupService, digest
from .config import validate_config, validate_receiver, doctor, load_config
from .paths import contained, protect_private
from .pagination import validate_page

MIGRATIONS = ("""
CREATE TABLE operations_alerts(alert_id TEXT PRIMARY KEY, code TEXT NOT NULL, severity TEXT NOT NULL,
 recorded_at TEXT NOT NULL, summary_json TEXT NOT NULL);
CREATE TABLE operations_deliveries(delivery_id TEXT PRIMARY KEY, alert_id TEXT NOT NULL REFERENCES operations_alerts(alert_id),
 recorded_at TEXT NOT NULL, outcome TEXT NOT NULL);
CREATE TABLE operations_heartbeats(component TEXT PRIMARY KEY, recorded_at REAL NOT NULL);
""",)

ALERT_CODES = {"WORKER_STALE", "QUEUE_BACKLOG", "JOB_RECOVERY_REQUIRED", "ORDER_PENDING",
               "ORDER_UNKNOWN", "ACCOUNT_FROZEN", "DATA_QUALITY_BLOCKED", "LOCAL_NOTIFICATION_TEST"}
NOTIFICATION_TIMEOUT_SECONDS = 3


def require_admin(principal):
    if principal.role != "admin":
        raise PlatformError("FORBIDDEN", 403)


class OperationsService:
    def __init__(self, runtime, config=None):
        self.runtime, self.database = runtime, runtime.database
        local_config = Path(runtime.root) / ".local/platform/operations.json"
        self.config = validate_config(config) if config is not None else load_config(runtime.root, local_config) if local_config.exists() else validate_config({})
        self.backups = BackupService(runtime.root, self.database)
        self.database.apply_module_migrations("operations", MIGRATIONS)
        with self.database.transaction() as con:
            immutable(con, "operations_alerts", ("alert_id",))
            immutable(con, "operations_deliveries", ("delivery_id",))

    def heartbeat(self, component):
        if component not in {"api", "worker"}:
            raise PlatformError("INVALID_INPUT")
        with self.database.transaction() as con:
            con.execute("INSERT INTO operations_heartbeats VALUES(?,?) ON CONFLICT(component) DO UPDATE SET recorded_at=excluded.recorded_at", (component, time.time()))

    def health(self):
        with closing(self.database.connect()) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            queue = {r["status"]: r["n"] for r in con.execute("SELECT status,count(*) n FROM jobs GROUP BY status")}
            stale = con.execute("SELECT count(*) FROM jobs WHERE status='RUNNING' AND lease_until<=?", (time.time(),)).fetchone()[0]
            oldest = con.execute("SELECT min(created_at) FROM jobs WHERE status='QUEUED'").fetchone()[0]
            age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds()) if oldest else 0
            hearts = {r["component"]: max(0, time.time() - r["recorded_at"]) for r in con.execute("SELECT * FROM operations_heartbeats")}
            trading = {"implementation": "MODULE_ABSENT"}
            if "trading_orders" in tables:
                trading = {"implementation": "IMPLEMENTED",
                           "pending_orders": con.execute("SELECT count(*) FROM trading_orders WHERE status NOT IN ('filled','cancelled','rejected','expired')").fetchone()[0],
                           "unknown_orders": con.execute("SELECT count(*) FROM trading_orders WHERE status IN ('unknown','sending')").fetchone()[0],
                           "frozen_accounts": con.execute("SELECT count(*) FROM trading_accounts WHERE frozen=1").fetchone()[0],
                           "reconciliation_records": con.execute("SELECT count(*) FROM trading_reconciliations").fetchone()[0]}
            data = {"implementation": "MODULE_ABSENT"}
            if "data_records" in tables:
                data = {"implementation": "IMPLEMENTED", "records": con.execute("SELECT count(*) FROM data_records").fetchone()[0],
                        "snapshots": con.execute("SELECT count(*) FROM data_snapshots").fetchone()[0]}
                quality = con.execute("SELECT sum(CASE WHEN json_extract(body,'$.quality.allowed')=0 THEN 1 ELSE 0 END), max(CAST(json_extract(body,'$.quality.max_delay_seconds') AS REAL)) FROM data_snapshots").fetchone()
                data.update({"quality_blocked_snapshots": quality[0] or 0, "max_snapshot_delay_seconds": quality[1],
                             "delay_semantics": "historical available_at minus bar_end/effective_at; not an online feed latency certification"})
        db = self.database.health()
        ready = db["database"] == "ok" and hearts.get("worker", float("inf")) <= self.config["worker_stale_seconds"]
        return {"liveness": "ok", "readiness": "ok" if ready else "degraded", "database": db,
                "queue": queue, "oldest_queued_seconds": round(age, 3), "expired_running_leases": stale,
                "component_age_seconds": {k: round(v, 3) for k, v in hearts.items()}, "trading": trading, "data": data,
                "recovery": "RECONCILIATION_REQUIRED" if trading.get("frozen_accounts", 0) else "NO_FROZEN_ACCOUNTS",
                "live_enabled": False, "measurement_at": now_text()}

    def evaluate_alerts(self):
        health = self.health()
        conditions = [("WORKER_STALE", "error", health["readiness"] != "ok"),
                      ("QUEUE_BACKLOG", "warning", health["queue"].get("QUEUED", 0) >= self.config["queue_warning"]),
                      ("JOB_RECOVERY_REQUIRED", "error", health["expired_running_leases"] > 0),
                      ("ORDER_PENDING", "warning", health["trading"].get("pending_orders", 0) >= self.config["pending_order_warning"]),
                      ("ORDER_UNKNOWN", "error", health["trading"].get("unknown_orders", 0) > 0),
                      ("ACCOUNT_FROZEN", "error", health["trading"].get("frozen_accounts", 0) > 0)]
        conditions.append(("DATA_QUALITY_BLOCKED", "warning", health["data"].get("quality_blocked_snapshots", 0) > 0))
        rows = []
        with self.database.transaction() as con:
            for code, severity, active in conditions:
                if not active:
                    continue
                # Coalesce unchanged active conditions within a five-minute bucket.
                identity = code + "-" + str(int(time.time() // 300))
                if con.execute("SELECT 1 FROM operations_alerts WHERE alert_id=?", (identity,)).fetchone():
                    continue
                summary = {"code": code, "severity": severity, "recorded_at": now_text()}
                con.execute("INSERT INTO operations_alerts VALUES(?,?,?,?,?)", (identity, code, severity, summary["recorded_at"], safe_json(summary)))
                rows.append({"alert_id": identity, **summary})
        return rows

    def list_alerts(self, principal, limit=50, offset=0):
        require_admin(principal)
        validate_page(limit, offset)
        with closing(self.database.connect()) as con:
            total = con.execute("SELECT count(*) FROM operations_alerts").fetchone()[0]
            rows = con.execute("SELECT * FROM operations_alerts ORDER BY recorded_at DESC,alert_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def notification_status(self, principal):
        require_admin(principal)
        notification = self.config["notification"]
        return {"enabled": notification["enabled"], "configured": notification["endpoint"] is not None,
                "scope": "LOCAL_RECEIVER_ONLY", "automatic_delivery": False,
                "socket_timeout_seconds": NOTIFICATION_TIMEOUT_SECONDS,
                "configuration_effect": "RESTART_REQUIRED", "provider_verification": "NOT_RUN"}

    def _notification_endpoint(self, principal):
        require_admin(principal)
        notification = self.config["notification"]
        if not notification["enabled"]:
            self.runtime.audit(principal, "operations.local_notification", details={"outcome": "DISABLED"})
            raise PlatformError("NOTIFICATION_DISABLED", 409)
        return validate_receiver(notification["endpoint"])

    def list_deliveries(self, principal, limit=50, offset=0):
        require_admin(principal)
        validate_page(limit, offset)
        with closing(self.database.connect()) as con:
            total = con.execute("SELECT count(*) FROM operations_deliveries").fetchone()[0]
            rows = con.execute("SELECT delivery_id,alert_id,recorded_at,outcome FROM operations_deliveries ORDER BY recorded_at DESC,delivery_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def test_notification(self, principal):
        self._notification_endpoint(principal)  # No test alert or network when disabled.
        alert_id = "LOCAL_NOTIFICATION_TEST-" + uuid.uuid4().hex
        summary = {"code": "LOCAL_NOTIFICATION_TEST", "severity": "info", "recorded_at": now_text()}
        with self.database.transaction() as con:
            con.execute("INSERT INTO operations_alerts VALUES(?,?,?,?,?)", (alert_id, summary["code"],
                        summary["severity"], summary["recorded_at"], safe_json(summary)))
        return self.notify_local(principal, alert_id)

    def notify_local(self, principal, alert_id):
        endpoint = self._notification_endpoint(principal)
        if type(alert_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", alert_id):
            raise PlatformError("INVALID_INPUT", 422)
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT alert_id,code,severity,recorded_at FROM operations_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        if not row:
            self.runtime.audit(principal, "operations.local_notification", details={"outcome": "NOT_FOUND"})
            raise PlatformError("NOT_FOUND", 404)
        if row["code"] not in ALERT_CODES or row["severity"] not in {"info", "warning", "error"}:
            raise PlatformError("INVALID_INPUT", 422)
        # Only fixed alert columns leave the process; no summary_json, logs,
        # arbitrary messages, endpoint/response text or environment values.
        payload = safe_json(dict(row)).encode("utf-8")
        url = urlsplit(endpoint)
        outcome = "FAILED"
        status = None
        # http.client bypasses proxy environment variables and never follows redirects.
        connection = http.client.HTTPConnection("127.0.0.1", url.port, timeout=NOTIFICATION_TIMEOUT_SECONDS)
        try:
            connection.request("POST", url.path or "/", payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            status = response.status
            if 200 <= response.status < 300:
                outcome = "LOCAL_RECEIVER_ACCEPTED"
            # The receiver body has no contract and is never read or retained.
        except TimeoutError:
            outcome = "TIMED_OUT"
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        delivery_id = uuid.uuid4().hex
        with self.database.transaction() as con:
            con.execute("INSERT INTO operations_deliveries VALUES(?,?,?,?)", (delivery_id, alert_id, now_text(), outcome))
        self.runtime.audit(principal, "operations.local_notification", alert_id, {"outcome": outcome, "delivery_id": delivery_id,
                                                                                 "http_status": status})
        return {"alert_id": alert_id, "delivery_id": delivery_id, "outcome": outcome, "http_status": status,
                "scope": "LOCAL_RECEIVER_ONLY", "provider_verification": "NOT_RUN"}

    def diagnostics(self, principal):
        require_admin(principal)
        base = contained(self.runtime.root, Path(self.runtime.root) / ".local/platform/diagnostics")
        base.mkdir(parents=True, exist_ok=True)
        protect_private(base)
        report_id = "diagnostics-" + uuid.uuid4().hex
        target = contained(base, base / (report_id + ".json"))
        report = {"schema_version": 1, "report_id": report_id, "created_at": now_text(),
                  "doctor": doctor(self.runtime.root, self.config), "health": self.health(),
                  "content_policy": "ALLOWLIST_METRICS_ONLY_NO_RAW_LOGS_ENV_SESSIONS_OR_DATABASE"}
        with target.open("x", encoding="utf-8") as stream:
            stream.write(safe_json(report))
        protect_private(target)
        self.runtime.audit(principal, "operations.diagnostics", report_id)
        return {"report_id": report_id, "sha256": digest(target), "report": report}
