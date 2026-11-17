"""Typed Tushare reference operations using the same admitted SDK and TLS port."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from json import dumps

from finance_platform.data.contracts import DataError
from .goal_data import day, symbol
from .provider_adapters import TS_SDK_URL

FIELDS = {
    "stock_basic": ("ts_code", "exchange", "market", "curr_type", "list_status", "list_date", "delist_date"),
    "trade_cal": ("exchange", "cal_date", "is_open"),
}
EXCHANGES = {"SH": ("SSE", "XSHG"), "SZ": ("SZSE", "XSHE")}
BOARD = {"SH": {"主板", "科创板"}, "SZ": {"主板", "创业板", "中小板"}}


def parameters(operation, unit):
    suffix = unit.symbol.split(".")[-1]
    if operation not in FIELDS or not symbol(unit.symbol, "CN") or not 0 <= (day(unit.end)-day(unit.start)).days < 366:
        raise DataError("GOAL_DATA_REFERENCE_INVALID")
    exchange, _ = EXCHANGES[suffix]
    if operation == "stock_basic":
        return dict(ts_code=unit.symbol, exchange=exchange, list_status="L")
    return dict(exchange=exchange, start_date=unit.start.replace("-", ""), end_date=unit.end.replace("-", ""))


def _date(value):
    if type(value) is not str or len(value) != 8 or not value.isascii() or not value.isdigit():
        raise DataError("GOAL_DATA_REFERENCE_INVALID")
    return day(value[:4] + "-" + value[4:6] + "-" + value[6:]).isoformat()


def collect_reference(operation, unit, transport, token):
    from tushare.pro import client

    args = parameters(operation, unit)
    fields = FIELDS[operation]
    seen = False

    def post(url, *, json, timeout):
        nonlocal seen
        if seen or url != TS_SDK_URL + "/" + operation or timeout != 5 or json != {
            "api_name": operation, "token": token, "fields": ",".join(fields),
            "params": {**args, "ts_type_name": TS_SDK_URL},
        }:
            raise DataError("GOAL_DATA_SDK_INTERFACE_CHANGED")
        seen = True
        body = transport("tushare", unit, json)
        if type(body) is not dict or type(body.get("code")) is not int:
            raise DataError("GOAL_DATA_REFERENCE_INVALID")
        if body["code"] != 0:
            raise DataError("GOAL_DATA_ACCESS_REJECTED" if body["code"] == 2002 else "GOAL_DATA_PROVIDER_REJECTED")
        data = body.get("data")
        if type(data) is not dict or data.get("fields") != list(fields) or type(data.get("items")) is not list:
            raise DataError("GOAL_DATA_REFERENCE_INVALID")
        return SimpleNamespace(text=dumps(body, default=str))

    with patch.object(client, "requests", SimpleNamespace(post=post)):
        frame = client.DataApi(token, timeout=5).query(operation, fields=",".join(fields), **args)
    if not seen:
        raise DataError("GOAL_DATA_SDK_INTERFACE_CHANGED")
    return frame.to_dict("records")


def calendar_records(records, unit):
    exchange, mic = EXCHANGES[unit.symbol.split(".")[-1]]
    start, end = day(unit.start), day(unit.end)
    expected = {(start + timedelta(days=offset)).isoformat() for offset in range((end-start).days + 1)}
    flags = {}
    for row in records:
        stamp = _date(row["cal_date"])
        if row["exchange"] != exchange or stamp not in expected or stamp in flags or type(row["is_open"]) not in {int, str} or row["is_open"] not in {0, 1, "0", "1"}:
            raise DataError("GOAL_DATA_CALENDAR_UNAVAILABLE")
        flags[stamp] = int(row["is_open"])
    if set(flags) != expected:
        raise DataError("GOAL_DATA_CALENDAR_UNAVAILABLE")
    return mic, [stamp for stamp, flag in sorted(flags.items()) if flag]


def identity_record(records, unit, *, received_date, evidence):
    if len(records) != 1:
        raise DataError("GOAL_DATA_IDENTITY_UNVERIFIED")
    row = records[0]
    suffix = unit.symbol.split(".")[-1]
    exchange, mic = EXCHANGES[suffix]
    if (row["ts_code"] != unit.symbol or row["exchange"] != exchange or row["market"] not in BOARD[suffix]
            or row["curr_type"] not in {"CNY", "人民币"} or row["list_status"] != "L" or row["delist_date"] not in {None, ""}):
        raise DataError("GOAL_DATA_IDENTITY_UNVERIFIED")
    listed = _date(row["list_date"])
    if not listed <= unit.start <= unit.end <= received_date:
        raise DataError("GOAL_DATA_IDENTITY_UNVERIFIED")
    return dict(symbol=unit.symbol, mic=mic, asset="CN_EQUITY", valid_from=listed, valid_through=received_date,
                evidence=dict(operation="stock_basic", observed_listing="L", delisted_on=None, capture=evidence))
