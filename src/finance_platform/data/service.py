"""Data domain persistence using the common platform Database migration API."""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3
from uuid import uuid4

from finance_platform.core.contracts import canonical, fingerprint, parse_instant, payload, identifier
from finance_platform.core.security import clean
from predictor.access import AccessPolicy
from predictor.data.raw_store import RawStore
from predictor.research.append_guards import install as install_guards
from .contracts import DataError, now, validate_record, safe_bytes, validate_paging
from .importer import preview_bytes, export_bytes

MIGRATIONS = ("""
CREATE TABLE data_sources(source_id TEXT NOT NULL,version INTEGER NOT NULL,owner_id TEXT NOT NULL,
 body TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(source_id,version));
CREATE TABLE data_datasets(dataset_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,source_id TEXT NOT NULL,
 source_version INTEGER NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL,
 FOREIGN KEY(source_id,source_version) REFERENCES data_sources(source_id,version));
CREATE TABLE data_records(dataset_id TEXT NOT NULL REFERENCES data_datasets(dataset_id),record_id TEXT NOT NULL,
 revision INTEGER NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL,PRIMARY KEY(dataset_id,record_id,revision));
CREATE TABLE data_snapshots(snapshot_id TEXT PRIMARY KEY,dataset_id TEXT NOT NULL REFERENCES data_datasets(dataset_id),
 owner_id TEXT NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE data_collection_pages(collection_id TEXT NOT NULL,ordinal INTEGER NOT NULL,owner_id TEXT NOT NULL,
 binding_hash TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(collection_id,ordinal));
CREATE TABLE data_collection_events(event_id INTEGER PRIMARY KEY,collection_id TEXT NOT NULL,owner_id TEXT NOT NULL,
 body TEXT NOT NULL,created_at TEXT NOT NULL);
""",)


