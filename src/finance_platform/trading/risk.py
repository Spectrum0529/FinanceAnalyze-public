"""Pure risk decisions. Service persists rate limits/day baseline under a transaction."""
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from finance_platform.core.contracts import Instrument, RunMode, instant
from .accounting import CashBook, FeeSchedule, TradingError, decimal, money, ZERO
from .models import OrderIntent, MarketEvent, validate_instrument_order


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal("10000")
    max_account_gross: Decimal = Decimal("100000")
    max_strategy_gross: Decimal = Decimal("50000")
    max_concentration: Decimal = Decimal("0.50")
    max_price_deviation: Decimal = Decimal("0.10")
    market_reserve_buffer: Decimal = Decimal("0.10")
    max_daily_loss: Decimal = Decimal("0.05")
    max_drawdown: Decimal = Decimal("0.10")
    max_orders_per_minute: int = 20
    max_data_age_seconds: int = 120
    reset_timezone: str = "America/New_York"

    def __post_init__(self):
        for f in fields(self):
            if f.name not in {"max_orders_per_minute", "max_data_age_seconds", "reset_timezone"}:
                value = decimal(getattr(self, f.name))
                if value <= ZERO:
                    raise TradingError("INVALID_RISK_LIMIT")
                object.__setattr__(self, f.name, value)
        for key in ("max_concentration", "max_price_deviation", "market_reserve_buffer", "max_daily_loss", "max_drawdown"):
            if not ZERO < getattr(self, key) <= 1:
                raise TradingError("INVALID_RISK_LIMIT")
        if type(self.max_orders_per_minute) is not int or self.max_orders_per_minute < 1:
            raise TradingError("INVALID_RISK_LIMIT")
        if type(self.max_data_age_seconds) is not int or self.max_data_age_seconds < 1:
            raise TradingError("INVALID_RISK_LIMIT")
        if self.reset_timezone != "America/New_York":
            raise TradingError("UNSUPPORTED_RISK_TIMEZONE")

    @classmethod
    def from_dict(cls, data: dict):
        defaults = cls()
        return cls(**{k: decimal(v) if isinstance(getattr(defaults, k), Decimal) else v for k, v in data.items()})


class RiskService:
    def __init__(self, limits: RiskLimits, fees: FeeSchedule):
        self.limits, self.fees = limits, fees

    def check(self, intent: OrderIntent, instrument: Instrument, market: MarketEvent,
              book: CashBook, prices: dict[str, Decimal], now: datetime, *,
              accepted_last_minute: int, emergency_stop: bool, frozen: bool,
              day_baseline: Decimal, day_high: Decimal, day_flows: Decimal) -> Decimal:
        now = instant(now)
        validate_instrument_order(instrument, intent)
        if frozen:
            raise TradingError("ACCOUNT_RECONCILIATION_REQUIRED")
        if market.instrument_id != intent.instrument_id or intent.effective_at > now:
            raise TradingError("INVALID_ORDER_CLOCK")
        if market.available_at > now or now < market.event_at or (
            now - market.event_at).total_seconds() > self.limits.max_data_age_seconds:
            raise TradingError("STALE_MARKET_DATA")
        if not market.session_open or not regular_session(now, instrument.calendar_id):
            raise TradingError("OUTSIDE_TRADING_SESSION")
        if accepted_last_minute >= self.limits.max_orders_per_minute:
            raise TradingError("ORDER_RATE_LIMIT")
        snapshot = book.snapshot(prices)
        if snapshot["equity"] is None:
            raise TradingError("VALUATION_UNKNOWN")
        equity = decimal(snapshot["equity"])
        adjusted = equity - (book.net_flows - day_flows)
        breached = (day_baseline > ZERO and adjusted <= day_baseline * (1 - self.limits.max_daily_loss)) or (
            day_high > ZERO and adjusted <= day_high * (1 - self.limits.max_drawdown))
        if intent.side == "buy" and (emergency_stop or breached):
            raise TradingError("EMERGENCY_STOP" if emergency_stop else "LOSS_LIMIT")
        price = intent.limit_price or market.price * (1 + self.limits.market_reserve_buffer)
        if intent.limit_price is not None and abs(price / market.price - 1) > self.limits.max_price_deviation:
            raise TradingError("PRICE_DEVIATION")
        notional = money(price * intent.quantity)
        if notional > self.limits.max_order_notional:
            raise TradingError("ORDER_NOTIONAL_LIMIT")
        if intent.instrument_id in book.pending_terminal:
            raise TradingError("TERMINAL_PENDING")
        if intent.side == "buy":
            reservation = notional + self.fees.fee(intent.quantity)
            if reservation > book.available:
                raise TradingError("INSUFFICIENT_CASH")
            position = next((p for p in snapshot["positions"] if p["instrument_id"] == intent.instrument_id), None)
            current = decimal(position["market_value"]) if position else ZERO
            instrument_reserved = sum((amount for order, amount in book.reservations.items()), ZERO)
            # Conservative account-wide pending buy amount counts against each concentration.
            if equity <= ZERO or current + notional + instrument_reserved > equity * self.limits.max_concentration:
                raise TradingError("CONCENTRATION_LIMIT")
            gross = sum((decimal(p["market_value"]) for p in snapshot["positions"]), ZERO)
            if gross + book.reserved + notional > self.limits.max_account_gross:
                raise TradingError("ACCOUNT_EXPOSURE_LIMIT")
            strategy_gross = sum((p.quantity * prices[i] for (s, i), p in book.contributions.items()
                                  if s == intent.strategy_id and p.quantity), ZERO)
            if strategy_gross + book.reserved + notional > self.limits.max_strategy_gross:
                raise TradingError("STRATEGY_EXPOSURE_LIMIT")
            return reservation
        if intent.quantity > book.available_quantity(intent.instrument_id):
            raise TradingError("SHORT_DISABLED")
        contribution = book.contributions.get((intent.strategy_id, intent.instrument_id))
        strategy_reserved = sum((q for s, i, q in book.strategy_sell_reservations.values()
                                 if s == intent.strategy_id and i == intent.instrument_id), ZERO)
        if contribution is None or intent.quantity > contribution.quantity - strategy_reserved:
            raise TradingError("STRATEGY_POSITION_LIMIT")
        minimum_proceeds = money(intent.quantity * (intent.limit_price or market.price * (1 - self.limits.market_reserve_buffer)))
        fee_cash = max(ZERO, self.fees.fee(intent.quantity) - minimum_proceeds)
        if fee_cash > book.available:
            raise TradingError("INSUFFICIENT_FEE_CASH")
        return fee_cash


def regular_session(now: datetime, calendar_id: str = "XNYS") -> bool:
    """Use locked exchange calendar including holidays, DST and early closes."""
    import exchange_calendars as xcals
    import pandas as pd
    try:
        calendar = xcals.get_calendar(calendar_id)
        return bool(calendar.is_open_on_minute(pd.Timestamp(instant(now)).floor("min"), ignore_breaks=True))
    except (ValueError, IndexError):
        return False
