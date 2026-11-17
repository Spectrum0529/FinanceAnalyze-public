"""Read-only aggregate projections; every domain query applies owner scope first."""
from contextlib import closing

from .security import now_text


class OverviewService:
    def __init__(self, runtime):
        self.runtime = runtime

    def _available(self, connection, module, schema):
        # Availability is a query/assembly fact, never engineering acceptance.
        if not self.runtime.modules.get(module, {}).get("router"):
            return False
        for table, columns in schema.items():
            actual = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
            if not set(columns) <= actual:
                return False
        return True

    @staticmethod
    def _scope(principal, alias):
        return ("1=1", ()) if principal.role == "admin" else (f"{alias}.owner_id=?", (principal.user_id,))

    def _data(self, connection, principal):
        unavailable = {"status": "UNAVAILABLE", "sources": None, "datasets": None, "snapshots": None,
                       "quality_allowed_snapshots": None, "quality_blocked_snapshots": None,
                       "quality_unknown_snapshots": None, "quality_state": "UNAVAILABLE"}
        if not self._available(connection, "data", {
                "data_sources": ("source_id", "owner_id"), "data_datasets": ("dataset_id", "owner_id"),
                "data_snapshots": ("snapshot_id", "owner_id", "body")}):
            return unavailable
        scope, args = self._scope(principal, "r")
        sources = connection.execute(f"SELECT count(DISTINCT r.source_id) FROM data_sources r WHERE {scope}", args).fetchone()[0]
        datasets = connection.execute(f"SELECT count(*) FROM data_datasets r WHERE {scope}", args).fetchone()[0]
        # json_type distinguishes true/false from numeric or string imitations.
        # Only the whitelisted quality flag is projected; records/connection data
        # never leave SQLite or enter the control-plane response.
        row = connection.execute(f"""SELECT count(*),
            coalesce(sum(CASE WHEN json_valid(r.body) THEN CASE WHEN json_type(r.body,'$.quality.allowed')='true' THEN 1 ELSE 0 END ELSE 0 END),0),
            coalesce(sum(CASE WHEN json_valid(r.body) THEN CASE WHEN json_type(r.body,'$.quality.allowed')='false' THEN 1 ELSE 0 END ELSE 0 END),0)
            FROM data_snapshots r WHERE {scope}""", args).fetchone()
        total, allowed, blocked = row
        unknown = total - allowed - blocked
        state = "BLOCKED" if blocked else "UNKNOWN" if unknown else "CLEAR" if total else "NO_SNAPSHOTS"
        return {"status": "AVAILABLE", "sources": sources, "datasets": datasets, "snapshots": total,
                "quality_allowed_snapshots": allowed, "quality_blocked_snapshots": blocked,
                "quality_unknown_snapshots": unknown, "quality_state": state}

    def _strategies(self, connection, principal):
        if not self._available(connection, "strategies", {"strategy_instances": ("owner_id", "status")}):
            return {"status": "UNAVAILABLE", "instances": None, "running": None, "by_status": None}
        scope, args = self._scope(principal, "r")
        counts = {r[0]: r[1] for r in connection.execute(f"SELECT r.status,count(*) FROM strategy_instances r WHERE {scope} GROUP BY r.status", args)}
        return {"status": "AVAILABLE", "instances": sum(counts.values()), "running": counts.get("RUNNING", 0), "by_status": counts}

    def _accounts(self, connection, principal):
        if not self._available(connection, "trading", {
                "trading_accounts": ("account_id", "owner_id", "mode", "frozen", "emergency"),
                "trading_intents": ("client_order_id", "account_id"), "trading_orders": ("client_order_id", "status")}):
            return {"status": "UNAVAILABLE", "total": None, "by_mode": None, "frozen": None,
                    "emergency_stopped": None, "unknown_orders": None, "sending_orders": None,
                    "accounts_with_uncertain_orders": None}
        scope, args = self._scope(principal, "a")
        predicate = scope + " AND a.mode IN ('LOCAL_SIMULATION','BROKER_PAPER')"
        by_mode = {"LOCAL_SIMULATION": 0, "BROKER_PAPER": 0}
        for row in connection.execute(f"SELECT a.mode,count(*) FROM trading_accounts a WHERE {predicate} GROUP BY a.mode", args):
            by_mode[row[0]] = row[1]
        counts = connection.execute(f"SELECT coalesce(sum(CASE WHEN a.frozen=1 THEN 1 ELSE 0 END),0),coalesce(sum(CASE WHEN a.emergency=1 THEN 1 ELSE 0 END),0) FROM trading_accounts a WHERE {predicate}", args).fetchone()
        uncertain = connection.execute(f"""SELECT
            coalesce(sum(CASE WHEN o.status='unknown' THEN 1 ELSE 0 END),0),
            coalesce(sum(CASE WHEN o.status='sending' THEN 1 ELSE 0 END),0),
            count(DISTINCT CASE WHEN o.status IN ('unknown','sending') THEN a.account_id END)
            FROM trading_accounts a JOIN trading_intents i ON i.account_id=a.account_id
            JOIN trading_orders o ON o.client_order_id=i.client_order_id WHERE {predicate}""", args).fetchone()
        return {"status": "AVAILABLE", "total": sum(by_mode.values()), "by_mode": by_mode,
                "frozen": counts[0], "emergency_stopped": counts[1], "unknown_orders": uncertain[0],
                "sending_orders": uncertain[1], "accounts_with_uncertain_orders": uncertain[2]}

    def summary(self, principal):
        with closing(self.runtime.database.connect()) as connection:
            # One WAL read transaction keeps the domain counts mutually consistent.
            connection.execute("BEGIN")
            return {"scope": "ALL_OWNERS" if principal.role == "admin" else "OWNED", "generated_at": now_text(),
                    "data": self._data(connection, principal), "strategies": self._strategies(connection, principal),
                    "accounts": self._accounts(connection, principal)}