class DataService:
    """Trusted installed service; routes/jobs must supply authenticated owner scope.

    All queries require owner_id, and only root runtime can grant administrative scope.
    No raw/holdout/prospective reads or research approval are implemented here.
    """
    def __init__(self, database, policy):
        self.db, self.policy = database, policy
        from .market_storage import MarketStorage
        self.market=MarketStorage(database,policy)
        database.apply_module_migrations("data", MIGRATIONS)
        with database.transaction() as conn:
            for table in ("data_sources", "data_datasets", "data_records", "data_snapshots",
                          "data_collection_pages", "data_collection_events"):
                for action in ("UPDATE", "DELETE"):
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Append-only data domain'); END")
            install_guards(conn, ("data_sources", "data_datasets", "data_records", "data_snapshots", "data_collection_pages", "data_collection_events"))

    def register_source(self, owner_id, source):
        fields = {"source_id", "name", "provider", "capabilities", "assets", "frequencies", "fields",
                  "declared_history", "verified_coverage", "license", "connection"}
        if type(source) is not dict or set(source) != fields:
            raise DataError("SOURCE_SCHEMA_INVALID")
        identifier(owner_id)
        identifier(source["source_id"])
        if source["provider"] not in {"local", "synthetic", "nyse", "alpaca", "sec", "rest"}:
            raise DataError("PROVIDER_UNSUPPORTED")
        if not isinstance(source["name"], str) or not 1 <= len(source["name"]) <= 200:
            raise DataError("SOURCE_SCHEMA_INVALID")
        for field in ("capabilities", "assets", "frequencies", "fields"):
            if not isinstance(source[field], list) or len(source[field]) > 100 or any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in source[field]):
                raise DataError("SOURCE_SCHEMA_INVALID")
        if source["verified_coverage"] != []:
            raise DataError("PROVIDER_COVERAGE_REVIEW_REQUIRED")
        license = source["license"]
        if set(license) != {"license_id", "evidence", "use_allowed", "rights_verified"} or type(license["rights_verified"]) is not bool:
            raise DataError("LICENSE_SCHEMA_INVALID")
        identifier(license["license_id"])
        if not isinstance(license["use_allowed"], list) or set(license["use_allowed"]) - {"development", "export"}:
            raise DataError("LICENSE_PURPOSE_INVALID")
        if license["rights_verified"] and not license["evidence"]:
            raise DataError("LICENSE_EVIDENCE_REQUIRED")
        from .network import ConnectionConfig
        ConnectionConfig.from_mapping(source["connection"], source["provider"])
        encoded = canonical(clean(source))
        with self.db.transaction() as conn:
            latest = conn.execute("SELECT * FROM data_sources WHERE source_id=? ORDER BY version DESC LIMIT 1", (source["source_id"],)).fetchone()
            if latest and latest["owner_id"] != owner_id:
                raise DataError("RESOURCE_NOT_FOUND")
            if latest and latest["content_hash"] == fingerprint(source):
                return {"source_id": source["source_id"], "version": latest["version"], "content_hash": latest["content_hash"]}
            version = latest["version"] + 1 if latest else 1
            conn.execute("INSERT INTO data_sources VALUES(?,?,?,?,?,?)", (source["source_id"], version, owner_id, encoded, fingerprint(source), now()))
        return {"source_id": source["source_id"], "version": version, "content_hash": fingerprint(source)}

    def source(self, owner_id, source_id, version=None):
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT * FROM data_sources WHERE owner_id=? AND source_id=? " +
                               ("AND version=?" if version is not None else "ORDER BY version DESC LIMIT 1"),
                               (owner_id, source_id, version) if version is not None else (owner_id, source_id)).fetchone()
        if row is None:
            raise DataError("RESOURCE_NOT_FOUND")
        body = json.loads(row["body"])
        if fingerprint(body) != row["content_hash"]:
            raise DataError("SOURCE_HASH_MISMATCH")
        return {**body, "version": row["version"], "content_hash": row["content_hash"]}

    def list_sources(self, owner_id, limit=50, offset=0):
        validate_paging(limit, offset)
        with closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT s.* FROM data_sources s WHERE owner_id=? AND version=(SELECT max(version) FROM data_sources t WHERE t.source_id=s.source_id) ORDER BY source_id LIMIT ? OFFSET ?", (owner_id, limit, offset)).fetchall()
            total = conn.execute("SELECT count(DISTINCT source_id) FROM data_sources WHERE owner_id=?", (owner_id,)).fetchone()[0]
        return {"items": [{**json.loads(r["body"]), "version": r["version"], "content_hash": r["content_hash"]} for r in rows], "total": total, "limit": limit, "offset": offset}

    def create_dataset(self, owner_id, source_id, *, scope, identities, title="Development dataset", issuer_ids=()):
        from finance_platform.core.contracts import Instrument
        source = self.source(owner_id, source_id)
        self._rights(source, scope, "development")
        mappings = [payload(Instrument.from_mapping(i)) for i in identities]
        # Reject overlapping stable/ticker mappings, including recycled symbols.
        for idx, a in enumerate(mappings):
            for b in mappings[idx+1:]:
                if a["ticker"] == b["ticker"] or a["instrument_id"] == b["instrument_id"]:
                    if (b["ticker_valid_to"] is None or parse_instant(a["ticker_valid_from"]) < parse_instant(b["ticker_valid_to"])) and (a["ticker_valid_to"] is None or parse_instant(b["ticker_valid_from"]) < parse_instant(a["ticker_valid_to"])):
                        raise DataError("IDENTITY_OVERLAP")
        import re
        if not isinstance(issuer_ids, (list, tuple)) or any(not isinstance(i, str) or not re.fullmatch(r"sec-cik:[0-9]{10}", i) for i in issuer_ids) or len(set(issuer_ids)) != len(issuer_ids):
            raise DataError("ISSUER_IDS_INVALID")
        body = {"scope": scope, "identities": mappings, "issuer_ids": list(issuer_ids), "title": clean(title), "source_id": source_id,
                "source_version": source["version"], "formal_research_approved": False}
        dataset_id = "dataset-" + uuid4().hex
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO data_datasets VALUES(?,?,?,?,?,?,?)", (dataset_id, owner_id, source_id, source["version"], canonical(body), fingerprint(body), now()))
        return {"dataset_id": dataset_id, **body, "content_hash": fingerprint(body)}

    @staticmethod
    def _rights(source, scope, purpose):
        if scope not in {"SYNTHETIC", "DEVELOPMENT"}:
            raise DataError("DATA_SCOPE_FORBIDDEN")
        lic = source["license"]
        if purpose not in lic["use_allowed"]:
            raise DataError("DATA_USE_NOT_LICENSED")
        if scope != "SYNTHETIC" and not lic["rights_verified"]:
            raise DataError("DATA_RIGHTS_UNVERIFIED")

    def dataset(self, owner_id, dataset_id):
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT * FROM data_datasets WHERE dataset_id=? AND owner_id=?", (dataset_id, owner_id)).fetchone()
        if row is None:
            raise DataError("RESOURCE_NOT_FOUND")
        body = json.loads(row["body"])
        if fingerprint(body) != row["content_hash"]:
            raise DataError("DATASET_HASH_MISMATCH")
        return {"dataset_id": dataset_id, **body, "content_hash": row["content_hash"]}

    def _raw(self, owner_id, *, binding=None):
        identifier(owner_id)
        return self.market.raw(owner_id,self.policy.partitions["development"] / "platform" / owner_id / "raw",binding=binding)

    def coverage(self, owner_id, dataset_id):
        dataset = self.dataset(owner_id, dataset_id)
        groups = {}
        with closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT body FROM data_records WHERE dataset_id=? ORDER BY record_id,revision", (dataset_id,)).fetchall()
        for row in rows:
            r = self.market.unpack(owner_id,row[0])
            key = (r["instrument_id"], r["kind"], r["values"].get("frequency", "event"))
            group = groups.setdefault(key, {"instrument_id": key[0], "kind": key[1], "frequency": key[2], "versions": 0, "logical_ids": set(), "dates": set(), "first": None, "last": None})
            day = r["metadata"]["effective_at"]
            group["versions"] += 1
            group["logical_ids"].add(r["metadata"]["record_id"])
            group["dates"].add(day[:10])
            group["first"] = min(group["first"] or day, day)
            group["last"] = max(group["last"] or day, day)
        items = [{**{k: v for k, v in g.items() if k not in {"logical_ids", "dates"}}, "logical_records": len(g["logical_ids"]), "observed_dates": sorted(g["dates"])} for g in groups.values()]
        return {"dataset_id": dataset_id, "scope": dataset["scope"], "items": items, "expected_universe": "UNKNOWN", "coverage_certification": "OBSERVED_IMPORTED_ROWS_ONLY", "formal_research_approved": False}

    def archive(self, owner_id, source, raw, *, parameters, received_at, scope, schema="platform-data-1", binding=None):
        """Every response is a single immutable RawStore batch; pagination lives above it."""
        safe_bytes(raw)
        batch = "capture-" + uuid4().hex
        store = self._raw(owner_id,binding=binding or self.market.bind(owner_id))
        url = source["connection"].get("endpoint") or "https://local.invalid/import"
        context = {"request": {"source_id": source["source_id"], "source_url": url, "parameters": parameters,
                               "schema_version": schema, "data_scope": "synthetic_only" if scope == "SYNTHETIC" else "public_provider_sample"},
                   "license": {"id": source["license"]["license_id"], "status": "DECLARED" if source["license"]["rights_verified"] else "UNVERIFIED", "usage": "development"},
                   "expected_pages": 1, "expected_response_hash": hashlib.sha256(raw).hexdigest(), "expected_records": None, "source_version": None}
        store.begin_batch(batch, context)
        store.put_page(batch, 0, raw, {"received_at": received_at, "http_status": 200, "response_headers": {}, "request_headers": None})
        return store.finish_batch(batch, record_count=None)

    def append_records(self, owner_id, dataset_id, records, *, lineage):
        dataset = self.dataset(owner_id, dataset_id)
        source = self.source(owner_id, dataset["source_id"], dataset["source_version"])
        self._rights(source, dataset["scope"], "development")
        count = 0
        binding=self.market.bind(owner_id)
        with self.db.transaction() as conn:
            for original in records:
                r = json.loads(canonical(original))
                p = validate_record(r, require_action_evidence=True)
                if p.source_id != dataset["source_id"] or p.scope.value != dataset["scope"] or p.license_id != source["license"]["license_id"]:
                    raise DataError("PROVENANCE_BINDING_INVALID")
                if r["kind"] in {"bar", "tick", "action", "identity"}:
                    from .contracts import identities_at
                    candidates = [i for i in dataset["identities"] if i["instrument_id"] == r["instrument_id"]]
                    if not any(parse_instant(i["ticker_valid_from"]) <= p.effective_at and (i["ticker_valid_to"] is None or p.effective_at < parse_instant(i["ticker_valid_to"])) for i in candidates):
                        raise DataError("IDENTITY_OUTSIDE_VALIDITY")
                    if r["kind"] == "tick" and r["values"]["trading_status"] == "TRADABLE" and not any(i["tradable"] for i in candidates):
                        raise DataError("IDENTITY_NOT_TRADABLE")
                elif r["instrument_id"] not in dataset["issuer_ids"]:
                    raise DataError("ISSUER_NOT_DECLARED")
                latest = conn.execute("SELECT body FROM data_records WHERE dataset_id=? AND record_id=? ORDER BY revision DESC LIMIT 1", (dataset_id, p.record_id)).fetchone()
                if latest:
                    old = self.market.unpack(owner_id,latest["body"])
                    if old["instrument_id"] != r["instrument_id"] or old["kind"] != r["kind"] or old["metadata"]["effective_at"] != r["metadata"]["effective_at"]:
                        raise DataError("REVISION_IDENTITY_CHANGED")
                    if old["metadata"]["content_hash"] == p.content_hash:
                        continue
                    if p.available_at < parse_instant(old["metadata"]["available_at"]) or p.received_at < parse_instant(old["metadata"]["received_at"]):
                        raise DataError("REVISION_TIME_REVERSED")
                    r["metadata"]["revision"] = old["metadata"]["revision"] + 1
                else:
                    r["metadata"]["revision"] = 1
                r["lineage"] = lineage
                conn.execute("INSERT INTO data_records VALUES(?,?,?,?,?)", (dataset_id, p.record_id, r["metadata"]["revision"], canonical(self.market.pack(owner_id,r,binding,connection=conn)), fingerprint(r)))
                count += 1
        return {"appended": count, "submitted": len(records)}

    def preview(self, owner_id, dataset_id, raw, format, mapping, *, received_at=None):
        dataset = self.dataset(owner_id, dataset_id)
        source = self.source(owner_id, dataset["source_id"], dataset["source_version"])
        return preview_bytes(raw, format, mapping, dataset["identities"], source_id=dataset["source_id"],
                             license_id=source["license"]["license_id"], scope=dataset["scope"], received_at=received_at or now())

    def import_bytes(self, owner_id, dataset_id, raw, format, mapping, *, received_at=None):
        from .market_storage import using
        with using(self.market.bind(owner_id)):
            return self._import_bytes_bound(owner_id,dataset_id,raw,format,mapping,received_at=received_at)

    def _import_bytes_bound(self, owner_id, dataset_id, raw, format, mapping, *, received_at=None):
        received_at = received_at or now()
        result = self.preview(owner_id, dataset_id, raw, format, mapping, received_at=received_at)
        if not result["can_import"]:
            raise DataError("IMPORT_PREVIEW_HAS_ERRORS")
        dataset = self.dataset(owner_id, dataset_id)
        source = self.source(owner_id, dataset["source_id"], dataset["source_version"])
        raw_ref = self.archive(owner_id, source, raw, parameters={"dataset_id": dataset_id, "format": format, "mapping": mapping, "mapping_hash": fingerprint(mapping)}, received_at=received_at, scope=dataset["scope"])
        return {**self.append_records(owner_id, dataset_id, result["records"], lineage=raw_ref), "raw": raw_ref}

    def import_path(self, owner_id, dataset_id, path, format, mapping):
        return self.import_bytes(owner_id, dataset_id, self.policy.read_bytes(path), format, mapping)

    def import_events_bytes(self, owner_id, dataset_id, raw):
        from .market_storage import using
        with using(self.market.bind(owner_id)):
            return self._import_events_bound(owner_id,dataset_id,raw)

    def _import_events_bound(self, owner_id, dataset_id, raw):
        from .contracts import load_json
        dataset = self.dataset(owner_id, dataset_id)
        source = self.source(owner_id, dataset["source_id"], dataset["source_version"])
        records = load_json(raw)
        if not isinstance(records, list) or not 1 <= len(records) <= 10000:
            raise DataError("EVENT_IMPORT_SCHEMA_INVALID")
        received_at = now()
        for record in records:
            validate_record(record, require_action_evidence=True)
            if record["kind"] == "tick":
                record["metadata"]["received_at"] = received_at
                if not (dataset["scope"] == "SYNTHETIC" and record["values"].get("availability_basis") == "SYNTHETIC_FIXTURE_CLOCK"):
                    record["metadata"]["available_at"] = payload(max(parse_instant(record["metadata"]["available_at"]), parse_instant(received_at)))
                validate_record(record)
            elif dataset["scope"] != "SYNTHETIC":
                record["metadata"]["received_at"] = received_at
                record["metadata"]["available_at"] = payload(max(parse_instant(record["metadata"]["available_at"]), parse_instant(received_at)))
        raw_ref = self.archive(owner_id, source, raw, parameters={"dataset_id": dataset_id, "format": "events-json"}, received_at=received_at,
                               scope=dataset["scope"], schema="platform-data-events-1")
        return {**self.append_records(owner_id, dataset_id, records, lineage=raw_ref), "raw": raw_ref}

    def snapshot(self, owner_id, dataset_id, *, as_of, instrument_ids, expected_keys=(), max_delay_seconds=None, expected_bars=(), effective_at=None, required_kinds=None):
        dataset = self.dataset(owner_id, dataset_id)
        cutoff = parse_instant(as_of)
        economic = parse_instant(effective_at) if effective_at is not None else cutoff
        if economic > cutoff:
            raise DataError("ECONOMIC_HORIZON_AFTER_ASOF")
        if not isinstance(instrument_ids, (list, tuple)) or not instrument_ids or any(i not in {v["instrument_id"] for v in dataset["identities"]} | set(dataset["issuer_ids"]) for i in instrument_ids):
            raise DataError("SNAPSHOT_INSTRUMENT_INVALID")
        selected = {}
        with closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT body,content_hash FROM data_records WHERE dataset_id=? ORDER BY record_id,revision", (dataset_id,)).fetchall()
        for row in rows:
            r = self.market.unpack(owner_id,row["body"])
            if fingerprint(r) != row["content_hash"]:
                raise DataError("RECORD_STORED_HASH_MISMATCH")
            validate_record({k: v for k, v in r.items() if k != "lineage"})
            m = r["metadata"]
            if r["instrument_id"] in instrument_ids and parse_instant(m["available_at"]) <= cutoff and parse_instant(m["effective_at"]) <= economic:
                selected[m["record_id"]] = r
        records = [selected[k] for k in sorted(selected) if selected[k]["kind"] != "identity" or selected[k]["metadata"]["valid_to"] is None or economic < parse_instant(selected[k]["metadata"]["valid_to"])]
        if required_kinds is not None and (type(required_kinds) not in (list, tuple) or not required_kinds or any(type(k) is not str or k not in {"bar", "tick", "action", "identity", "filing", "fact"} for k in required_kinds) or len(set(required_kinds)) != len(required_kinds)):
            raise DataError("QUALITY_RULE_INVALID")
        require_bars = ("bar" in required_kinds) if required_kinds is not None else bool(set(instrument_ids) & {v["instrument_id"] for v in dataset["identities"]})
        quality = quality_report(records, expected_keys=expected_keys, max_delay_seconds=max_delay_seconds, require_bars=require_bars, expected_bars=expected_bars, required_kinds=required_kinds or ())
        body = {"dataset_id": dataset_id, "as_of": payload(cutoff), "effective_at": payload(economic), "instrument_ids": sorted(set(instrument_ids)),
                "records": records, "quality": quality, "scope": dataset["scope"], "formal_research_approved": False}
        if required_kinds is not None:
            body["required_kinds"] = sorted(required_kinds)
        snapshot_id = "snapshot-" + uuid4().hex
        stored_body=self.market.pack(owner_id,body,self.market.bind(owner_id))
        stored_body['quality']={'allowed':body['quality']['allowed']}
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO data_snapshots VALUES(?,?,?,?,?,?)", (snapshot_id, dataset_id, owner_id, canonical(stored_body), fingerprint(body), now()))
        return {"snapshot_id": snapshot_id, "content_hash": fingerprint(body), **body}

    def get_snapshot(self, owner_id, snapshot_id, *, require_quality=True):
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT * FROM data_snapshots WHERE snapshot_id=? AND owner_id=?", (snapshot_id, owner_id)).fetchone()
        if row is None:
            raise DataError("RESOURCE_NOT_FOUND")
        body = self.market.unpack(owner_id,row["body"])
        if fingerprint(body) != row["content_hash"]:
            raise DataError("SNAPSHOT_HASH_MISMATCH")
        if require_quality and not body["quality"]["allowed"]:
            raise DataError("DATA_QUALITY_BLOCKED")
        return {"snapshot_id": snapshot_id, "content_hash": row["content_hash"], **body}

    def authorize_snapshot(self, owner_id, snapshot_id, *, purpose="development", expected_origin=None):
        """Read-only purpose check plus complete immutable origin binding.

        owner_id comes from the authenticated caller, never from supplied origin.
        Returned metadata is an identity binding, not a reusable permission token.
        Export callers must recheck the stored binding against this authoritative
        snapshot/dataset/source version on every download.
        """
        if type(purpose) is not str or purpose not in {"development", "export"}:
            raise DataError("DATA_PURPOSE_FORBIDDEN")
        snap = self.get_snapshot(owner_id, snapshot_id)
        dataset = self.dataset(owner_id, snap["dataset_id"])
        source = self.source(owner_id, dataset["source_id"], dataset["source_version"])
        if snap["scope"] != dataset["scope"]:
            raise DataError("SNAPSHOT_ORIGIN_MISMATCH")
        for record in snap["records"]:
            meta = record["metadata"]
            if (meta["source_id"] != dataset["source_id"] or meta["license_id"] != source["license"]["license_id"]
                    or meta["scope"] != dataset["scope"]):
                raise DataError("SNAPSHOT_ORIGIN_MISMATCH")
        origin = {"schema_version": "platform-data-origin-1", "origin_kind": "data_snapshot",
                  "snapshot_id": snapshot_id, "snapshot_hash": snap["content_hash"],
                  "dataset_id": dataset["dataset_id"], "dataset_hash": dataset["content_hash"],
                  "source_id": dataset["source_id"], "source_version": dataset["source_version"],
                  "source_hash": source["content_hash"], "license_id": source["license"]["license_id"],
                  "license_hash": fingerprint(source["license"]), "scope": dataset["scope"],
                  "as_of": snap["as_of"], "effective_at": snap["effective_at"]}
        if expected_origin is not None:
            if type(expected_origin) is not dict or set(expected_origin) != set(origin):
                raise DataError("SNAPSHOT_ORIGIN_MISMATCH")
            try:
                # canonical also rejects credential/service/non-finite metadata.
                matches = canonical(expected_origin) == canonical(origin)
            except (TypeError, ValueError):
                raise DataError("SNAPSHOT_ORIGIN_MISMATCH") from None
            if not matches:
                raise DataError("SNAPSHOT_ORIGIN_MISMATCH")
        self._rights(source, dataset["scope"], purpose)
        return origin

    def export(self, owner_id, snapshot_id, format):
        self.authorize_snapshot(owner_id, snapshot_id, purpose="export")
        snap = self.get_snapshot(owner_id, snapshot_id)
        return export_bytes(snap["records"], format)


