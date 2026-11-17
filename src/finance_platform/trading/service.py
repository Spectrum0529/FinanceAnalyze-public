"""One authenticated risk/OMS entry point; immutable ledger plus transactional projections."""
from __future__ import annotations

import json
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from finance_platform.core.contracts import (DataScope, Instrument, RunMode, canonical, fingerprint,
                                             identifier, instant, parse_instant, payload)
from .accounting import (CashBook, FeeSchedule, LedgerEvent, Position, TradingError,
                         decimal, money, ZERO, floor_step, PortfolioAllocator)
from .models import MarketEvent, OrderIntent
from .risk import RiskLimits, RiskService
from .storage import install, load_book, append_event, bind_database, unresolved_restore
from .risk_configuration import ensure_initial, policy
from .execution_clock import read_clock, require_forward, advance, seed

TERMINAL = {"filled", "cancelled", "rejected", "expired", "replaced"}


def stamp(at: datetime) -> str:
    return instant(at).isoformat()


def settlement_at(at: datetime) -> datetime:
    from .settlement import release_at
    return release_at(at)


def simulation_start(value):
    """Explicit model birth, never infer the user's timezone or account history."""
    if type(value) is str:
        if not value.endswith(("Z", "+00:00")):
            raise TradingError("SIMULATION_START_UTC_REQUIRED")
        return parse_instant(value)
    if type(value) is datetime and value.utcoffset() == timedelta(0):
        return instant(value)
    raise TradingError("SIMULATION_START_UTC_REQUIRED")


