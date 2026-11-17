"""Explicit local independent-print mapping; no bar-derived liquidity."""
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from finance_platform.core.contracts import parse_instant, payload, identifier
from .contracts import DataError, identities_at, number, tick_values, envelope

TICK_FIELDS = {"event_id", "ticker", "instrument_id", "event_at", "available_at", "price", "volume", "trading_status"}


def tick_instant(value):
    """Keep source text; round submicrosecond precision forward, never truncate."""
    if type(value) is not str:
        raise DataError("TICK_TIMESTAMP_INVALID")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    if not match:
        raise DataError("TICK_TIMESTAMP_INVALID")
    digits = match[2] or ""
    point = parse_instant(match[1] + ("." + digits[:6] if digits else "") + match[3])
    if any(c != "0" for c in digits[6:]):
        point += timedelta(microseconds=1)
    return point


def source_clock(value):
    """Comparison before rounding so a reversed submicrosecond clock is rejected."""
    tick_instant(value)
    match = re.fullmatch(r"(.*T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    return parse_instant(match[1]+match[3]), int((match[2] or "").ljust(9, "0"))


def preview_ticks(rows, format, mapping, identities, *, source_id, license_id, scope, received_at):
    required = {"record_kind", "event_type", "fields", "price_unit", "volume_unit", "timezone", "availability"}
    if type(mapping) is not dict or set(mapping) != required or type(mapping["fields"]) is not dict or set(mapping["fields"]) != TICK_FIELDS:
        raise DataError("MAPPING_FIELDS_INVALID")
    if (mapping["record_kind"], mapping["event_type"], mapping["price_unit"], mapping["volume_unit"]) != ("tick", "TRADE", "USD_per_share", "shares"):
        raise DataError("MAPPING_UNITS_INVALID")
    if any(type(v) is not str or not v for v in mapping["fields"].values()) or len(set(mapping["fields"].values())) != len(TICK_FIELDS):
        raise DataError("MAPPING_COLLISION")
    availability = mapping["availability"]
    if type(availability) is not dict or set(availability) != {"basis", "reference"} or type(availability["reference"]) is not str or not 1 <= len(availability["reference"]) <= 256:
        raise DataError("TICK_AVAILABILITY_REFERENCE_REQUIRED")
    basis = availability["basis"]
    if basis not in {"OBSERVED_RECEIPT_ONLY", "SYNTHETIC_FIXTURE_CLOCK"}:
        raise DataError("TICK_AVAILABILITY_BASIS_INVALID")
    if basis == "SYNTHETIC_FIXTURE_CLOCK" and scope != "SYNTHETIC":
        raise DataError("TICK_AVAILABILITY_SCOPE_INVALID")
    try:
        zone = ZoneInfo(mapping["timezone"])
    except (KeyError, ValueError, TypeError):
        raise DataError("TIMEZONE_INVALID") from None
    received = parse_instant(received_at)
    records, errors, seen = [], [], set()
    for idx, original in enumerate(rows, 2 if format == "csv" else 1):
        try:
            row = {k: original[v] for k, v in mapping["fields"].items()}
            event, declared = tick_instant(row["event_at"]), tick_instant(row["available_at"])
            for field, point in (("event_at", event), ("available_at", declared)):
                if datetime.fromisoformat(row[field]).utcoffset() != point.astimezone(zone).utcoffset():
                    raise DataError("TIMEZONE_OFFSET_MISMATCH")
            if source_clock(row["available_at"]) < source_clock(row["event_at"]):
                raise DataError("TICK_CLOCK_INVALID")
            instrument = identities_at(identities, row["ticker"], payload(event), row["instrument_id"])
            identifier(row["event_id"])
            key = (instrument.instrument_id, row["event_id"])
            if key in seen:
                raise DataError("DUPLICATE_TICK_ID")
            seen.add(key)
            from decimal import Decimal
            values = {"event_id": row["event_id"], "event_type": "TRADE", "price": number(row["price"], minimum=Decimal("0.000000000001")),
                      "volume": number(row["volume"], minimum=Decimal(0)), "currency": "USD", "price_unit": "USD_per_share", "volume_unit": "shares",
                      "trading_status": row["trading_status"], "availability_basis": basis, "availability_reference": availability["reference"],
                      "source_timestamp": row["event_at"], "source_available_timestamp": row["available_at"],
                      "timestamp_normalization": "CEIL_MICROSECOND", "source_timezone": mapping["timezone"]}
            tick_values(values)
            if not instrument.tradable and values["trading_status"] == "TRADABLE":
                raise DataError("IDENTITY_NOT_TRADABLE")
            available = declared if basis == "SYNTHETIC_FIXTURE_CLOCK" else max(event, declared, received)
            records.append(envelope(kind="tick", source_id=source_id, license_id=license_id, scope=scope,
                instrument_id=instrument.instrument_id, effective_at=payload(event), available_at=payload(available),
                received_at=payload(received), values=values, natural_key=list(key)))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({"row": idx, "code": exc.code if isinstance(exc, DataError) else "ROW_FIELD_INVALID"})
    return {"records": records, "errors": errors, "row_count": len(rows), "valid_rows": len(records), "can_import": bool(records) and not errors}
