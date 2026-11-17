"""Allowed market events drive deterministic, persistent partial fills."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from copy import deepcopy

from finance_platform.core.contracts import DataScope, RunMode, canonical, instant
from .accounting import (FeeSchedule, LedgerEvent, TradingError, ZERO, decimal, money,
                         execution_price, executable_quantity)
from .models import MarketEvent, OrderIntent
from .service import TERMINAL, settlement_at, stamp
from .storage import load_book, append_event
from .risk import RiskLimits, RiskService, regular_session
from .risk_configuration import policy, intent_policy
from .execution_clock import require_forward, advance
from finance_platform.core.contracts import parse_instant


class SimulationBroker:
    def __init__(self, service, participation="0.10", slippage_bps="0"):
        self.service = service
        self.participation, self.slippage_bps = decimal(participation), decimal(slippage_bps)
        if not ZERO < self.participation <= 1 or not ZERO <= self.slippage_bps < 10000:
            raise TradingError("INVALID_SIMULATION_CONFIG")

    def on_market(self, principal, account_id, event: MarketEvent, now=None):
        now = instant(now or datetime.now(timezone.utc))
        if type(event) is not MarketEvent or event.available_at > now:
            raise TradingError("MARKET_EVENT_NOT_AVAILABLE")
        if event.provenance.scope is DataScope.DEVELOPMENT:
            if self.service.data_authorizer is None or not self.service.data_authorizer(event):
                raise TradingError("DEVELOPMENT_DATA_PERMISSION_REQUIRED")
        service = self.service
        fills = []
        with service.database.transaction() as con:
            account = service._account(con, account_id, principal, True)
            service._require_started(account, now)
            service._require_started(account, event.event_at)
            if account["mode"] != RunMode.LOCAL_SIMULATION.value:
                raise TradingError("SIMULATION_MODE_MISMATCH")
            instrument = service._instrument(con, account_id, event.instrument_id)
            old = con.execute("SELECT body FROM trading_market WHERE account_id=? AND event_id=?", (account_id, event.event_id)).fetchone()
            if old:
                if old["body"] != canonical(event):
                    raise TradingError("MARKET_EVENT_ID_COLLISION")
                return []
            previous = service._markets(con, account_id, now).get(event.instrument_id)
            clock = require_forward(con, account, now)
            late = event.event_at < parse_instant(clock["watermark"]) or previous is not None and event.event_at <= previous.event_at
            advance(con, account, "market:" + event.event_id, now, {"market": json.loads(canonical(event)), "matching": "OBSERVATION_ONLY" if late else "ELIGIBLE"})
            con.execute("INSERT INTO trading_market(account_id,event_id,instrument_id,body) VALUES(?,?,?,?)", (account_id, event.event_id, event.instrument_id, canonical(event)))
            book = load_book(con, account)
            service._settle(con, account, book, now)
            if late or account["frozen"] or service._research_action_blocked(con, account_id):
                return []
            if not instrument.tradable or event.event_at < instrument.ticker_valid_from or (
                instrument.ticker_valid_to is not None and event.event_at >= instrument.ticker_valid_to) or event.instrument_id in book.pending_terminal or not event.session_open or not regular_session(event.event_at, instrument.calendar_id):
                return []
            capacity = executable_quantity(event.volume, event.volume, self.participation, instrument.lot_size)
            rows = con.execute("SELECT o.*,i.body,i.created_at FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? ORDER BY i.created_at,i.client_order_id", (account_id,)).fetchall()
            fees = FeeSchedule(**json.loads(account["fee_json"]))
            for row in rows:
                intent = OrderIntent.from_dict(json.loads(row["body"]))
                if intent.instrument_id != event.instrument_id or row["status"] in TERMINAL or row["status"] in {"unknown", "sending"}:
                    continue
                if event.event_at <= max(intent.effective_at, parse_instant(row["created_at"])):
                    continue
                if row["status"] == "queued" and intent.side == "buy" and account["emergency"]:
                    continue
                if event.event_at.astimezone(ZoneInfo("America/New_York")).date() > intent.effective_at.astimezone(ZoneInfo("America/New_York")).date():
                    service._terminal(con, account_id, intent.client_order_id, "expired", now)
                    continue
                if row["status"] == "cancel_pending":
                    service._terminal(con, account_id, intent.client_order_id, "cancelled", now)
                    con.execute("UPDATE trading_outbox SET status='done' WHERE client_order_id=? AND action='cancel'", (intent.client_order_id,))
                    continue
                if row["status"] == "queued":
                    candidate = deepcopy(book)
                    candidate.reservations.pop(intent.client_order_id, None)
                    candidate.sell_reservations.pop(intent.client_order_id, None)
                    candidate.strategy_sell_reservations.pop(intent.client_order_id, None)
                    markets = service._markets(con, account_id, now)
                    try:
                        for limits in (intent_policy(con, account, intent.client_order_id), policy(con, account)[0]):
                            RiskService(limits, fees).check(intent, instrument, event, candidate,
                                {i: e.price for i, e in markets.items()}, now,
                                accepted_last_minute=0, emergency_stop=bool(account["emergency"]), frozen=False,
                                day_baseline=decimal(account["baseline"]), day_high=decimal(account["high"]), day_flows=decimal(account["day_flows"]))
                    except TradingError as exc:
                        service._terminal(con, account_id, intent.client_order_id, "rejected", now)
                        con.execute("INSERT INTO trading_order_events(account_id,event_id,client_order_id,body,at) VALUES(?,?,?,?,?)",
                            (account_id, "dispatch-risk:" + intent.client_order_id + ":" + event.event_id, intent.client_order_id,
                             canonical({"event": "rejected", "code": exc.code, "boundary": "risk:before_sim_dispatch"}), stamp(now)))
                        book = load_book(con, account)
                        continue
                con.execute("UPDATE trading_outbox SET status='done' WHERE client_order_id=? AND action='submit'", (intent.client_order_id,))
                con.execute("UPDATE trading_orders SET status='new',broker_id=? WHERE client_order_id=? AND status='queued'", ("sim:" + intent.client_order_id, intent.client_order_id))
                price = execution_price(intent.side, event.price, instrument.tick_size, self.slippage_bps)
                if intent.limit_price is not None and ((intent.side == "buy" and price > intent.limit_price) or (intent.side == "sell" and price < intent.limit_price)):
                    continue
                before = decimal(row["filled_qty"])
                quantity = executable_quantity(intent.quantity - before, capacity, decimal("1"), instrument.lot_size)
                if quantity == ZERO:
                    continue
                fee = fees.incremental(before, before + quantity)
                fill = LedgerEvent("sim-fill:" + intent.client_order_id + ":" + event.event_id, "fill",
                    instrument_id=intent.instrument_id, strategy_id=intent.strategy_id, side=intent.side,
                    quantity=quantity, price=price, fee=fee, order_id=intent.client_order_id,
                    source=event.provenance.source_id + ":" + event.provenance.content_hash)
                try:
                    book.apply(fill)
                except TradingError:
                    service._terminal(con, account_id, intent.client_order_id, "rejected", now)
                    append_event(con, account_id, LedgerEvent("sim-reject:" + intent.client_order_id + ":" + event.event_id, "release", order_id=intent.client_order_id, source="sim:cash-or-quantity"), stamp(now))
                    book = load_book(con, account)
                    continue
                append_event(con, account_id, fill, stamp(now))
                filled = before + quantity
                total_fee = decimal(row["fees"]) + fee
                status = "filled" if filled == intent.quantity else "partial"
                due = settlement_at(event.event_at) if intent.side == "sell" else None
                con.execute("UPDATE trading_orders SET status=?,filled_qty=?,filled_notional=?,fees=?,settlement_at=? WHERE client_order_id=?",
                    (status, str(filled), str(decimal(row["filled_notional"]) + money(quantity * price)), str(total_fee),
                     None if due is None else stamp(due), intent.client_order_id))
                release = LedgerEvent("sim-release:" + fill.event_id, "release", order_id=intent.client_order_id, source="sim:fill")
                book.apply(release)
                append_event(con, account_id, release, stamp(now))
                if status != "filled":
                    remaining = intent.quantity - filled
                    maxprice = intent.limit_price or price * (1 + max(intent_policy(con, account, intent.client_order_id).market_reserve_buffer, policy(con, account)[0].market_reserve_buffer))
                    reserve = LedgerEvent("sim-reserve:" + fill.event_id, "reserve" if intent.side == "buy" else "reserve_sell",
                        instrument_id=intent.instrument_id, strategy_id=intent.strategy_id, quantity=remaining,
                        amount=(money(remaining * maxprice) + fees.incremental(filled, intent.quantity)) if intent.side == "buy" else
                               max(ZERO, fees.incremental(filled, intent.quantity) - money(remaining * price * (1 - policy(con, account)[0].market_reserve_buffer))),
                        order_id=intent.client_order_id, source="sim:remaining")
                    try:
                        book.apply(reserve)
                        append_event(con, account_id, reserve, stamp(now))
                    except TradingError:
                        service._terminal(con, account_id, intent.client_order_id, "rejected", now)
                fills.append(fill.to_dict())
                capacity -= quantity
            # Mark high-water even if no order was submitted at this price.
            limits = policy(con, account)[0]
            prices = {i: e.price for i, e in service._markets(con, account_id, now).items()}
            equity = book.snapshot(prices)["equity"]
            if equity is not None:
                day = now.astimezone(ZoneInfo(limits.reset_timezone)).date().isoformat()
                flows = decimal(account["day_flows"]) if account["day"] == day else book.net_flows
                baseline = decimal(account["baseline"]) if account["day"] == day else decimal(equity)
                high = max(decimal(account["high"]), decimal(equity) - book.net_flows + flows) if account["day"] == day else decimal(equity)
                con.execute("UPDATE trading_accounts SET day=?,baseline=?,high=?,day_flows=? WHERE account_id=?", (day, str(baseline), str(high), str(flows), account_id))
        return fills
