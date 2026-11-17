"""Explicit WRDS values and CIZ trade-bar projection shared by worker handlers."""
import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import re

from finance_platform.core.contracts import canonical, identifier
from .contracts import DataError
from .simple_inputs import validate_rows
from .wrds_transport import FETCH_ROWS, MAX_IDENTITIES, MAX_ROWS, PsycopgTransport, run_bounded


PROFILE = "crsp_ciz_us_daily_trade_v1"
SCOPE = "licensed_local"
META = {"currency": "USD", "timezone": "America/New_York", "adjustment": "raw",
        "price_unit": "per_share", "volume_unit": "shares"}
MAX_SECURITIES = 8
MAX_DAYS = 3660
MAX_BYTES = 8 * 1024 * 1024
HISTORY_SECONDS = 60
LOOKUP_SECONDS = 30
_ASSET = {("EQTY", "COM"): "US_EQUITY", ("FUND", "ETF"): "US_ETF"}
_TICKER = re.compile(r"[A-Z][A-Z0-9.-]{0,15}")
_USERNAME = re.compile(r"[A-Za-z0-9_.@-]{1,128}")


@dataclass(frozen=True)
class WrdsCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self):
        if (type(self.username) is not str or not _USERNAME.fullmatch(self.username)
                or type(self.password) is not str or not 1 <= len(self.password) <= 1024
                or "\0" in self.password or any(0xD800 <= ord(c) <= 0xDFFF for c in self.password)):
            raise DataError("WRDS_CREDENTIAL_REQUIRED")

    @classmethod
    def from_value(cls, value):
        if type(value) is cls:
            return value
        if type(value) is dict and set(value) == {"username", "password"}:
            return cls(**value)
        return cls(getattr(value, "username", None), getattr(value, "password", None))


@dataclass(frozen=True)
class Security:
    ticker: str
    permno: int


def _day(value, *, today):
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value and date(1925, 1, 1) <= day < today:
            return day
    except (ValueError, TypeError):
        pass
    raise DataError("WRDS_INPUT_INVALID")


def _request(value, names):
    if type(value) is not dict or set(value) != names:
        raise DataError("WRDS_INPUT_INVALID")
    try:
        identifier(value["connection_id"])
    except (ValueError, TypeError):
        raise DataError("WRDS_INPUT_INVALID") from None


def lookup_request(value, *, today=None):
    _request(value, {"connection_id", "query", "as_of", "limit"})
    query, limit = value["query"], value["limit"]
    if type(query) is not str or len(query) > 32 or type(limit) is not int or not 1 <= limit <= 50:
        raise DataError("WRDS_INPUT_INVALID")
    query = query.strip().upper()
    if re.fullmatch(r"[1-9][0-9]{0,8}", query):
        query = int(query)
    elif not _TICKER.fullmatch(query):
        raise DataError("WRDS_INPUT_INVALID")
    return query, _day(value["as_of"], today=today or datetime.now(timezone.utc).date()), limit


def history_request(value, *, today=None):
    _request(value, {"connection_id", "name", "securities", "start", "end"})
    today = today or datetime.now(timezone.utc).date()
    start, end = (_day(value[key], today=today) for key in ("start", "end"))
    name, items = value["name"], value["securities"]
    if (not 0 <= (end - start).days < MAX_DAYS
            or type(name) is not str or not 1 <= len(name) <= 120
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in name)
            or not name.strip() or type(items) is not list or not 1 <= len(items) <= MAX_SECURITIES):
        raise DataError("WRDS_INPUT_INVALID")
    securities = []
    for item in items:
        if (type(item) is not dict or set(item) != {"ticker", "permno"}
                or type(item["ticker"]) is not str or not _TICKER.fullmatch(item["ticker"])
                or type(item["permno"]) is not int or not 1 <= item["permno"] <= 999_999_999):
            raise DataError("WRDS_INPUT_INVALID")
        securities.append(Security(**item))
    if len({item.ticker for item in securities}) != len(securities) or len(
            {item.permno for item in securities}) != len(securities):
        raise DataError("WRDS_SECURITY_BINDING")
    return tuple(sorted(securities, key=lambda item: item.ticker)), start, end


def _identity(row):
    if len(row) != 7:
        raise DataError("WRDS_RESPONSE_INVALID")
    permno, ticker, start, end, exchange, kind, subtype = row
    if (type(permno) is not int or not 1 <= permno <= 999_999_999
            or type(start) is not date or type(end) is not date or start > end):
        raise DataError("WRDS_RESPONSE_INVALID")
    if (type(ticker) is not str or not _TICKER.fullmatch(ticker)
            or type(exchange) is not str or not re.fullmatch("[A-Z]{1,3}", exchange)
            or (kind, subtype) not in _ASSET):
        raise DataError("WRDS_SECURITY_BINDING")
    return {"permno": permno, "ticker": ticker, "valid_from": start.isoformat(),
            "valid_to": end.isoformat(), "primary_exchange": exchange,
            "asset": _ASSET[kind, subtype]}


