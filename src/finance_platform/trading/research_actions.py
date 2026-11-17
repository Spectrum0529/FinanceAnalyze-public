"""Causal action bridge: control receipts only; TradingService owns all economics."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import json

from finance_platform.core.contracts import DataScope, Provenance, RunMode, canonical, fingerprint, instant, parse_instant
from finance_platform.data.contracts import validate_record, DataError
from .accounting import LedgerEvent, TradingError, decimal
from .service import stamp
from .storage import load_book

MODEL = "research-actions-1"


@dataclass(frozen=True)
class ActionEvent:
    envelope_json: str
    instrument_id: str
    provenance: Provenance
    kind: str
    status: str
    due_at: datetime
    record_at: datetime | None
    pay_at: datetime | None

    @property
    def event_id(self):
        return "research-action:" + fingerprint([self.provenance.source_id, self.provenance.record_id])[:48]

    @property
    def source(self):
        return "research-action:" + self.provenance.source_id + ":" + self.provenance.record_id + ":" + fingerprint(json.loads(self.envelope_json))


def parse_action(envelope):
    try:
        if type(envelope) is not dict or set(envelope) - {"kind", "instrument_id", "metadata", "values", "lineage"} or envelope.get("kind") != "action":
            raise TradingError("RESEARCH_ACTION_SCHEMA_INVALID")
        record = {k: v for k, v in envelope.items() if k != "lineage"}
        meta = validate_record(record)
        if meta.scope not in {DataScope.SYNTHETIC, DataScope.DEVELOPMENT}:
            raise TradingError("RESEARCH_ACTION_SCOPE_FORBIDDEN")
        if meta.revision != 1 or meta.valid_to is not None:
            raise TradingError("RESEARCH_ACTION_REVISION_UNSUPPORTED")
        values = record["values"]
        kind, status = values["type"], values["status"]
        if status == "CANCELLED" or kind == "TERMINAL" and status != "PENDING":
            raise TradingError("RESEARCH_ACTION_STATUS_UNSUPPORTED")
        if status == "RESOLVED":
            field = "ratio" if kind == "SPLIT" else "cash_per_share"
            if type(values.get(field)) is not str:
                raise TradingError("RESEARCH_ACTION_DECIMAL_STRING_REQUIRED")
            decimal(values[field])
        record_at = pay_at = None
        due = max(meta.effective_at, meta.available_at)
        if kind == "DIVIDEND" and status == "RESOLVED":
            if not {"record_at", "pay_at"} <= set(values):
                raise TradingError("DIVIDEND_RECORD_AND_PAYMENT_TIME_REQUIRED")
            record_at, pay_at = parse_instant(values["record_at"]), parse_instant(values["pay_at"])
            if record_at > pay_at:
                raise TradingError("INVALID_DIVIDEND")
            due = max(due, pay_at)
        return ActionEvent(canonical(envelope), record["instrument_id"], meta, kind, status, due, record_at, pay_at)
    except TradingError:
        raise
    except (DataError, KeyError, TypeError, ValueError):
        raise TradingError("RESEARCH_ACTION_SCHEMA_INVALID") from None


def action_due_at(envelope):
    return parse_action(envelope).due_at


class ResearchActionBridge:
    def __init__(self, service, principal, account_id, *, source_id, license_id, scope, authorize_action=None):
        if type(scope) is not DataScope or scope not in {DataScope.SYNTHETIC, DataScope.DEVELOPMENT}:
            raise TradingError("RESEARCH_ACTION_SCOPE_FORBIDDEN")
        self.service, self.principal, self.account_id = service, principal, account_id
        self.source_id, self.license_id, self.scope = source_id, license_id, scope
        self.authorize_action = authorize_action
        with closing(service.database.connect()) as con:
            self._account(con)

    def _account(self, con):
        account = self.service._account(con, self.account_id, self.principal, True)
        if account["mode"] != RunMode.LOCAL_SIMULATION.value:
            raise TradingError("RESEARCH_ACTION_MODE_DISABLED")
        return account

    def _validate(self, envelope, now):
        action = parse_action(envelope)
        meta = action.provenance
        if (meta.source_id, meta.license_id, meta.scope) != (self.source_id, self.license_id, self.scope):
            raise TradingError("RESEARCH_ACTION_ORIGIN_MISMATCH")
        if meta.scope is DataScope.DEVELOPMENT and (self.authorize_action is None or self.authorize_action(meta) is not True):
            raise TradingError("DEVELOPMENT_DATA_PERMISSION_REQUIRED")
        at = instant(now)
        if at < action.due_at:
            raise TradingError("RESEARCH_ACTION_NOT_DUE")
        return action, at

    def _result(self, action, status, code, now, *, duplicate=False):
        return {"event_id": action.event_id, "record_id": action.provenance.record_id, "kind": action.kind,
                "status": status, "code": code, "observed_at": stamp(now),
                "applied_at": stamp(now) if status in {"APPLIED", "TERMINAL_PENDING"} else None, "duplicate": duplicate}

    def _record_result(self, con, action, phase, result, now):
        if con.execute("SELECT 1 FROM trading_research_action_results WHERE account_id=? AND event_id=? AND phase=?", (self.account_id, action.event_id, phase)).fetchone() is None:
            con.execute("INSERT INTO trading_research_action_results VALUES(?,?,?,?,?)", (self.account_id, action.event_id, phase, canonical(result), stamp(now)))

    def _prior(self, con, action, phase):
        row = con.execute("SELECT body FROM trading_research_action_results WHERE account_id=? AND event_id=? AND phase=?", (self.account_id, action.event_id, phase)).fetchone()
        return None if row is None else json.loads(row["body"]) | {"duplicate": True}

    def _admit(self, action, now):
        service = self.service
        with service.database.transaction() as con:
            account = self._account(con)
            service._require_started(account, now)
            service._instrument(con, self.account_id, action.instrument_id)
            if action.record_at is not None and action.record_at < parse_instant(account["created_at"]):
                raise TradingError("DIVIDEND_RECORD_BEFORE_ACCOUNT_START")
            prior = con.execute("SELECT body FROM trading_research_actions WHERE account_id=? AND event_id=?", (self.account_id, action.event_id)).fetchone()
            if prior and prior["body"] != action.envelope_json:
                result = self._result(action, "BLOCKED", "RESEARCH_ACTION_IDENTITY_CONFLICT", now)
                self._record_result(con, action, "BLOCKED", result, now)
                service._freeze(con, self.account_id, result["code"])
                return result
            if prior:
                for phase in ("BLOCKED", "APPLIED", "PENDING_CANCEL"):
                    result = self._prior(con, action, phase)
                    if result is not None:
                        return result
                # A crash after ADMITTED is safe: control receipt blocks all fills
                # until this idempotent service action completes on retry.
                return None
            if service._research_action_blocked(con, self.account_id):
                raise TradingError("RESEARCH_ACTION_PENDING")
            from .execution_clock import advance
            advance(con, account, "research-action:" + action.event_id, now, {"envelope": json.loads(action.envelope_json)})
            con.execute("INSERT INTO trading_research_actions VALUES(?,?,?,?,?,?,?)", (self.account_id, action.event_id,
                action.provenance.source_id, action.provenance.record_id, fingerprint(json.loads(action.envelope_json)), action.envelope_json, stamp(now)))
            if action.kind == "SPLIT":
                boundary = action.provenance.effective_at
                history = any(parse_instant(r["at"]) >= boundary and json.loads(r["body"])["kind"] in {"fill", "split"} and json.loads(r["body"])["instrument_id"] == action.instrument_id
                    for r in con.execute("SELECT at,body FROM trading_ledger WHERE account_id=?", (self.account_id,)))
                history = history or any(parse_instant(json.loads(r["body"])["event_at"]) >= boundary
                    for r in con.execute("SELECT body FROM trading_market WHERE account_id=? AND instrument_id=?", (self.account_id, action.instrument_id)))
                if history:
                    result = self._result(action, "BLOCKED", "LATE_SPLIT_RECONCILIATION_REQUIRED", now)
                    self._record_result(con, action, "BLOCKED", result, now)
                    service._freeze(con, self.account_id, result["code"])
                    return result
            if not account["frozen"]:
                service._freeze(con, self.account_id, "RESEARCH_ACTION_APPLYING")
            self._record_result(con, action, "ADMITTED", self._result(action, "ADMITTED", None, now), now)
            return None

    def _complete(self, action, status, now):
        service = self.service
        result = self._result(action, status, None, now)
        with service.database.transaction() as con:
            account = self._account(con)
            effect = con.execute("SELECT at FROM trading_ledger WHERE account_id=? AND event_id=?", (self.account_id, action.event_id)).fetchone()
            if effect is None:
                raise TradingError("RESEARCH_ACTION_EFFECT_MISSING")
            result["applied_at"] = effect["at"]
            self._record_result(con, action, "APPLIED", result, now)
            book = load_book(con, account)
            if account["freeze_reason"] in {"RESEARCH_ACTION_APPLYING", "CORPORATE_ACTION_PENDING"} and not service._research_action_blocked(con, self.account_id) and not service._settlement_blocked(con, self.account_id) and not book.pending_terminal:
                pending = con.execute("SELECT 1 FROM trading_pending_actions p LEFT JOIN trading_action_resolutions r USING(account_id,event_id) WHERE p.account_id=? AND r.event_id IS NULL", (self.account_id,)).fetchone()
                if pending is None:
                    con.execute("UPDATE trading_accounts SET frozen=0,freeze_reason=NULL WHERE account_id=?", (self.account_id,))
        return result

    def apply(self, envelope, now):
        action, at = self._validate(envelope, now)
        result = self._admit(action, at)
        if result is not None:
            return result
        values = json.loads(action.envelope_json)["values"]
        if action.status == "PENDING" and action.kind != "TERMINAL":
            result = self._result(action, "BLOCKED", "RESEARCH_ACTION_VALUE_PENDING", at)
            with self.service.database.transaction() as con:
                self._account(con)
                self._record_result(con, action, "BLOCKED", result, at)
                self.service._freeze(con, self.account_id, result["code"])
            return result
        if action.kind == "DIVIDEND":
            self.service.dividend(self.principal, self.account_id, action.event_id, action.instrument_id,
                values["cash_per_share"], action.record_at, action.pay_at, now=at, source=action.source)
        else:
            event = LedgerEvent(action.event_id, "split" if action.kind == "SPLIT" else "terminal_pending",
                instrument_id=action.instrument_id, quantity=decimal(values["ratio"]) if action.kind == "SPLIT" else decimal("0"), source=action.source)
            try:
                self.service.corporate_action(self.principal, self.account_id, event, at)
            except TradingError as exc:
                if exc.code != "OPEN_ORDERS_CORPORATE_ACTION":
                    raise
                result = self._result(action, "PENDING_CANCEL", exc.code, at)
                with self.service.database.transaction() as con:
                    self._account(con)
                    self._record_result(con, action, "PENDING_CANCEL", result, at)
                return result
        return self._complete(action, "TERMINAL_PENDING" if action.kind == "TERMINAL" else "APPLIED", at)

    def resolve(self, envelope, now):
        action, at = self._validate(envelope, now)
        if action.kind != "SPLIT" or action.status != "RESOLVED":
            raise TradingError("RESEARCH_ACTION_RESOLUTION_UNSUPPORTED")
        with closing(self.service.database.connect()) as con:
            self._account(con)
            prior = con.execute("SELECT body FROM trading_research_actions WHERE account_id=? AND event_id=?", (self.account_id, action.event_id)).fetchone()
            if prior is None or prior["body"] != action.envelope_json:
                raise TradingError("RESEARCH_ACTION_BINDING_REQUIRED")
            blocked = self._prior(con, action, "BLOCKED")
            if blocked is not None:
                return blocked
            applied = self._prior(con, action, "APPLIED")
            if applied is not None:
                return applied
            if self._prior(con, action, "PENDING_CANCEL") is None:
                raise TradingError("RESEARCH_ACTION_PENDING_CANCEL_REQUIRED")
        self.service.resolve_corporate_action(self.principal, self.account_id, action.event_id, at)
        return self._complete(action, "APPLIED", at)
