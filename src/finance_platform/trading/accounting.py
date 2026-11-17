"""Deterministic USD cash accounting shared by research and simulation.

All amounts are Decimal; no network, filesystem or research data reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping
import re

ZERO = Decimal("0")
CENT = Decimal("0.01")


class TradingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def decimal(value: str | int | Decimal) -> Decimal:
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise TradingError("DECIMAL_REQUIRED")
    if isinstance(value, str) and not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,18})(?:\.[0-9]{1,12})?", value):
        raise TradingError("INVALID_DECIMAL")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise TradingError("INVALID_DECIMAL") from exc
    if not result.is_finite() or abs(result) > Decimal("1e18") or len(result.as_tuple().digits) > 36:
        raise TradingError("INVALID_DECIMAL")
    return result


def money(value: str | int | Decimal) -> Decimal:
    return decimal(value).quantize(CENT, rounding=ROUND_HALF_EVEN)


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    value, step = decimal(value), decimal(step)
    if step <= ZERO or value < ZERO:
        raise TradingError("INVALID_STEP")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class FeeSchedule:
    per_share: Decimal = Decimal("0.005")
    minimum: Decimal = Decimal("1.00")

    def __post_init__(self):
        object.__setattr__(self, "per_share", decimal(self.per_share))
        object.__setattr__(self, "minimum", money(self.minimum))
        if self.per_share < ZERO or self.minimum < ZERO:
            raise TradingError("INVALID_FEE")

    def fee(self, quantity: Decimal) -> Decimal:
        quantity = decimal(quantity)
        if quantity < ZERO:
            raise TradingError("INVALID_QUANTITY")
        return money(max(quantity * self.per_share, self.minimum)) if quantity else ZERO

    def incremental(self, before: Decimal, after: Decimal) -> Decimal:
        """Minimum per order, not per partial fill."""
        if decimal(after) < decimal(before):
            raise TradingError("FILL_REGRESSION")
        return self.fee(after) - self.fee(before)


def execution_price(side: str, observed_price: Decimal, tick_size: Decimal,
                    slippage_bps: Decimal = ZERO) -> Decimal:
    price, tick, slip = decimal(observed_price), decimal(tick_size), decimal(slippage_bps)
    if side not in {"buy", "sell"} or min(price, tick) <= ZERO or not ZERO <= slip < Decimal("10000"):
        raise TradingError("INVALID_EXECUTION")
    adjusted = price * (1 + slip / Decimal("10000") * (1 if side == "buy" else -1))
    return (adjusted / tick).to_integral_value(
        rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR) * tick


def executable_quantity(remaining: Decimal, volume: Decimal, participation: Decimal,
                        lot_size: Decimal) -> Decimal:
    remaining, volume, participation = map(decimal, (remaining, volume, participation))
    if min(remaining, volume) < ZERO or not ZERO <= participation <= 1:
        raise TradingError("INVALID_LIQUIDITY")
    return floor_step(min(remaining, volume * participation), decimal(lot_size))


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    kind: str
    instrument_id: str = ""
    strategy_id: str = ""
    side: str = ""
    quantity: Decimal = ZERO
    price: Decimal = ZERO
    fee: Decimal = ZERO
    amount: Decimal = ZERO
    order_id: str = ""
    source: str = ""

    def __post_init__(self):
        for key in ("quantity", "price", "fee", "amount"):
            object.__setattr__(self, key, decimal(getattr(self, key)))
        if not self.event_id or not self.source:
            raise TradingError("EVENT_PROVENANCE_REQUIRED")

    def to_dict(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in vars(self).items()}


@dataclass
class Position:
    quantity: Decimal = ZERO
    cost: Decimal = ZERO


@dataclass
class CashBook:
    initial_cash: Decimal
    cash: Decimal = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    contributions: dict[tuple[str, str], Position] = field(default_factory=dict)
    reservations: dict[str, Decimal] = field(default_factory=dict)
    sell_reservations: dict[str, tuple[str, Decimal]] = field(default_factory=dict)
    strategy_sell_reservations: dict[str, tuple[str, str, Decimal]] = field(default_factory=dict)
    unsettled: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    income: Decimal = ZERO
    net_flows: Decimal = ZERO
    pending_terminal: set[str] = field(default_factory=set)
    events: dict[str, LedgerEvent] = field(default_factory=dict)

    def __post_init__(self):
        self.initial_cash = money(self.initial_cash)
        if self.initial_cash < ZERO:
            raise TradingError("NEGATIVE_INITIAL_CASH")
        self.cash = self.initial_cash

    @property
    def reserved(self) -> Decimal:
        return sum(self.reservations.values(), ZERO)

    @property
    def available(self) -> Decimal:
        return self.cash - self.reserved - sum(self.unsettled.values(), ZERO)

    def available_quantity(self, instrument_id: str) -> Decimal:
        held = self.positions.get(instrument_id, Position()).quantity
        return held - sum((q for inst, q in self.sell_reservations.values() if inst == instrument_id), ZERO)

    def apply(self, event: LedgerEvent) -> bool:
        old = self.events.get(event.event_id)
        if old:
            if old != event:
                raise TradingError("EVENT_ID_COLLISION")
            return False
        e = event
        if e.kind == "reserve":
            if e.amount < ZERO or e.amount > self.available + self.reservations.get(e.order_id, ZERO):
                raise TradingError("INSUFFICIENT_CASH")
            self.reservations[e.order_id] = money(e.amount)
        elif e.kind == "reserve_sell":
            prior = self.sell_reservations.get(e.order_id, (e.instrument_id, ZERO))[1]
            if e.quantity <= ZERO or e.quantity > self.available_quantity(e.instrument_id) + prior:
                raise TradingError("SHORT_DISABLED")
            if e.amount < ZERO or e.amount > self.available + self.reservations.get(e.order_id, ZERO):
                raise TradingError("INSUFFICIENT_FEE_CASH")
            if e.amount > ZERO:
                self.reservations[e.order_id] = money(e.amount)
            self.sell_reservations[e.order_id] = (e.instrument_id, e.quantity)
            self.strategy_sell_reservations[e.order_id] = (e.strategy_id, e.instrument_id, e.quantity)
        elif e.kind == "release":
            self.reservations.pop(e.order_id, None)
            self.sell_reservations.pop(e.order_id, None)
            self.strategy_sell_reservations.pop(e.order_id, None)
        elif e.kind == "fill":
            if e.side not in {"buy", "sell"} or min(e.quantity, e.price) <= ZERO or e.fee < ZERO:
                raise TradingError("INVALID_FILL")
            position = self.positions.get(e.instrument_id, Position())
            contribution = self.contributions.get((e.strategy_id, e.instrument_id), Position())
            gross, fee = money(e.quantity * e.price), money(e.fee)
            if e.side == "buy":
                if self.available + self.reservations.get(e.order_id, ZERO) < gross + fee:
                    raise TradingError("INSUFFICIENT_CASH")
                self.cash -= gross + fee
                position = Position(position.quantity + e.quantity, position.cost + gross + fee)
                contribution = Position(contribution.quantity + e.quantity, contribution.cost + gross + fee)
            else:
                if position.quantity < e.quantity or contribution.quantity < e.quantity:
                    raise TradingError("SHORT_OR_CONTRIBUTION_DISABLED")
                if gross < fee and self.available + self.reservations.get(e.order_id, ZERO) < fee - gross:
                    raise TradingError("INSUFFICIENT_FEE_CASH")
                attributed = money(contribution.cost * e.quantity / contribution.quantity)
                removed = attributed
                self.cash += gross - fee
                if gross > fee:
                    self.unsettled[e.order_id] = self.unsettled.get(e.order_id, ZERO) + gross - fee
                self.realized_pnl += gross - fee - removed
                position = Position(position.quantity - e.quantity, position.cost - removed)
                contribution = Position(contribution.quantity - e.quantity, contribution.cost - attributed)
            self.positions[e.instrument_id] = position
            self.contributions[(e.strategy_id, e.instrument_id)] = contribution
            self.fees += fee
        elif e.kind == "cash":
            amount = money(e.amount)
            if self.available + amount < ZERO:
                raise TradingError("INSUFFICIENT_CASH")
            self.cash += amount
            self.net_flows += amount
        elif e.kind == "dividend":
            if e.amount < ZERO:
                raise TradingError("INVALID_DIVIDEND")
            self.cash += money(e.amount)
            self.income += money(e.amount)
        elif e.kind == "settle":
            self.unsettled.pop(e.order_id, None)
        elif e.kind == "split":
            if e.quantity <= ZERO:
                raise TradingError("INVALID_SPLIT")
            if self.reservations or self.sell_reservations:
                raise TradingError("OPEN_ORDERS_CORPORATE_ACTION")
            if e.instrument_id in self.positions:
                self.positions[e.instrument_id].quantity *= e.quantity
            for (strategy, inst), contribution in self.contributions.items():
                if inst == e.instrument_id:
                    contribution.quantity *= e.quantity
        elif e.kind == "terminal_pending":
            self.pending_terminal.add(e.instrument_id)
        else:
            raise TradingError("UNSUPPORTED_LEDGER_EVENT")
        self.events[e.event_id] = e
        return True

    def snapshot(self, prices: Mapping[str, Decimal]) -> dict:
        value, unrealized, unknown = ZERO, ZERO, []
        rows = []
        for inst, pos in sorted(self.positions.items()):
            if not pos.quantity:
                continue
            mark = prices.get(inst)
            if mark is None or inst in self.pending_terminal:
                unknown.append(inst)
                marked = None
            else:
                if decimal(mark) <= ZERO:
                    raise TradingError("INVALID_MARK")
                marked = money(pos.quantity * decimal(mark))
                value += marked
                unrealized += marked - pos.cost
            rows.append({"instrument_id": inst, "quantity": str(pos.quantity), "cost": str(pos.cost),
                         "market_value": None if marked is None else str(marked)})
        return {"cash": str(self.cash), "available_cash": str(self.available), "reserved_cash": str(self.reserved),
                "unsettled_cash": str(sum(self.unsettled.values(), ZERO)),
                "equity": None if unknown else str(self.cash + value), "positions": rows,
                "realized_pnl": str(self.realized_pnl), "unrealized_pnl": None if unknown else str(unrealized),
                "fees": str(self.fees), "income": str(self.income), "net_flows": str(self.net_flows),
                "unknown_valuations": unknown,
                "strategy_contributions": [{"strategy_id": s, "instrument_id": i, "quantity": str(p.quantity),
                                             "cost": str(p.cost)} for (s, i), p in sorted(self.contributions.items())]}


class PortfolioAllocator:
    """Explicit budget-weighted additivity, no last-writer conflict resolution."""
    @staticmethod
    def targets(equity: Decimal, budgets: Mapping[str, Decimal],
                weights: Mapping[str, Mapping[str, Decimal]], prices: Mapping[str, Decimal],
                lots: Mapping[str, Decimal]) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]]]:
        equity = decimal(equity)
        if equity <= ZERO or set(weights) != set(budgets):
            raise TradingError("INVALID_PORTFOLIO")
        parsed = {s: decimal(b) for s, b in budgets.items()}
        if any(not ZERO <= b <= 1 for b in parsed.values()) or sum(parsed.values(), ZERO) > 1:
            raise TradingError("BUDGET_EXCEEDED")
        total, contributions = {}, {}
        for strategy, target in weights.items():
            values = {i: decimal(w) for i, w in target.items()}
            if any(not ZERO <= w <= 1 for w in values.values()) or sum(values.values(), ZERO) > 1:
                raise TradingError("WEIGHT_EXCEEDED")
            contributions[strategy] = {}
            for inst, weight in values.items():
                price = decimal(prices[inst])
                if price <= ZERO:
                    raise TradingError("INVALID_MARK")
                qty = floor_step(equity * parsed[strategy] * weight / price, decimal(lots[inst]))
                contributions[strategy][inst] = qty
                total[inst] = total.get(inst, ZERO) + qty
        return total, contributions
