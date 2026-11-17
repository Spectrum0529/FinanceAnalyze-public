"""Versioned long-history capacity; network/process limits remain legacy limits."""
from datetime import date, datetime, timezone
import re

from finance_platform.data.contracts import DataError

HISTORY_PROFILE = "history-daily-2"
HISTORY_DAYS = 11346
HISTORY_ROWS = 9000
HISTORY_EARLIEST = "1900-01-02"
SINA_FULL_ROWS = 40000
SINA_JSON_BYTES = 8 * 1024**2
YAHOO_PRICE_POLICY = "YAHOO_NATIVE_SPLIT_ADJUSTED_PRICE_PATH_V1"
SINA_PRICE_POLICY = "SINA_QFQ_AFFINE_OBSERVED_V1"
SINA_QFQ_INTERFACE = "stock_us_daily_sina_qfq_affine_v1"
NATIVE_INTERFACE = "stock_us_daily_native_qfq_v1"
NATIVE_SOURCE_ID = "history-v4-akshare-stock-us-daily-native-qfq"
NATIVE_PRICE_POLICY = "AKSHARE_STOCK_US_DAILY_NATIVE_QFQ_ROUND4_V1"
QUANTITY_BASIS = "ADJUSTED_SIMULATION_UNITS"


def symbol(value):
    if type(value) is not str:
        raise DataError("SECURITY_SYMBOL_INVALID")
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,13}(?:[.-][A-Z0-9]{1,3})?", value) or len(value) > 18:
        raise DataError("SECURITY_SYMBOL_INVALID")
    return value


def sina_symbol(value):
    return type(value) is str and re.fullmatch(r"[A-Z][A-Z0-9]{0,17}", value) is not None


def query_text(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9 .&'\-]{1,64}", value.strip()):
        raise DataError("SECURITY_SEARCH_QUERY_INVALID")
    return value.strip()


def history_window(start, end, *, today=None):
    try:
        if any(type(v) is not str or date.fromisoformat(v).isoformat() != v for v in (start, end)):
            raise ValueError
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        raise DataError("SDK_DATE_RANGE_VIOLATION") from None
    if start < HISTORY_EARLIEST or not 0 < (b-a).days <= HISTORY_DAYS or b > (today or datetime.now(timezone.utc).date()):
        raise DataError("SDK_DATE_RANGE_VIOLATION")
    return a, b


def output_limits(call=None):
    profile = (call or {}).get("profile")
    if profile not in {None, HISTORY_PROFILE}:
        raise DataError("SDK_PROFILE_UNSUPPORTED")
    if profile == HISTORY_PROFILE:
        return {"observations": 32*1024**2, "snapshot": 8*1024**2, "total": 48*1024**2, "nodes": 2000000}
    return {"observations": 2*1024**2, "snapshot": 2*1024**2, "total": 12*1024**2, "nodes": 100000}


def artifact_limit(name, call=None):
    limits = output_limits(call)
    return limits["observations"] if name == "observations.json" else limits["snapshot"] if name == "sdk-frame.json" else 2*1024**2
