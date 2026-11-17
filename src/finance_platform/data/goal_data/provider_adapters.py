"""Official SDK adapters for an admitted child with a bound transport."""
import asyncio
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from unittest.mock import patch

from finance_platform.data.contracts import DataError
from finance_platform.data.simple_inputs import validate_rows

from .goal_data import day, symbol

TS_FIELDS = ("ts_code", "trade_date", "open", "high", "low", "close", "vol")
TS_SDK_URL = "http://api.waditu.com/dataapi"
AK_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
NASDAQ_URL = "https://api.nasdaq.com/api"


def error(code):
    raise DataError(code)


def _guard_unit(unit, market):
    if not symbol(unit.symbol, market) or not 0 <= (day(unit.end)-day(unit.start)).days < 366:
        error("GOAL_DATA_RESPONSE_SCOPE")


def _decimal(value, multiplier=1):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 64:
        error("GOAL_DATA_RESPONSE_INVALID")
    try:
        number = Decimal(value)
        if not number.is_finite() or abs(number) >= Decimal("1e24") or abs(number.as_tuple().exponent) > 24:
            error("GOAL_DATA_RESPONSE_INVALID")
        if multiplier == 1:
            return format(number, "f")
        shares = number * multiplier
        if shares != shares.to_integral_value():
            error("GOAL_DATA_RESPONSE_INVALID")
        return format(shares.to_integral_value(), "f")
    except InvalidOperation:
        error("GOAL_DATA_RESPONSE_INVALID")


def _rows(rows, unit, *, allow_missing_volume=False):
    if not rows:
        return []
    span = (day(unit.end) - day(unit.start)).days + 1
    if len(rows) > span or any(not unit.start <= row["session_date"] <= unit.end for row in rows):
        error("GOAL_DATA_RESPONSE_SCOPE")
    return validate_rows(rows, unit.start, unit.end, [unit.symbol], allow_missing_volume=allow_missing_volume)


def _bar(unit, stamp, values, volume, *, multiplier=1):
    return dict(ticker=unit.symbol, session_date=stamp,
                **dict(zip(("open", "high", "low", "close"), map(_decimal, values), strict=True)),
                volume=None if volume is None else _decimal(volume, multiplier))


def project_tushare(body, unit):
    _guard_unit(unit, "CN")
    if type(body) is not dict or type(body.get("code")) is not int:
        error("GOAL_DATA_RESPONSE_INVALID")
    if body["code"] != 0:
        error("GOAL_DATA_ACCESS_REJECTED" if body["code"] == 2002 else "GOAL_DATA_PROVIDER_REJECTED")
    data = body.get("data")
    if type(data) is not dict or data.get("fields") != list(TS_FIELDS) or type(data.get("items")) is not list:
        error("GOAL_DATA_RESPONSE_INVALID")
    rows = []
    for item in data["items"]:
        if type(item) is not list or len(item) != len(TS_FIELDS) or item[0] != unit.symbol:
            error("GOAL_DATA_RESPONSE_SCOPE")
        try:
            stamp = datetime.strptime(item[1], "%Y%m%d").date()
            if stamp.strftime("%Y%m%d") != item[1]:
                raise ValueError()
        except (TypeError, ValueError):
            error("GOAL_DATA_RESPONSE_INVALID")
        rows.append(_bar(unit, stamp.isoformat(), item[2:6], item[6], multiplier=100))
    return _rows(rows, unit)


def project_akshare(body, unit):
    _guard_unit(unit, "CN")
    data = body.get("data") if type(body) is dict else None
    code, exchange = unit.symbol.split(".")
    if type(data) is not dict or data.get("code") != code or data.get("market") != int(exchange == "SH"):
        error("GOAL_DATA_RESPONSE_SCOPE")
    if type(data.get("klines")) is not list:
        error("GOAL_DATA_RESPONSE_INVALID")
    rows = []
    for raw in data["klines"]:
        if type(raw) is not str or len(raw) > 512:
            error("GOAL_DATA_RESPONSE_INVALID")
        values = raw.split(",")
        if len(values) != 11:
            error("GOAL_DATA_RESPONSE_INVALID")
        stamp, opening, closing, high, low, volume = values[:6]
        rows.append(_bar(unit, day(stamp).isoformat(), (opening, high, low, closing), volume, multiplier=100))
    return _rows(rows, unit)


def project_nasdaq(data, unit):
    _guard_unit(unit, "US")
    if type(data) is not dict or data.get("symbol") != unit.symbol:
        error("GOAL_DATA_RESPONSE_SCOPE")
    table = data.get("tradesTable")
    if type(table) is not dict or type(table.get("rows")) is not list:
        error("GOAL_DATA_RESPONSE_INVALID")
    rows = []
    for row in table["rows"]:
        try:
            stamp = datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat()
            values = [row[key].removeprefix("$").replace(",", "") for key in ("open", "high", "low", "close")]
            volume = row.get("volume")
            volume = None if volume in (None, "", "--", "N/A") else volume.replace(",", "")
        except (TypeError, ValueError, KeyError, AttributeError):
            error("GOAL_DATA_RESPONSE_INVALID")
        rows.append(_bar(unit, stamp, values, volume))
    return _rows(rows, unit, allow_missing_volume=True)


