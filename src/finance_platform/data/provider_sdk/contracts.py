"""Strict daily-observation inputs, evidence classes and finance projection."""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import re

from finance_platform.core.contracts import Instrument, canonical, fingerprint, payload, parse_instant
from finance_platform.data.contracts import DataError
from .history_contracts import HISTORY_PROFILE, HISTORY_DAYS, HISTORY_ROWS, SINA_QFQ_INTERFACE, NATIVE_INTERFACE, sina_symbol as valid_sina_symbol

FAMILY = "provider-observation-1"
PROVIDERS = {"yfinance": {"version": "1.8.0", "origin": "https://query2.finance.yahoo.com"},
             "akshare": {"version": "1.19.3+guard1", "origin": "https://finance.sina.com.cn"}}
SINA_INTERFACE = "stock_us_daily_sina_unadjusted"
PRICE_BASIS = "PROVIDER_OBSERVED"
DECLARATION = "PERSONAL_DEVELOPMENT_SOURCE_USE_DECLARATION"
LIMITS = {"seconds": 45, "job_seconds": 240, "memory_bytes": 1024**3,
          "requests": 8, "response_bytes": 2*1024**2, "unit_bytes": 8*1024**2,
          "header_bytes": 32768, "cookie_bytes": 32768, "rows": 400,
          "manifest_bytes": 65536, "stdout_bytes": 65536, "stderr_bytes": 65536}


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def number(value, *, minimum=None):
    # Provider observations retain source decimal precision; do not pass through
    # the older raw trading contract (whose fractional limit is twelve).
    if type(value) not in {str, int, Decimal} or len(str(value)) > 96:
        raise DataError("SDK_NUMBER_INVALID")
    try:
        result = Decimal(value)
    except Exception:
        raise DataError("SDK_NUMBER_INVALID") from None
    if not result.is_finite() or len(result.as_tuple().digits) > 48 or not -36 <= result.as_tuple().exponent <= 24 or abs(result) >= Decimal("1e24"):
        raise DataError("SDK_NUMBER_INVALID")
    if minimum is not None and result < minimum:
        raise DataError("NUMBER_OUT_OF_RANGE")
    return format(result, "f")


def require_declaration(source, purpose="development"):
    if source.get("provider") not in PROVIDERS:
        raise DataError("PROVIDER_UNSUPPORTED")
    license = source.get("license", {})
    evidence = license.get("evidence")
    if license.get("rights_verified") is not False:
        raise DataError("SDK_EXTERNAL_RIGHTS_VERIFICATION_FORBIDDEN")
    if type(evidence) is not dict or set(evidence) != {"evidence_class", "statement", "affirmed"}:
        raise DataError("PROVIDER_USE_DECLARATION_REQUIRED")
    if evidence["evidence_class"] != DECLARATION or evidence["affirmed"] is not True or type(evidence["statement"]) is not str or not 1 <= len(evidence["statement"].strip()) <= 2000:
        raise DataError("PROVIDER_USE_DECLARATION_REQUIRED")
    if type(license.get("use_allowed")) is not list or set(license["use_allowed"]) - {"development", "export"}:
        raise DataError("LICENSE_PURPOSE_INVALID")
    if purpose not in license["use_allowed"]:
        raise DataError("DATA_USE_NOT_LICENSED")
    return {"evidence_class": DECLARATION, "evidence_hash": fingerprint(evidence), "purpose": purpose,
            "external_rights_verified": False}


