"""Value-only research receipts shared with the native executor at build time."""

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


CN_MARKET = "CN_EQUITY_RAW_CNY_DAILY_SIMULATION_1"
CN_PROVIDERS = {"tushare", "akshare_cn"}
CN_METADATA = {"currency": "CNY", "timezone": "Asia/Shanghai", "adjustment": "raw",
               "price_unit": "per_share", "volume_unit": "shares"}
CN_SIMULATION = {
    "market_semantics": CN_MARKET, "currency": "CNY", "price_basis": "RAW_PRICE_RETURN",
    "scope": "DEVELOPMENT_ONLY",
    "execution": "LONG_CASH_INTEGER_SHARES_NEXT_OBSERVED_OPEN",
    "unsupported": ["EXCHANGE_LOT_SIZE", "T_PLUS_ONE_SELL_RESTRICTION", "PRICE_LIMIT_FILLS",
                    "TAXES", "DIVIDENDS_AND_CORPORATE_ACTIONS", "TOTAL_RETURN"],
}


class ReceiptRejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ReceiptRejected(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def cn_market(value, tickers):
    require(type(value) is dict and set(value) == {
        "schema", "calendar", "securities", "required_sessions_sha256", "corporate_actions",
    } and value["schema"] == CN_MARKET, "CN_RESEARCH_EVIDENCE_REQUIRED")
    require(value["corporate_actions"] in ("UNVERIFIED", "VERIFIED_NONE"), "CN_RESEARCH_CORPORATE_ACTION_UNSUPPORTED")
    securities, calendar = value["securities"], value["calendar"]
    require(type(securities) is dict and 1 <= len(securities) <= 8 and set(securities) == set(tickers)
            and type(calendar) is dict, "CN_RESEARCH_IDENTITY_INVALID")
    require(type(value["required_sessions_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", value["required_sessions_sha256"]), "CN_RESEARCH_CALENDAR_INVALID")
    exchanges = set()
    for ticker, evidence in securities.items():
        require(type(ticker) is str and re.fullmatch(r"(?:6[0-9]{5}\.SH|[03][0-9]{5}\.SZ)", ticker),
                "CN_RESEARCH_IDENTITY_INVALID")
        exchange = "XSHG" if ticker.endswith(".SH") else "XSHE"
        require(type(evidence) is dict and set(evidence) == {"mic", "identity_sha256"}
                and evidence["mic"] == exchange and type(evidence["identity_sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", evidence["identity_sha256"]), "CN_RESEARCH_IDENTITY_INVALID")
        exchanges.add(exchange)
    require(set(calendar) == exchanges and all(type(version) is str and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", version) for version in calendar.values()), "CN_RESEARCH_CALENDAR_INVALID")
    return value


def _cn_coverage(body, market, symbols):
    dates = {symbol: [] for symbol in sorted(symbols)}
    window = body.get("window")
    require(type(window) is dict and type(window.get("start")) is str and type(window.get("end")) is str,
            "CN_RESEARCH_COVERAGE_INVALID")
    previous = None
    for row in body["rows"]:
        ticker, stamp = row["ticker"], row["session_date"]
        key = (ticker, stamp)
        try:
            valid_day = type(stamp) is str and date.fromisoformat(stamp).isoformat() == stamp
            raw_volume = row["volume"]
            volume = Decimal(raw_volume) if type(raw_volume) is str and len(raw_volume) <= 64 else Decimal("NaN")
            valid_volume = volume.is_finite() and 0 <= volume < 10**24 and volume == volume.to_integral_value()
        except (ValueError, InvalidOperation):
            valid_day = valid_volume = False
        require(valid_day and valid_volume and (previous is None or previous < key)
                and window["start"] <= stamp <= window["end"], "CN_RESEARCH_COVERAGE_INVALID")
        previous = key
        dates[ticker].append(stamp)
    first = next(iter(dates.values()))
    train = len(first) * 7 // 10
    require(80 <= train <= len(first) - 40 and all(value == first for value in dates.values())
            and digest(dates) == market["required_sessions_sha256"], "CN_RESEARCH_COVERAGE_INVALID")


def checked_package(package, *, engineering_only=False, allow_cn=False):
    require(type(package) is dict and set(package) == {"manifest", "payload"}, "INPUT_PACKAGE_INVALID")
    body, manifest = package["payload"], package["manifest"]
    require(type(body) is dict and body.get("format") == "finance-research-input-1",
            "INPUT_PACKAGE_INVALID")
    expected = {"format": body["format"], "sha256": digest(body), "bytes": len(encoded(body)),
                "rows": len(body["rows"]), "dataset_reference": body["dataset_reference"]}
    require(manifest == expected and body["rows_hash"] == digest(body["rows"]), "INPUT_PACKAGE_HASH_MISMATCH")
    scope = body["provenance"]["scope"]
    require(scope in {"synthetic", "public"} and (not engineering_only or scope == "synthetic"),
            "INPUT_SCOPE_UNAUTHORIZED")
    is_cn = "research_market" in body
    if is_cn:
        require(allow_cn, "CN_RESEARCH_DEVELOPMENT_ONLY")
        require(body["metadata"] == CN_METADATA and body["provenance"]["provider"] in CN_PROVIDERS,
                "CN_RESEARCH_UNITS_UNSUPPORTED")
    else:
        require(body["metadata"]["currency"] == "USD", "CURRENCY_UNSUPPORTED")
    require(body["metadata"]["price_unit"] in {"per_share", "per_adjusted_simulation_unit"},
            "PRICE_UNIT_UNSUPPORTED")
    symbols = set()
    for row in body["rows"]:
        require(set(row) == {"ticker", "session_date", "open", "high", "low", "close", "volume"},
                "EXPORTED_FEATURE_COLUMNS_INVALID")
        require(row["volume"] is not None, "INITIAL_NULL_VOLUME_UNSUPPORTED")
        symbols.add(row["ticker"])
    require(1 <= len(symbols) <= 8, "INITIAL_SYMBOL_CAP")
    receipt = {"manifest": manifest, "metadata": body["metadata"], "provenance": body["provenance"],
               "coverage": body["coverage"], "pit_status": body["pit_status"],
               "conversion": "DECIMAL_SOURCE_TO_ENGINE_FLOAT64_EXPLICIT"}
    if is_cn:
        market = cn_market(body["research_market"], symbols)
        require(body["universe"]["tickers"] == sorted(symbols), "CN_RESEARCH_IDENTITY_INVALID")
        _cn_coverage(body, market, symbols)
        receipt["research_market"] = market
    return body, symbols, receipt


def checked_packages(packages, *, engineering_only=False, allow_cn=False):
    require(type(packages) is list and 1 <= len(packages) <= 16, "INPUT_PACKAGES_INVALID")
    verified = []
    seen = set()
    units = None
    for package in packages:
        body, symbols, receipt = checked_package(package, engineering_only=engineering_only, allow_cn=allow_cn)
        require("research_market" not in receipt or len(packages) == 1, "CN_RESEARCH_SINGLE_DATASET_REQUIRED")
        current = receipt["metadata"]
        require(units is None or current == units, "INPUT_UNITS_MISMATCH")
        units = current
        require(seen.isdisjoint(symbols), "OVERLAPPING_SYMBOL_PACKAGES_UNSUPPORTED")
        seen.update(symbols)
        verified.append((body, receipt))
    require(1 <= len(seen) <= 8, "INITIAL_SYMBOL_CAP")
    return verified


def package_receipts(packages, *, engineering_only=False):
    return [receipt for _, receipt in checked_packages(packages, engineering_only=engineering_only)]
