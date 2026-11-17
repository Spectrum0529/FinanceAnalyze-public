"""Use the pinned AKShare exchange-list functions for A-share identity."""
from datetime import date, datetime
from io import BytesIO
from zipfile import BadZipFile, ZipFile
from types import SimpleNamespace
from unittest.mock import patch

from finance_platform.data.contracts import DataError
from .goal_data import day, symbol

SZ_URL = "https://www.szse.cn/api/report/ShowReport"
SH_URL = "https://query.sse.com.cn/sseQuery/commonQuery.do"
SSE_REFERER = "https://www.sse.com.cn/assortment/stock/list/share/"


def validate_xlsx(raw):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries) > 100 or sum(entry.file_size for entry in entries) > 16 * 1024**2
                    or any(entry.flag_bits & 1 for entry in entries)):
                raise DataError("GOAL_DATA_REFERENCE_INVALID")
    except BadZipFile:
        raise DataError("GOAL_DATA_REFERENCE_INVALID") from None
    return raw


def roster_category(ticker):
    if not symbol(ticker, "CN"):
        raise DataError("GOAL_DATA_SYMBOL_INVALID")
    if ticker.endswith(".SZ"):
        return "A股列表"
    return "科创板" if ticker.startswith(("688", "689")) else "主板A股"


def collect_roster(unit, transport):
    from akshare.stock import stock_info

    category = roster_category(unit.symbol)
    shanghai = category != "A股列表"
    function = stock_info.stock_info_sh_name_code if shanghai else stock_info.stock_info_sz_name_code
    if shanghai:
        params = {"STOCK_TYPE": "8" if category == "科创板" else "1", "REG_PROVINCE": "", "CSRC_CODE": "",
                  "STOCK_CODE": "", "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L", "COMPANY_STATUS": "2,4,5,7,8",
                  "type": "inParams", "isPagination": "true", "pageHelp.cacheSize": "1", "pageHelp.beginPage": "1",
                  "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1", "pageHelp.endPage": "1"}
    else:
        params = dict(SHOWTYPE="xlsx", CATALOGID="1110", TABKEY="tab1", random="0.6935816432433362")

    def get(url, *, params, headers=None, timeout=None):
        expected = SH_URL if shanghai else SZ_URL
        if (url != expected or params != expected_params or
                (shanghai and (timeout is not None or not headers or headers.get("Referer") != SSE_REFERER)) or
                (not shanghai and (headers is not None or timeout != 15))):
            raise DataError("GOAL_DATA_SDK_INTERFACE_CHANGED")
        data = transport(url, params, shanghai)
        return SimpleNamespace(json=lambda: data) if shanghai else SimpleNamespace(content=data)

    expected_params = params
    with patch.object(stock_info, "requests", SimpleNamespace(get=get)):
        frame = function.__wrapped__(symbol=category)
    code, listing = ("证券代码", "上市日期") if shanghai else ("A股代码", "A股上市日期")
    if len(frame) > 10000 or code not in frame or listing not in frame:
        raise DataError("GOAL_DATA_IDENTITY_UNVERIFIED")
    # Keep the complete matching board in the child observation to reuse once.
    suffix = ".SH" if shanghai else ".SZ"
    result = []
    for item in frame[[code, listing]].itertuples(index=False, name=None):
        ticker = str(item[0]) + suffix
        if symbol(ticker, "CN") and isinstance(item[1], (str, date)):
            try:
                value = item[1]
                stamp = value.date() if isinstance(value, datetime) else value
                stamp = day(stamp) if isinstance(stamp, str) else stamp
                stamp = day(stamp.isoformat()).isoformat()
            except (DataError, ValueError):
                continue
            result.append(dict(symbol=ticker, listed_on=stamp))
    return result


def listed_identity(rows, unit, observed_date, evidence):
    selected = [row for row in rows if row["symbol"] == unit.symbol]
    if len(selected) != 1 or not selected[0]["listed_on"] <= unit.start <= unit.end <= observed_date:
        raise DataError("GOAL_DATA_IDENTITY_UNVERIFIED")
    return dict(symbol=unit.symbol, mic="XSHG" if unit.symbol.endswith(".SH") else "XSHE",
                asset="CN_EQUITY", valid_from=selected[0]["listed_on"], valid_through=observed_date,
                evidence=dict(operation="akshare_exchange_list", observed_listing="CURRENT_LIST", capture=evidence))