def quality_report(records, *, expected_keys=(), max_delay_seconds=None, require_bars=True, expected_bars=(), required_kinds=()):
    if not isinstance(expected_keys, (list, tuple)) or any(not isinstance(k, str) for k in expected_keys) or (max_delay_seconds is not None and (type(max_delay_seconds) is not int or max_delay_seconds < 0)):
        raise DataError("QUALITY_RULE_INVALID")
    issues, seen, delays = [], set(), []
    bars = [r for r in records if r["kind"] == "bar"]
    ticks = [r for r in records if r["kind"] == "tick"]
    for kind in required_kinds:
        if not any(r["kind"] == kind for r in records):
            issues.append({"code": "MISSING_REQUIRED_KIND", "kind": kind})
    if not isinstance(expected_bars, (list, tuple)) or len(expected_bars) > 100000:
        raise DataError("QUALITY_RULE_INVALID")
    observed = {(r["instrument_id"], r["metadata"]["effective_at"], r["values"]["frequency"]) for r in bars}
    missing_bars = []
    for expected in expected_bars:
        if type(expected) is not dict or set(expected) != {"instrument_id", "effective_at", "frequency"} or expected["frequency"] not in {"1Day", "1Min"}:
            raise DataError("QUALITY_RULE_INVALID")
        key = (identifier(expected["instrument_id"]), payload(parse_instant(expected["effective_at"])), expected["frequency"])
        if key not in observed:
            missing_bars.append(expected)
    if missing_bars:
        issues.append({"code": "MISSING_EXPECTED_BARS", "count": len(missing_bars)})
    for r in records:
        m, v = r["metadata"], r["values"]
        key = m["record_id"]
        if key in seen:
            issues.append({"code": "DUPLICATE", "record_id": key})
        seen.add(key)
        delay = (parse_instant(m["available_at"]) - parse_instant(v.get("bar_end", m["effective_at"]))).total_seconds()
        delays.append(delay)
        if max_delay_seconds is not None and delay > max_delay_seconds:
            issues.append({"code": "DELAY_LIMIT", "record_id": key})
        if r["kind"] in {"bar", "tick"} and (v["trading_status"] != "TRADABLE" or __import__("decimal").Decimal(v["volume"]) == 0):
            issues.append({"code": "NOT_TRADABLE_OR_ZERO_VOLUME", "record_id": key})
        if r["kind"] == "tick" and m["revision"] > 1:
            issues.append({"code": "TICK_REVISION_EXECUTION_BLOCKED", "record_id": key})
        if r["kind"] == "action" and v["status"] == "PENDING":
            issues.append({"code": "ACTION_PENDING", "record_id": key})
        if r["kind"] == "action":
            from .contracts import validate_action_evidence
            try:
                validate_action_evidence(v)
            except (DataError, ValueError):
                issues.append({"code": "ACTION_EXECUTION_EVIDENCE_INVALID", "record_id": key})
            if m["revision"] > 1:
                issues.append({"code": "ACTION_REVISION_EXECUTION_BLOCKED", "record_id": key})
            if v["status"] == "CANCELLED" or (v["type"] == "TERMINAL" and v["status"] == "RESOLVED"):
                issues.append({"code": "ACTION_EXECUTION_UNSUPPORTED", "record_id": key})
    missing = sorted(set(expected_keys) - seen)
    if missing:
        issues.append({"code": "MISSING_EXPECTED_RECORDS", "count": len(missing)})
    if require_bars and not bars:
        issues.append({"code": "NO_PRICE_BARS"})
    if not records:
        issues.append({"code": "NO_VISIBLE_RECORDS"})
    result = {"allowed": not issues, "issues": issues, "missing_record_ids": missing, "missing_bars": missing_bars,
            "bar_count": len(bars), "record_count": len(records), "coverage_status": "EXPECTED_KEYS_CHECKED" if expected_keys or expected_bars else "UNIVERSE_COVERAGE_UNKNOWN",
            "first_effective_at": min((r["metadata"]["effective_at"] for r in records), default=None),
            "last_effective_at": max((r["metadata"]["effective_at"] for r in records), default=None),
            "max_delay_seconds": str(max(delays)) if delays else None}
    if ticks or required_kinds:
        result.update(tick_count=len(ticks), required_kinds=sorted(required_kinds))
    return result
