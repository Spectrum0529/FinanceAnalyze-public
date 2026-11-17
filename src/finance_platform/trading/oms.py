"""Persistent outbox, monotonic executions and four-way recovery. No blind resend."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid
from contextlib import closing
from collections import Counter

from finance_platform.core.contracts import RunMode, canonical, fingerprint, parse_instant
from .accounting import LedgerEvent, TradingError, ZERO, decimal, money
from .models import OrderIntent
from .service import TERMINAL, settlement_at, stamp
from .storage import append_event, load_book, unresolved_restore
from .risk_configuration import policy, intent_policy

KNOWN_EVENTS = {"new", "accepted", "pending_new", "partial_fill", "fill", "canceled", "expired", "rejected",
                "pending_cancel", "pending_replace", "replaced", "order_cancel_rejected", "order_replace_rejected"}


class OrderManagement:
    def __init__(self, service, broker):
        self.service, self.broker = service, broker

    def dispatch_one(self, authority):
        service = self.service
        now = datetime.now(timezone.utc)
        with service.database.transaction() as con:
            authority.require(con)
            account = service._account(con, authority.account_id, authority.principal, True)
            if account["mode"] != RunMode.BROKER_PAPER.value:
                raise TradingError("PAPER_ACCOUNT_MODE_MISMATCH")
            if account["frozen"]:
                raise TradingError("ACCOUNT_RECONCILIATION_REQUIRED")
            if unresolved_restore(con, authority.account_id):
                raise TradingError("RESTORE_RECONCILIATION_REQUIRED")
            if con.execute("SELECT 1 FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? AND o.status IN ('sending','unknown') LIMIT 1", (authority.account_id,)).fetchone():
                raise TradingError("ACCOUNT_RECONCILIATION_REQUIRED")
            row = con.execute("SELECT b.*,i.body,o.broker_id FROM trading_outbox b JOIN trading_intents i USING(client_order_id) JOIN trading_orders o USING(client_order_id) WHERE i.account_id=? AND b.status='pending' ORDER BY b.at,b.action_id LIMIT 1", (authority.account_id,)).fetchone()
            if row is None:
                return None
            intent = OrderIntent.from_dict(json.loads(row["body"]))
            # Revalidate current market/session/loss/kill before opening transmission.
            if row["action"] == "submit":
                from .risk import RiskService, RiskLimits
                from .accounting import FeeSchedule
                book = load_book(con, account)
                book.reservations.pop(intent.client_order_id, None)
                book.sell_reservations.pop(intent.client_order_id, None)
                book.strategy_sell_reservations.pop(intent.client_order_id, None)
                markets = service._markets(con, intent.account_id, now)
                if intent.instrument_id not in markets:
                    raise TradingError("MARKET_DATA_REQUIRED")
                for limits in (intent_policy(con, account, intent.client_order_id), policy(con, account)[0]):
                    risk = RiskService(limits, FeeSchedule(**json.loads(account["fee_json"])))
                    risk.check(intent, service._instrument(con, intent.account_id, intent.instrument_id), markets[intent.instrument_id],
                        book, {i: e.price for i, e in markets.items()}, now, accepted_last_minute=0,
                        emergency_stop=bool(account["emergency"]), frozen=False,
                        day_baseline=decimal(account["baseline"]), day_high=decimal(account["high"]), day_flows=decimal(account["day_flows"]))
            con.execute("UPDATE trading_outbox SET status='sending',fencing=? WHERE action_id=?", (authority.fencing, row["action_id"]))
            con.execute("UPDATE trading_orders SET status='sending' WHERE client_order_id=?", (intent.client_order_id,))
            attempt_id = "send:" + str(uuid.uuid4())
            con.execute("INSERT INTO trading_order_events(account_id,event_id,client_order_id,body,at) VALUES(?,?,?,?,?)",
                (intent.account_id, attempt_id, intent.client_order_id,
                 canonical({"event": "sending", "action": row["action"], "fencing": authority.fencing, "worker_id": authority.worker_id}), stamp(now)))
            instrument = service._instrument(con, intent.account_id, intent.instrument_id)
        try:
            if row["action"] == "submit":
                response = self.broker.send_order(intent, instrument.ticker, authority)
                if not isinstance(response, dict) or response.get("client_order_id") != intent.client_order_id:
                    raise TradingError("BROKER_PROTOCOL_INVALID")
                self.apply_event(authority.principal, intent.account_id,
                    {"event": "accepted", "order": response, "timestamp": stamp(now)})
            else:
                if not row["broker_id"]:
                    raise TradingError("BROKER_ORDER_ID_REQUIRED")
                self.broker.cancel_order(intent, row["broker_id"], authority)
                # HTTP 204 is cancellation request acknowledgement, not terminal cancellation.
                with service.database.transaction() as con:
                    authority.require(con)
                    con.execute("UPDATE trading_orders SET status='cancel_pending' WHERE client_order_id=? AND status='sending'", (intent.client_order_id,))
            with service.database.transaction() as con:
                authority.require(con)
                con.execute("UPDATE trading_outbox SET status='done' WHERE action_id=?", (row["action_id"],))
            return intent.client_order_id
        except BaseException as exc:
            with service.database.transaction() as con:
                # Record uncertainty even if lease was lost; never authorize another send here.
                code = exc.code if isinstance(exc, TradingError) else "UNEXPECTED_SEND_FAILURE"
                confirmed_rejection = isinstance(exc, TradingError) and exc.code == "BROKER_ORDER_REJECTED" and row["action"] == "submit"
                if confirmed_rejection:
                    service._terminal(con, intent.account_id, intent.client_order_id, "rejected", now)
                    con.execute("UPDATE trading_outbox SET status='rejected' WHERE action_id=?", (row["action_id"],))
                else:
                    con.execute("UPDATE trading_orders SET status='unknown' WHERE client_order_id=?", (intent.client_order_id,))
                    con.execute("UPDATE trading_outbox SET status='unknown' WHERE action_id=?", (row["action_id"],))
                    service._freeze(con, intent.account_id, "SEND_UNKNOWN_QUERY_REQUIRED")
                con.execute("INSERT INTO trading_order_events(account_id,event_id,client_order_id,body,at) VALUES(?,?,?,?,?)",
                    (intent.account_id, attempt_id + ":result", intent.client_order_id,
                     canonical({"event": "rejected" if confirmed_rejection else "unknown", "code": code, "action": row["action"]}), stamp(datetime.now(timezone.utc))))
            if isinstance(exc, TradingError) or not isinstance(exc, Exception):
                raise
            raise TradingError("BROKER_TRANSPORT_UNCERTAIN") from None

    def apply_event(self, principal, account_id, data):
        service = self.service
        now = datetime.now(timezone.utc)
        name, order = data.get("event"), data.get("order", {})
        if name not in KNOWN_EVENTS or not isinstance(order, dict):
            raise TradingError("BROKER_EVENT_UNSUPPORTED")
        order_id = order.get("client_order_id")
        at = parse_instant(data.get("timestamp") or order.get("updated_at") or stamp(now))
        normalized = {"event": name, "client_order_id": order_id, "broker_id": order.get("id"),
                      "execution_id": data.get("execution_id"), "qty": data.get("qty"),
                      "price": data.get("price"), "fee": data.get("fee", "0"),
                      "filled_qty": order.get("filled_qty", "0"), "at": stamp(at)}
        event_id = "exec:" + data["execution_id"] if name in {"fill", "partial_fill"} and data.get("execution_id") else fingerprint(normalized)
        with service.database.transaction() as con:
            account = service._account(con, account_id, principal, True)
            if account["mode"] != RunMode.BROKER_PAPER.value:
                raise TradingError("PAPER_ACCOUNT_MODE_MISMATCH")
            prior = con.execute("SELECT body FROM trading_order_events WHERE account_id=? AND event_id=?", (account_id, event_id)).fetchone()
            if prior:
                if prior["body"] != canonical(normalized):
                    service._freeze(con, account_id, "EXECUTION_ID_COLLISION")
                    return "frozen"
                return "duplicate"
            con.execute("INSERT INTO trading_order_events(account_id,event_id,client_order_id,body,at) VALUES(?,?,?,?,?)", (account_id, event_id, order_id or "external", canonical(normalized), stamp(at)))
            try:
                row = service._order(con, account_id, order_id)
            except TradingError:
                service._freeze(con, account_id, "EXTERNAL_MANUAL_ORDER")
                return "external"
            intent = OrderIntent.from_dict(json.loads(row["body"]))
            instrument = service._instrument(con, account_id, intent.instrument_id)
            if order.get("symbol", instrument.ticker) != instrument.ticker or order.get("side", intent.side) != intent.side or (row["broker_id"] is not None and order.get("id") != row["broker_id"]):
                service._freeze(con, account_id, "BROKER_ORDER_SCOPE_MISMATCH")
                return "frozen"
            if name in {"fill", "partial_fill"}:
                if not data.get("execution_id"):
                    service._freeze(con, account_id, "EXECUTION_ID_REQUIRED")
                    return "frozen"
                qty, price, fee = decimal(data["qty"]), decimal(data["price"]), money(data.get("fee", "0"))
                before = decimal(row["filled_qty"])
                if qty <= ZERO or price <= ZERO or fee < ZERO or before + qty > intent.quantity:
                    service._freeze(con, account_id, "BROKER_FILL_INCONSISTENT")
                    return "frozen"
                book = load_book(con, account)
                fill = LedgerEvent(event_id, "fill", instrument_id=intent.instrument_id, strategy_id=intent.strategy_id,
                    side=intent.side, quantity=qty, price=price, fee=fee, order_id=intent.client_order_id, source="alpaca:paper")
                try:
                    book.apply(fill)
                except TradingError:
                    service._freeze(con, account_id, "BROKER_LEDGER_DIFFERENCE")
                    return "frozen"
                append_event(con, account_id, fill, stamp(at))
                filled = before + qty
                status = "filled" if filled == intent.quantity else (row["status"] if row["status"] in TERMINAL | {"cancel_pending"} else "partial")
                try:
                    due = settlement_at(at) if intent.side == "sell" else None
                except TradingError:
                    # Record a real broker fill even when its settlement schedule
                    # is uncovered. It remains unavailable pending broker evidence.
                    due = None
                    service._freeze(con, account_id, "SETTLEMENT_EVIDENCE_REQUIRED")
                con.execute("UPDATE trading_orders SET status=?,broker_id=?,filled_qty=?,filled_notional=?,fees=?,settlement_at=? WHERE client_order_id=?",
                    (status, order.get("id"), str(filled), str(decimal(row["filled_notional"]) + money(qty * price)), str(decimal(row["fees"]) + fee),
                     None if due is None else stamp(due), order_id))
                release = LedgerEvent("release:" + event_id, "release", order_id=order_id, source="oms:fill")
                book.apply(release)
                append_event(con, account_id, release, stamp(at))
                if row["status"] in TERMINAL:
                    service._freeze(con, account_id, "LATE_FILL_AFTER_TERMINAL")
                elif status != "filled":
                    remaining = intent.quantity - filled
                    from .risk import RiskLimits
                    from .accounting import FeeSchedule
                    buffer = max(intent_policy(con, account, order_id).market_reserve_buffer, policy(con, account)[0].market_reserve_buffer)
                    fee_reserve = max(ZERO, FeeSchedule(**json.loads(account["fee_json"])).fee(intent.quantity) - decimal(row["fees"]) - fee)
                    reserve = LedgerEvent("reserve:" + event_id, "reserve" if intent.side == "buy" else "reserve_sell",
                        instrument_id=intent.instrument_id, strategy_id=intent.strategy_id, quantity=remaining,
                        amount=money(remaining * (intent.limit_price or price * (1 + buffer))) + fee_reserve if intent.side == "buy" else ZERO,
                        order_id=order_id, source="oms:remaining")
                    try:
                        book.apply(reserve)
                        append_event(con, account_id, reserve, stamp(at))
                    except TradingError:
                        service._freeze(con, account_id, "BROKER_RESERVATION_DIFFERENCE")
                # Out-of-order reports may have a lower cumulative qty. Higher requires missing execution recovery.
                if decimal(order.get("filled_qty", "0")) > filled:
                    service._freeze(con, account_id, "MISSING_EXECUTION_RECOVERY")
            elif name in {"canceled", "expired", "rejected", "replaced"}:
                if decimal(order.get("filled_qty", "0")) != decimal(row["filled_qty"]):
                    service._freeze(con, account_id, "MISSING_EXECUTION_RECOVERY")
                status = {"canceled": "cancelled"}.get(name, name)
                if row["status"] != "filled":
                    service._terminal(con, account_id, order_id, status, at)
            elif name in {"new", "accepted", "pending_new"}:
                if row["status"] not in TERMINAL | {"partial", "cancel_pending"}:
                    con.execute("UPDATE trading_orders SET status='new',broker_id=? WHERE client_order_id=?", (order.get("id"), order_id))
            elif name == "pending_cancel":
                if row["status"] not in TERMINAL:
                    con.execute("UPDATE trading_orders SET status='cancel_pending' WHERE client_order_id=?", (order_id,))
            elif name in {"order_cancel_rejected", "order_replace_rejected", "pending_replace"}:
                service._freeze(con, account_id, "BROKER_ACTION_RECONCILIATION_REQUIRED")
            return "applied"

    def freeze_disconnected(self, principal, account_id):
        with self.service.database.transaction() as con:
            self.service._account(con, account_id, principal, True)
            self.service._freeze(con, account_id, "BROKER_DISCONNECTED")

    def reconcile(self, principal, account_id, snapshot, now=None):
        """Strict cash/positions/open orders/fills equality. External records never adopted."""
        now = now or datetime.now(timezone.utc)
        service = self.service
        with service.database.transaction() as con:
            account = service._account(con, account_id, principal, True)
            if account["mode"] != RunMode.BROKER_PAPER.value:
                raise TradingError("PAPER_ACCOUNT_MODE_MISMATCH")
            book = load_book(con, account)
            differences, external = [], []
            if service._settlement_blocked(con, account_id):
                differences.append("legacy_settlement_evidence")
            if money(snapshot["cash"]) != book.cash:
                differences.append("cash")
            instruments = {service._instrument(con, account_id, row["instrument_id"]).ticker: row["instrument_id"]
                           for row in con.execute("SELECT instrument_id FROM trading_instruments WHERE account_id=?", (account_id,))}
            positions = {}
            for row in snapshot["positions"]:
                if row["symbol"] not in instruments:
                    external.append("position:" + row["symbol"])
                    continue
                positions[instruments[row["symbol"]]] = decimal(row["qty"])
            expected = {i: p.quantity for i, p in book.positions.items() if p.quantity}
            if {i: q for i, q in positions.items() if q} != expected:
                differences.append("positions")
            if unresolved_restore(con, account_id):
                differences.append("restore_barrier")
            local = {row["client_order_id"]: row for row in con.execute("SELECT o.*,i.body FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=?", (account_id,))}
            observed = {r["client_order_id"]: r for r in snapshot["open_orders"]}
            for cid in observed:
                if cid not in local:
                    external.append("order:" + cid)
                else:
                    intent = OrderIntent.from_dict(json.loads(local[cid]["body"]))
                    inst = service._instrument(con, account_id, intent.instrument_id)
                    remote = observed[cid]
                    if (remote.get("symbol") != inst.ticker or remote.get("side") != intent.side or remote.get("id") != local[cid]["broker_id"] or
                        decimal(remote.get("qty", "0")) != intent.quantity or remote.get("type") != intent.order_type or
                        remote.get("time_in_force") != "day" or remote.get("extended_hours", False) is not False or
                        (intent.limit_price is not None and decimal(remote.get("limit_price", "0")) != intent.limit_price)):
                        differences.append("order_scope:" + cid)
            expected_open = {cid for cid, row in local.items() if row["status"] not in TERMINAL | {"queued"}}
            if set(observed) != expected_open:
                differences.append("open_orders")
            fill_totals = {}
            for fill in snapshot["fills"]:
                broker_id = fill.get("order_id")
                candidates = [cid for cid, row in local.items() if row["broker_id"] == broker_id]
                if len(candidates) != 1:
                    external.append("fill:" + str(fill.get("id", "unknown")))
                    continue
                cid = candidates[0]
                qty, gross = fill_totals.get(cid, (ZERO, ZERO))
                fill_totals[cid] = (qty + decimal(fill["qty"]), gross + money(decimal(fill["qty"]) * decimal(fill["price"])))
            for cid, row in local.items():
                if fill_totals.get(cid, (ZERO, ZERO)) != (decimal(row["filled_qty"]), decimal(row["filled_notional"])):
                    differences.append("fills:" + cid)
                if row["status"] in {"unknown", "sending"}:
                    differences.append("unknown:" + cid)
                if cid in observed and decimal(observed[cid].get("filled_qty", "0")) != decimal(row["filled_qty"]):
                    differences.append("order_filled_qty:" + cid)
            report = {"clean": not differences and not external, "differences": differences,
                      "external_manual": external, "mode": "BROKER_PAPER"}
            con.execute("INSERT INTO trading_reconciliations(account_id,at,body) VALUES(?,?,?)", (account_id, stamp(now), canonical(report)))
            con.execute("UPDATE trading_accounts SET frozen=?,freeze_reason=? WHERE account_id=?",
                (0 if report["clean"] else 1, None if report["clean"] else "RECONCILIATION_DIFFERENCE", account_id))
            return report

    def recover(self, principal, account_id):
        """Query first. Even 404 preserves unknown; do not resurrect outbox pending."""
        self.freeze_disconnected(principal, account_id)
        with closing(self.service.database.connect()) as con:
            account = self.service._account(con, account_id, principal, True)
            created_at = parse_instant(account["created_at"])
            ids = [row[0] for row in con.execute("SELECT client_order_id FROM trading_intents WHERE account_id=?", (account_id,))]
        snapshot = self.broker.snapshot(created_at)
        queried = {}
        for cid in ids:
            order = self.broker.lookup(cid)
            if order:
                queried[cid] = order
                with self.service.database.transaction() as con:
                    row = self.service._order(con, account_id, cid)
                    intent = OrderIntent.from_dict(json.loads(row["body"]))
                    inst = self.service._instrument(con, account_id, intent.instrument_id)
                    if order.get("symbol") != inst.ticker or order.get("side") != intent.side or decimal(order.get("qty", "0")) != intent.quantity or order.get("type") != intent.order_type or order.get("time_in_force") != "day" or (row["broker_id"] is not None and row["broker_id"] != order.get("id")):
                        self.service._freeze(con, account_id, "BROKER_ORDER_SCOPE_MISMATCH")
                        continue
                    if row["broker_id"] is None:
                        con.execute("UPDATE trading_orders SET broker_id=? WHERE client_order_id=?", (order["id"], cid))
                    for barrier in con.execute("SELECT b.restore_id FROM trading_restore_barriers b LEFT JOIN trading_restore_resolutions r USING(account_id,restore_id,client_order_id) WHERE b.account_id=? AND b.client_order_id=? AND r.client_order_id IS NULL", (account_id, cid)).fetchall():
                        con.execute("INSERT INTO trading_restore_resolutions VALUES(?,?,?,?,?)", (account_id, barrier["restore_id"], cid, order["id"], stamp(datetime.now(timezone.utc))))
        # Match already-accounted executions to activities by order/quantity/price multiset;
        # stream execution_id and REST activity id are distinct provider identifiers.
        with closing(self.service.database.connect()) as con:
            accounted = Counter()
            for row in con.execute("SELECT body FROM trading_ledger WHERE account_id=? ORDER BY sequence", (account_id,)):
                value = json.loads(row["body"])
                if value["kind"] == "fill" and value["source"] == "alpaca:paper":
                    accounted[(value["order_id"], decimal(value["quantity"]), decimal(value["price"]))] += 1
        for fill in snapshot["fills"]:
            order = next((o for o in queried.values() if o["id"] == fill.get("order_id")), None)
            if order:
                key = (order["client_order_id"], decimal(fill["qty"]), decimal(fill["price"]))
                if accounted[key]:
                    accounted[key] -= 1
                    continue
                self.apply_event(principal, account_id, {"event": "partial_fill", "order": order,
                    "execution_id": "activity:" + fill["id"], "qty": fill["qty"], "price": fill["price"],
                    "timestamp": fill["transaction_time"]})
        for cid, order in queried.items():
            name = {"filled": "fill", "canceled": "canceled", "expired": "expired", "rejected": "rejected", "replaced": "replaced"}.get(order.get("status"), "accepted")
            if name == "fill":
                with self.service.database.transaction() as con:
                    row = self.service._order(con, account_id, cid)
                    if decimal(row["filled_qty"]) == decimal(order["filled_qty"]):
                        self.service._terminal(con, account_id, cid, "filled", datetime.now(timezone.utc))
            else:
                self.apply_event(principal, account_id, {"event": name, "order": order})
        return self.reconcile(principal, account_id, snapshot)