def request_parameters(provider, dataset, parameters, *, probe=False, today=None):
    long_history = dataset.get("history_profile") == HISTORY_PROFILE
    if provider not in PROVIDERS or dataset.get("data_family") != FAMILY or dataset.get("scope") != "DEVELOPMENT":
        raise DataError("PROVIDER_OBSERVATION_ONLY")
    if type(parameters) is not dict:
        raise DataError("SDK_PARAMETERS_INVALID")
    allowed = {"symbols", "provider_symbols"} if probe else {"symbols", "provider_symbols", "start", "end", "timeframe"}
    required = {"symbols"} if probe else {"symbols", "start", "end", "timeframe"}
    if set(parameters)-allowed or not required <= set(parameters):
        raise DataError("SDK_PARAMETERS_INVALID")
    symbols = parameters["symbols"]
    if type(symbols) is not list or len(symbols) != 1 or type(symbols[0]) is not str:
        raise DataError("SDK_SYMBOL_LIMIT")
    aliases = parameters.get("provider_symbols", {})
    if type(aliases) is not dict or set(aliases)-set(symbols):
        raise DataError("SDK_SYMBOL_BINDING_REQUIRED")
    identity = [Instrument.from_mapping(v) for v in dataset["identities"] if v["ticker"] == symbols[0]]
    if len(identity) != 1:
        raise DataError("PROVIDER_IDENTITY_MISMATCH")
    instrument = identity[0]
    if instrument.currency != "USD" or instrument.asset_class.value not in {"US_EQUITY", "US_ETF"}:
        raise DataError("PROVIDER_IDENTITY_MISMATCH")
    symbol = aliases.get(symbols[0], symbols[0] if provider == "yfinance" else None)
    pattern = r"[A-Z][A-Z0-9.\-]{0,17}" if provider == "yfinance" else r"(?:105|106|107)\.[A-Z][A-Z0-9.\-]{0,17}"
    if type(symbol) is not str or not re.fullmatch(pattern, symbol):
        raise DataError("SDK_SYMBOL_BINDING_REQUIRED")
    if (symbol if provider=="yfinance" else symbol.split('.',1)[1])!=instrument.ticker:
        raise DataError("PROVIDER_IDENTITY_MISMATCH")
    if probe:
        # A bounded server-resolved date window, not a claim of session completion.
        from datetime import timedelta
        end = today or datetime.now(timezone.utc).date()
        start = end-timedelta(days=7)
    else:
        if parameters["timeframe"] != "1Day":
            raise DataError("SDK_INTERVAL_UNSUPPORTED")
        try:
            start, end = (date.fromisoformat(parameters[k]) for k in ("start", "end"))
        except (TypeError, ValueError):
            raise DataError("SDK_DATE_RANGE_VIOLATION") from None
        if not 0 < (end-start).days <= (HISTORY_DAYS if long_history else 366) or end > (today or datetime.now(timezone.utc).date()):
            raise DataError("SDK_DATE_RANGE_VIOLATION")
    # Daily precision cannot resolve an identity transition on the boundary date.
    if start <= instrument.ticker_valid_from.date() or (instrument.ticker_valid_to and end > instrument.ticker_valid_to.date()):
        raise DataError("PROVIDER_IDENTITY_DATE_BOUNDARY_UNCERTAIN")
    result = {"provider": provider, "existing_instrument_id": instrument.instrument_id, "asset_class": instrument.asset_class.value,
            "ticker": instrument.ticker, "provider_symbol": symbol,
            "start": start.isoformat(), "end": end.isoformat(), "timeframe": "1Day",
            "max_rows": 5 if probe else HISTORY_ROWS if long_history else 400, "operation": "probe" if probe else "collect"}
    if long_history:
        result.update(profile=HISTORY_PROFILE, identity_evidence_ref=dataset["identity_evidence_ref"], identity_binding=dataset["identity_binding"])
    return result


