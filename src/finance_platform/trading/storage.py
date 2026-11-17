"""Trading module migrations on the single core platform Database."""
import json
from decimal import Decimal

from finance_platform.core.contracts import canonical
from .accounting import CashBook, LedgerEvent, TradingError

MIGRATIONS = ("""
CREATE TABLE trading_accounts (
 account_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, mode TEXT NOT NULL,
 initial_cash TEXT NOT NULL, fee_json TEXT NOT NULL, limits_json TEXT NOT NULL,
 emergency INTEGER NOT NULL DEFAULT 0, frozen INTEGER NOT NULL DEFAULT 0,
 freeze_reason TEXT, day TEXT, baseline TEXT, high TEXT, day_flows TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE trading_instruments (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),
 instrument_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(account_id,instrument_id)
);
CREATE TABLE trading_market (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),
 event_id TEXT NOT NULL, instrument_id TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(account_id,event_id)
);
CREATE TABLE trading_intents (
 client_order_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),
 body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE trading_orders (
 client_order_id TEXT PRIMARY KEY REFERENCES trading_intents(client_order_id),
 status TEXT NOT NULL, filled_qty TEXT NOT NULL DEFAULT '0', filled_notional TEXT NOT NULL DEFAULT '0',
 fees TEXT NOT NULL DEFAULT '0', broker_id TEXT, settlement_at TEXT
);
CREATE TABLE trading_ledger (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),
 event_id TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(account_id,event_id)
);
CREATE TABLE trading_order_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),
 event_id TEXT NOT NULL, client_order_id TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL,
 UNIQUE(account_id,event_id)
);
CREATE TABLE trading_outbox (
 action_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL REFERENCES trading_intents(client_order_id),
 action TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL, fencing INTEGER
);
CREATE TABLE trading_leases (
 account_id TEXT PRIMARY KEY REFERENCES trading_accounts(account_id), worker_id TEXT NOT NULL,
 fencing INTEGER NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE trading_reconciliations (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),
 at TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TRIGGER trading_intents_immutable_update BEFORE UPDATE ON trading_intents BEGIN SELECT RAISE(ABORT,'immutable intent'); END;
CREATE TRIGGER trading_intents_immutable_delete BEFORE DELETE ON trading_intents BEGIN SELECT RAISE(ABORT,'immutable intent'); END;
CREATE TRIGGER trading_ledger_immutable_update BEFORE UPDATE ON trading_ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TRIGGER trading_ledger_immutable_delete BEFORE DELETE ON trading_ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TRIGGER trading_events_immutable_update BEFORE UPDATE ON trading_order_events BEGIN SELECT RAISE(ABORT,'immutable broker event'); END;
CREATE TRIGGER trading_events_immutable_delete BEFORE DELETE ON trading_order_events BEGIN SELECT RAISE(ABORT,'immutable broker event'); END;
CREATE TRIGGER trading_market_immutable_update BEFORE UPDATE ON trading_market BEGIN SELECT RAISE(ABORT,'immutable market'); END;
CREATE TRIGGER trading_market_immutable_delete BEFORE DELETE ON trading_market BEGIN SELECT RAISE(ABORT,'immutable market'); END;
CREATE TRIGGER trading_instruments_immutable_update BEFORE UPDATE ON trading_instruments BEGIN SELECT RAISE(ABORT,'immutable instrument'); END;
CREATE TRIGGER trading_instruments_immutable_delete BEFORE DELETE ON trading_instruments BEGIN SELECT RAISE(ABORT,'immutable instrument'); END;
CREATE TRIGGER trading_reconcile_immutable_update BEFORE UPDATE ON trading_reconciliations BEGIN SELECT RAISE(ABORT,'immutable reconciliation'); END;
CREATE TRIGGER trading_reconcile_immutable_delete BEFORE DELETE ON trading_reconciliations BEGIN SELECT RAISE(ABORT,'immutable reconciliation'); END;
""", """
CREATE TABLE trading_plans (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id), request_id TEXT NOT NULL,
 body TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(account_id,request_id)
);
""", """
CREATE TABLE trading_paper_configs (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),
 body TEXT NOT NULL, at TEXT NOT NULL
);
""", """
CREATE TRIGGER trading_intents_rowid BEFORE INSERT ON trading_intents WHEN EXISTS(SELECT 1 FROM trading_intents WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_ledger_rowid BEFORE INSERT ON trading_ledger WHEN EXISTS(SELECT 1 FROM trading_ledger WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_events_rowid BEFORE INSERT ON trading_order_events WHEN EXISTS(SELECT 1 FROM trading_order_events WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_market_rowid BEFORE INSERT ON trading_market WHEN EXISTS(SELECT 1 FROM trading_market WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_instruments_rowid BEFORE INSERT ON trading_instruments WHEN EXISTS(SELECT 1 FROM trading_instruments WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_reconciliations_rowid BEFORE INSERT ON trading_reconciliations WHEN EXISTS(SELECT 1 FROM trading_reconciliations WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_plans_rowid BEFORE INSERT ON trading_plans WHEN EXISTS(SELECT 1 FROM trading_plans WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_paper_configs_rowid BEFORE INSERT ON trading_paper_configs WHEN EXISTS(SELECT 1 FROM trading_paper_configs WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
""", """
CREATE TABLE trading_pending_actions (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id), event_id TEXT NOT NULL,
 body TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(account_id,event_id)
);
CREATE TABLE trading_action_resolutions (
 account_id TEXT NOT NULL, event_id TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(account_id,event_id), FOREIGN KEY(account_id,event_id) REFERENCES trading_pending_actions(account_id,event_id)
);
CREATE TRIGGER trading_pending_actions_rowid BEFORE INSERT ON trading_pending_actions WHEN EXISTS(SELECT 1 FROM trading_pending_actions WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_action_resolutions_rowid BEFORE INSERT ON trading_action_resolutions WHEN EXISTS(SELECT 1 FROM trading_action_resolutions WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
""", """
CREATE TABLE trading_restore_barriers (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id), restore_id TEXT NOT NULL,
 client_order_id TEXT NOT NULL REFERENCES trading_intents(client_order_id), at TEXT NOT NULL,
 PRIMARY KEY(account_id,restore_id,client_order_id)
);
CREATE TABLE trading_restore_resolutions (
 account_id TEXT NOT NULL, restore_id TEXT NOT NULL, client_order_id TEXT NOT NULL, broker_id TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(account_id,restore_id,client_order_id),
 FOREIGN KEY(account_id,restore_id,client_order_id) REFERENCES trading_restore_barriers(account_id,restore_id,client_order_id)
);
CREATE TABLE trading_database_bindings (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TRIGGER trading_restore_barriers_rowid BEFORE INSERT ON trading_restore_barriers WHEN EXISTS(SELECT 1 FROM trading_restore_barriers WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_restore_resolutions_rowid BEFORE INSERT ON trading_restore_resolutions WHEN EXISTS(SELECT 1 FROM trading_restore_resolutions WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_database_bindings_rowid BEFORE INSERT ON trading_database_bindings WHEN EXISTS(SELECT 1 FROM trading_database_bindings WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
""", """
CREATE TABLE trading_replacements (
 old_order_id TEXT PRIMARY KEY REFERENCES trading_intents(client_order_id),
 new_order_id TEXT UNIQUE NOT NULL REFERENCES trading_intents(client_order_id), at TEXT NOT NULL
);
CREATE TRIGGER trading_replacements_rowid BEFORE INSERT ON trading_replacements WHEN EXISTS(SELECT 1 FROM trading_replacements WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_replacements_new_id BEFORE INSERT ON trading_replacements WHEN EXISTS(SELECT 1 FROM trading_replacements WHERE new_order_id=NEW.new_order_id) BEGIN SELECT RAISE(ABORT,'immutable replacement identity'); END;
""", """
CREATE TRIGGER trading_account_terms_immutable BEFORE UPDATE OF account_id,owner_id,mode,initial_cash,fee_json,limits_json,created_at ON trading_accounts BEGIN SELECT RAISE(ABORT,'immutable account terms'); END;
CREATE TRIGGER trading_account_delete_immutable BEFORE DELETE ON trading_accounts BEGIN SELECT RAISE(ABORT,'immutable account identity'); END;
""", """
CREATE TABLE trading_risk_versions (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id), version TEXT NOT NULL,
 body TEXT NOT NULL, content_hash TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(user_id), at TEXT NOT NULL,
 PRIMARY KEY(account_id,version)
);
CREATE TABLE trading_risk_active (
 account_id TEXT PRIMARY KEY REFERENCES trading_accounts(account_id), version TEXT NOT NULL,
 FOREIGN KEY(account_id,version) REFERENCES trading_risk_versions(account_id,version)
);
CREATE TABLE trading_intent_risk (
 client_order_id TEXT PRIMARY KEY REFERENCES trading_intents(client_order_id), account_id TEXT NOT NULL,
 version TEXT NOT NULL, content_hash TEXT NOT NULL,
 FOREIGN KEY(account_id,version) REFERENCES trading_risk_versions(account_id,version)
);
CREATE TABLE trading_run_risk (
 run_id TEXT NOT NULL REFERENCES runs(run_id), account_id TEXT NOT NULL,
 version TEXT NOT NULL, content_hash TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(run_id,account_id), FOREIGN KEY(account_id,version) REFERENCES trading_risk_versions(account_id,version)
);
CREATE TRIGGER trading_risk_versions_rowid BEFORE INSERT ON trading_risk_versions WHEN EXISTS(SELECT 1 FROM trading_risk_versions WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_intent_risk_rowid BEFORE INSERT ON trading_intent_risk WHEN EXISTS(SELECT 1 FROM trading_intent_risk WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_run_risk_rowid BEFORE INSERT ON trading_run_risk WHEN EXISTS(SELECT 1 FROM trading_run_risk WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
""", """
CREATE TABLE trading_settlement_barriers (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id), event_id TEXT NOT NULL,
 profile TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(account_id,event_id,profile)
);
CREATE TRIGGER trading_settlement_barriers_rowid BEFORE INSERT ON trading_settlement_barriers WHEN EXISTS(SELECT 1 FROM trading_settlement_barriers WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
""", """
CREATE TABLE trading_research_actions (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id), event_id TEXT NOT NULL,
 source_id TEXT NOT NULL, record_id TEXT NOT NULL, envelope_hash TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(account_id,event_id), UNIQUE(account_id,source_id,record_id)
);
CREATE TABLE trading_research_action_results (
 account_id TEXT NOT NULL, event_id TEXT NOT NULL, phase TEXT NOT NULL, body TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(account_id,event_id,phase), FOREIGN KEY(account_id,event_id) REFERENCES trading_research_actions(account_id,event_id)
);
CREATE TRIGGER trading_research_actions_rowid BEFORE INSERT ON trading_research_actions WHEN EXISTS(SELECT 1 FROM trading_research_actions WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_research_actions_identity BEFORE INSERT ON trading_research_actions WHEN EXISTS(SELECT 1 FROM trading_research_actions WHERE account_id=NEW.account_id AND source_id=NEW.source_id AND record_id=NEW.record_id) BEGIN SELECT RAISE(ABORT,'immutable action identity'); END;
CREATE TRIGGER trading_research_action_results_rowid BEFORE INSERT ON trading_research_action_results WHEN EXISTS(SELECT 1 FROM trading_research_action_results WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
""", """
CREATE TABLE trading_execution_steps (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL REFERENCES trading_accounts(account_id),
 operation_key TEXT NOT NULL, at TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(account_id,operation_key)
);
CREATE INDEX trading_execution_account ON trading_execution_steps(account_id,sequence);
CREATE TABLE trading_execution_barriers (
 account_id TEXT NOT NULL REFERENCES trading_accounts(account_id), version TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(account_id,version)
);
CREATE TRIGGER trading_execution_steps_rowid BEFORE INSERT ON trading_execution_steps WHEN EXISTS(SELECT 1 FROM trading_execution_steps WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_execution_barriers_rowid BEFORE INSERT ON trading_execution_barriers WHEN EXISTS(SELECT 1 FROM trading_execution_barriers WHERE rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable rowid'); END;
CREATE TRIGGER trading_execution_forward BEFORE INSERT ON trading_execution_steps WHEN NEW.at < (SELECT at FROM trading_execution_steps WHERE account_id=NEW.account_id ORDER BY sequence DESC LIMIT 1) BEGIN SELECT RAISE(ABORT,'execution time reversed'); END;
""")


