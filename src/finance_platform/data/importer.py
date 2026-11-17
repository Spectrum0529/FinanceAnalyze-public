"""Bounded CSV/Parquet mapping preview with safe per-row diagnostics."""
import csv
import io
from datetime import timedelta
from zoneinfo import ZoneInfo

from finance_platform.core.contracts import parse_instant, payload, fingerprint
from .contracts import DataError, safe_bytes, bar_values, identities_at, envelope

FIELDS = {"ticker", "instrument_id", "effective_at", "available_at", "open", "high", "low", "close", "volume"}


def read_rows(raw, format):
    safe_bytes(raw)
    if format == "csv":
        try:
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise DataError("COLUMN_NAMES_INVALID")
            rows = []
            for row in reader:
                if None in row or len(rows) >= 100000:
                    raise DataError("ROW_SHAPE_OR_LIMIT_INVALID")
                rows.append(row)
            return rows
        except (UnicodeError, csv.Error):
            raise DataError("CSV_INVALID") from None
    if format == "parquet":
        try:
            import pyarrow.parquet as pq
            import pyarrow as pa
        except ImportError:
            raise DataError("DEPENDENCY_MISSING_PYARROW") from None
        try:
            reader = pq.ParquetFile(pa.BufferReader(raw))
            if reader.metadata.num_rows > 100000 or reader.metadata.serialized_size > 1024 * 1024:
                raise DataError("ROW_LIMIT_EXCEEDED")
            rows = []
            decoded_bytes = 0
            for batch in reader.iter_batches(batch_size=1024):
                # Values must be strings/integers/Decimal; reject float financial columns.
                decoded_bytes += batch.nbytes
                if batch.nbytes > 32 * 1024 * 1024 or decoded_bytes > 64 * 1024 * 1024:
                    raise DataError("PARQUET_EXPANSION_LIMIT")
                rows.extend(batch.to_pylist())
            return rows
        except DataError:
            raise
        except Exception:
            raise DataError("PARQUET_INVALID") from None
    raise DataError("FORMAT_UNSUPPORTED")


def preview_bytes(raw, format, mapping, identities, *, source_id, license_id, scope, received_at):
    """Strict preview; errors carry row number/code only, never row values."""
    if type(mapping) is dict and mapping.get("record_kind") == "tick":
        from .ticks import preview_ticks
        result = preview_ticks(read_rows(raw, format), format, mapping, identities, source_id=source_id,
                               license_id=license_id, scope=scope, received_at=received_at)
        return {**result, "input_hash": __import__("hashlib").sha256(raw).hexdigest()}
    required = {"fields", "price_unit", "volume_unit", "timezone", "frequency", "adjustment"}
    if set(mapping) != required or set(mapping["fields"]) != FIELDS:
        raise DataError("MAPPING_FIELDS_INVALID")
    if mapping["price_unit"] != "USD_per_share" or mapping["volume_unit"] != "shares" or mapping["adjustment"] != "raw":
        raise DataError("MAPPING_UNITS_INVALID")
    if len(set(mapping["fields"].values())) != len(FIELDS):
        raise DataError("MAPPING_COLLISION")
    try:
        zone = ZoneInfo(mapping["timezone"])
    except (KeyError, ValueError, TypeError):
        raise DataError("TIMEZONE_INVALID") from None
    received = parse_instant(received_at)
    records, errors, seen = [], [], set()
    rows = read_rows(raw, format)
    for idx, original in enumerate(rows, 2 if format == "csv" else 1):
        try:
            row = {k: original[v] for k, v in mapping["fields"].items()}
            effective = parse_instant(row["effective_at"])
            if mapping["frequency"] == "1Min" and (effective.second or effective.microsecond):
                raise DataError("BAR_START_NOT_MINUTE_ALIGNED")
            # Require an explicit offset that matches the declared timezone at that date.
            from datetime import datetime
            supplied = datetime.fromisoformat(row["effective_at"])
            if supplied.utcoffset() != effective.astimezone(zone).utcoffset():
                raise DataError("TIMEZONE_OFFSET_MISMATCH")
            instrument = identities_at(identities, row["ticker"], row["effective_at"], row["instrument_id"])
            row["frequency"] = mapping["frequency"]
            row["trading_status"] = "TRADABLE" if instrument.tradable else "HALTED"
            values = bar_values(row)
            end = effective + timedelta(minutes=1) if mapping["frequency"] == "1Min" else effective + timedelta(days=1)
            values["bar_end"] = payload(end)
            values["source_timezone"] = mapping["timezone"]
            declared = parse_instant(row["available_at"])
            # Synthetic clocks are explicit fixture clocks, never historical evidence.
            observed = declared if scope == "SYNTHETIC" else received
            available = max(declared, observed, end)
            values["availability_basis"] = "SYNTHETIC_FIXTURE_CLOCK" if scope == "SYNTHETIC" else "OBSERVED_RECEIPT_ONLY"
            natural = [instrument.instrument_id, payload(effective), mapping["frequency"]]
            key = fingerprint(natural)
            if key in seen:
                raise DataError("DUPLICATE_BAR")
            seen.add(key)
            records.append(envelope(kind="bar", source_id=source_id, license_id=license_id, scope=scope,
                instrument_id=instrument.instrument_id, effective_at=payload(effective), available_at=payload(available),
                received_at=payload(observed), values=values, natural_key=natural))
        except (ValueError, KeyError, TypeError):
            exc = __import__("sys").exc_info()[1]
            errors.append({"row": idx, "code": exc.code if isinstance(exc, DataError) else "ROW_FIELD_INVALID"})
    return {"records": records, "errors": errors, "row_count": len(rows), "valid_rows": len(records),
            "can_import": bool(records) and not errors, "input_hash": __import__("hashlib").sha256(raw).hexdigest()}


def export_bytes(records, format):
    reserved = {"instrument_id", "effective_at", "available_at", "received_at", "source_id", "license_id", "revision"}
    if any(reserved & set(r["values"]) for r in records):
        raise DataError("EXPORT_FIELD_COLLISION")
    rows = [{"instrument_id": r["instrument_id"], **{k: r["metadata"][k] for k in
             ("effective_at", "available_at", "received_at", "source_id", "license_id", "revision")},
             **r["values"]} for r in records]
    if not rows:
        raise DataError("EXPORT_EMPTY")
    if format == "csv":
        fields = sorted(set().union(*(r.keys() for r in rows)))
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for row in rows:
            # Spreadsheet formula injection is blocked for downloaded CSV.
            writer.writerow({k: ("'" + str(v) if str(v).startswith(("=", "+", "-", "@")) else v) for k, v in row.items()})
        return stream.getvalue().encode("utf-8")
    if format == "parquet":
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            raise DataError("DEPENDENCY_MISSING_PYARROW") from None
        stream = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows), stream)
        return stream.getvalue()
    raise DataError("FORMAT_UNSUPPORTED")
