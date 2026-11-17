"""Saved, identified ETF proxies. Only accepted dataset versions are compared."""
from finance_platform.core.contracts import identifier
from finance_platform.data.contracts import DataError
from finance_platform.data.provider_sdk.history_contracts import YAHOO_PRICE_POLICY

from .adjusted_portfolio import acquisition
from .portfolio import benchmark, fail


OPTIONS = {
    "sp500": ("SPY", "标普500（ETF SPY）"),
    "nasdaq100": ("QQQ", "纳斯达克100（ETF QQQ）"),
    "csi300": ("ASHR", "沪深300（美股ETF ASHR）"),
}


def catalog():
    return [{"id": key, "ticker": ticker, "label": label} for key, (ticker, label) in OPTIONS.items()]


def selections(items):
    if type(items) is not list or not 1 <= len(items) <= len(OPTIONS):
        fail("BENCHMARK_SELECTION_INVALID")
    seen = set()
    for item in items:
        if type(item) is not dict or set(item) != {"id", "dataset_id"}:
            fail("BENCHMARK_SELECTION_INVALID")
        key = item["id"]
        if type(key) is not str or key not in OPTIONS or key in seen:
            fail("BENCHMARK_SELECTION_INVALID")
        try:
            identifier(item["dataset_id"])
        except ValueError:
            fail("BENCHMARK_SELECTION_INVALID")
        seen.add(key)
    return items


def identity(dataset, key):
    ticker, label = OPTIONS[key]
    meta = dataset.get("metadata", {})
    supplied = dataset.get("benchmark_identity")
    if (dataset.get("kind") != "benchmark" or dataset.get("scope") != "public"
            or dataset.get("source", {}).get("provider") != "yfinance"
            or dataset.get("universe", {}).get("tickers") != [ticker]
            or meta.get("currency") != "USD" or (dataset.get("benchmark") or {}).get("return_basis") != "price"):
        fail("BENCHMARK_PROXY_DATASET_MISMATCH")
    from finance_platform.data.benchmark_sources import capture_identity
    try:
        expected = capture_identity("yfinance", [ticker], meta, dataset.get("security_metadata"))
    except DataError:
        fail("BENCHMARK_PROXY_IDENTITY_UNVERIFIED")
    evidence = dataset.get("row_storage", {}).get("acquisition", {}).get("securities", [])
    if (expected is None or supplied != expected or expected["index_id"] != key
            or len(evidence) != 1 or evidence[0].get("ticker") != ticker
            or evidence[0].get("identity_evidence_ref") != expected["provider_identity_reference"]):
        fail("BENCHMARK_PROXY_IDENTITY_UNVERIFIED")
    return supplied


def compare(dataset, key, spec, strategy_result, *, active):
    verified = identity(dataset, key)
    ticker, label = OPTIONS[key]
    start = strategy_result["coverage"]["first"]
    end = strategy_result["coverage"]["last"]
    days = {row["session_date"] for row in dataset["rows"]}
    filtered = {day for day in days if start <= day <= end}
    comparable_dates = {point["at"] for point in strategy_result["equity_curve"] if point["equity"] is not None}
    same_calendar = comparable_dates == filtered
    price_basis = dataset["metadata"]["adjustment"]
    currency_matches = strategy_result["currency"] == verified["currency"]
    strategy_policy = strategy_result.get("native_acquisition") or {}
    benchmark_policy = acquisition(dataset) or {}
    strategy_basis = strategy_result["return_basis"]
    same_raw = price_basis == "raw" and strategy_basis == "PRICE_ONLY_DIVIDENDS_NOT_PROVIDED"
    same_adjusted = (price_basis == "split_adjusted"
                     and strategy_basis == "ADJUSTED_PRICE_PATH_NOT_SHAREHOLDER_TOTAL_RETURN"
                     and all(policy.get("provider") == "yfinance" and policy.get("price_policy") == YAHOO_PRICE_POLICY
                             for policy in (strategy_policy, benchmark_policy)))
    same_basis = strategy_result["price_adjustment"] == price_basis and (same_raw or same_adjusted)
    reason = ("MISSING_START_SESSION" if start not in filtered else
              "MISSING_END_SESSION" if end not in filtered else
              "CURRENCY_MISMATCH" if not currency_matches else
              "RETURN_BASIS_MISMATCH" if not same_basis else
              "OBSERVATION_CALENDAR_MISMATCH" if not same_calendar else "SAME_CAPITAL_CURRENCY_START_END_PRICE_BASIS")
    result = {
        "id": key, "ticker": ticker, "label": label, "currency": verified["currency"], "identity": verified,
        "timezone": dataset["metadata"].get("timezone"), "price_adjustment": price_basis,
        "dataset_reference": {k: dataset[k] for k in ("dataset_id", "version", "content_hash")},
        "requested_start": spec["start_date"], "requested_end": spec["end_date"],
        "anchor_session": start, "target_last_session": end, "first_session": None, "last_session": None,
        "initial_capital": spec["initial_cash"], "fees": None, "cash_remaining": None,
        "comparison_basis": "SAME_CURRENCY" if currency_matches else "LOCAL_CURRENCY_RETURN_ONLY",
        "comparable": False, "amount_comparable": False, "comparability_reason": reason,
        "curve": [], "fills": [], "status": "MISSING_START_SESSION",
        "quantity_basis": "ADJUSTED_SIMULATION_UNITS" if price_basis == "split_adjusted" else "WHOLE_UNITS",
        "return_basis": "ETF_PRICE_ONLY_NO_ADDED_CASH_DIVIDENDS",
        "strategy_return_basis": strategy_basis,
        "strategy_price_policy": strategy_policy.get("price_policy"),
        "benchmark_return_basis": dataset["benchmark"].get("return_basis"),
        "benchmark_dividend_policy": dataset["benchmark"].get("dividend_policy"),
        "benchmark_price_policy": benchmark_policy.get("price_policy"),
        "missing_strategy_sessions": sorted(comparable_dates-filtered),
        "additional_benchmark_sessions": sorted(filtered-comparable_dates),
    }
    if start not in filtered or not currency_matches:
        if start in filtered:
            result.update(status="CURRENCY_MISMATCH", initial_capital=None)
        return result
    numeric = benchmark(dataset, {**spec, "benchmark_mode": "buy_hold", "start_date": start, "end_date": end},
                        active=active, verified_benchmark=True)
    comparable = same_basis and same_calendar and end in filtered
    status = "MISSING_END_SESSION" if end not in filtered else "READY" if numeric["fills"] else "NO_AFFORDABLE_UNIT"
    result.update({k: numeric[k] for k in ("curve", "fills", "fees", "cash_remaining", "quantity", "lot_size")})
    result.update(status=status, first_session=start, last_session=max(filtered), comparable=comparable,
                  amount_comparable=comparable, entry_session=numeric["fills"][0]["at"] if numeric["fills"] else None)
    return result
