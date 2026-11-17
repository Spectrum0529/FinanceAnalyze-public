"""Operations integration: restored strategy sessions remain suspended until explicit action."""
from contextlib import closing
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import now_text

MIGRATIONS = ("""
CREATE TABLE strategy_restore_control (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 active INTEGER NOT NULL,restore_id TEXT NOT NULL,created_at TEXT NOT NULL,cleared_at TEXT);
CREATE TABLE strategy_restore_events (event_id INTEGER PRIMARY KEY,kind TEXT NOT NULL,
 restore_id TEXT NOT NULL,recorded_at TEXT NOT NULL);
""",)


def install_restore_schema(database):
    database.apply_module_migrations("strategy_restore", MIGRATIONS)
    with database.transaction() as con:
        immutable(con, "strategy_restore_events", ("event_id",))


def require_execution_enabled(database):
    with closing(database.connect()) as con:
        row = con.execute("SELECT active FROM strategy_restore_control WHERE singleton=1").fetchone()
    if row and row["active"]:
        raise PlatformError("STRATEGY_RESTORE_REQUIRES_EXPLICIT_RESUME", 409)


def suspend_for_restore(database, restore_id, *, connection=None):
    """Call install_restore_schema before opening an operations restore transaction.

    Pauses projections only; immutable acceptance/result/run/ledger evidence stays.
    Core jobs must also be cancelled by the operations restore barrier.
    """
    from finance_platform.core.contracts import identifier
    identifier(restore_id)
    def suspend(con):
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.execute("INSERT INTO strategy_restore_control VALUES(1,1,?,?,NULL) ON CONFLICT(singleton) DO UPDATE SET active=1,restore_id=excluded.restore_id,created_at=excluded.created_at,cleared_at=NULL", (restore_id, now_text()))
        con.execute("INSERT INTO strategy_restore_events(kind,restore_id,recorded_at) VALUES('SUSPENDED',?,?)", (restore_id, now_text()))
        if "strategy_instances" in tables:
            con.execute("UPDATE strategy_instances SET status='PAUSED',active_run_id=NULL,updated_at=? WHERE status='RUNNING'", (now_text(),))
        if "strategy_signal_bindings" in tables:
            con.execute("UPDATE strategy_signal_bindings SET status='SUSPENDED',generation=generation+1,updated_at=?", (now_text(),))
        if "strategy_signal_work" in tables:
            con.execute("UPDATE strategy_signal_work SET status='SUSPENDED',error_code='RESTORE_REQUIRES_EXPLICIT_RESUME',updated_at=? WHERE status IN ('STAGED','QUEUED','RUNNING')", (now_text(),))
        if "strategy_simulation_sessions" in tables:
            con.execute("UPDATE strategy_simulation_sessions SET status='SUSPENDED',generation=generation+1,updated_at=?", (now_text(),))
    if connection is not None:
        suspend(connection)
    else:
        with database.transaction() as con:
            suspend(con)


def resume_after_restore(runtime, principal, restore_id):
    if principal.role != "admin":
        raise PlatformError("FORBIDDEN", 403)
    with runtime.database.transaction() as con:
        row = con.execute("SELECT * FROM strategy_restore_control WHERE singleton=1").fetchone()
        if row is None or row["restore_id"] != restore_id or not row["active"]:
            raise PlatformError("CONFLICT", 409)
        # Clearing the global gate does not reactivate sessions or queue old messages.
        con.execute("UPDATE strategy_restore_control SET active=0,cleared_at=? WHERE singleton=1", (now_text(),))
        con.execute("INSERT INTO strategy_restore_events(kind,restore_id,recorded_at) VALUES('EXPLICITLY_CLEARED',?,?)", (restore_id, now_text()))
    runtime.audit(principal, "strategy.restore_cleared", restore_id)
    return {"restore_id": restore_id, "gate": "CLEARED", "sessions": "STILL_SUSPENDED_EXPLICIT_NEW_START_REQUIRED"}