def source_request_parameters(source, dataset, parameters, *, probe=False, today=None, acquisition=True):
    """Endpoint-bound acquisition; historical Eastmoney observations remain readable."""
    provider = source.get("provider")
    if provider == "akshare":
        endpoint = source.get("connection", {}).get("endpoint")
        if endpoint == PROVIDERS["akshare"]["origin"]:
            call = request_parameters("yfinance", dataset, parameters, probe=probe, today=today)
            if not valid_sina_symbol(call["provider_symbol"]):
                raise DataError("SINA_SYMBOL_MAPPING_UNVERIFIED")
            result={**call, "provider": "akshare", "interface": SINA_QFQ_INTERFACE if dataset.get("history_profile") == HISTORY_PROFILE else SINA_INTERFACE}
            if acquisition or NATIVE_INTERFACE in source.get("capabilities", []):
                if NATIVE_INTERFACE not in source.get("capabilities", []):
                    raise DataError("SDK_SOURCE_REDECLARATION_REQUIRED")
                if dataset.get("quality_policy") is not None:
                    raise DataError("SINA_QUALITY_POLICY_INVALID")
                return {**call, "provider": "akshare", "interface": NATIVE_INTERFACE}
            from .sina_quality import POLICY_ID,POLICY_HASH,enabled
            quality=dataset.get("quality_policy")
            if quality is not None:
                if quality!={"id":POLICY_ID,"content_hash":POLICY_HASH} or POLICY_ID not in source["capabilities"]:raise DataError("SINA_QUALITY_POLICY_INVALID")
                result.update(quality_policy_id=POLICY_ID,quality_policy_hash=POLICY_HASH)
                enabled(result)
            elif dataset.get("history_profile")==HISTORY_PROFILE and POLICY_ID in source["capabilities"]:raise DataError("SINA_QUALITY_POLICY_INVALID")
            return result
        if endpoint != "https://63.push2his.eastmoney.com" or acquisition:
            raise DataError("SDK_SOURCE_REDECLARATION_REQUIRED")
    return request_parameters(provider, dataset, parameters, probe=probe, today=today)


def checked_json(raw, limit=2*1024**2, *, max_nodes=100000):
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        raise DataError("SDK_RESPONSE_LIMIT_EXCEEDED")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise DataError("SDK_SCHEMA_INVALID")
            value[key] = item
        return value
    try:
        value = json.loads(raw, parse_float=Decimal, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(DataError("SDK_SCHEMA_INVALID")))
    except (ValueError, RecursionError, UnicodeError):
        raise DataError("SDK_SCHEMA_INVALID") from None
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 32 or count > max_nodes:
            raise DataError("SDK_SCHEMA_INVALID")
        if isinstance(item, dict):
            stack.extend((v, depth+1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth+1) for v in item)
    return value


def sdk_scalar(value):
    """Lossless finite binary64 representation, explicitly not original lexical data."""
    if hasattr(value, "item"):
        value = value.item()
    if value is None or type(value).__name__ in {"NAType", "NaTType"}:
        return {"kind": "missing"}
    if type(value) is float:
        if math.isnan(value):
            return {"kind": "missing"}
        if not math.isfinite(value):
            raise DataError("SDK_NONFINITE_VALUE")
        return {"kind": "binary64", "hex": value.hex()}
    if type(value) is int:
        return {"kind": "integer", "value": str(value)}
    raise DataError("SDK_SCALAR_UNSUPPORTED")


def snapshot_frame(frame, max_rows):
    if frame.empty or len(frame) > max_rows or len(frame.columns) > 16:
        raise DataError("PROVIDER_EMPTY_OR_FIELDS_MISSING" if frame.empty else "SDK_ROW_LIMIT_EXCEEDED")
    columns = [str(c) for c in frame.columns]
    if len(set(columns)) != len(columns):
        raise DataError("SDK_SCHEMA_INVALID")
    result = {"schema_version": "sdk-frame-snapshot-1", "columns": columns,
              "dtypes": {str(k): str(v) for k, v in frame.dtypes.items()}, "rows": []}
    for index, row in zip(frame.index, frame.itertuples(index=False, name=None)):
        projected = {}
        for column, item in zip(columns, row):
            if column in {"日期", "date"}:
                projected[column] = {"kind": "date_label", "value": str(item)}
            else:
                projected[column] = sdk_scalar(item)
        result["rows"].append({"sdk_index": index.isoformat() if hasattr(index, "isoformat") else str(index), "values": projected})
    return result


def scalar_agrees(exact, scalar):
    if scalar.get("kind") == "binary64":
        return float(exact).hex() == scalar["hex"]
    if scalar.get("kind") == "integer":
        return Decimal(exact) == Decimal(scalar["value"])
    return False


