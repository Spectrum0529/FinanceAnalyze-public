"""Lossless provider-history rows via native PyArrow, original guarded RawStore.

The platform owns every path. SQLite publishes a descriptor only in the same
fenced transaction as its observation. As with RawStore, crashes/cancellation
may retain unreferenced complete blobs, never a partially indexed row file.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from finance_platform.core.contracts import canonical, fingerprint, identifier
from finance_platform.data.contracts import DataError
from predictor.data.raw_store import RawStore
from .provider_sdk.artifacts import regular_owned
from .provider_sdk.history_contracts import HISTORY_ROWS

FORMAT = "provider-history-parquet-1"
MAX_BYTES = 64 * 1024**2
FIELDS = ("open", "high", "low", "close", "volume")
MIGRATIONS = ("""
CREATE TABLE data_provider_row_storage(
 result_id TEXT PRIMARY KEY REFERENCES data_provider_observation_results(result_id),
 owner_id TEXT NOT NULL,format TEXT NOT NULL,blob_sha256 TEXT NOT NULL,
 rows_content_hash TEXT NOT NULL,row_count INTEGER NOT NULL,byte_count INTEGER NOT NULL);
""",)


def _arrow():
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pyarrow.dataset as ds
    if pa.__version__ != "21.0.0":
        raise DataError("PARQUET_RUNTIME_UNREVIEWED")
    return pa, pq, ds


def _schema(pa):
    # Exact provider decimal strings stay strings: no float conversion,
    # quantization, inferred decimal scale or change to upstream SDK rounding.
    return pa.schema([pa.field("ordinal", pa.uint32(), nullable=False),
        pa.field("session_date", pa.date32(), nullable=False),
        *(pa.field(k, pa.string(), nullable=False) for k in FIELDS),
        pa.field("attributes", pa.binary(), nullable=False)],
        metadata={b"format": FORMAT.encode()})


def _valid_rows(rows):
    if type(rows) is not list or not 0 < len(rows) <= HISTORY_ROWS:
        raise DataError("PARQUET_ROWS_INVALID")
    days = []
    for row in rows:
        if type(row) is not dict:
            raise DataError("PARQUET_ROWS_INVALID")
        day = row.get("session_date")
        try:
            if type(day) is not str or date.fromisoformat(day).isoformat() != day:
                raise ValueError
            for key in FIELDS:
                if type(row.get(key)) is not str or not Decimal(row[key]).is_finite():
                    raise ValueError
            # Full canonical/secret validation is performed by the logical
            # hash before publication, and again on every verified read.
        except (ValueError, InvalidOperation):
            raise DataError("PARQUET_ROWS_INVALID") from None
        days.append(day)
    if days != sorted(set(days)):
        raise DataError("PARQUET_ROWS_INVALID")


def window_indices(rows, start, end):
    """Filter authorized in-memory rows with the official Dataset API.

    Full blob/logical hashes are verified before selecting a window. This is
    an in-memory dataset scan, not a claim of file-level predicate pushdown.
    """
    pa, _, ds = _arrow()
    table = pa.table({"ordinal": pa.array(range(len(rows)), type=pa.uint32()),
        "session_date": pa.array([date.fromisoformat(r["session_date"]) for r in rows], type=pa.date32())})
    selection = ((ds.field("session_date") >= pa.scalar(date.fromisoformat(start), type=pa.date32()))
                 & (ds.field("session_date") < pa.scalar(date.fromisoformat(end), type=pa.date32())))
    return ds.dataset(table).to_table(columns=["ordinal"], filter=selection, use_threads=False)["ordinal"].to_pylist()


class ParquetRows:
    def __init__(self, policy):
        self.policy = policy

    def _store(self, owner, *, binding=None, read_scope=None):
        identifier(owner)
        directory = self.policy.partitions["development"] / "platform" / owner / "history-parquet"
        # No caller-supplied path. Refuse aliases across owner namespaces even
        # when both endpoints are in the allowed development partition.
        for item in (directory, *directory.parents):
            if item.exists() or item.is_symlink():
                info = item.lstat()
                if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise DataError("PARQUET_PATH_INVALID")
            if item == self.policy.root:
                break
        approved = self.policy.authorize(directory)
        if approved != directory or not approved.is_relative_to(self.policy.partitions["development"]):
            raise DataError("PARQUET_PATH_INVALID")
        market=getattr(self.policy,'market_storage',None)
        return market.raw(owner,directory,binding=binding,scope=read_scope) if market else RawStore(self.policy,directory)

    @staticmethod
    def _decode(raw, descriptor):
        pa, pq, _ = _arrow()
        if (type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES
                or len(raw) != descriptor["byte_count"]
                or hashlib.sha256(raw).hexdigest() != descriptor["blob_sha256"]):
            raise DataError("PARQUET_BYTE_HASH_MISMATCH")
        try:
            table = pq.read_table(pa.BufferReader(raw), use_threads=False)
            if not table.schema.equals(_schema(pa), check_metadata=True) or table.num_rows != descriptor["row_count"]:
                raise ValueError
            rows = []
            for index, record in enumerate(table.to_pylist()):
                attrs = json.loads(record["attributes"])
                if (record["ordinal"] != index or type(attrs) is not dict
                        or set(attrs) & {"session_date", *FIELDS}):
                    raise ValueError
                rows.append({**attrs, "session_date": record["session_date"].isoformat(),
                             **{k: record[k] for k in FIELDS}})
            _valid_rows(rows)
        except (ValueError, TypeError, KeyError, AttributeError, pa.ArrowException):
            raise DataError("PARQUET_CONTENT_INVALID") from None
        if fingerprint(rows) != descriptor["rows_content_hash"]:
            raise DataError("PARQUET_LOGICAL_HASH_MISMATCH")
        return rows

    def write(self, context, owner, rows):
        if context.run.owner_id != owner:
            raise DataError("RESOURCE_NOT_FOUND")
        context.require_active()
        _valid_rows(rows)
        pa, pq, _ = _arrow()
        # Canonicalize the complete logical input once. Attributes below
        # are subsets of that already validated JSON object, not new input.
        canonical_rows = canonical(rows).encode()
        normalized = json.loads(canonical_rows)
        records = [{"ordinal": i, "session_date": date.fromisoformat(r["session_date"]),
                    **{k: r[k] for k in FIELDS},
                    "attributes": json.dumps({k: v for k, v in r.items() if k not in {"session_date", *FIELDS}},
                        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()}
                   for i, r in enumerate(normalized)]
        sink = pa.BufferOutputStream()
        pq.write_table(pa.Table.from_pylist(records, schema=_schema(pa)), sink,
                       version="2.6", compression="zstd", row_group_size=1024, write_statistics=True)
        raw = sink.getvalue().to_pybytes()
        descriptor = {"format": FORMAT, "blob_sha256": hashlib.sha256(raw).hexdigest(),
                      "rows_content_hash": hashlib.sha256(canonical_rows).hexdigest(), "row_count": len(rows), "byte_count": len(raw)}
        # Verify the serialized native format before any SQL publication.
        if self._decode(raw, descriptor) != normalized:
            raise DataError("PARQUET_LOGICAL_HASH_MISMATCH")
        context.require_active()
        market=getattr(self.policy,'market_storage',None)
        binding=market.bind(owner,context=context) if market else None
        store = self._store(owner,binding=binding)
        digest = store._publish(raw)  # Original fsync/exclusive-link implementation.
        context.require_active()     # Cancellation leaves only an unreferenced blob.
        if digest != descriptor["blob_sha256"]:
            raise DataError("PARQUET_BYTE_HASH_MISMATCH")
        # The final object must be acceptable to the supported reader BEFORE
        # returning a descriptor or entering the fenced SQLite publication.
        self._final_bytes(owner, descriptor,read_scope=store.scope if market else None)
        context.require_active()
        return descriptor

    def read(self, owner, descriptor, *, read_scope=None):
        expected = {"format", "blob_sha256", "rows_content_hash", "row_count", "byte_count"}
        if (type(descriptor) is not dict or set(descriptor) != expected or descriptor["format"] != FORMAT
                or any(type(descriptor[k]) is not str or not re.fullmatch("[0-9a-f]{64}", descriptor[k])
                       for k in ("blob_sha256", "rows_content_hash"))
                or type(descriptor["row_count"]) is not int or not 0 < descriptor["row_count"] <= HISTORY_ROWS
                or type(descriptor["byte_count"]) is not int or not 0 < descriptor["byte_count"] <= MAX_BYTES):
            raise DataError("PARQUET_DESCRIPTOR_INVALID")
        return self._decode(self._final_bytes(owner, descriptor,read_scope=read_scope), descriptor)

    def _final_bytes(self, owner, descriptor, *, read_scope=None):
        """Same lexical/type/link/byte rules for write acceptance and reads.

        An interrupted RawStore publication may leave a complete two-link
        object. Refuse it before SQL; retain its names and original evidence.
        """
        store = self._store(owner,read_scope=read_scope)
        if read_scope is not None:
            raw=store._blob(descriptor['blob_sha256'])
            if len(raw)!=descriptor['byte_count']:raise DataError('PARQUET_BYTE_HASH_MISMATCH')
            return raw
        try:
            path = regular_owned(store.directory, "objects/" + descriptor["blob_sha256"] + ".bin")
            if path.stat().st_size != descriptor["byte_count"]:
                raise DataError("PARQUET_BYTE_HASH_MISMATCH")
            raw = self.policy.read_bytes(path)
        except OSError:
            raise DataError("PARQUET_ARTIFACT_UNAVAILABLE") from None
        if (len(raw) != descriptor["byte_count"]
                or hashlib.sha256(raw).hexdigest() != descriptor["blob_sha256"]):
            raise DataError("PARQUET_BYTE_HASH_MISMATCH")
        return raw