class TradingService:
    def __init__(self, database, *, authorize=None, data_authorizer=None):
        self.database = database
        self.authorize = authorize
        self.data_authorizer = data_authorizer
        install(database)
        bind_database(database)
        self._check_legacy_settlement()
        with self.database.transaction() as con:
            for account in con.execute("SELECT * FROM trading_accounts").fetchall():
                seed(con, account, legacy=True)

    def _check_legacy_settlement(self):
        """Preserve early legacy releases, but prevent their balances funding new risk."""
        with self.database.transaction() as con:
            for account in con.execute("SELECT * FROM trading_accounts").fetchall():
                fills = {}
                settlements = []
                for row in con.execute("SELECT body,at FROM trading_ledger WHERE account_id=? ORDER BY sequence", (account["account_id"],)):
                    event = json.loads(row["body"])
                    if event["kind"] == "fill" and event["side"] == "sell":
                        fills.setdefault(event["order_id"], []).append(self._trade_at(con, account["account_id"], event, row["at"]))
                    elif event["kind"] == "settle":
                        settlements.append((event, parse_instant(row["at"])))
                for event, released_at in settlements:
                    due = None
                    try:
                        due = max(settlement_at(at) for at in fills.get(event["order_id"], []) if at <= released_at)
                        if released_at >= due:
                            continue
                    except (TradingError, ValueError):
                        pass
                    from .settlement import PROFILE
                    if con.execute("SELECT 1 FROM trading_settlement_barriers WHERE account_id=? AND event_id=? AND profile=?", (account["account_id"], event["event_id"], PROFILE)).fetchone() is None:
                        con.execute("INSERT INTO trading_settlement_barriers VALUES(?,?,?,?,?)", (account["account_id"], event["event_id"], PROFILE,
                            canonical({"code": "LEGACY_SETTLEMENT_RECONCILIATION_REQUIRED", "released_at": stamp(released_at), "required_at": None if due is None else stamp(due)}), stamp(datetime.now(timezone.utc))))
                    self._freeze(con, account["account_id"], "LEGACY_SETTLEMENT_RECONCILIATION_REQUIRED")
                    break

    def _account(self, con, account_id, principal=None, write=False):
        row = con.execute("SELECT * FROM trading_accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            raise TradingError("ACCOUNT_NOT_FOUND")
        if principal is not None:
            if self.authorize:
                self.authorize(principal, row["owner_id"], write=write)
            elif getattr(principal, "role", None) == "viewer" and write or (
                getattr(principal, "role", None) != "admin" and getattr(principal, "user_id", None) != row["owner_id"]):
                raise TradingError("ACCOUNT_FORBIDDEN")
        else:
            raise TradingError("PRINCIPAL_REQUIRED")
        return row

    def create_account(self, principal, account_id, initial_cash, *, mode=RunMode.LOCAL_SIMULATION,
                       fees=FeeSchedule(), limits=RiskLimits(), now=None, currency="USD",
                       account_type="cash", leverage="1", allow_short=False, simulation_started_at=None):
        identifier(account_id)
        if principal is None or getattr(principal, "role", None) not in {"admin", "researcher"}:
            raise TradingError("ACCOUNT_FORBIDDEN")
        if type(mode) is not RunMode or mode not in {RunMode.LOCAL_SIMULATION, RunMode.BROKER_PAPER}:
            raise TradingError("TRADING_MODE_DISABLED")
        if currency != "USD" or account_type != "cash" or decimal(leverage) != 1 or allow_short is not False:
            raise TradingError("UNSUPPORTED_ACCOUNT_CAPABILITY")
        initial_cash = money(initial_cash)
        if initial_cash <= ZERO:
            raise TradingError("INVALID_INITIAL_CASH")
        clock_source = "TRUSTED_NOW_ARGUMENT" if now is not None else "SERVICE_CURRENT_UTC"
        now = instant(now or datetime.now(timezone.utc))
        initial_at = now
        if simulation_started_at is not None:
            if mode is not RunMode.LOCAL_SIMULATION:
                raise TradingError("SIMULATION_START_MODE_DISABLED")
            initial_at = simulation_start(simulation_started_at)
            if initial_at > now:
                raise TradingError("SIMULATION_START_IN_FUTURE")
            clock_source = "EXPLICIT_UTC_SIMULATION_STARTED_AT"
        initialization = {"mode": mode.value, "initial_cash": str(initial_cash), "account_initial_at": stamp(initial_at),
                          "simulation_started_at": stamp(initial_at) if mode is RunMode.LOCAL_SIMULATION else None,
                          "initial_holdings": [], "clock_source": clock_source,
                          "assumption": "SIMULATED_INITIAL_CASH_EMPTY_HOLDINGS" if mode is RunMode.LOCAL_SIMULATION else "PAPER_INITIAL_CASH_REQUIRES_RECONCILIATION"}
        with self.database.transaction() as con:
            if con.execute("SELECT 1 FROM trading_accounts WHERE account_id=?", (account_id,)).fetchone() is not None:
                raise TradingError("ACCOUNT_ALREADY_EXISTS")
            con.execute("INSERT INTO trading_accounts(account_id,owner_id,mode,initial_cash,fee_json,limits_json,frozen,freeze_reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (account_id, principal.user_id, mode.value, str(initial_cash), canonical(fees), canonical(limits),
                         int(mode is RunMode.BROKER_PAPER), "INITIAL_RECONCILIATION" if mode is RunMode.BROKER_PAPER else None, stamp(initial_at)))
            self.database.audit(principal.user_id, "trading.account_created", account_id, initialization, connection=con)
            ensure_initial(con, self._account(con, account_id, principal, True))
            seed(con, self._account(con, account_id, principal, True))
        return self.snapshot(principal, account_id, initial_at)

    @staticmethod
    def _require_started(account, at):
        if account["mode"] == RunMode.LOCAL_SIMULATION.value and instant(at) < parse_instant(account["created_at"]):
            raise TradingError("ACCOUNT_TIMELINE_NOT_STARTED")

    def add_instrument(self, principal, account_id, instrument: Instrument):
        if type(instrument) is not Instrument:
            raise TradingError("INVALID_INSTRUMENT")
        with self.database.transaction() as con:
            self._account(con, account_id, principal, True)
            old = con.execute("SELECT body FROM trading_instruments WHERE account_id=? AND instrument_id=?",
                              (account_id, instrument.instrument_id)).fetchone()
            body = canonical(instrument)
            if old:
                if old["body"] != body:
                    raise TradingError("INSTRUMENT_VERSION_CONFLICT")
                return
            con.execute("INSERT INTO trading_instruments VALUES(?,?,?)", (account_id, instrument.instrument_id, body))
            self.database.audit(principal.user_id, "trading.instrument_registered", account_id, {"instrument_id": instrument.instrument_id}, connection=con)

    def execution_clock(self, principal, account_id):
        with closing(self.database.connect()) as con:
            return read_clock(con, self._account(con, account_id, principal))

    @staticmethod
    def _existing_event(con, account_id, event):
        old = con.execute("SELECT body FROM trading_ledger WHERE account_id=? AND event_id=?", (account_id, event.event_id)).fetchone()
        if old and old["body"] != canonical(event.to_dict()):
            raise TradingError("EVENT_ID_COLLISION")
        return old is not None

    def preflight_execution(self, principal, account_id, now, *, intent=None, market=None, event=None):
        if sum(x is not None for x in (intent, market, event)) > 1:
            raise TradingError("EXECUTION_PREFLIGHT_PAYLOAD_INVALID")
        with closing(self.database.connect()) as con:
            account = self._account(con, account_id, principal)
            retry = False
            if intent is not None:
                if type(intent) is not OrderIntent or intent.account_id != account_id:
                    raise TradingError("EXECUTION_PREFLIGHT_PAYLOAD_INVALID")
                old = con.execute("SELECT body FROM trading_intents WHERE client_order_id=?", (intent.client_order_id,)).fetchone()
                if old and old["body"] != canonical(intent):
                    raise TradingError("IDEMPOTENCY_CONFLICT")
                retry = old is not None
            elif market is not None:
                if type(market) is not MarketEvent:
                    raise TradingError("EXECUTION_PREFLIGHT_PAYLOAD_INVALID")
                old = con.execute("SELECT body FROM trading_market WHERE account_id=? AND event_id=?", (account_id, market.event_id)).fetchone()
                if old and old["body"] != canonical(market):
                    raise TradingError("MARKET_EVENT_ID_COLLISION")
                retry = old is not None
            elif event is not None:
                if type(event) is not LedgerEvent:
                    raise TradingError("EXECUTION_PREFLIGHT_PAYLOAD_INVALID")
                retry = self._existing_event(con, account_id, event)
            clock = read_clock(con, account) if retry else require_forward(con, account, now)
            if not retry:
                self._require_started(account, instant(now))
            return clock | {"allowed": True, "retry": retry}

    def _instrument(self, con, account_id, instrument_id):
        row = con.execute("SELECT body FROM trading_instruments WHERE account_id=? AND instrument_id=?", (account_id, instrument_id)).fetchone()
        if row is None:
            raise TradingError("INSTRUMENT_NOT_FOUND")
        return Instrument.from_mapping(json.loads(row["body"]))

    def _markets(self, con, account_id, now):
        boundaries = {}
        for row in con.execute("SELECT a.body,r.at FROM trading_research_actions a JOIN trading_research_action_results r USING(account_id,event_id) WHERE a.account_id=? AND r.phase='APPLIED'", (account_id,)):
            action = json.loads(row["body"])
            if action["values"]["type"] == "SPLIT" and parse_instant(row["at"]) <= now:
                boundary = parse_instant(action["metadata"]["effective_at"])
                inst = action["instrument_id"]
                boundaries[inst] = max(boundaries.get(inst, boundary), boundary)
        events = {}
        for row in con.execute("SELECT body FROM trading_market WHERE account_id=? ORDER BY sequence", (account_id,)):
            event = MarketEvent.from_dict(json.loads(row["body"]))
            if event.instrument_id in boundaries and event.event_at < boundaries[event.instrument_id]:
                continue
            if event.available_at <= now and (event.instrument_id not in events or
                                             event.event_at >= events[event.instrument_id].event_at):
                events[event.instrument_id] = event
        return events

    def snapshot(self, principal, account_id, now=None):
        now = instant(now or datetime.now(timezone.utc))
        with closing(self.database.connect()) as con:
            account = self._account(con, account_id, principal)
            self._require_started(account, now)
            book = load_book(con, account)
            events = self._markets(con, account_id, now)
            result = book.snapshot({i: e.price for i, e in events.items()})
            result.update({"account_id": account_id, "mode": account["mode"], "currency": "USD", "settlement": "T+1",
                           "emergency_stop": bool(account["emergency"]), "frozen": bool(account["frozen"]),
                           "freeze_reason": account["freeze_reason"]})
            result["account_initial_at"] = account["created_at"]
            result["simulation_started_at"] = account["created_at"] if account["mode"] == RunMode.LOCAL_SIMULATION.value else None
            audit = con.execute("SELECT recorded_at,details_json FROM audit_events WHERE action='trading.account_created' AND resource_id=? ORDER BY event_id LIMIT 1", (account_id,)).fetchone()
            result["initialization"] = None if audit is None else {"recorded_at": audit["recorded_at"], **json.loads(audit["details_json"])}
            from .settlement import declaration
            result["settlement_policy"] = declaration()
            result["research_action_pending"] = self._research_action_blocked(con, account_id)
            result["execution_clock"] = read_clock(con, account)
            if result["execution_clock"]["blocked"]:
                result["frozen"], result["equity"] = True, None
                result["freeze_reason"] = "ACCOUNT_EXECUTION_HISTORY_INVALID"
            if result["research_action_pending"]:
                result["frozen"], result["equity"] = True, None
            result["settlement_policy"]["balance_verification"] = "LEGACY_RECONCILIATION_REQUIRED" if self._settlement_blocked(con, account_id) else "LOCAL_LEDGER_ONLY"
            if account["mode"] == RunMode.BROKER_PAPER.value:
                result["settlement_policy"]["release_status"] = "WAITING_BROKER_SETTLED_CASH_EVIDENCE"
            result["orders"] = [dict(row) | {"intent": json.loads(row["body"])} for row in con.execute(
                "SELECT o.*,i.body FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? ORDER BY i.created_at", (account_id,))]
            for order in result["orders"]:
                order.pop("body")
            result["pending_actions"] = [{"event": json.loads(r["body"]), "at": r["at"]} for r in con.execute(
                "SELECT p.body,p.at FROM trading_pending_actions p LEFT JOIN trading_action_resolutions r USING(account_id,event_id) WHERE p.account_id=? AND r.event_id IS NULL ORDER BY p.at", (account_id,))]
            _, result["risk"] = policy(con, account)
            for order in result["orders"]:
                bound = con.execute("SELECT version,content_hash FROM trading_intent_risk WHERE client_order_id=?", (order["client_order_id"],)).fetchone()
                order["risk"] = dict(bound) if bound else {"version": "initial", "binding": "LEGACY_INITIAL_ACCOUNT_TERMS"}
            return result

    def list_accounts(self, principal, limit=50, offset=0):
        self._paging(limit, offset)
        with closing(self.database.connect()) as con:
            where, args = ("", ()) if principal.role == "admin" else ("WHERE owner_id=?", (principal.user_id,))
            total = con.execute("SELECT count(*) FROM trading_accounts " + where, args).fetchone()[0]
            rows = con.execute("SELECT account_id,mode,emergency,frozen,freeze_reason FROM trading_accounts " + where + " ORDER BY created_at LIMIT ? OFFSET ?", args + (limit, offset)).fetchall()
            return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _paging(limit, offset):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= 2_147_483_647:
            raise TradingError("INVALID_PAGINATION")

    def list_ledger(self, principal, account_id, *, fills_only=False, limit=50, offset=0):
        self._paging(limit, offset)
        with closing(self.database.connect()) as con:
            self._account(con, account_id, principal)
            condition = " AND json_extract(body,'$.kind')='fill'" if fills_only else ""
            total = con.execute("SELECT count(*) FROM trading_ledger WHERE account_id=?" + condition, (account_id,)).fetchone()[0]
            rows = con.execute("SELECT sequence,event_id,body,at FROM trading_ledger WHERE account_id=?" + condition + " ORDER BY sequence DESC LIMIT ? OFFSET ?", (account_id, limit, offset)).fetchall()
            items = []
            for row in rows:
                event = json.loads(row["body"])
                bound = con.execute("SELECT version,content_hash FROM trading_intent_risk WHERE client_order_id=?", (event["order_id"],)).fetchone()
                items.append({"sequence": row["sequence"], "recorded_at": row["at"], **event,
                              "risk": dict(bound) if bound else None})
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def list_orders(self, principal, account_id, limit=50, offset=0):
        self._paging(limit, offset)
        with closing(self.database.connect()) as con:
            self._account(con, account_id, principal)
            total = con.execute("SELECT count(*) FROM trading_intents WHERE account_id=?", (account_id,)).fetchone()[0]
            rows = con.execute("SELECT o.*,i.body,i.created_at FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? ORDER BY i.created_at DESC,i.client_order_id LIMIT ? OFFSET ?", (account_id, limit, offset)).fetchall()
            items = []
            for row in rows:
                value = dict(row)
                value["intent"] = json.loads(value.pop("body"))
                bound = con.execute("SELECT version,content_hash FROM trading_intent_risk WHERE client_order_id=?", (row["client_order_id"],)).fetchone()
                value["risk"] = dict(bound) if bound else {"version": "initial", "binding": "LEGACY_INITIAL_ACCOUNT_TERMS"}
                items.append(value)
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def risk_configuration(self, principal, account_id):
        with closing(self.database.connect()) as con:
            account = self._account(con, account_id, principal)
            limits, ref = policy(con, account)
            return {"account_id": account_id, **ref, "limits": payload(limits)}

    def risk_versions(self, principal, account_id, limit=50, offset=0):
        self._paging(limit, offset)
        with closing(self.database.connect()) as con:
            self._account(con, account_id, principal)
            total = con.execute("SELECT count(*) FROM trading_risk_versions WHERE account_id=?", (account_id,)).fetchone()[0]
            rows = con.execute("SELECT version,content_hash,body,actor_id,at FROM trading_risk_versions WHERE account_id=? ORDER BY at DESC,version DESC LIMIT ? OFFSET ?", (account_id, limit, offset)).fetchall()
            return {"items": [{"version": r["version"], "content_hash": r["content_hash"], "limits": json.loads(r["body"]), "actor_id": r["actor_id"], "created_at": r["at"]} for r in rows], "total": total, "limit": limit, "offset": offset}

    def update_risk(self, principal, account_id, version, expected_version, limits: RiskLimits):
        identifier(version)
        identifier(expected_version)
        if type(limits) is not RiskLimits:
            raise TradingError("INVALID_RISK_LIMIT")
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            ensure_initial(con, account)
            _, current = policy(con, account)
            existing = con.execute("SELECT body FROM trading_risk_versions WHERE account_id=? AND version=?", (account_id, version)).fetchone()
            body, sha = canonical(limits), fingerprint(limits)
            if existing:
                if existing["body"] != body:
                    raise TradingError("RISK_VERSION_CONFLICT")
                if current["version"] == version:
                    return {"account_id": account_id, "version": version, "content_hash": sha, "limits": payload(limits)}
                raise TradingError("RISK_VERSION_REACTIVATION_DISABLED")
            if current["version"] != expected_version:
                raise TradingError("RISK_VERSION_CONFLICT")
            con.execute("INSERT INTO trading_risk_versions VALUES(?,?,?,?,?,?)", (account_id, version, body, sha, principal.user_id, stamp(datetime.now(timezone.utc))))
            con.execute("UPDATE trading_risk_active SET version=? WHERE account_id=?", (version, account_id))
            self.database.audit(principal.user_id, "trading.risk_version_updated", account_id, {"version": version, "content_hash": sha, "previous_version": expected_version}, connection=con)
            return {"account_id": account_id, "version": version, "content_hash": sha, "limits": payload(limits)}

    def bind_run_risk(self, principal, account_id, run_id, version=None):
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            ensure_initial(con, account)
            run = con.execute("SELECT owner_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["owner_id"] != principal.user_id:
                raise TradingError("RUN_RISK_OWNER_MISMATCH")
            _, ref = policy(con, account, version)
            existing = con.execute("SELECT version,content_hash FROM trading_run_risk WHERE run_id=? AND account_id=?", (run_id, account_id)).fetchone()
            if existing:
                if dict(existing) != ref:
                    raise TradingError("RUN_RISK_VERSION_CONFLICT")
                return ref
            con.execute("INSERT INTO trading_run_risk VALUES(?,?,?,?,?)", (run_id, account_id, ref["version"], ref["content_hash"], stamp(datetime.now(timezone.utc))))
            return ref

    def reconcile_local(self, principal, account_id, now=None):
        now = instant(now or datetime.now(timezone.utc))
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            if account["mode"] != RunMode.LOCAL_SIMULATION.value:
                raise TradingError("PAPER_RECONCILIATION_USES_BROKER_WORKER")
            book = load_book(con, account)
            if not read_clock(con, account)["blocked"]:
                self._settle(con, account, book, now)
            differences, totals = [], {}
            for event in book.events.values():
                if event.kind == "fill":
                    qty, gross, fees = totals.get(event.order_id, (ZERO, ZERO, ZERO))
                    totals[event.order_id] = qty + event.quantity, gross + money(event.quantity * event.price), fees + money(event.fee)
            for row in con.execute("SELECT o.* FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=?", (account_id,)):
                expected = totals.get(row["client_order_id"], (ZERO, ZERO, ZERO))
                if expected != (decimal(row["filled_qty"]), decimal(row["filled_notional"]), decimal(row["fees"])):
                    differences.append("order_projection:" + row["client_order_id"])
                if row["status"] in TERMINAL and (row["client_order_id"] in book.reservations or row["client_order_id"] in book.sell_reservations):
                    differences.append("terminal_reservation:" + row["client_order_id"])
            for inst, position in book.positions.items():
                contributions = [p for (s, i), p in book.contributions.items() if i == inst]
                if position.quantity != sum((p.quantity for p in contributions), ZERO) or position.cost != sum((p.cost for p in contributions), ZERO):
                    differences.append("strategy_contributions:" + inst)
            if book.available < ZERO:
                differences.append("available_cash")
            pending = con.execute("SELECT 1 FROM trading_pending_actions p LEFT JOIN trading_action_resolutions r USING(account_id,event_id) WHERE p.account_id=? AND r.event_id IS NULL", (account_id,)).fetchone()
            blocks = ["pending_corporate_action"] if pending else []
            if self._settlement_blocked(con, account_id):
                blocks.append("legacy_settlement_evidence")
            if self._research_action_blocked(con, account_id):
                blocks.append("research_action_pending")
            if read_clock(con, account)["blocked"]:
                blocks.append("invalid_execution_history")
            if book.pending_terminal or account["freeze_reason"] == "CASH_IN_LIEU_PENDING":
                blocks.append("unresolved_economic_event")
            report = {"mode": "LOCAL_SIMULATION", "clean": not differences and not blocks,
                      "differences": differences, "operational_blocks": blocks, "basis": "IMMUTABLE_LEDGER_VS_ORDER_PROJECTIONS_AND_CONTRIBUTIONS"}
            con.execute("INSERT INTO trading_reconciliations(account_id,at,body) VALUES(?,?,?)", (account_id, stamp(now), canonical(report)))
            if differences:
                self._freeze(con, account_id, "LOCAL_RECONCILIATION_DIFFERENCE")
            elif not blocks and account["freeze_reason"] in {"LOCAL_RECONCILIATION_DIFFERENCE", "RESTORE_RECONCILIATION_REQUIRED"}:
                con.execute("UPDATE trading_accounts SET frozen=0,freeze_reason=NULL WHERE account_id=?", (account_id,))
            self.database.audit(principal.user_id, "trading.local_reconciled", account_id, {"clean": report["clean"]}, connection=con)
            return report

    @staticmethod
    def _trade_at(con, account_id, event, recorded_at):
        prefix = "sim-fill:" + event["order_id"] + ":"
        if event["event_id"].startswith(prefix):
            market = con.execute("SELECT body FROM trading_market WHERE account_id=? AND event_id=?", (account_id, event["event_id"][len(prefix):])).fetchone()
            if market:
                return parse_instant(json.loads(market["body"])["event_at"])
        return parse_instant(recorded_at)

    def _settle(self, con, account, book, now):
        if account["mode"] != RunMode.LOCAL_SIMULATION.value:
            # Alpaca cash/buying_power is not verified settled cash. A calendar
            # projection alone never makes broker-paper sale proceeds spendable.
            return
        # Recompute from immutable sale evidence, never trust a pre-upgrade mutable
        # settlement_at projection. Missing/uncovered evidence cannot release funds.
        for order_id in list(book.unsettled):
            times = [self._trade_at(con, account["account_id"], json.loads(r["body"]), r["at"]) for r in con.execute("SELECT at,body FROM trading_ledger WHERE account_id=? AND json_extract(body,'$.kind')='fill' AND json_extract(body,'$.side')='sell' AND json_extract(body,'$.order_id')=?", (account["account_id"], order_id))]
            try:
                due = max(settlement_at(at) for at in times)
            except (TradingError, ValueError):
                continue
            if due <= now:
                from .settlement import PROFILE
                event = LedgerEvent("settle:" + order_id + ":" + stamp(due), "settle", order_id=order_id, source=PROFILE)
                if book.apply(event):
                    append_event(con, account["account_id"], event, stamp(now))

    @staticmethod
    def _settlement_blocked(con, account_id):
        return con.execute("SELECT 1 FROM trading_settlement_barriers WHERE account_id=? LIMIT 1", (account_id,)).fetchone() is not None

    @staticmethod
    def _research_action_blocked(con, account_id):
        return con.execute("SELECT 1 FROM trading_research_actions a WHERE a.account_id=? AND (EXISTS(SELECT 1 FROM trading_research_action_results r WHERE r.account_id=a.account_id AND r.event_id=a.event_id AND r.phase='BLOCKED') OR NOT EXISTS(SELECT 1 FROM trading_research_action_results r WHERE r.account_id=a.account_id AND r.event_id=a.event_id AND r.phase='APPLIED')) LIMIT 1", (account_id,)).fetchone() is not None

    def record_market(self, principal, account_id, event: MarketEvent, now=None):
        """Paper marks only; local fills must use SimulationBroker.on_market."""
        now = instant(now or datetime.now(timezone.utc))
        if type(event) is not MarketEvent or event.available_at > now:
            raise TradingError("MARKET_EVENT_NOT_AVAILABLE")
        if event.provenance.scope is DataScope.DEVELOPMENT and (
            self.data_authorizer is None or not self.data_authorizer(event)):
            raise TradingError("DEVELOPMENT_DATA_PERMISSION_REQUIRED")
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            if account["mode"] != RunMode.BROKER_PAPER.value:
                raise TradingError("PAPER_MARK_MODE_REQUIRED")
            self._instrument(con, account_id, event.instrument_id)
            old = con.execute("SELECT body FROM trading_market WHERE account_id=? AND event_id=?", (account_id, event.event_id)).fetchone()
            if old:
                if old["body"] != canonical(event):
                    raise TradingError("MARKET_EVENT_ID_COLLISION")
                return
            con.execute("INSERT INTO trading_market(account_id,event_id,instrument_id,body) VALUES(?,?,?,?)", (account_id, event.event_id, event.instrument_id, canonical(event)))
            book = load_book(con, account)
            equity = book.snapshot({i: e.price for i, e in self._markets(con, account_id, now).items()})["equity"]
            if equity is not None:
                limits = policy(con, account)[0]
                day = now.astimezone(ZoneInfo(limits.reset_timezone)).date().isoformat()
                flows = decimal(account["day_flows"]) if account["day"] == day else book.net_flows
                baseline = decimal(account["baseline"]) if account["day"] == day else decimal(equity)
                high = max(decimal(account["high"]), decimal(equity) - book.net_flows + flows) if account["day"] == day else decimal(equity)
                con.execute("UPDATE trading_accounts SET day=?,baseline=?,high=?,day_flows=? WHERE account_id=?", (day, str(baseline), str(high), str(flows), account_id))

    def _submit(self, con, principal, intent, now, risk_version=None):
        account = self._account(con, intent.account_id, principal, True)
        old = con.execute("SELECT body FROM trading_intents WHERE client_order_id=?", (intent.client_order_id,)).fetchone()
        if old:
            if old["body"] != canonical(intent):
                raise TradingError("IDEMPOTENCY_CONFLICT")
            return intent.client_order_id
        self._require_started(account, now)
        self._require_started(account, intent.effective_at)
        require_forward(con, account, now)
        if account["mode"] != intent.mode.value:
            raise TradingError("ACCOUNT_MODE_MISMATCH")
        if self._settlement_blocked(con, intent.account_id):
            raise TradingError("LEGACY_SETTLEMENT_RECONCILIATION_REQUIRED")
        if self._research_action_blocked(con, intent.account_id):
            raise TradingError("RESEARCH_ACTION_PENDING")
        if unresolved_restore(con, intent.account_id):
            raise TradingError("RESTORE_RECONCILIATION_REQUIRED")
        if con.execute("SELECT 1 FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? AND o.status IN ('unknown','sending') LIMIT 1", (intent.account_id,)).fetchone():
            raise TradingError("ACCOUNT_RECONCILIATION_REQUIRED")
        if intent.mode is RunMode.BROKER_PAPER and len(intent.client_order_id) > 48:
            raise TradingError("BROKER_CLIENT_ID_LENGTH")
        if intent.mode is RunMode.BROKER_PAPER:
            if intent.quantity.as_tuple().exponent < -9:
                raise TradingError("BROKER_QUANTITY_PRECISION")
            if intent.limit_price is not None and intent.limit_price % (decimal("0.01") if intent.limit_price >= 1 else decimal("0.0001")):
                raise TradingError("BROKER_PRICE_PRECISION")
        # Both buy and sell submissions need a covered settlement date, including
        # the end-of-year rollover. No future-year holiday extrapolation.
        settlement_at(now)
        book = load_book(con, account)
        self._settle(con, account, book, now)
        markets = self._markets(con, intent.account_id, now)
        if intent.instrument_id not in markets:
            raise TradingError("MARKET_DATA_REQUIRED")
        prices = {i: e.price for i, e in markets.items()}
        ensure_initial(con, account)
        current_limits, current_ref = policy(con, account)
        limits, risk_ref = policy(con, account, risk_version)
        if any(p.quantity and (i not in markets or (now - markets[i].event_at).total_seconds() > limits.max_data_age_seconds)
               for i, p in book.positions.items()):
            raise TradingError("STALE_PORTFOLIO_MARK")
        fees = FeeSchedule(**json.loads(account["fee_json"]))
        day = now.astimezone(ZoneInfo(limits.reset_timezone)).date().isoformat()
        equity = book.snapshot(prices)["equity"]
        if equity is None:
            raise TradingError("VALUATION_UNKNOWN")
        equity = decimal(equity)
        baseline = decimal(account["baseline"]) if account["day"] == day else equity
        flows = decimal(account["day_flows"]) if account["day"] == day else book.net_flows
        high = max(decimal(account["high"]), equity - book.net_flows + flows) if account["day"] == day else equity
        count = con.execute("SELECT count(*) FROM trading_intents WHERE account_id=? AND created_at>=?", (intent.account_id, stamp(now - timedelta(minutes=1)))).fetchone()[0]
        reservation = RiskService(limits, fees).check(intent, self._instrument(con, intent.account_id, intent.instrument_id),
            markets[intent.instrument_id], book, prices, now, accepted_last_minute=count,
            emergency_stop=bool(account["emergency"]), frozen=bool(account["frozen"]),
            day_baseline=baseline, day_high=high, day_flows=flows)
        if risk_ref != current_ref:
            reservation = max(reservation, RiskService(current_limits, fees).check(intent, self._instrument(con, intent.account_id, intent.instrument_id),
                markets[intent.instrument_id], book, prices, now, accepted_last_minute=count,
                emergency_stop=bool(account["emergency"]), frozen=bool(account["frozen"]),
                day_baseline=baseline, day_high=high, day_flows=flows))
        con.execute("UPDATE trading_accounts SET day=?,baseline=?,high=?,day_flows=? WHERE account_id=?", (day, str(baseline), str(high), str(flows), intent.account_id))
        con.execute("INSERT INTO trading_intents VALUES(?,?,?,?)", (intent.client_order_id, intent.account_id, canonical(intent), stamp(now)))
        con.execute("INSERT INTO trading_orders(client_order_id,status) VALUES(?,?)", (intent.client_order_id, "queued"))
        con.execute("INSERT INTO trading_intent_risk VALUES(?,?,?,?)", (intent.client_order_id, intent.account_id, risk_ref["version"], risk_ref["content_hash"]))
        event = LedgerEvent("reserve:" + intent.client_order_id, "reserve" if intent.side == "buy" else "reserve_sell",
                            instrument_id=intent.instrument_id, strategy_id=intent.strategy_id, quantity=intent.quantity, amount=reservation,
                            order_id=intent.client_order_id, source="risk:v1")
        book.apply(event)
        append_event(con, intent.account_id, event, stamp(now))
        con.execute("INSERT INTO trading_outbox VALUES(?,?,?,?,?,NULL)", ("submit:" + intent.client_order_id, intent.client_order_id, "submit", "pending", stamp(now)))
        self.database.audit(principal.user_id, "trading.order_intent", intent.account_id,
                            {"order_id": intent.client_order_id, "source": intent.source, "mode": intent.mode.value}, connection=con)
        return intent.client_order_id

    def submit(self, principal, intent: OrderIntent, now=None, *, risk_version=None):
        if type(intent) is not OrderIntent:
            raise TradingError("INVALID_ORDER")
        try:
            with self.database.transaction() as con:
                return self._submit(con, principal, intent, instant(now or datetime.now(timezone.utc)), risk_version)
        except TradingError as exc:
            if principal is not None:
                self.database.audit(principal.user_id, "trading.order_rejected", intent.account_id, {"code": exc.code})
            raise

    def emergency_stop(self, principal, account_id, enabled=True):
        if type(enabled) is not bool:
            raise TradingError("BOOLEAN_REQUIRED")
        with self.database.transaction() as con:
            self._account(con, account_id, principal, True)
            con.execute("UPDATE trading_accounts SET emergency=? WHERE account_id=?", (int(enabled), account_id))
            self.database.audit(principal.user_id, "trading.emergency_stop", account_id, {"enabled": enabled}, connection=con)

    def cash_event(self, principal, account_id, event_id, amount, now=None):
        now = instant(now or datetime.now(timezone.utc))
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            event = LedgerEvent(event_id, "cash", amount=decimal(amount), source="local:explicit_cash")
            if self._existing_event(con, account_id, event):
                return
            self._require_started(account, now)
            require_forward(con, account, now)
            if account["mode"] != RunMode.LOCAL_SIMULATION.value:
                raise TradingError("PAPER_CASH_MANUAL_DISABLED")
            book = load_book(con, account)
            book.apply(event)
            append_event(con, account_id, event, stamp(now))
            self.database.audit(principal.user_id, "trading.cash_event", account_id, {"event_id": event_id}, connection=con)

    def cancel(self, principal, account_id, client_order_id, now=None):
        now = instant(now or datetime.now(timezone.utc))
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            row = self._order(con, account_id, client_order_id)
            if row["status"] in TERMINAL:
                return row["status"]
            require_forward(con, account, now)
            if row["status"] in {"unknown", "sending"}:
                raise TradingError("ORDER_RECONCILIATION_REQUIRED")
            if row["status"] == "queued" or account["mode"] == RunMode.LOCAL_SIMULATION.value:
                con.execute("UPDATE trading_outbox SET status='cancelled' WHERE client_order_id=? AND status='pending'", (client_order_id,))
                self._terminal(con, account_id, client_order_id, "cancelled", now)
            else:
                action = "cancel:" + client_order_id
                existing = con.execute("SELECT status FROM trading_outbox WHERE action_id=?", (action,)).fetchone()
                active = con.execute("SELECT 1 FROM trading_outbox WHERE client_order_id=? AND action='cancel' AND status IN ('pending','sending')", (client_order_id,)).fetchone()
                if existing and not active:
                    action += ":" + uuid.uuid4().hex
                if not active:
                    con.execute("INSERT INTO trading_outbox VALUES(?,?,?,?,?,NULL)", (action, client_order_id, "cancel", "pending", stamp(now)))
                con.execute("UPDATE trading_orders SET status='cancel_pending' WHERE client_order_id=?", (client_order_id,))
            self.database.audit(principal.user_id, "trading.cancel_requested", account_id, {"order_id": client_order_id}, connection=con)
            return "cancel_requested"

    def replace(self, principal, account_id, old_id, replacement: OrderIntent, now=None):
        """Conservative cancel-confirm-new: atomic replacement unsupported."""
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            old = self._order(con, account_id, old_id)
            original = OrderIntent.from_dict(json.loads(old["body"]))
            linked = con.execute("SELECT new_order_id FROM trading_replacements WHERE old_order_id=?", (old_id,)).fetchone()
            if linked:
                if linked["new_order_id"] == replacement.client_order_id and self._order(con, account_id, linked["new_order_id"])["body"] == canonical(replacement):
                    return linked["new_order_id"]
                raise TradingError("REPLACE_ALREADY_SCHEDULED")
            if old["status"] != "cancelled" or account["frozen"]:
                raise TradingError("REPLACE_REQUIRES_CANCEL_RECONCILIATION")
            if account["mode"] == RunMode.BROKER_PAPER.value:
                latest = con.execute("SELECT at,body FROM trading_reconciliations WHERE account_id=? ORDER BY sequence DESC LIMIT 1", (account_id,)).fetchone()
                cancelled_at = [parse_instant(row["at"]) for row in con.execute("SELECT at,body FROM trading_order_events WHERE account_id=? AND client_order_id=?", (account_id, old_id))
                                if json.loads(row["body"]).get("event") == "canceled"]
                if latest is None or not json.loads(latest["body"]).get("clean") or not cancelled_at or parse_instant(latest["at"]) < max(cancelled_at):
                    raise TradingError("REPLACE_REQUIRES_CANCEL_RECONCILIATION")
            if replacement.account_id != account_id or replacement.instrument_id != original.instrument_id or replacement.side != original.side or replacement.strategy_id != original.strategy_id:
                raise TradingError("REPLACE_SCOPE_MISMATCH")
            at = instant(now or datetime.now(timezone.utc))
            result = self._submit(con, principal, replacement, at)
            con.execute("INSERT INTO trading_replacements VALUES(?,?,?)", (old_id, result, stamp(at)))
            con.execute("UPDATE trading_orders SET status='replaced' WHERE client_order_id=?", (old_id,))
            self.database.audit(principal.user_id, "trading.order_replaced", account_id, {"old_order_id": old_id, "new_order_id": result}, connection=con)
            return result

    def _order(self, con, account_id, order_id):
        row = con.execute("SELECT o.*,i.body FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? AND o.client_order_id=?", (account_id, order_id)).fetchone()
        if row is None:
            raise TradingError("ORDER_NOT_FOUND")
        return row

    def _terminal(self, con, account_id, order_id, status, now):
        con.execute("UPDATE trading_orders SET status=? WHERE client_order_id=?", (status, order_id))
        append_event(con, account_id, LedgerEvent("release:" + order_id + ":" + status, "release", order_id=order_id, source="oms:" + status), stamp(now))

    def _freeze(self, con, account_id, reason):
        con.execute("UPDATE trading_accounts SET frozen=1,freeze_reason=? WHERE account_id=?", (reason, account_id))

    def corporate_action(self, principal, account_id, event: LedgerEvent, now=None):
        now = instant(now or datetime.now(timezone.utc))
        if event.kind not in {"split", "dividend", "terminal_pending"}:
            raise TradingError("UNSUPPORTED_CORPORATE_ACTION")
        if event.kind == "split" and event.quantity <= ZERO:
            raise TradingError("INVALID_SPLIT")
        pending = False
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            if self._existing_event(con, account_id, event):
                return
            self._require_started(account, now)
            if account["mode"] != RunMode.LOCAL_SIMULATION.value:
                raise TradingError("PAPER_CORPORATE_ACTION_MANUAL_DISABLED")
            book = load_book(con, account)
            self._instrument(con, account_id, event.instrument_id)
            prior = con.execute("SELECT body FROM trading_pending_actions WHERE account_id=? AND event_id=?", (account_id, event.event_id)).fetchone()
            if not prior:
                advance(con, account, "corporate:" + event.event_id, now, event.to_dict())
            elif prior["body"] != canonical(event.to_dict()):
                raise TradingError("EVENT_ID_COLLISION")
            open_orders = con.execute("SELECT 1 FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? AND o.status NOT IN ('filled','cancelled','rejected','expired','replaced') LIMIT 1", (account_id,)).fetchone()
            if event.kind == "split" and (open_orders or prior):
                if prior and prior["body"] != canonical(event.to_dict()):
                    raise TradingError("EVENT_ID_COLLISION")
                if not prior:
                    con.execute("INSERT INTO trading_pending_actions VALUES(?,?,?,?)", (account_id, event.event_id, canonical(event.to_dict()), stamp(now)))
                if not con.execute("SELECT 1 FROM trading_action_resolutions WHERE account_id=? AND event_id=?", (account_id, event.event_id)).fetchone():
                    self._freeze(con, account_id, "CORPORATE_ACTION_PENDING")
                    pending = True
            else:
                book.apply(event)
                append_event(con, account_id, event, stamp(now))
            if event.kind == "split" and not pending:
                inst = self._instrument(con, account_id, event.instrument_id)
                qty = book.positions.get(event.instrument_id, Position()).quantity
                if qty % inst.lot_size:
                    self._freeze(con, account_id, "CASH_IN_LIEU_PENDING")
            elif event.kind == "terminal_pending":
                self._freeze(con, account_id, "TERMINAL_RECOVERY_PENDING")
            self.database.audit(principal.user_id, "trading.corporate_action", account_id, {"event_id": event.event_id, "kind": event.kind}, connection=con)
        # Raising inside transaction would roll back the durable freeze and evidence.
        if pending:
            raise TradingError("OPEN_ORDERS_CORPORATE_ACTION")

    def resolve_corporate_action(self, principal, account_id, event_id, now=None):
        now = instant(now or datetime.now(timezone.utc))
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            self._require_started(account, now)
            row = con.execute("SELECT body FROM trading_pending_actions WHERE account_id=? AND event_id=?", (account_id, event_id)).fetchone()
            if row is None:
                raise TradingError("PENDING_ACTION_NOT_FOUND")
            if con.execute("SELECT 1 FROM trading_action_resolutions WHERE account_id=? AND event_id=?", (account_id, event_id)).fetchone():
                return
            require_forward(con, account, now)
            if con.execute("SELECT 1 FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? AND o.status NOT IN ('filled','cancelled','rejected','expired','replaced') LIMIT 1", (account_id,)).fetchone():
                raise TradingError("CORPORATE_ACTION_CANCEL_CONFIRM_REQUIRED")
            book = load_book(con, account)
            event = LedgerEvent(**json.loads(row["body"]))
            book.apply(event)
            append_event(con, account_id, event, stamp(now))
            con.execute("INSERT INTO trading_action_resolutions VALUES(?,?,?)", (account_id, event_id, stamp(now)))
            pending = con.execute("SELECT 1 FROM trading_pending_actions p LEFT JOIN trading_action_resolutions r USING(account_id,event_id) WHERE p.account_id=? AND r.event_id IS NULL", (account_id,)).fetchone()
            instrument = self._instrument(con, account_id, event.instrument_id)
            if book.positions.get(event.instrument_id, Position()).quantity % instrument.lot_size:
                self._freeze(con, account_id, "CASH_IN_LIEU_PENDING")
            elif not pending and not book.pending_terminal and account["freeze_reason"] == "CORPORATE_ACTION_PENDING":
                con.execute("UPDATE trading_accounts SET frozen=0,freeze_reason=NULL WHERE account_id=?", (account_id,))
            self.database.audit(principal.user_id, "trading.corporate_action_resolved", account_id, {"event_id": event_id}, connection=con)

    def settle(self, principal, account_id, now):
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            self._require_started(account, now)
            self._settle(con, account, load_book(con, account), instant(now))
            # Consumed-prefix recovery may revisit a settlement check with no new
            # effect. Such a check neither rewinds nor advances the frontier.
            clock = read_clock(con, account)
            if not clock["blocked"] and instant(now) >= parse_instant(clock["watermark"]):
                advance(con, account, "settle-check:" + stamp(now), now, {"kind": "settle-check"})

    def dividend(self, principal, account_id, event_id, instrument_id, per_share, record_at, pay_at, *, now=None, source=None):
        """Entitlement from immutable record-date holdings, not payment-date position."""
        record_at, pay_at = instant(record_at), instant(pay_at)
        recorded_at = instant(now) if now is not None else pay_at
        per_share = decimal(per_share)
        if per_share < ZERO or record_at > pay_at or recorded_at < pay_at or source is not None and (type(source) is not str or not source):
            raise TradingError("INVALID_DIVIDEND")
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            event_source = (source + ":" if source is not None else "") + "dividend:record-at:" + stamp(record_at) + ":pay-at:" + stamp(pay_at) + ":per-share:" + str(per_share)
            old = con.execute("SELECT body FROM trading_ledger WHERE account_id=? AND event_id=?", (account_id, event_id)).fetchone()
            if old:
                existing = LedgerEvent(**json.loads(old["body"]))
                if existing.kind != "dividend" or existing.instrument_id != instrument_id or existing.source != event_source:
                    raise TradingError("EVENT_ID_COLLISION")
                return
            self._require_started(account, recorded_at)
            require_forward(con, account, recorded_at)
            if record_at < parse_instant(account["created_at"]):
                raise TradingError("DIVIDEND_RECORD_BEFORE_ACCOUNT_START")
            if account["mode"] != RunMode.LOCAL_SIMULATION.value:
                raise TradingError("PAPER_CORPORATE_ACTION_MANUAL_DISABLED")
            historical = CashBook(decimal(account["initial_cash"]))
            for row in con.execute("SELECT body,at FROM trading_ledger WHERE account_id=? ORDER BY sequence", (account_id,)):
                if parse_instant(row["at"]) <= record_at:
                    historical.apply(LedgerEvent(**json.loads(row["body"])))
            held = historical.positions.get(instrument_id, Position()).quantity
            event = LedgerEvent(event_id, "dividend", instrument_id=instrument_id, amount=money(held * per_share),
                source=event_source)
            book = load_book(con, account)
            book.apply(event)
            append_event(con, account_id, event, stamp(recorded_at))
            self.database.audit(principal.user_id, "trading.dividend", account_id, {"event_id": event_id}, connection=con)

    def submit_targets(self, principal, account_id, request_id, budgets, weights, now=None):
        """Atomically aggregate budget-weighted targets; all generated intents pass _submit."""
        identifier(request_id)
        now = instant(now or datetime.now(timezone.utc))
        with self.database.transaction() as con:
            account = self._account(con, account_id, principal, True)
            body = canonical({"budgets": budgets, "weights": weights})
            old_plan = con.execute("SELECT body,result FROM trading_plans WHERE account_id=? AND request_id=?", (account_id, request_id)).fetchone()
            if old_plan:
                if old_plan["body"] != body:
                    raise TradingError("IDEMPOTENCY_CONFLICT")
                return json.loads(old_plan["result"])
            require_forward(con, account, now)
            book = load_book(con, account)
            markets = self._markets(con, account_id, now)
            prices = {i: e.price for i, e in markets.items()}
            equity = book.snapshot(prices)["equity"]
            if equity is None:
                raise TradingError("VALUATION_UNKNOWN")
            instruments = {i: self._instrument(con, account_id, i) for i in set(prices) | {i for values in weights.values() for i in values}}
            total, contributions = PortfolioAllocator.targets(decimal(equity), budgets, weights, prices,
                                                               {i: v.lot_size for i, v in instruments.items()})
            for (strategy, inst), position in book.contributions.items():
                if strategy not in budgets:
                    total[inst] = total.get(inst, ZERO) + position.quantity
            # A new rebalance cannot overlap another pending allocation for these strategies.
            for row in con.execute("SELECT o.status,i.body FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=?", (account_id,)):
                if row["status"] not in TERMINAL and json.loads(row["body"])["strategy_id"] in budgets:
                    raise TradingError("REBALANCE_OPEN_ORDERS")
            planned = []
            for strategy, targets in contributions.items():
                ids = set(targets) | {i for s, i in book.contributions if s == strategy}
                for inst in sorted(ids):
                    held = book.contributions.get((strategy, inst), Position()).quantity
                    delta = targets.get(inst, ZERO) - held
                    if delta:
                        cid = "t-" + fingerprint({"account": account_id, "request": request_id, "strategy": strategy, "instrument": inst})[:40]
                        planned.append(OrderIntent(cid, account_id, strategy, inst, "buy" if delta > ZERO else "sell", abs(delta), "market", now, RunMode(account["mode"]), source="rebalance"))
            order_ids = [self._submit(con, principal, order, now) for order in sorted(planned, key=lambda o: o.side != "sell")]
            result = {"orders": order_ids, "final_targets": {i: str(q) for i, q in total.items()},
                      "strategy_targets": {s: {i: str(q) for i, q in values.items()} for s, values in contributions.items()},
                      "conflict_rule": "BUDGET_WEIGHTED_ADDITIVE"}
            con.execute("INSERT INTO trading_plans VALUES(?,?,?,?)", (account_id, request_id, body, canonical(result)))
            advance(con, account, "targets:" + request_id, now, json.loads(body))
            return result