def install(database):
    database.apply_module_migrations("trading", MIGRATIONS)
    from finance_platform.core.database import immutable
    with database.transaction() as con:
        for table, keys in (("trading_intents", ("client_order_id",)), ("trading_ledger", ("account_id", "event_id")),
                            ("trading_order_events", ("account_id", "event_id")), ("trading_market", ("account_id", "event_id")),
                            ("trading_instruments", ("account_id", "instrument_id")), ("trading_reconciliations", ("sequence",)),
                            ("trading_plans", ("account_id", "request_id"))):
            immutable(con, table, keys)
        immutable(con, "trading_paper_configs", ("sequence",))
        immutable(con, "trading_pending_actions", ("account_id", "event_id"))
        immutable(con, "trading_action_resolutions", ("account_id", "event_id"))
        immutable(con, "trading_restore_barriers", ("account_id", "restore_id", "client_order_id"))
        immutable(con, "trading_restore_resolutions", ("account_id", "restore_id", "client_order_id"))
        immutable(con, "trading_database_bindings", ("sequence",))
        immutable(con, "trading_replacements", ("old_order_id",))
        immutable(con, "trading_risk_versions", ("account_id", "version"))
        immutable(con, "trading_intent_risk", ("client_order_id",))
        immutable(con, "trading_run_risk", ("run_id", "account_id"))
        immutable(con, "trading_settlement_barriers", ("account_id", "event_id", "profile"))
        immutable(con, "trading_research_actions", ("account_id", "event_id"))
        immutable(con, "trading_research_action_results", ("account_id", "event_id", "phase"))
        immutable(con, "trading_execution_steps", ("account_id", "operation_key"))
        immutable(con, "trading_execution_barriers", ("account_id", "version"))


