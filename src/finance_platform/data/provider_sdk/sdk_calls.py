"""Genuine pinned SDK calls and original/SDK numeric correspondence."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import metadata
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import stat
from types import FunctionType
from zoneinfo import ZoneInfo

from finance_platform.data.contracts import DataError
from .contracts import PROVIDERS, checked_json, snapshot_frame, observed_row, scalar_agrees, number
from .network_gate import Budget, BoundedSafeHTTP, YAHOO_PRIVATE_CA, gated_yahoo_session
from .history_contracts import HISTORY_PROFILE, SINA_QFQ_INTERFACE, YAHOO_PRICE_POLICY, QUANTITY_BASIS


def verify_distribution(provider, pins):
    expected = PROVIDERS[provider]["version"]
    try:
        dist = metadata.distribution(provider)
    except metadata.PackageNotFoundError:
        raise DataError("SDK_DEPENDENCY_MISSING") from None
    if dist.version != expected:
        raise DataError("SDK_VERSION_UNREVIEWED")
    pin = pins[provider]
    if provider == "akshare":
        import re
        if (pin.get("version") != expected or type(pin.get("artifact_sha256")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", pin["artifact_sha256"]) is None):
            raise DataError("SDK_ARTIFACT_PIN_REQUIRED")
    for relative, digest in pin["modules"].items():
        path = Path(dist.locate_file(relative))
        raw=path.read_bytes()
        if hashlib.sha256(raw.replace(b"\r\n", b"\n") if path.suffix==".py" else raw).hexdigest() != digest:
            raise DataError("SDK_INTERFACE_CHANGED")
    return {"name": provider, "version": expected, "artifact_sha256": pin["artifact_sha256"]}


def yahoo(call, budget, cache_root, pins, *, fixture_exchange=None):
    sdk_ref = verify_distribution("yfinance", pins)
    bundle, ca_bytes = verify_curl(pins)
    import yfinance as yf
    from yfinance import _http
    from curl_cffi import requests as curl_requests
    #1.8 permits a plain requests fallback; this guarded path never does.
    if _http.HAS_CURL_CFFI is not True or _http.requests is not curl_requests:
        raise DataError('SDK_SESSION_ESCAPE')
    from yfinance.config import YfConfig
    from yfinance.scrapers.history import PriceHistory
    logging.getLogger("yfinance").disabled = True
    YfConfig.network.proxy = None
    YfConfig.network.retries = 0
    YfConfig.debug.hide_exceptions = False
    YfConfig.debug.logging = False
    # Fresh, ACL-protected child path set before ticker/cache use (including cookie pickle).
    yf.set_tz_cache_location(str(cache_root))
    private_ca = _stage_yahoo_ca(cache_root, bundle, ca_bytes)
    session = gated_yahoo_session(budget, private_ca=private_ca, fixture_exchange=fixture_exchange)
    try:
        if call.get("operation") == "search":
            from .security_identity import search_from_entity
            yf.Search(call["query"], max_results=10, news_count=0, lists_count=0, include_cb=False,
                      include_nav_links=False, include_research=False, include_cultural_assets=False,
                      enable_fuzzy_query=False, recommended=0, session=session, timeout=5, raise_errors=True)
            budget.check()
            entities = [e for e in budget.entities if e["role"] == "security_search"]
            if len(entities) != 1: budget.abort("SECURITY_SEARCH_RESPONSE_INVALID")
            return {**sdk_ref, "operation": "search"}, {"schema_version": "security-search-1"}, search_from_entity(entities[0])
        ticker = yf.Ticker(call["provider_symbol"], session=session)
        if call.get("operation") == "identity":
            from .security_identity import identity_from_entity
            # This exact SDK method never invokes _get_ticker_tz's info fallback.
            ticker._fetch_ticker_tz(timeout=5)
            budget.check()
            entities = [e for e in budget.entities if e["role"] == "metadata_chart"]
            if len(entities) != 1: budget.abort("SECURITY_IDENTITY_UNAVAILABLE")
            return {**sdk_ref, "operation": "identity"}, {"schema_version": "security-identity-1"}, [identity_from_entity(call, entities[0])]
        #1.8.0 lazy initializer does NOT forward session to PriceHistory. Populate
        # ONLY this instance with the real SDK class, so no default session exists.
        tz = ticker._get_ticker_tz(timeout=5)
        if not tz:
            budget.abort("PROVIDER_TIMEZONE_MISSING")
        ticker._price_history = PriceHistory(ticker._data, ticker.ticker, tz, session=session)
        if ticker.session is not session or ticker._data._session is not session or ticker._price_history.session is not session:
            budget.abort("SDK_SESSION_ESCAPE")
        frame = ticker.history(start=call["start"], end=call["end"], interval="1d", auto_adjust=False,
            back_adjust=False, repair=False, rounding=False, actions=call.get("profile") == HISTORY_PROFILE, prepost=False, keepna=True, timeout=5)
        budget.check()  # Sticky callback failures cannot become accepted partial frames.
        snapshot = snapshot_frame(frame, call["max_rows"])
        rows = normalize_yahoo(call, snapshot, budget.entities)
        if call.get("profile") == HISTORY_PROFILE:
            sdk_ref = {**sdk_ref, "corporate_action_basis": YAHOO_PRICE_POLICY, "price_policy": YAHOO_PRICE_POLICY, "quantity_basis": QUANTITY_BASIS}
        return sdk_ref, snapshot, rows
    finally:
        session.close()


def verify_curl(pins):
    try:
        dist = metadata.distribution("curl_cffi")
    except metadata.PackageNotFoundError:
        raise DataError("SDK_DEPENDENCY_MISSING") from None
    if dist.version != "0.17.0":
        raise DataError("SDK_VERSION_UNREVIEWED")
    for relative, digest in pins["curl_cffi"]["modules"].items():
        path=Path(dist.locate_file(relative));raw=path.read_bytes()
        if hashlib.sha256(raw.replace(b"\r\n", b"\n") if path.suffix==".py" else raw).hexdigest() != digest:
            raise DataError("SDK_INTERFACE_CHANGED")
    #0.17's selected security update also refreshes the CA bundle. Its exact
    # reviewed wheel/RECORD-derived identity is supplied with this new pin set.
    ca=pins.get('certifi')
    bundle = "certifi/cacert.pem"
    if not ca or ca.get('version')!='2026.10.23' or bundle not in (ca.get('modules') or {}):
        raise DataError('SDK_ARTIFACT_PIN_REQUIRED')
    dist=metadata.distribution('certifi')
    if dist.version!=ca['version']:raise DataError('SDK_VERSION_UNREVIEWED')
    for relative,digest in ca['modules'].items():
        path=Path(dist.locate_file(relative));raw=path.read_bytes()
        if hashlib.sha256(raw.replace(b'\r\n',b'\n') if path.suffix=='.py' else raw).hexdigest()!=digest:raise DataError('SDK_INTERFACE_CHANGED')
        if relative == bundle:
            verified_bundle = path, raw
    return verified_bundle


def _stage_yahoo_ca(cache_root, bundle, ca_bytes):
    from curl_cffi.curl import DEFAULT_CACERT

    # Only relocate the pinned bundle already selected by curl. Keep any other
    # effective default trust source on its existing path.
    if os.name != "nt" or str(bundle).isascii() or Path(DEFAULT_CACERT) != bundle:
        return False
    try:
        cwd = Path.cwd()
        if cache_root != cwd / "cache":
            raise DataError("SDK_CA_CACHE_UNAVAILABLE")
        info = cache_root.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise DataError("SDK_CA_CACHE_UNAVAILABLE")
        with (cwd / YAHOO_PRIVATE_CA).open("xb") as output:
            output.write(ca_bytes)
    except OSError:
        raise DataError("SDK_CA_CACHE_UNAVAILABLE") from None
    return True


class _AkRequests:
    def __init__(self, call, budget, port):
        self.call, self.budget, self.port = call, budget, port

    def get(self, url, *, params, timeout=15, **unknown):
        call, budget = self.call, self.budget
        if url != "https://63.push2his.eastmoney.com/api/qt/stock/kline/get" or unknown:
            budget.abort("SDK_INTERFACE_CHANGED")
        keys = {"secid", "fields1", "fields2", "klt", "fqt", "end", "lmt"}
        if type(params) is not dict or set(params) != keys or params["secid"] != call["provider_symbol"] or params["klt"] != "101" or params["fqt"] != "0" or params["lmt"] != "1000000" or params["end"] != "20500000":
            budget.abort("SDK_INTERFACE_CHANGED")
        if params["fields1"] != "f1,f2,f3,f4,f5,f6" or params["fields2"] != "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61":
            budget.abort("SDK_INTERFACE_CHANGED")
        from datetime import date, timedelta
        bounded = {**params, "beg": call["start"].replace("-", ""),
                   "end": (date.fromisoformat(call["end"])-timedelta(days=1)).strftime("%Y%m%d"),
                   "lmt": str(min(call["max_rows"], (date.fromisoformat(call["end"])-date.fromisoformat(call["start"])).days+1))}
        response = self.port.fetch(url, bounded)
        budget.check()
        if response.status != 200:
            budget.abort("PROVIDER_RATE_LIMITED" if response.status == 429 else "PROVIDER_ACCESS_DENIED" if response.status in {401,403} else "SDK_UPSTREAM_HTTP_ERROR")
        data = checked_json(response.body)
        try:
            values = data["data"]["klines"]
            expected_market, expected_symbol = call["provider_symbol"].split(".",1)
            if data["data"].get("code") != expected_symbol or str(data["data"].get("market")) != expected_market:
                budget.abort("PROVIDER_IDENTITY_MISMATCH")
            if type(values) is not list or not values:
                budget.abort("PROVIDER_EMPTY_OR_FIELDS_MISSING")
            if len(values) > call["max_rows"]:
                budget.abort("SDK_ROW_LIMIT_EXCEEDED")
            for value in values:
                fields = value.split(",")
                if len(fields) != 11 or not call["start"] <= fields[0] < call["end"]:
                    budget.abort("SDK_DATE_RANGE_VIOLATION")
        except (KeyError, TypeError, AttributeError):
            budget.abort("SDK_SCHEMA_INVALID")
        budget.capture(url, bounded, response.body, role="ak_daily_klines")
        from types import SimpleNamespace
        return SimpleNamespace(json=lambda: data)


def akshare(call, budget, pins, *, fixture_exchange=None):
    from .history_contracts import NATIVE_INTERFACE
    if call.get("interface") != NATIVE_INTERFACE:
        budget.abort("SDK_SOURCE_REDECLARATION_REQUIRED")
    sdk_ref = verify_distribution("akshare", pins)
    from .sina_native import native_qfq
    from .sina_native_rows import native_rows
    frame, provenance = native_qfq(call, budget, pins, fixture_exchange=fixture_exchange)
    snapshot = snapshot_frame(frame, call["max_rows"])
    rows = native_rows(call, snapshot, budget.entities)
    return {**sdk_ref, **provenance}, snapshot, rows


def _historical_eastmoney_implementation_not_dispatched(call, budget, pins, *, fixture_exchange=None):
    sdk_ref = verify_distribution("akshare", pins)
    import akshare as ak
    function = ak.stock_us_hist
    if not isinstance(function, FunctionType) or function.__name__ != "stock_us_hist" or function.__module__ != "akshare.stock_feature.stock_hist_em" or function.__closure__:
        budget.abort("SDK_INTERFACE_CHANGED")
    actual = hashlib.sha256(inspect.getsource(function).replace("\r\n", "\n").encode()).hexdigest()
    if actual != pins["akshare"]["function_sha256"]:
        budget.abort("SDK_INTERFACE_CHANGED")
    namespace = dict(function.__globals__)
    namespace["requests"] = _AkRequests(call, budget, BoundedSafeHTTP(budget, fixture_exchange=fixture_exchange))
    bound = FunctionType(function.__code__, namespace, function.__name__, function.__defaults__)
    bound.__kwdefaults__ = function.__kwdefaults__
    from datetime import date, timedelta
    frame = bound(symbol=call["provider_symbol"], period="daily", start_date=call["start"].replace("-", ""),
                  end_date=(date.fromisoformat(call["end"])-timedelta(days=1)).strftime("%Y%m%d"), adjust="")
    budget.check()
    snapshot = snapshot_frame(frame, call["max_rows"])
    return sdk_ref, snapshot, normalize_ak(call, snapshot, budget.entities)


def normalize_yahoo(call, snapshot, entities):
    selected = [(index, e) for index,e in enumerate(entities) if e["role"] == "history_chart"]
    if len(selected) != 1:
        raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
    entity_index, entity = selected[0]
    data = checked_json(entity["body"])["chart"]["result"][0]
    meta = data["meta"]
    timezone_name = meta.get("exchangeTimezoneName")
    if type(timezone_name) is not str:
        raise DataError("PROVIDER_TIMEZONE_MISSING")
    zone = ZoneInfo(timezone_name)
    quote = data["indicators"]["quote"][0]
    timestamps = data["timestamp"]
    adj = data["indicators"].get("adjclose")
    if any(len(quote.get(k, [])) != len(timestamps) for k in ("open", "high", "low", "close")):
        raise DataError("SDK_SCHEMA_INVALID")
    by_date = {}
    for index, at in enumerate(timestamps):
        if type(at) is not int:
            raise DataError("SDK_SCHEMA_INVALID")
        label = (datetime(1970, 1, 1, tzinfo=timezone.utc)+timedelta(seconds=at)).astimezone(zone).date().isoformat()
        if label in by_date:
            raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
        by_date[label] = (index, at)
    action_by_date = {}
    if call.get("profile") == HISTORY_PROFILE:
        for kind in ("dividends", "splits"):
            events = data.get("events", {}).get(kind, {})
            if type(events) is not dict or len(events) > call["max_rows"]: raise DataError("SDK_ACTION_EVIDENCE_INVALID")
            mapped = {}
            for event in events.values():
                if type(event) is not dict or type(event.get("date")) is not int: raise DataError("SDK_ACTION_EVIDENCE_INVALID")
                label = (datetime(1970, 1, 1, tzinfo=timezone.utc)+timedelta(seconds=event["date"])).astimezone(zone).date().isoformat()
                if label in mapped: raise DataError("SDK_ACTION_EVIDENCE_INVALID")
                mapped[label] = event
            action_by_date[kind] = mapped
    rows = []
    seen=set()
    names = {"open":"Open", "high":"High", "low":"Low", "close":"Close", "volume":"Volume"}
    if len(snapshot["rows"]) != len(by_date):
        raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
    for sdk_index, item in enumerate(snapshot["rows"]):
        label = item["sdk_index"][:10]
        if label not in by_date or label in seen:
            raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
        seen.add(label)
        index, timestamp = by_date[label]
        volume = quote.get("volume", [None]*len(timestamps))[index]
        prices = {k: quote[k][index] for k in ("open", "high", "low", "close")}
        scalar = {k:item["values"][v] for k,v in names.items()}
        original_adj = adj[0]["adjclose"][index] if adj else None
        row = observed_row(call,label,prices,volume,scalar,received_at=entity["observed_received_at"],
            unit_evidence="yahoo_chart_currency_USD_provider_volume_shares",raw_locator={"entity":entity_index,"row":index},
            sdk_locator={"row":sdk_index},source_timestamp={"encoding":"UNIX_SECONDS_AS_SUPPLIED","value":str(timestamp)},
            source_timezone=timezone_name,adj_close=original_adj)
        sdk_adj = item["values"].get("Adj Close")
        if original_adj is None and sdk_adj is not None:
            row["sdk_transform_flags"].append("ORIGINAL_ADJ_CLOSE_MISSING_SDK_VALUE_NOT_ORIGINAL")
        elif original_adj is not None and sdk_adj is not None and not scalar_agrees(str(original_adj),sdk_adj):
            raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
        if call.get("profile") == HISTORY_PROFILE:
            evidence = {}
            for kind, column in (("dividends", "Dividends"), ("splits", "Stock Splits")):
                match = action_by_date[kind].get(label)
                scalar_action = item["values"].get(column)
                if match is None:
                    evidence[kind] = None
                    if scalar_action is not None and not scalar_agrees("0", scalar_action): raise DataError("SDK_ACTION_EVIDENCE_INVALID")
                elif kind == "dividends":
                    amount = number(match.get("amount"), minimum=Decimal(0))
                    if scalar_action is None or not scalar_agrees(amount, scalar_action): raise DataError("SDK_ACTION_EVIDENCE_INVALID")
                    evidence[kind] = {"amount": amount, "source_timestamp": str(match["date"])}
                else:
                    numerator = number(match.get("numerator"), minimum=Decimal("0.000000000001"))
                    denominator = number(match.get("denominator"), minimum=Decimal("0.000000000001"))
                    if scalar_action is None or not scalar_agrees(str(Decimal(numerator)/Decimal(denominator)), scalar_action): raise DataError("SDK_ACTION_EVIDENCE_INVALID")
                    evidence[kind] = {"numerator": numerator, "denominator": denominator, "source_timestamp": str(match["date"])}
            row.update(price_policy=YAHOO_PRICE_POLICY, quantity_basis=QUANTITY_BASIS,
                       price_unit="USD_per_adjusted_simulation_unit", source_corporate_action_evidence=evidence)
            row["sdk_transform_flags"].extend(["PRICE_PATH_ONLY_NO_DIVIDEND_CREDIT_NO_DOUBLE_SPLIT", "ABSENT_ACTION_EVENT_NOT_CERTIFIED_ZERO"])
        rows.append(row)
    if seen!=set(by_date):raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
    return rows


def normalize_ak(call, snapshot, entities):
    if len(entities) != 1 or entities[0]["role"] != "ak_daily_klines":
        raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
    entity = entities[0]
    raw_rows = checked_json(entity["body"])["data"]["klines"]
    source = {line.split(",",1)[0]:(index,line.split(",")) for index,line in enumerate(raw_rows)}
    if len(source) != len(raw_rows) or len(snapshot["rows"]) != len(source):
        raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
    rows = []
    seen=set()
    names = {"open":"开盘", "high":"最高", "low":"最低", "close":"收盘", "volume":"成交量"}
    for sdk_index, item in enumerate(snapshot["rows"]):
        label = item["values"]["日期"]["value"][:10]
        if label not in source or label in seen:
            raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
        seen.add(label)
        index, fields = source[label]
        prices = dict(open=fields[1], close=fields[2], high=fields[3], low=fields[4])
        volume = None if fields[5] in {"", "-", "null"} else fields[5]
        rows.append(observed_row(call,label,prices,volume,{k:item["values"][v] for k,v in names.items()},
            received_at=entity["observed_received_at"],unit_evidence="akshare1.19.1_exact_US_interface_USD_and_shares_no_x100",
            raw_locator={"entity":0,"row":index},sdk_locator={"row":sdk_index}))
    if seen!=set(source):raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
    return rows
