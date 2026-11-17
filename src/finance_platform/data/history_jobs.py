"""Search/download in the existing durable registry; no retry scheduler."""
from datetime import date, timedelta
from finance_platform.core.contracts import RunMode
from finance_platform.data.contracts import DataError
from .history_gateway import HistoryGateway
from .provider_sdk.history_contracts import history_window, symbol, query_text


def validate_download(value):
    if type(value) is not dict or not {"ticker", "start_date", "end_date"} <= set(value) or set(value)-{"ticker", "start_date", "end_date", "provider", "history_policy", "history_id", "history_version"}:
        raise DataError("HISTORY_REQUEST_INVALID")
    symbol(value["ticker"])
    try:
        end = date.fromisoformat(value["end_date"])
        if end.isoformat() != value["end_date"]: raise ValueError
        history_window(value["start_date"], (end+timedelta(days=1)).isoformat())
    except (ValueError, TypeError): raise DataError("SDK_DATE_RANGE_VIOLATION") from None
    if value.get("provider", "yfinance") not in {"yfinance", "akshare"} or value.get("history_policy", "reuse_or_download") not in {"reuse_or_download", "refresh"}: raise DataError("HISTORY_REQUEST_INVALID")
    version = value.get("history_version")
    if version is not None and (type(version) is not int or version < 1): raise DataError("HISTORY_VERSION_INVALID")


def register_history_handlers(registry, runtime, service, runner):
    gateway = HistoryGateway(service, runner)
    def validate_search(params):
        if type(params) is not dict or set(params) != {"query"}: raise DataError("SECURITY_SEARCH_QUERY_INVALID")
        query_text(params["query"])
    def download(context, params):
        value = gateway.obtain(context, params)
        summary = gateway.summary(context.run.owner_id, value["history_id"], value["version"])
        cache = {k: v for k, v in value["history_cache"].items() if k != "selected_row_indices"}
        return {"history": summary, "cache": cache}
    registry.register("data.history_search", lambda c, p: gateway.search(c, p["query"]), validate=validate_search, recoverable=False, auto_recovery_limit=0, allowed_modes=(RunMode.BACKTEST,))
    registry.register("data.history_download", download, validate=validate_download, recoverable=False, auto_recovery_limit=0, allowed_modes=(RunMode.BACKTEST,))