def unresolved_restore(con, account_id):
    return con.execute("SELECT 1 FROM trading_restore_barriers b LEFT JOIN trading_restore_resolutions r USING(account_id,restore_id,client_order_id) WHERE b.account_id=? AND r.client_order_id IS NULL LIMIT 1", (account_id,)).fetchone() is not None


def _mark_restored(con, restore_id, at):
    rows = con.execute("SELECT o.client_order_id,o.status,i.account_id FROM trading_orders o JOIN trading_intents i USING(client_order_id) JOIN trading_accounts a ON i.account_id=a.account_id WHERE a.mode='BROKER_PAPER' AND o.status NOT IN ('filled','cancelled','rejected','expired','replaced')").fetchall()
    for row in rows:
        if con.execute("SELECT 1 FROM trading_restore_barriers WHERE account_id=? AND restore_id=? AND client_order_id=?", (row["account_id"], restore_id, row["client_order_id"])).fetchone():
            continue
        con.execute("INSERT INTO trading_restore_barriers VALUES(?,?,?,?)", (row["account_id"], restore_id, row["client_order_id"], at))
        body = canonical({"event": "restore_unknown", "restore_id": restore_id, "prior_status": row["status"]})
        con.execute("INSERT INTO trading_order_events(account_id,event_id,client_order_id,body,at) VALUES(?,?,?,?,?)",
            (row["account_id"], "restore:" + restore_id + ":" + row["client_order_id"], row["client_order_id"], body, at))
        con.execute("UPDATE trading_orders SET status='unknown' WHERE client_order_id=?", (row["client_order_id"],))
        con.execute("UPDATE trading_outbox SET status='unknown' WHERE client_order_id=? AND status IN ('pending','sending')", (row["client_order_id"],))
    con.execute("UPDATE trading_accounts SET frozen=1,freeze_reason='RESTORED_PAPER_RECONCILIATION_REQUIRED' WHERE mode='BROKER_PAPER'")
    return len(rows)