def _numeric(value):
    if type(value) not in (Decimal, int, float):
        raise DataError("WRDS_OHLC_UNAVAILABLE")
    number = Decimal(str(value))
    fields = number.as_tuple()
    if (not number.is_finite() or len(fields.digits) > 36
            or not -24 <= fields.exponent <= 24):
        raise DataError("WRDS_OHLC_UNAVAILABLE")
    return format(number, "f")


class WrdsSource:
    def __init__(self, transport=None):
        self.transport = transport if transport is not None else PsycopgTransport()

    def lookup(self, context, credentials, request):
        query, as_of, limit = request

        async def read(session):
            table, _ = await session.probe(history=False)
            rows = await session.lookup(table, query, as_of, limit + 1)
            if len(rows) > limit + 1:
                raise DataError("WRDS_DATA_LIMIT")
            items = [_identity(row) for row in rows]
            day = as_of.isoformat()
            field = "permno" if type(query) is int else "ticker"
            if any(item[field] != query or not item["valid_from"] <= day <= item["valid_to"] for item in items):
                raise DataError("WRDS_SECURITY_BINDING")
            return {"items": items[:limit], "truncated": len(items) > limit,
                    "as_of": day, "profile": PROFILE}

        result = run_bounded(context, LOOKUP_SECONDS, lambda: self.transport.run(credentials, read))
        context.require_active()
        return result

    def history(self, context, credentials, request, sink):
        securities, start, end = request
        low, high = start.isoformat(), end.isoformat()
        by_id = {security.permno: security.ticker for security in securities}

        async def read(session):
            names, bars = await session.probe(history=True)
            identities = [_identity(row) for row in
                          await session.history_identities(names, securities, start, end)]
            if len(identities) > MAX_IDENTITIES:
                raise DataError("WRDS_DATA_LIMIT")
            if (len(identities) != len(securities) or
                    {item["permno"]: item["ticker"] for item in identities} != by_id or
                    any(not item["valid_from"] <= low <= high <= item["valid_to"] for item in identities)):
                raise DataError("WRDS_SECURITY_BINDING")
            identities.sort(key=lambda item: item["ticker"])
            seen, row_count, byte_count, previous = set(), 0, 0, None
            async with aclosing(session.history_batches(bars, securities, start, end)) as batches:
                async for batch in batches:
                    context.require_active()
                    if not 0 < len(batch) <= FETCH_ROWS or row_count + len(batch) > MAX_ROWS:
                        raise DataError("WRDS_DATA_LIMIT")
                    rows, evidence = [], []
                    for raw_row in batch:
                        if len(raw_row) != 8:
                            raise DataError("WRDS_RESPONSE_INVALID")
                        permno, day, opening, high_price, low_price, close, volume, flag = raw_row
                        if type(day) is not date or not start <= day <= end or permno not in by_id:
                            raise DataError("WRDS_RESPONSE_INVALID")
                        ticker = by_id[permno]
                        key = (ticker, day)
                        if previous is not None and key <= previous:
                            raise DataError("WRDS_RESPONSE_INVALID")
                        previous = key
                        if flag != "TR":
                            raise DataError("WRDS_OHLC_UNAVAILABLE")
                        values = [_numeric(number) for number in (opening, high_price, low_price, close, volume)]
                        evidence.append([permno, day.isoformat(), *values, flag])
                        rows.append(dict(zip(("ticker", "session_date", "open", "high", "low", "close", "volume"),
                                             [ticker, day.isoformat(), *values], strict=True)))
                    try:
                        rows = validate_rows(rows, low, high, sorted({row["ticker"] for row in rows}))
                    except DataError:
                        raise DataError("WRDS_OHLC_UNAVAILABLE") from None
                    source_bytes = canonical(evidence).encode("utf-8")
                    byte_count += len(source_bytes)
                    if byte_count > MAX_BYTES:
                        raise DataError("WRDS_DATA_LIMIT")
                    row_count += len(rows)
                    seen.update(row["ticker"] for row in rows)
                    sink(source_bytes, rows)
                    await asyncio.sleep(0)
            if not row_count:
                raise DataError("WRDS_HISTORY_EMPTY")
            if seen != set(by_id.values()):
                raise DataError("WRDS_SECURITY_BINDING")
            return {"profile": PROFILE, "identity_relation": names, "daily_relation": bars,
                    "identities": identities, "rows": row_count, "source_bytes": byte_count,
                    "pit_status": "UNVERIFIED", "coverage": "PROVIDER_RESPONSE_ONLY"}

        result = run_bounded(context, HISTORY_SECONDS, lambda: self.transport.run(credentials, read))
        context.require_active()
        return result