def volume_agrees(exact, scalar):
    if type(scalar)is not dict:return False
    try:
        if scalar.get('kind')=='integer':value=Decimal(scalar['value'])
        elif scalar.get('kind')=='binary64':
            number=float.fromhex(scalar['hex'])
            if not math.isfinite(number) or not number.is_integer():return False
            value=Decimal(int(number))
        else:return False
        return value==Decimal(exact) and value>=0 and value==value.to_integral_value()
    except (ValueError,KeyError,TypeError):return False


def observed_row(call, date_label, prices, volume, sdk_values, *, received_at, unit_evidence,
                 raw_locator, sdk_locator, source_timestamp=None, source_timezone=None, adj_close=None):
    try:
        day = date.fromisoformat(date_label)
    except (TypeError, ValueError):
        raise DataError("SDK_DATE_RANGE_VIOLATION") from None
    if not date.fromisoformat(call["start"]) <= day < date.fromisoformat(call["end"]):
        raise DataError("SDK_DATE_RANGE_VIOLATION")
    normalized = {k: number(v, minimum=Decimal("0.000000000001")) for k, v in prices.items()}
    if set(normalized) != {"open", "high", "low", "close"}:
        raise DataError("SDK_SCHEMA_INVALID")
    for key in normalized:
        if not scalar_agrees(normalized[key], sdk_values[key]):
            raise DataError("SDK_FINANCIAL_TRANSFORM_UNSUPPORTED")
    if Decimal(normalized["low"]) > min(Decimal(normalized["open"]), Decimal(normalized["close"])) or Decimal(normalized["high"]) < max(Decimal(normalized["open"]), Decimal(normalized["close"])):
        raise DataError("PROVIDER_OHLC_INVALID")
    flags = []
    if volume is None:
        flags.append("ORIGINAL_VOLUME_MISSING")
        if volume_agrees("0", sdk_values["volume"]):
            flags.append("SDK_NORMALIZED_MISSING_TO_ZERO_NOT_OBSERVED_ZERO")
        elif sdk_values["volume"]!={"kind":"missing"}:
            raise DataError("PROVIDER_VOLUME_TRANSFORM_UNSUPPORTED")
    else:
        volume = number(volume, minimum=Decimal(0))
        if Decimal(volume) % 1 or not volume_agrees(volume, sdk_values["volume"]):
            raise DataError("PROVIDER_VOLUME_TRANSFORM_UNSUPPORTED")
    provider = call["provider"]
    return {"existing_instrument_id": call["existing_instrument_id"], "provider_symbol": call["provider_symbol"],
            "session_date": date_label, "source_timestamp": source_timestamp, "source_timestamp_timezone": source_timezone,
            **normalized, "volume": volume, "currency": "USD", "price_unit": "USD_per_share", "volume_unit": "shares",
            "currency_and_unit_evidence": unit_evidence, "price_basis": PRICE_BASIS, "raw_compatible": False,
            "source_adjustment_request": ({"interval": "1d", "auto_adjust": False, "back_adjust": False, "repair": False, "rounding": False} if provider=="yfinance" else {"period": "daily", "adjust": "", "fqt": "0"}),
            "provider_basis_claim": "YAHOO_NATIVE_SPLIT_ADJUSTED_NO_SDK_AUTO_ADJUST" if provider=="yfinance" else "EASTMONEY_FQT0_REQUESTED_NOT_PER_ROW_CERTIFIED",
            "basis_verification": "PROVIDER_DOCUMENTED_OR_REQUESTED_NOT_PER_ROW_CERTIFIED",
            "provider_identity_verification":"EXPLICIT_USER_BINDING_NOT_EXTERNALLY_CERTIFIED",
            "volume_share_basis": "PROVIDER_REPORTED_UNVERIFIED_SPLIT_SHARE_BASIS", "basis_reference_time": None,
            "observed_received_at": received_at, "availability_basis": "OBSERVED_RECEIPT_ONLY", "historical_first_available_at": None,
            "completeness": "UNKNOWN", "sdk_transform_flags": flags, "raw_row_locator": raw_locator,
            "sdk_row_locator": sdk_locator, "source_adj_close": None if adj_close is None else number(adj_close, minimum=Decimal(0))}