def mark_restored_paper(database, restore_id, at=None):
    """Operations restore activation MUST call this, including same-path restoration."""
    from datetime import datetime, timezone
    from finance_platform.core.contracts import identifier, instant
    identifier(restore_id)
    install(database)
    at = instant(at or datetime.now(timezone.utc)).isoformat()
    with database.transaction() as con:
        if con.execute("SELECT 1 FROM trading_restore_resolutions WHERE restore_id=? LIMIT 1", (restore_id,)).fetchone():
            raise TradingError("RESTORE_ID_REUSE")
        return {"mode": "BROKER_PAPER", "unknown_intents": _mark_restored(con, restore_id, at), "restore_id": restore_id}


def bind_database(database):
    """Detect relocation of a snapshot. Same-path recovery uses mark_restored_paper."""
    from datetime import datetime, timezone
    import uuid
    with database.transaction() as con:
        at = datetime.now(timezone.utc).isoformat()
        path = str(database.path.resolve())
        old = con.execute("SELECT path FROM trading_database_bindings ORDER BY sequence DESC LIMIT 1").fetchone()
        if old is None or old["path"] != path:
            if old is not None:
                _mark_restored(con, "relocated-" + uuid.uuid4().hex, at)
            con.execute("INSERT INTO trading_database_bindings(path,at) VALUES(?,?)", (path, at))


def load_book(connection, account):
    book = CashBook(Decimal(account["initial_cash"]))
    for row in connection.execute("SELECT body FROM trading_ledger WHERE account_id=? ORDER BY sequence", (account["account_id"],)):
        book.apply(LedgerEvent(**json.loads(row["body"])))
    return book


def append_event(connection, account_id, event: LedgerEvent, at):
    old = connection.execute("SELECT body FROM trading_ledger WHERE account_id=? AND event_id=?",
                             (account_id, event.event_id)).fetchone()
    body = canonical(event.to_dict())
    if old:
        if old["body"] != body:
            raise TradingError("EVENT_ID_COLLISION")
        return False
    from .execution_clock import advance
    account = connection.execute("SELECT * FROM trading_accounts WHERE account_id=?", (account_id,)).fetchone()
    advance(connection, account, "ledger:" + event.event_id, at, event.to_dict())
    connection.execute("INSERT INTO trading_ledger(account_id,event_id,body,at) VALUES(?,?,?,?)",
                       (account_id, event.event_id, body, at))
    return True
