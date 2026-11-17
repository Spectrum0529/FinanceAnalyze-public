"""Data validation built on the platform's shared value contract."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re

from finance_platform.core.contracts import (
    Instrument, Provenance, DataScope, canonical, fingerprint, parse_instant,
    parse_decimal, payload, identifier, digest,
)
from predictor.secrets import secret_values
from finance_platform.core.security import scoped_values, clean
from finance_platform.core.errors import PlatformError
from finance_platform.core.pagination import validate_pagination


class DataError(PlatformError):
    def __init__(self, code):
        super().__init__(code, 422)
        from .library_errors import MESSAGES
        self.message = MESSAGES.get(code, self.message)
        self.args = (code,)


def validate_paging(limit, offset):
    """Finite service/HTTP pagination; reject before SQLite or delegation."""
    try:
        validate_pagination(limit, offset)
    except PlatformError:
        raise DataError("PAGINATION_INVALID") from None


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_bytes(raw, limit=16 * 1024 * 1024):
    if type(raw) is not bytes or not 0 < len(raw) <= limit:
        raise DataError("EMPTY_OR_OVERSIZED_INPUT")
    if any(v.encode("utf-8") in raw for v in secret_values() + scoped_values()):
        raise DataError("CREDENTIAL_IN_INPUT")
    return raw


def load_json(raw):
    safe_bytes(raw)
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise DataError("DUPLICATE_JSON_FIELD")
            result[k] = v
        return result
    try:
        return json.loads(raw, parse_float=Decimal, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(DataError("NONFINITE_NUMBER")))
    except RecursionError:
        raise DataError("JSON_DEPTH_INVALID") from None
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("INVALID_JSON") from None


def number(value, *, minimum=None):
    if type(value) is Decimal:
        value = format(value, "f")
    elif type(value) is int:
        value = str(value)
    result = parse_decimal(value)
    if minimum is not None and result < minimum:
        raise DataError("NUMBER_OUT_OF_RANGE")
    return format(result, "f")


def identities_at(identities, ticker, instant, instrument_id=None):
    point = parse_instant(instant)
    values = [i if type(i) is Instrument else Instrument.from_mapping(i) for i in identities]
    matches = [i for i in values if i.ticker == ticker and i.ticker_valid_from <= point
               and (i.ticker_valid_to is None or point < i.ticker_valid_to)]
    if len(matches) != 1 or (instrument_id is not None and matches[0].instrument_id != instrument_id):
        raise DataError("IDENTITY_MISSING_OR_AMBIGUOUS")
    return matches[0]


def bar_values(row):
    values = {k: number(row[k], minimum=Decimal("0.000000000001")) for k in ("open", "high", "low", "close")}
    prices = {k: Decimal(v) for k, v in values.items()}
    if prices["low"] > min(prices["open"], prices["close"]) or prices["high"] < max(prices["open"], prices["close"]) or prices["low"] > prices["high"]:
        raise DataError("OHLC_INCONSISTENT")
    volume = number(row["volume"], minimum=Decimal(0))
    if Decimal(volume) % 1:
        raise DataError("VOLUME_NOT_WHOLE_SHARES")
    values.update(volume=volume, price_unit="USD_per_share", volume_unit="shares",
                  frequency=row["frequency"], currency="USD", adjustment="raw",
                  trading_status=row.get("trading_status", "UNKNOWN"))
    if values["frequency"] not in {"1Day", "1Min"}:
        raise DataError("FREQUENCY_UNSUPPORTED")
    if values["trading_status"] not in {"TRADABLE", "HALTED", "UNKNOWN", "TERMINATED"}:
        raise DataError("TRADING_STATUS_INVALID")
    return values


def tick_values(values):
    """Independent trade prints; aggregates and unknown units are never inferred."""
    required = {"price", "volume", "currency", "price_unit", "volume_unit", "trading_status"}
    optional = {"event_id", "event_type", "availability_basis", "availability_reference",
                "source_timestamp", "source_available_timestamp", "timestamp_normalization", "source_timezone"}
    if type(values) is not dict or not required <= set(values) or set(values) - required - optional:
        raise DataError("TICK_SCHEMA_INVALID")
    number(values["price"], minimum=Decimal("0.000000000001"))
    number(values["volume"], minimum=Decimal(0))
    if (values["currency"], values["price_unit"], values["volume_unit"]) != ("USD", "USD_per_share", "shares"):
        raise DataError("TICK_UNITS_INVALID")
    if values["trading_status"] not in {"TRADABLE", "HALTED", "UNKNOWN"}:
        raise DataError("TRADING_STATUS_INVALID")
    if values.get("event_type", "TRADE") != "TRADE":
        raise DataError("TICK_EVENT_TYPE_INVALID")
    if "event_id" in values:
        identifier(values["event_id"])
    basis = values.get("availability_basis", "OBSERVED_RECEIPT_ONLY")
    if basis not in {"OBSERVED_RECEIPT_ONLY", "SYNTHETIC_FIXTURE_CLOCK"}:
        raise DataError("TICK_AVAILABILITY_BASIS_INVALID")
    if basis == "SYNTHETIC_FIXTURE_CLOCK" and (type(values.get("availability_reference")) is not str or not 1 <= len(values["availability_reference"]) <= 256):
        raise DataError("TICK_AVAILABILITY_REFERENCE_REQUIRED")


def synthetic_tick_clock(kind, values, scope):
    return kind == "tick" and scope == DataScope.SYNTHETIC and values.get("availability_basis") == "SYNTHETIC_FIXTURE_CLOCK"


def validate_action_evidence(values, *, required=True):
    """Execution dates are explicit evidence, never inferred from effective_at.

    Missing legacy dates may be inspected with required=False. Newly appended
    records and the trading execution bridge must require the evidence.
    """
    if values["status"] != "RESOLVED":
        return
    field = "ratio" if values["type"] == "SPLIT" else "cash_per_share"
    if required and type(values.get(field)) is not str:
        raise DataError("ACTION_DECIMAL_STRING_REQUIRED")
    if values["type"] == "DIVIDEND":
        present = {k for k in ("record_at", "pay_at") if k in values}
        if not present and not required:
            return
        if present != {"record_at", "pay_at"}:
            raise DataError("DIVIDEND_ENTITLEMENT_EVIDENCE_REQUIRED")
        if type(values["record_at"]) is not str or type(values["pay_at"]) is not str:
            raise DataError("DIVIDEND_ENTITLEMENT_CLOCK_INVALID")
        if parse_instant(values["record_at"]) > parse_instant(values["pay_at"]):
            raise DataError("DIVIDEND_ENTITLEMENT_CLOCK_INVALID")


def envelope(*, kind, source_id, license_id, scope, instrument_id, effective_at,
             available_at, received_at, values, natural_key, revision=1, valid_to=None):
    key = hashlib.sha256(canonical([source_id, kind, natural_key]).encode()).hexdigest()
    meta = Provenance(key, source_id, license_id, fingerprint(values), revision, DataScope(scope),
                      parse_instant(effective_at), parse_instant(available_at), parse_instant(received_at),
                      None if valid_to is None else parse_instant(valid_to))
    identifier(instrument_id)
    if meta.available_at < meta.received_at and not synthetic_tick_clock(kind, values, meta.scope):
        # Platform first-release paths are observed/synthetic, not historical reconstruction.
        raise DataError("AVAILABILITY_PRECEDES_RECEIPT")
    return {"kind": kind, "instrument_id": instrument_id, "metadata": payload(meta), "values": payload(values)}


def validate_record(record, *, require_action_evidence=False):
    try:
        if set(record) != {"kind", "instrument_id", "metadata", "values"}:
            raise DataError("RECORD_SCHEMA_INVALID")
        m = record["metadata"]
        p = Provenance(m["record_id"], m["source_id"], m["license_id"], m["content_hash"],
                       m["revision"], DataScope(m["scope"]), *[parse_instant(m[k]) for k in
                       ("effective_at", "available_at", "received_at")],
                       None if m["valid_to"] is None else parse_instant(m["valid_to"]))
        if fingerprint(record["values"]) != p.content_hash or (p.available_at < p.received_at and not synthetic_tick_clock(record["kind"], record["values"], p.scope)):
            raise DataError("RECORD_HASH_OR_TIME_INVALID")
        identifier(record["instrument_id"])
        if record["kind"] == "tick":
            v = record["values"]
            tick_values(v)
            if "event_id" in v and p.record_id != fingerprint([p.source_id, "tick", [record["instrument_id"], v["event_id"]]]):
                raise DataError("TICK_LOGICAL_ID_MISMATCH")
            if p.available_at < p.effective_at or p.valid_to is not None:
                raise DataError("TICK_CLOCK_INVALID")
            if v.get("availability_basis") == "SYNTHETIC_FIXTURE_CLOCK" and p.scope != DataScope.SYNTHETIC:
                raise DataError("TICK_AVAILABILITY_SCOPE_INVALID")
            if "source_timestamp" in v:
                from .ticks import tick_instant, source_clock
                if tick_instant(v["source_timestamp"]) != p.effective_at:
                    raise DataError("TICK_SOURCE_TIMESTAMP_MISMATCH")
                if "source_available_timestamp" in v and (source_clock(v["source_available_timestamp"]) < source_clock(v["source_timestamp"]) or tick_instant(v["source_available_timestamp"]) > p.available_at):
                    raise DataError("TICK_SOURCE_TIMESTAMP_MISMATCH")
        elif record["kind"] == "bar":
            v = record["values"]
            if v["price_unit"] != "USD_per_share" or v["volume_unit"] != "shares" or v["adjustment"] != "raw":
                raise DataError("UNIT_OR_ADJUSTMENT_INVALID")
            bar_values(v)
            end = parse_instant(v["bar_end"])
            expected_end = p.effective_at + (timedelta(minutes=1) if v["frequency"] == "1Min" else timedelta(days=1))
            if end != expected_end or p.available_at < end:
                raise DataError("BAR_COMPLETION_TIME_INVALID")
        elif record["kind"] == "action":
            v = record["values"]
            if v["type"] not in {"SPLIT", "DIVIDEND", "TERMINAL"} or v["status"] not in {"RESOLVED", "PENDING", "CANCELLED"}:
                raise DataError("ACTION_INVALID")
            if v["status"] == "RESOLVED":
                field = "ratio" if v["type"] == "SPLIT" else "cash_per_share"
                number(v[field], minimum=Decimal("0.000000000001") if field == "ratio" else Decimal(0))
                if field == "cash_per_share" and v.get("currency") != "USD":
                    raise DataError("ACTION_CURRENCY_INVALID")
            elif any(v.get(k) is not None for k in ("ratio", "cash_per_share")):
                raise DataError("PENDING_ACTION_VALUE_FORBIDDEN")
            validate_action_evidence(v, required=require_action_evidence)
        elif record["kind"] == "identity":
            v = record["values"]
            instrument = Instrument.from_mapping(v["instrument"])
            if instrument.instrument_id != record["instrument_id"] or not isinstance(v["evidence_reference"], str) or not v["evidence_reference"]:
                raise DataError("IDENTITY_EVIDENCE_INVALID")
            if p.effective_at != instrument.ticker_valid_from or p.valid_to != instrument.ticker_valid_to:
                raise DataError("IDENTITY_VALIDITY_MISMATCH")
        elif record["kind"] not in {"filing", "fact"}:
            raise DataError("RECORD_KIND_UNSUPPORTED")
        clean(record)
        canonical(record)
        return p
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, DataError):
            raise
        raise DataError("RECORD_SCHEMA_INVALID") from None