def _agree(rows, projected):
    if len(rows) != len(projected):
        error("GOAL_DATA_SDK_TRANSFORM_MISMATCH")
    for row, transformed in zip(rows, projected, strict=True):
        if row["session_date"] != transformed["session_date"]:
            error("GOAL_DATA_SDK_TRANSFORM_MISMATCH")
        for field in ("open", "high", "low", "close", "volume"):
            expected, actual = row[field], transformed[field]
            if expected is None:
                if actual is not None:
                    error("GOAL_DATA_SDK_TRANSFORM_MISMATCH")
            elif actual is None or float(expected) != float(actual):
                error("GOAL_DATA_SDK_TRANSFORM_MISMATCH")


def collect_tushare(unit, transport, *, token):
    from tushare.pro import client

    _guard_unit(unit, "CN")
    if not isinstance(token, str) or not token:
        error("GOAL_DATA_CREDENTIAL_REQUIRED")
    args = dict(ts_code=unit.symbol, start_date=unit.start.replace("-", ""), end_date=unit.end.replace("-", ""))
    rows = None

    def post(url, *, json, timeout):
        import json as codec
        nonlocal rows
        if rows is not None or url != TS_SDK_URL + "/daily" or timeout != 5 or json != {
            "api_name": "daily", "token": token, "fields": ",".join(TS_FIELDS),
            "params": {**args, "ts_type_name": TS_SDK_URL},
        }:
            error("GOAL_DATA_SDK_INTERFACE_CHANGED")
        # The admitted transport owns the approved TLS target and token handling.
        body = transport("tushare", unit, json)
        rows = project_tushare(body, unit)
        return SimpleNamespace(text=codec.dumps(body, default=str))

    with patch.object(client, "requests", SimpleNamespace(post=post)):
        frame = client.DataApi(token, timeout=5).daily(fields=",".join(TS_FIELDS), **args)
    if rows is None:
        error("GOAL_DATA_SDK_INTERFACE_CHANGED")
    sdk = [dict(session_date=datetime.strptime(item["trade_date"], "%Y%m%d").date().isoformat(),
                **{k: item[k] for k in ("open", "high", "low", "close")}, volume=Decimal(str(item["vol"])) * 100)
           for item in frame.to_dict("records")]
    _agree(rows, sorted(sdk, key=lambda item: item["session_date"]))
    return rows


def collect_akshare(unit, transport):
    from akshare.stock_feature import stock_hist_em

    _guard_unit(unit, "CN")
    code, exchange = unit.symbol.split(".")
    start, end = unit.start.replace("-", ""), unit.end.replace("-", "")
    rows = None

    def get(url, *, params, timeout):
        nonlocal rows
        expected = dict(fields1="f1,f2,f3,f4,f5,f6",
                        fields2="f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                        ut="7eea3edcaed734bea9cbfc24409ed989", klt="101", fqt="0",
                        secid=f"{int(exchange == 'SH')}.{code}", beg=start, end=end)
        if rows is not None or url != AK_URL or timeout != 5 or params != expected:
            error("GOAL_DATA_SDK_INTERFACE_CHANGED")
        body = transport("akshare_cn", unit, params)
        rows = project_akshare(body, unit)
        return SimpleNamespace(json=lambda: body)

    with patch.object(stock_hist_em, "requests", SimpleNamespace(get=get)):
        frame = stock_hist_em.stock_zh_a_hist(code, period="daily", start_date=start,
                                              end_date=end, adjust="", timeout=5)
    if rows is None:
        error("GOAL_DATA_SDK_INTERFACE_CHANGED")
    sdk = [dict(session_date=item["日期"].isoformat(), open=item["开盘"], high=item["最高"],
                low=item["最低"], close=item["收盘"], volume=Decimal(str(item["成交量"])) * 100)
           for item in frame.to_dict("records")]
    _agree(rows, sorted(sdk, key=lambda item: item["session_date"]))
    return rows


def collect_nasdaq(unit, transport):
    from openbb_nasdaq.models.equity_historical import NasdaqEquityHistoricalFetcher as Fetcher
    from openbb_nasdaq.utils import helpers

    _guard_unit(unit, "US")
    rows = None
    failure = None

    async def read(path, use_cache=True, **kwargs):
        nonlocal rows, failure
        try:
            expected = (f"quote/{unit.symbol}/historical?assetclass=stocks"
                        f"&fromdate={unit.start}&todate={unit.end}&limit=10000")
            if rows is not None or path != expected or kwargs:
                error("GOAL_DATA_SDK_INTERFACE_CHANGED")
            reply = transport("openbb_nasdaq", unit, path)
            if type(reply) is not dict or type(reply.get("status")) is not dict or reply["status"].get("rCode") != 200:
                error("GOAL_DATA_PROVIDER_REJECTED")
            data = reply.get("data")
            rows = project_nasdaq(data, unit)
            return data
        except DataError as exc:
            failure = exc
            raise DataError("GOAL_DATA_PROVIDER_REJECTED") from None

    query = Fetcher.transform_query(dict(symbol=unit.symbol, start_date=unit.start, end_date=unit.end))
    with patch.object(helpers, "get_nasdaq_data", read):
        try:
            raw = asyncio.run(Fetcher.aextract_data(query, None))
        except Exception:
            if failure is not None:
                raise failure from None
            if rows == []:
                return []
            raise DataError("GOAL_DATA_SDK_TRANSFORM_MISMATCH") from None
    if rows is None or failure is not None:
        error("GOAL_DATA_SDK_INTERFACE_CHANGED")
    sdk = [{**item.model_dump(), "session_date": item.date.isoformat()}
           for item in Fetcher.transform_data(query, raw)]
    _agree(rows, sdk)
    return rows
