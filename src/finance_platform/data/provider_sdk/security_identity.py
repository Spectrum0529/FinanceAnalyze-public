"""Current provider metadata binding, never a historical security master."""
from datetime import datetime, timedelta, timezone
import hashlib

from finance_platform.core.contracts import fingerprint
from finance_platform.data.contracts import DataError
from .contracts import checked_json
from .history_contracts import symbol, sina_symbol, query_text

EXCHANGES = {"NMS": ("XNAS", "XNAS"), "NGM": ("XNAS", "XNAS"), "NCM": ("XNAS", "XNAS"),
             "NAS": ("XNAS", "XNAS"), "NYQ": ("XNYS", "XNYS"), "NYSE": ("XNYS", "XNYS"),
             "PCX": ("ARCX", "XNYS"), "ASE": ("XASE", "XNYS"), "BTS": ("BATS", "XNYS"), "IEX": ("IEXG", "XNYS")}


def lookup_call(ticker):
    ticker = symbol(ticker)
    return {"provider": "yfinance", "provider_symbol": ticker, "ticker": ticker, "operation": "identity",
            "asset_class": "UNRESOLVED", "max_rows": 5, "timeframe": "1Day"}


def search_call(query):
    return {"provider": "yfinance", "provider_symbol": "SEARCH", "ticker": "SEARCH", "operation": "search",
            "query": query_text(query), "asset_class": "UNRESOLVED", "max_rows": 10, "timeframe": "1Day"}


def _name(value, fallback):
    if type(value) is str and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value):
        return value
    return fallback


def identity_from_entity(call, entity):
    try:
        meta = checked_json(entity["body"])["chart"]["result"][0]["meta"]
        ticker = symbol(call["ticker"])
        exchange = meta["exchangeName"]
        if (meta["symbol"] != ticker or meta.get("currency") != "USD" or meta.get("instrumentType") not in {"EQUITY", "ETF"}
                or exchange not in EXCHANGES or meta.get("exchangeTimezoneName") != "America/New_York"):
            raise DataError("SECURITY_MARKET_OR_IDENTITY_UNSUPPORTED")
        mic, calendar = EXCHANGES[exchange]
        asset = "US_ETF" if meta["instrumentType"] == "ETF" else "US_EQUITY"
        first = meta.get("firstTradeDate")
        if first is not None:
            if type(first) is not int or first < -2208988800 or first > int(datetime.now(timezone.utc).timestamp()):
                raise DataError("SECURITY_FIRST_TRADE_HINT_INVALID")
            first = (datetime(1970, 1, 1, tzinfo=timezone.utc)+timedelta(seconds=first)).date().isoformat()
        binding = {"ticker": ticker, "asset_class": asset, "mic": mic, "currency": "USD", "first_trade_hint": first}
        instrument = {"instrument_id": "observed-us-" + fingerprint(binding)[:40], "ticker": ticker,
                      "asset_class": asset, "mic": mic, "currency": "USD", "tick_size": "0.01", "lot_size": "1",
                      "fractional": False, "calendar_id": calendar, "settlement": "T+1",
                      "ticker_valid_from": "1900-01-01T00:00:00Z", "ticker_valid_to": None, "tradable": False}
        return {"schema_version": "provider-security-identity-1", "ticker": ticker,
                "name": _name(meta.get("longName", meta.get("shortName")), ticker), "instrument": instrument,
                "provider_symbols": {"yfinance": ticker, "akshare": ticker if sina_symbol(ticker) else None},
                "semantic_hash": fingerprint(binding), "provider_first_trade_hint": first,
                "authority": "YAHOO_CHART_CURRENT_METADATA", "source_entity_sha256": hashlib.sha256(entity["body"]).hexdigest(),
                "observed_received_at": entity["observed_received_at"], "historical_identity_verified": False,
                "assumptions": ["NAMESPACE_VALIDITY_NOT_HISTORICAL_SYMBOL_VALIDITY", "CURRENT_CALENDAR_AND_SETTLEMENT_ASSUMPTIONS",
                                "CURRENT_META_NOT_HISTORICAL_SECURITY_MASTER"]}
    except DataError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        raise DataError("SECURITY_IDENTITY_UNAVAILABLE") from None


def search_from_entity(entity):
    data = checked_json(entity["body"])
    values = data.get("quotes")
    if type(values) is not list or len(values) > 10:
        raise DataError("SECURITY_SEARCH_RESPONSE_INVALID")
    rows, seen = [], set()
    for item in values:
        if type(item) is not dict or item.get("quoteType") not in {"EQUITY", "ETF"} or item.get("exchange") not in EXCHANGES:
            continue
        try:
            ticker = symbol(item.get("symbol"))
        except DataError:
            continue
        if ticker != item.get("symbol") or ticker in seen:
            continue
        seen.add(ticker)
        rows.append({"ticker": ticker, "name": _name(item.get("longname", item.get("shortname")), ticker),
                     "asset_class_hint": "US_ETF" if item["quoteType"] == "ETF" else "US_EQUITY",
                     "identity_verified": False, "requires_exact_chart_identity_lookup": True})
    return rows
