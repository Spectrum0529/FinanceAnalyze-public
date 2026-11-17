"""Owner-scoped immutable security/history vintages and recoverable lifecycle."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
from uuid import uuid4

from finance_platform.core.pending_writes import require_open_key
from finance_platform.core.contracts import canonical, fingerprint, identifier, parse_instant
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.data.contracts import DataError, validate_paging
from .provider_sdk.contracts import now
from .provider_sdk.history_contracts import HISTORY_PROFILE, HISTORY_ROWS, history_window, symbol
from .provider_sdk.history_contracts import YAHOO_PRICE_POLICY, SINA_PRICE_POLICY, QUANTITY_BASIS, NATIVE_INTERFACE, NATIVE_PRICE_POLICY, NATIVE_SOURCE_ID
from .provider_sdk import sina_quality
from .parquet_store import window_indices

MIGRATIONS = ("""
CREATE TABLE data_security_identities(security_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,ticker TEXT NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX data_security_owner_ticker ON data_security_identities(owner_id,ticker,created_at);
CREATE TABLE data_history_series(history_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,provider TEXT NOT NULL,ticker TEXT NOT NULL,series_key TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX data_history_owner_key ON data_history_series(owner_id,series_key,created_at);
CREATE INDEX data_history_owner_lookup ON data_history_series(owner_id,provider,ticker,created_at);
CREATE TABLE data_history_versions(history_id TEXT NOT NULL REFERENCES data_history_series(history_id),version INTEGER NOT NULL,owner_id TEXT NOT NULL,observation_id TEXT NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(history_id,version));
CREATE INDEX data_history_owner_observation ON data_history_versions(owner_id,observation_id);
CREATE TABLE data_history_events(history_id TEXT NOT NULL REFERENCES data_history_series(history_id),revision INTEGER NOT NULL,owner_id TEXT NOT NULL,action TEXT NOT NULL,name TEXT NOT NULL,deleted INTEGER NOT NULL,idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(history_id,revision),UNIQUE(owner_id,idempotency_key));
CREATE TABLE data_history_pins(history_id TEXT NOT NULL,version INTEGER NOT NULL,owner_id TEXT NOT NULL,kind TEXT NOT NULL,reference_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(history_id,version,owner_id,kind,reference_id),FOREIGN KEY(history_id,version) REFERENCES data_history_versions(history_id,version));
""","""
CREATE TABLE data_security_identity_namespaces(security_id TEXT PRIMARY KEY REFERENCES data_security_identities(security_id),namespace TEXT NOT NULL);
""")


def _verified(row):
    body = json.loads(row["body"])
    if fingerprint(body) != row["content_hash"]:
        raise DataError("HISTORY_CONTENT_HASH_MISMATCH")
    return body


def view_hash(snapshot_hash, window, first, stop):
    return fingerprint({"history_hash": snapshot_hash, "window": window, "slice": {"start": first, "stop": stop}})


def coverage(rows, start, end, first_hint=None):
    history_window(start, end)
    days = [r["session_date"] for r in rows]
    if not 0 < len(days) <= HISTORY_ROWS or days != sorted(set(days)) or any(not start <= d < end for d in days):
        raise DataError("HISTORY_ROWS_OR_WINDOW_INVALID")
    present = set(days)
    missing, before_hint, examples = 0, 0, []
    day, stop = date.fromisoformat(start), date.fromisoformat(end)
    while day < stop:
        label = day.isoformat()
        if day.weekday() < 5 and label not in present:
            if first_hint and label < first_hint:
                before_hint += 1
            else:
                missing += 1
                if len(examples) < 50: examples.append(label)
        day += timedelta(days=1)
    return {"requested_start": start, "requested_end": end, "actual_start": days[0], "actual_end": days[-1],
            "row_count": len(rows), "provider_first_trade_hint": first_hint,
            "weekday_gap_candidates": missing, "weekday_gap_examples": examples,
            "weekday_candidates_before_provider_hint": before_hint,
            "calendar_basis": "WEEKDAY_CANDIDATES_ONLY_HOLIDAYS_HALTS_UNKNOWN",
            "completeness": "UNVERIFIED_PROVIDER_RESPONSE", "request_envelope_fetched": True,
            "boundary_coverage": "OBSERVED_PARTIAL_OR_UNCERTAIN"}


class HistoryStore:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.db.apply_module_migrations("data_saved_history_2", MIGRATIONS)
        with self.db.transaction() as con:
            for table, keys in [("data_security_identities", ("security_id",)), ("data_history_series", ("history_id",)),
                                ("data_history_versions", ("history_id", "version")), ("data_history_events", ("history_id", "revision")),
                                ("data_history_pins", ("history_id", "version", "owner_id", "kind", "reference_id")),
                                ("data_security_identity_namespaces", ("security_id",))]:
                immutable(con, table, keys)

    def save_security(self, context, value, *, evidence, namespace="legacy"):
        context.require_active()
        owner = context.run.owner_id
        identifier(owner)
        identifier(namespace)
        body = {**value, "evidence": evidence}
        key = "security-" + uuid4().hex
        digest = fingerprint(body)
        with self.db.transaction() as con:
            context.assert_fenced(con)
            con.execute("INSERT INTO data_security_identities VALUES(?,?,?,?,?,?)", (key, owner, body["ticker"], canonical(body), digest, now()))
            con.execute("INSERT INTO data_security_identity_namespaces VALUES(?,?)", (key, namespace))
        return {**body, "security_id": key, "content_hash": digest}

    def security(self, owner, security_id):
        identifier(owner); identifier(security_id)
        with closing(self.db.connect()) as con:
            row = con.execute("SELECT * FROM data_security_identities WHERE security_id=? AND owner_id=?", (security_id, owner)).fetchone()
        if row is None: raise DataError("RESOURCE_NOT_FOUND")
        return {**_verified(row), "security_id": security_id, "content_hash": row["content_hash"]}

    def latest_security(self, owner, ticker, *, fresh=True):
        ticker = symbol(ticker)
        with closing(self.db.connect()) as con:
            # Historical rows without a namespace are the original legacy cache.
            # Provider/library snapshots remain addressable by ID but cannot be
            # silently adopted by legacy ticker-only resolution.
            row = con.execute("SELECT i.* FROM data_security_identities i LEFT JOIN data_security_identity_namespaces n ON n.security_id=i.security_id WHERE i.owner_id=? AND i.ticker=? AND (n.namespace IS NULL OR n.namespace='legacy') ORDER BY i.created_at DESC,i.security_id DESC LIMIT 1", (owner, ticker)).fetchone()
        if row is None: return None
        value = {**_verified(row), "security_id": row["security_id"], "content_hash": row["content_hash"]}
        if fresh and (datetime.now(timezone.utc)-parse_instant(value["observed_received_at"])).total_seconds() > 7*86400: return None
        return value

    @staticmethod
    def state(con, owner, history_id):
        series = con.execute("SELECT * FROM data_history_series WHERE history_id=? AND owner_id=?", (history_id, owner)).fetchone()
        if series is None: raise DataError("RESOURCE_NOT_FOUND")
        event = con.execute("SELECT name,deleted,revision FROM data_history_events WHERE history_id=? AND owner_id=? ORDER BY revision DESC LIMIT 1", (history_id, owner)).fetchone()
        return {"history_id": history_id, "name": event["name"] if event else series["ticker"]+" / "+series["provider"],
                "deleted": bool(event["deleted"]) if event else False, "revision": event["revision"] if event else 0}

    def _version(self, con, owner, history_id, version=None, *, allow_deleted=False):
        state = self.state(con, owner, history_id)
        if state["deleted"] and not allow_deleted: raise DataError("RESOURCE_NOT_FOUND")
        if version is not None and (type(version) is not int or version < 1): raise DataError("HISTORY_VERSION_INVALID")
        row = con.execute("SELECT * FROM data_history_versions WHERE owner_id=? AND history_id=? " + ("AND version=?" if version is not None else "ORDER BY version DESC LIMIT 1"),
                          (owner, history_id, version) if version is not None else (owner, history_id)).fetchone()
        if row is None: raise DataError("RESOURCE_NOT_FOUND")
        return {**_verified(row), "history_id": history_id, "version": row["version"], "content_hash": row["content_hash"], **state}

    def summary(self, owner, history_id, version=None, *, include_deleted=False):
        identifier(owner); identifier(history_id)
        with closing(self.db.connect()) as con:
            return self._summary(con, owner, history_id, version, include_deleted=include_deleted)

    def _summary(self, con, owner, history_id, version=None, *, include_deleted=False):
        value = self._version(con, owner, history_id, version, allow_deleted=include_deleted)
        age = max(0, int((datetime.now(timezone.utc)-parse_instant(value["observed_received_at"])).total_seconds()))
        recent = date.fromisoformat(value["requested_range"]["end"]) >= datetime.now(timezone.utc).date()-timedelta(days=30)
        ttl = 86400 if recent else 30*86400
        fresh = {"age_seconds": age, "ttl_seconds": ttl, "stale": age > ttl, "policy": "FROZEN_VINTAGE_REUSE_EXPLICIT_REFRESH_NO_AUTO_MERGE"}
        snapshot_hash = value["content_hash"]
        return {**value, "content_hash": view_hash(snapshot_hash, value["requested_range"], 0, value["actual_coverage"]["row_count"]),
                "snapshot_content_hash": snapshot_hash, "coverage": value["actual_coverage"], "row_count": value["actual_coverage"]["row_count"],
                "cache": {"freshness": fresh, "immutable_vintage": True}, "freshness": fresh}

    def freeze_reference(self, owner, history_id, version=None):
        with self.db.transaction() as con:
            value = self._version(con, owner, history_id, version)
            return {"history_id": history_id, "version": value["version"],
                    "content_hash": view_hash(value["content_hash"], value["requested_range"], 0, value["actual_coverage"]["row_count"])}

    def compatible(self, owner, ticker, provider, start, end):
        with closing(self.db.connect()) as con:
            saved = con.execute("SELECT v.* FROM data_history_series s JOIN data_history_versions v USING(history_id) "
                "WHERE s.owner_id=? AND v.owner_id=? AND s.ticker=? AND s.provider=? "
                "AND json_extract(v.body,'$.profile')=? AND json_extract(v.body,'$.price_policy')=? "
                "AND coalesce(json_extract(v.body,'$.data_quality.quality_policy.id'),'')=? "
                "AND coalesce(json_extract(v.body,'$.data_quality.quality_policy.content_hash'),'')=? "
                "AND (? IS NULL OR json_extract(v.body,'$.source_ref.source_id')=?) "
                "AND json_extract(v.body,'$.requested_range.start')<=? AND json_extract(v.body,'$.requested_range.end')>=? "
                "AND coalesce((SELECT deleted FROM data_history_events e WHERE e.history_id=s.history_id AND e.owner_id=s.owner_id ORDER BY revision DESC LIMIT 1),0)=0 "
                "ORDER BY v.created_at DESC,v.history_id DESC LIMIT 1", (owner, owner, ticker, provider, HISTORY_PROFILE,
                    YAHOO_PRICE_POLICY if provider == "yfinance" else NATIVE_PRICE_POLICY,"","",
                    NATIVE_SOURCE_ID if provider == "akshare" else None, NATIVE_SOURCE_ID if provider == "akshare" else None, start, end)).fetchone()
            if saved:
                _verified(saved)
                return {"history_id": saved["history_id"], "version": saved["version"], "content_hash": saved["content_hash"]}
        return None

    def save(self, context, observation, security, *, series_key):
        owner = context.run.owner_id
        context.require_active()
        call = observation["request_parameters"]
        cov = coverage(observation["rows"], call["start"], call["end"], security.get("provider_first_trade_hint"))
        policy = observation.get("price_policy")
        expected = YAHOO_PRICE_POLICY if observation["provider"] == "yfinance" else NATIVE_PRICE_POLICY if call.get("interface") == NATIVE_INTERFACE else SINA_PRICE_POLICY
        if policy != expected:
            raise DataError("HISTORY_PRICE_POLICY_UNSUPPORTED")
        body = {"profile": HISTORY_PROFILE, "provider": observation["provider"], "ticker": call["ticker"],
                "requested_range": {"start": call["start"], "end": call["end"]}, "actual_coverage": cov,
                "identity": security["instrument"], "identity_evidence_ref": {k: security[k] for k in ("security_id", "content_hash")},
                "source_ref": observation["source_ref"], "observation_ref": {"result_id": observation["result_id"], "content_hash": observation["content_hash"]},
                "price_policy": policy, "quantity_basis": QUANTITY_BASIS, "observed_received_at": observation["observed_received_at"],
                "corporate_action_basis": observation["sdk_ref"].get("corporate_action_basis"),
                "price_transform": observation["sdk_ref"].get("price_transform"),
                "data_family": observation["data_family"], "raw_compatible": False, "availability_basis": "OBSERVED_RECEIPT_ONLY"}
        if sina_quality.enabled(call):
            body["data_quality"]=sina_quality.validate_selection(observation.get("data_quality"),call["start"],call["end"],observation["rows"])
        digest = fingerprint(body)
        with self.db.transaction() as con:
            context.assert_fenced(con)
            old = con.execute("SELECT history_id,version,content_hash FROM data_history_versions WHERE owner_id=? AND observation_id=?", (owner, observation["result_id"])).fetchone()
            if old: return {"history_id": old[0], "version": old[1], "content_hash": old[2]}
            series = con.execute("SELECT history_id FROM data_history_series WHERE owner_id=? AND series_key=? ORDER BY created_at DESC", (owner, series_key)).fetchall()
            key = next((r[0] for r in series if not self.state(con, owner, r[0])["deleted"]), None)
            if key is None:
                key = "history-"+uuid4().hex
                con.execute("INSERT INTO data_history_series VALUES(?,?,?,?,?,?)", (key, owner, body["provider"], body["ticker"], series_key, now()))
            version = con.execute("SELECT coalesce(max(version),0)+1 FROM data_history_versions WHERE history_id=?", (key,)).fetchone()[0]
            con.execute("INSERT INTO data_history_versions VALUES(?,?,?,?,?,?,?)", (key, version, owner, observation["result_id"], canonical(body), digest, now()))
        return {"history_id": key, "version": version, "content_hash": digest}

    def pin(self, owner, history_id, version, reference_id, *, kind="run", connection=None, context=None):
        identifier(owner); identifier(reference_id)
        if kind not in {"run", "result"}: raise DataError("HISTORY_PIN_KIND_INVALID")
        if context is None or context.run.owner_id != owner: raise DataError("HISTORY_FENCED_CONTEXT_REQUIRED")
        def perform(con):
            context.assert_fenced(con)
            self._version(con, owner, history_id, version)
            old = con.execute("SELECT 1 FROM data_history_pins WHERE history_id=? AND version=? AND owner_id=? AND kind=? AND reference_id=?", (history_id, version, owner, kind, reference_id)).fetchone()
            if old is None: con.execute("INSERT INTO data_history_pins VALUES(?,?,?,?,?,?)", (history_id, version, owner, kind, reference_id, now()))
            return {"history_id": history_id, "version": version, "kind": kind, "reference_id": reference_id}
        if connection is not None: return perform(connection)
        context.require_active()
        with self.db.transaction() as con: return perform(con)

    def view(self, owner, history_id, version, start, end, *, pinned=None, durable_reuse=False):
        history_window(start, end)
        with closing(self.db.connect()) as con:
            allow = durable_reuse is True
            if pinned:
                allow = con.execute("SELECT 1 FROM data_history_pins WHERE history_id=? AND version=? AND owner_id=? AND kind=? AND reference_id=?", (history_id, version, owner, pinned["kind"], pinned["reference_id"])).fetchone() is not None
                if not allow: raise DataError("RESOURCE_NOT_FOUND")
            value = self._version(con, owner, history_id, version, allow_deleted=allow)
        if not value["requested_range"]["start"] <= start < end <= value["requested_range"]["end"]: raise DataError("HISTORY_WINDOW_UNAVAILABLE")
        observation = self.service._full_observation(owner, value["observation_ref"]["result_id"])
        if observation["content_hash"] != value["observation_ref"]["content_hash"] or observation["source_ref"] != value["source_ref"]:
            raise DataError("HISTORY_OBSERVATION_BINDING_MISMATCH")
        indices = window_indices(observation["rows"], start, end)
        rows = [observation["rows"][i] for i in indices]
        if not rows: raise DataError("HISTORY_WINDOW_NO_OBSERVED_ROWS")
        digest = view_hash(value["content_hash"], {"start": start, "end": end}, indices[0], indices[-1]+1)
        quality={}
        if value.get("data_quality")is not None:
            quality["data_quality"]=sina_quality.selected(value["data_quality"],start,end,rows)
        return {**value, "content_hash": digest, "history_content_hash": value["content_hash"], "rows": rows,
                "requested_range": {"start": start, "end": end}, "actual_coverage": coverage(rows, start, end, value["actual_coverage"].get("provider_first_trade_hint")),
                "request_parameters": {**observation["request_parameters"], "start": start, "end": end},
                "history_cache": {"immutable_observation_ref": value["observation_ref"], "selected_row_indices": indices, "no_vintage_merge": True},**quality}

    def list(self, owner, limit=50, offset=0, *, state="active"):
        identifier(owner); validate_paging(limit, offset)
        if state not in {"active", "deleted", "all"}: raise DataError("HISTORY_STATE_INVALID")
        with closing(self.db.connect()) as con:
            base = "FROM data_history_series s WHERE owner_id=?"
            params = [owner]
            if state != "all":
                base += " AND coalesce((SELECT deleted FROM data_history_events e WHERE e.history_id=s.history_id AND e.owner_id=s.owner_id ORDER BY revision DESC LIMIT 1),0)=?"
                params.append(int(state == "deleted"))
            total = con.execute("SELECT count(*) " + base, params).fetchone()[0]
            keys = con.execute("SELECT history_id " + base + " ORDER BY created_at DESC,history_id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"items": [self.summary(owner, key[0], include_deleted=True) for key in keys], "total": total, "limit": limit, "offset": offset}

    def lifecycle(self, owner, history_id, *, action, expected_revision, idempotency_key, name=None, publication_guard=None):
        identifier(owner); identifier(history_id); identifier(idempotency_key)
        if action not in {"rename", "delete", "restore"} or type(expected_revision) is not int or expected_revision < 0: raise DataError("HISTORY_LIFECYCLE_INVALID")
        if action == "rename" and (type(name) is not str or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name)): raise DataError("HISTORY_NAME_INVALID")
        if action != "rename" and name is not None: raise DataError("HISTORY_LIFECYCLE_INVALID")
        request_hash = fingerprint({"history_id": history_id, "action": action, "name": name, "expected_revision": expected_revision})
        with self.db.transaction() as con:
            if publication_guard is not None:
                publication_guard(con)
            old = con.execute("SELECT request_hash,response_json FROM data_history_events WHERE owner_id=? AND idempotency_key=?", (owner, idempotency_key)).fetchone()
            if old:
                if old[0] != request_hash: raise PlatformError("HISTORY_IDEMPOTENCY_CONFLICT", 409)
                return json.loads(old[1])
            require_open_key(con, owner, 'history', idempotency_key)
            current = self.state(con, owner, history_id)
            if current["revision"] != expected_revision: raise PlatformError("HISTORY_REVISION_CONFLICT", 409)
            if action == "delete":
                active = con.execute("SELECT 1 FROM jobs WHERE owner_id=? AND status IN ('QUEUED','RUNNING') AND json_extract(parameters_json,'$.request.history_id')=? LIMIT 1", (owner, history_id)).fetchone()
                pins = con.execute("SELECT 1 FROM data_history_pins p JOIN jobs j ON j.run_id=p.reference_id AND j.owner_id=p.owner_id WHERE p.owner_id=? AND p.history_id=? AND p.kind='run' AND j.status IN ('QUEUED','RUNNING') LIMIT 1", (owner, history_id)).fetchone()
                if active or pins: raise PlatformError("HISTORY_IN_USE", 409)
            result = {**current, "name": name.strip() if action == "rename" else current["name"], "deleted": action == "delete" if action in {"delete", "restore"} else current["deleted"], "revision": current["revision"]+1}
            con.execute("INSERT INTO data_history_events VALUES(?,?,?,?,?,?,?,?,?,?)", (history_id, result["revision"], owner, action, result["name"], int(result["deleted"]), idempotency_key, request_hash, canonical(result), now()))
            return result
