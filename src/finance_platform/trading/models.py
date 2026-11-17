from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from finance_platform.core.contracts import (DataScope, Instrument, RunMode, identifier,
                                             instant, parse_decimal, payload, Provenance)
from .accounting import TradingError, ZERO, decimal


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    account_id: str
    strategy_id: str
    instrument_id: str
    side: str
    quantity: Decimal
    order_type: str
    effective_at: datetime
    mode: RunMode = RunMode.LOCAL_SIMULATION
    limit_price: Decimal | None = None
    time_in_force: str = "day"
    source: str = "strategy"

    def __post_init__(self):
        for name in ("client_order_id", "account_id", "strategy_id", "instrument_id"):
            identifier(getattr(self, name))
        object.__setattr__(self, "effective_at", instant(self.effective_at))
        object.__setattr__(self, "quantity", decimal(self.quantity))
        if self.quantity <= ZERO or self.side not in {"buy", "sell"}:
            raise TradingError("INVALID_ORDER")
        if self.order_type not in {"market", "limit"} or self.time_in_force != "day":
            raise TradingError("UNSUPPORTED_ORDER_TYPE")
        if self.order_type == "limit":
            if self.limit_price is None or decimal(self.limit_price) <= ZERO:
                raise TradingError("INVALID_LIMIT")
            object.__setattr__(self, "limit_price", decimal(self.limit_price))
        elif self.limit_price is not None:
            raise TradingError("INVALID_LIMIT")
        if type(self.mode) is not RunMode or self.mode not in {RunMode.LOCAL_SIMULATION, RunMode.BROKER_PAPER}:
            raise TradingError("TRADING_MODE_DISABLED")
        if self.source not in {"strategy", "admin", "external", "rebalance"}:
            raise TradingError("INVALID_ORDER_SOURCE")

    @classmethod
    def from_dict(cls, data: dict):
        from finance_platform.core.contracts import parse_instant
        v = dict(data)
        v["quantity"] = parse_decimal(v["quantity"])
        v["limit_price"] = None if v.get("limit_price") is None else parse_decimal(v["limit_price"])
        v["effective_at"] = parse_instant(v["effective_at"])
        v["mode"] = RunMode(v["mode"])
        return cls(**v)


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    instrument_id: str
    event_at: datetime
    available_at: datetime
    price: Decimal
    volume: Decimal
    provenance: Provenance
    session_open: bool = True

    def __post_init__(self):
        identifier(self.event_id)
        identifier(self.instrument_id)
        for key in ("event_at", "available_at"):
            object.__setattr__(self, key, instant(getattr(self, key)))
        for key in ("price", "volume"):
            object.__setattr__(self, key, decimal(getattr(self, key)))
        if self.price <= ZERO or self.volume < ZERO or type(self.session_open) is not bool:
            raise TradingError("INVALID_MARKET_EVENT")
        if type(self.provenance) is not Provenance or self.provenance.scope not in {DataScope.SYNTHETIC, DataScope.DEVELOPMENT}:
            raise TradingError("UNAUTHORIZED_MARKET_EVENT")
        if self.available_at < max(self.event_at, self.provenance.available_at):
            raise TradingError("MARKET_AVAILABILITY_INVALID")

    @classmethod
    def from_dict(cls, data: dict):
        from finance_platform.core.contracts import parse_instant
        v = dict(data)
        for k in ("event_at", "available_at"):
            v[k] = parse_instant(v[k])
        for k in ("price", "volume"):
            v[k] = parse_decimal(v[k])
        p = dict(v["provenance"])
        p["scope"] = DataScope(p["scope"])
        for k in ("effective_at", "available_at", "received_at", "valid_to"):
            p[k] = None if p[k] is None else parse_instant(p[k])
        v["provenance"] = Provenance(**p)
        return cls(**v)


def validate_instrument_order(instrument: Instrument, intent: OrderIntent):
    if type(instrument) is not Instrument or instrument.instrument_id != intent.instrument_id:
        raise TradingError("INSTRUMENT_MISMATCH")
    if not instrument.tradable or not instrument.ticker_valid_from <= intent.effective_at or (
        instrument.ticker_valid_to is not None and intent.effective_at >= instrument.ticker_valid_to):
        raise TradingError("INSTRUMENT_NOT_TRADABLE")
    if intent.quantity % instrument.lot_size or (not instrument.fractional and intent.quantity % 1):
        raise TradingError("QUANTITY_PRECISION")
    if intent.limit_price is not None and intent.limit_price % instrument.tick_size:
        raise TradingError("PRICE_PRECISION")
