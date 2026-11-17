"""Bounded data requirements and coverage for the existing dataset library."""
from dataclasses import dataclass
from datetime import date
import re

from finance_platform.data.contracts import DataError
from finance_platform.data.simple_inputs import validate_rows

REF_FIELDS = ("dataset_id", "version", "content_hash")
MAX_SYMBOLS = 32
MAX_DAYS = 366
MAX_ROWS = 10000


@dataclass(frozen=True)
class Capability:
    provider: str
    market: str
    assets: tuple[str, ...]
    currency: str
    timezone: str
    adjustments: tuple[str, ...]
    credentials: bool = False

    def view(self, *, runtime_ready=False, credential_configured=False):
        return dict(provider=self.provider, market=self.market, assets=list(self.assets),
                    kind="bars", frequency="1Day", currency=self.currency, timezone=self.timezone,
                    adjustment=list(self.adjustments),
                    credentials="REQUIRED" if self.credentials else "NONE",
                    entitlement="UNVERIFIED" if self.credentials else "PUBLIC_UNVERIFIED",
                    coverage="UNVERIFIED",
                    availability=("RUNTIME_UNAVAILABLE" if not runtime_ready else
                                  "CREDENTIAL_REQUIRED" if self.credentials and not credential_configured else
                                  "BOUND_UNVERIFIED"))


US_ASSETS = ("US_EQUITY", "US_ETF")
US_ZONE = "America/New_York"
CN_ZONE = "Asia/Shanghai"
CAPABILITIES = {
    c.provider: c for c in (
        Capability("tushare", "CN", ("CN_EQUITY",), "CNY", CN_ZONE, ("raw",), True),
        Capability("akshare_cn", "CN", ("CN_EQUITY",), "CNY", CN_ZONE, ("raw",)),
        Capability("openbb_nasdaq", "US", ("US_EQUITY",), "USD", US_ZONE, ("provider_observed_unverified",)),
        Capability("yfinance", "US", US_ASSETS, "USD", US_ZONE, ("split_adjusted",)),
        Capability("akshare", "US", ("US_EQUITY",), "USD", US_ZONE, ("qfq",)),
        Capability("massive", "US", US_ASSETS, "USD", US_ZONE, ("raw", "split_adjusted"), True),
        Capability("openbb_cboe", "US", US_ASSETS, "USD", US_ZONE, ("provider_observed_unverified",)),
    )
}


def day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except (TypeError, ValueError):
        pass
    raise DataError("GOAL_DATA_DATE_INVALID")


def symbol(value, market):
    pattern = r"(?:6[0-9]{5}\.SH|[03][0-9]{5}\.SZ)" if market == "CN" else r"[A-Z][A-Z0-9.-]{0,15}"
    return type(value) is str and re.fullmatch(pattern, value) is not None


@dataclass(frozen=True)
class Unit:
    symbol: str
    start: str
    end: str


@dataclass(frozen=True)
class Requirement:
    market: str
    asset: str
    kind: str
    frequency: str
    currency: str
    adjustment: str
    start: str
    end: str
    symbols: tuple[str, ...]
    keys: frozenset[tuple[str, str]]

    @classmethod
    def parse(cls, value, *, today):
        fields = ("market", "asset", "kind", "frequency", "currency", "adjustment", "start", "end")
        if type(value) is not dict or set(value) != set(fields) | {"symbols", "required_sessions"}:
            raise DataError("GOAL_DATA_REQUIREMENT_INVALID")
        if value["market"] not in ("US", "CN") or value["kind"] != "bars" or value["frequency"] != "1Day":
            raise DataError("GOAL_DATA_REQUIREMENT_UNSUPPORTED")
        start, end = day(value["start"]), day(value["end"])
        if not start <= end < today or (end-start).days >= MAX_DAYS:
            raise DataError("GOAL_DATA_DATE_INVALID")
        symbols, sessions = value["symbols"], value["required_sessions"]
        if (type(symbols) is not list or not 1 <= len(symbols) <= MAX_SYMBOLS
                or not all(symbol(item, value["market"]) for item in symbols)
                or len(set(symbols)) != len(symbols) or type(sessions) is not dict or set(sessions) != set(symbols)):
            raise DataError("GOAL_DATA_SYMBOL_INVALID")
        keys = set()
        for ticker in symbols:
            dates = sessions[ticker]
            if type(dates) is not list or not 1 <= len(dates) <= MAX_DAYS:
                raise DataError("GOAL_DATA_SESSIONS_REQUIRED")
            if any(not start <= day(item) <= end for item in dates) or len(set(dates)) != len(dates):
                raise DataError("GOAL_DATA_DATE_INVALID")
            keys.update((ticker, item) for item in dates)
        if len(keys) > MAX_ROWS:
            raise DataError("GOAL_DATA_ROW_BUDGET")
        return cls(*(value[field] for field in fields), tuple(sorted(symbols)), frozenset(keys))

    def require_capability(self, provider):
        cap = CAPABILITIES.get(provider)
        if (cap is None or self.market != cap.market or self.asset not in cap.assets
                or self.currency != cap.currency or self.adjustment not in cap.adjustments):
            raise DataError("GOAL_DATA_REQUIREMENT_UNSUPPORTED")
        return cap


def missing_units(requirement, rows):
    observed = {(row["ticker"], row["session_date"]) for row in rows}
    units = []
    pending = None
    for ticker, current in sorted(requirement.keys):
        if pending is not None and (pending.symbol != ticker or (ticker, current) in observed):
            units.append(pending)
            pending = None
        if (ticker, current) not in observed:
            pending = Unit(ticker, pending.start if pending else current, current)
    if pending is not None:
        units.append(pending)
    return tuple(units)


def merge_rows(requirement, rows, *, allow_missing_volume=False):
    selected = {}
    for row in rows:
        key = (row["ticker"], row["session_date"])
        if key not in requirement.keys:
            raise DataError("GOAL_DATA_RESPONSE_SCOPE")
        if key in selected and selected[key] != row:
            raise DataError("GOAL_DATA_SOURCE_CONFLICT")
        selected[key] = row
    if selected.keys() != requirement.keys:
        raise DataError("GOAL_DATA_MISSING_SESSIONS")
    return validate_rows(list(selected.values()), requirement.start, requirement.end,
                         list(requirement.symbols), allow_missing_volume=allow_missing_volume)
