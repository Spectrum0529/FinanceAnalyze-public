"""Owner-scoped cache of completed, rebuildable computations."""

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import BoundedSemaphore

from .auth import require_writer
from .contracts import digest, identifier
from .errors import PlatformError
from .jobs import WorkerJobContext
from .security import clean, safe_json

MIB = 1024 ** 2
MIN_BYTES = MIB
DEFAULT_BYTES = 64 * MIB
MAX_BYTES = 256 * MIB
ENTRY_BYTES = 8 * MIB
TOTAL_BYTES = 512 * MIB
OWNER_ENTRIES = 2048
TOTAL_ENTRIES = 8192
COMMANDS = 128
KINDS = ("dataset_parse", "trusted_indicator", "exact_backtest")
MANUAL_KINDS = {
    "research.daily_backtest": {"exact_backtest"},
    "research.dataset_task": {"trusted_indicator", "exact_backtest"},
}
MIGRATIONS = ("""
CREATE TABLE computation_cache_settings(
 owner_id TEXT PRIMARY KEY REFERENCES users(user_id),enabled INTEGER NOT NULL,
 max_bytes INTEGER NOT NULL,revision INTEGER NOT NULL,hits INTEGER NOT NULL DEFAULT 0,
 misses INTEGER NOT NULL DEFAULT 0,bypasses INTEGER NOT NULL DEFAULT 0,evictions INTEGER NOT NULL DEFAULT 0);
CREATE TABLE computation_cache_entries(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,owner_id TEXT NOT NULL REFERENCES computation_cache_settings(owner_id),
 cache_key TEXT NOT NULL,payload BLOB NOT NULL,byte_count INTEGER NOT NULL
 CHECK(byte_count=length(payload) AND byte_count BETWEEN 1 AND 8388608),
 body_sha256 TEXT NOT NULL,
 UNIQUE(owner_id,cache_key));
CREATE INDEX computation_cache_owner_order ON computation_cache_entries(owner_id,sequence);
CREATE TABLE computation_cache_commands(
 sequence INTEGER PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES computation_cache_settings(owner_id),
 command_key TEXT NOT NULL,request_sha256 TEXT NOT NULL,result_json TEXT NOT NULL,UNIQUE(owner_id,command_key));
CREATE INDEX computation_cache_command_order ON computation_cache_commands(owner_id,sequence);
""",)


@dataclass(frozen=True)
class CacheResult:
    value: object
    disposition: str


class ComputationCache:
    def __init__(self, database):
        self.database = database
        self._buffers = BoundedSemaphore(2)
        database.apply_module_migrations("computation_cache", MIGRATIONS)

    @staticmethod
    def _user(con, owner):
        if not con.execute("SELECT 1 FROM users WHERE user_id=? AND enabled=1", (owner,)).fetchone():
            raise PlatformError("FORBIDDEN", 403)

    @staticmethod
    def _settings(con, owner):
        return con.execute("SELECT * FROM computation_cache_settings WHERE owner_id=?", (owner,)).fetchone()

    @staticmethod
    def _defaults(con, owner):
        con.execute("""
            INSERT INTO computation_cache_settings(owner_id,enabled,max_bytes,revision)
            VALUES (?,1,?,0) ON CONFLICT(owner_id) DO NOTHING
        """, (owner, DEFAULT_BYTES))

    @staticmethod
    def _totals(con, owner):
        if owner is None:
            query = "SELECT COUNT(*),COALESCE(SUM(byte_count),0) FROM computation_cache_entries"
            return tuple(con.execute(query).fetchone())
        query = "SELECT COUNT(*),COALESCE(SUM(byte_count),0) FROM computation_cache_entries WHERE owner_id=?"
        return tuple(con.execute(query, (owner,)).fetchone())

    def _view(self, con, owner):
        state = self._settings(con, owner)
        count, size = self._totals(con, owner)
        stats = {name: state[name] if state is not None else 0
                 for name in ("hits", "misses", "bypasses", "evictions")}
        return {
            "schema": "computation-cache-1",
            "settings": {
                "enabled": bool(state["enabled"]) if state is not None else True,
                "max_bytes": state["max_bytes"] if state is not None else DEFAULT_BYTES,
                "revision": state["revision"] if state is not None else 0,
            },
            "stats": {"entries": count, "bytes": size, **stats},
            "location": "platform_database",
            "eligible": list(KINDS),
        }

    def get(self, principal):
        with closing(self.database.connect()) as con:
            con.execute("BEGIN")
            self._user(con, principal.user_id)
            return self._view(con, principal.user_id)

    @staticmethod
    def _count(con, owner, metric):
        # Metric is a fixed value selected by the service.
        con.execute(f"UPDATE computation_cache_settings SET {metric}={metric}+1 WHERE owner_id=?", (owner,))

    def _trim(self, con, *, owner, max_entries, max_bytes):
        count, size = self._totals(con, owner)
        if count <= max_entries and size <= max_bytes:
            return
        if owner is None:
            rows = con.execute("""
                SELECT sequence,owner_id,byte_count FROM computation_cache_entries ORDER BY sequence
            """).fetchall()
        else:
            rows = con.execute("""
                SELECT sequence,owner_id,byte_count FROM computation_cache_entries
                WHERE owner_id=? ORDER BY sequence
            """, (owner,)).fetchall()
        for sequence, entry_owner, length in rows:
            con.execute("DELETE FROM computation_cache_entries WHERE sequence=?", (sequence,))
            self._count(con, entry_owner, "evictions")
            count -= 1
            size -= length
            if count <= max_entries and size <= max_bytes:
                break

    def _command(self, principal, action, expected_revision, idempotency_key, changes):
        require_writer(principal)
        owner = principal.user_id
        if type(expected_revision) is not int or expected_revision < 0:
            raise PlatformError("INVALID_INPUT", 422)
        try:
            identifier(idempotency_key)
        except ValueError:
            raise PlatformError("INVALID_INPUT", 422) from None
        request_sha = sha256(safe_json([action, expected_revision, changes]).encode()).hexdigest()
        with self.database.transaction() as con:
            self._user(con, owner)
            self._defaults(con, owner)
            previous = con.execute("""
                SELECT request_sha256,result_json FROM computation_cache_commands
                WHERE owner_id=? AND command_key=?
            """, (owner, idempotency_key)).fetchone()
            if previous is not None:
                if previous["request_sha256"] != request_sha:
                    raise PlatformError("CONFLICT", 409)
                return json.loads(previous["result_json"])
            if self._settings(con, owner)["revision"] != expected_revision:
                raise PlatformError("CONFLICT", 409)
            cleared = {}
            if action == "clear":
                count, size = self._totals(con, owner)
                con.execute("DELETE FROM computation_cache_entries WHERE owner_id=?", (owner,))
                con.execute("UPDATE computation_cache_settings SET revision=revision+1 WHERE owner_id=?", (owner,))
                cleared = {"cleared_entries": count, "cleared_bytes": size}
            else:
                con.execute("""
                    UPDATE computation_cache_settings SET enabled=?,max_bytes=?,revision=revision+1 WHERE owner_id=?
                """, (int(changes["enabled"]), changes["max_bytes"], owner))
                self._trim(con, owner=owner, max_entries=OWNER_ENTRIES, max_bytes=changes["max_bytes"])
            result = self._view(con, owner) | cleared
            con.execute("""
                INSERT INTO computation_cache_commands(owner_id,command_key,request_sha256,result_json) VALUES (?,?,?,?)
            """, (owner, idempotency_key, request_sha, safe_json(result)))
            con.execute("""
                DELETE FROM computation_cache_commands WHERE sequence IN (
                    SELECT sequence FROM computation_cache_commands WHERE owner_id=?
                    ORDER BY sequence DESC LIMIT -1 OFFSET ?
                )
            """, (owner, COMMANDS))
            self.database.audit(owner, f"computation_cache.{action}", details=cleared or changes, connection=con)
            return result

    def settings(self, principal, *, enabled, max_bytes, expected_revision, idempotency_key):
        if type(enabled) is not bool or type(max_bytes) is not int or not MIN_BYTES <= max_bytes <= MAX_BYTES:
            raise PlatformError("INVALID_INPUT", 422)
        return self._command(principal, "settings", expected_revision, idempotency_key,
                             {"enabled": enabled, "max_bytes": max_bytes})

    def clear(self, principal, *, expected_revision, idempotency_key, confirm):
        if confirm is not True:
            raise PlatformError("INVALID_INPUT", 422)
        return self._command(principal, "clear", expected_revision, idempotency_key, {})

    def _job(self, con, context):
        if type(context) is not WorkerJobContext or context.service.database is not self.database:
            raise PlatformError("LEASE_LOST", 409)
        context.assert_fenced(con)
        row = con.execute("SELECT owner_id,run_id,handler FROM jobs WHERE job_id=?", (context.job_id,)).fetchone()
        if tuple(row) != (context.run.owner_id, context.run.run_id, context.run.handler):
            raise PlatformError("LEASE_LOST", 409)
        self._user(con, context.run.owner_id)
        return context.run.owner_id

    def run_manual(self, context, *, kind, identity, compute, validate, cache_policy="use"):
        def scope(con):
            owner = self._job(con, context)
            if kind not in MANUAL_KINDS.get(context.run.handler, ()):
                raise PlatformError("INVALID_INPUT", 422)
            return owner, ["manual", context.run.handler]
        return self._run(context, scope, kind, identity, compute, validate, cache_policy)

    def run_research(self, context, sessions, *, kind, identity, compute, validate, cache_policy="use"):
        from .autonomous_bridge import DeadlineContext

        if kind != "dataset_parse":
            raise PlatformError("INVALID_INPUT", 422)
        worker = context.context if type(context) is DeadlineContext else context

        def scope(con):
            owner = self._job(con, worker)
            if sessions.database is not self.database:
                raise PlatformError("LEASE_LOST", 409)
            attempt, session = sessions._attempt(con, context)
            if (attempt["phase"] != "development" or session["status"] != "RUNNING" or attempt["status"] != "IN_FLIGHT"
                    or sessions.clock() >= session["deadline"]):
                raise PlatformError("LEASE_LOST", 409)
            snapshot = sha256(session["resolved_json"].encode()).hexdigest()
            return owner, ["research", session["session_id"], session["spec_hash"], snapshot, attempt["phase"]]
        return self._run(context, scope, kind, identity, compute, validate, cache_policy)

    @staticmethod
    def _key(context, scope, kind, identity):
        if kind not in KINDS or type(identity) is not dict:
            raise PlatformError("INVALID_INPUT", 422)
        try:
            for name in ("input_sha256", "computation_sha256", "engine_sha256"):
                digest(identity[name])
            value = safe_json(["computation-cache-1", context.run.code_hash, context.run.dependency_hash,
                               scope, kind, identity])
        except (ValueError, TypeError, KeyError):
            raise PlatformError("INVALID_INPUT", 422) from None
        return sha256(value.encode()).hexdigest()

    @staticmethod
    def _load(row, validate):
        raw = row["payload"]
        if (type(raw) is not bytes or len(raw) != row["byte_count"] or len(raw) > ENTRY_BYTES
                or sha256(raw).hexdigest() != row["body_sha256"]):
            raise ValueError("cache integrity")
        value = json.loads(raw)
        validate(value)
        return value

    @staticmethod
    def _encode(value):
        try:
            encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
            raw = bytearray()
            for piece in encoder.iterencode(value):
                raw.extend(piece.encode("utf-8"))
                if len(raw) > ENTRY_BYTES:
                    return None
            # Preserve the product's metadata safety rules on a size-bounded
            # object. The serialized form remains independent of caller edits.
            guarded = clean(json.loads(raw))
            if guarded != value:
                return None
        except (ValueError, TypeError, RecursionError):
            return None
        return raw

    def _read(self, scope, owner, scope_key, key, validate):
        # Read an immutable snapshot. Decode and check domains outside the
        # writer lock. Before use, re-check the version and row under a fence.
        with closing(self.database.connect()) as con:
            con.execute("BEGIN")
            state = self._settings(con, owner)
            if not state["enabled"]:
                return None, "disabled", state["revision"]
            revision = state["revision"]
            row = con.execute("""
                SELECT sequence,payload,byte_count,body_sha256 FROM computation_cache_entries
                WHERE owner_id=? AND cache_key=? AND byte_count BETWEEN 1 AND ?
            """, (owner, key, ENTRY_BYTES)).fetchone()
        value = None
        intact = False
        if row is not None:
            try:
                value = self._load(row, validate)
                intact = True
            except (ValueError, TypeError, RecursionError):
                pass
        with self.database.transaction() as con:
            self._require_scope(con, scope, owner, scope_key)
            state = self._settings(con, owner)
            if not state["enabled"]:
                return None, "disabled", state["revision"]
            if state["revision"] == revision and row is not None:
                exists = con.execute("""
                    SELECT 1 FROM computation_cache_entries WHERE owner_id=? AND sequence=?
                """, (owner, row["sequence"])).fetchone()
                if exists is not None:
                    if intact:
                        self._count(con, owner, "hits")
                        return value, "hit", revision
                    con.execute("DELETE FROM computation_cache_entries WHERE owner_id=? AND sequence=?",
                                (owner, row["sequence"]))
                    self.database.audit(owner, "computation_cache.integrity_miss", connection=con)
            self._count(con, owner, "misses")
            return None, "miss", state["revision"]

    @staticmethod
    def _require_scope(con, scope, owner, scope_key):
        if scope(con) != (owner, scope_key):
            raise PlatformError("LEASE_LOST", 409)

    def _run(self, context, scope, kind, identity, compute, validate, cache_policy):
        if cache_policy not in ("use", "bypass") or not callable(compute) or not callable(validate):
            raise PlatformError("INVALID_INPUT", 422)
        with self.database.transaction() as con:
            owner, scope_key = scope(con)
            key = self._key(context, scope_key, kind, identity)
            self._defaults(con, owner)
            state = self._settings(con, owner)
            if cache_policy == "bypass":
                self._count(con, owner, "bypasses")
                disposition = "bypassed"
            elif not state["enabled"]:
                disposition = "disabled"
            else:
                disposition = None

        if disposition is None:
            if self._buffers.acquire(blocking=False):
                try:
                    value, disposition, revision = self._read(scope, owner, scope_key, key, validate)
                finally:
                    self._buffers.release()
                if disposition == "hit":
                    return CacheResult(value, disposition)
            else:
                disposition = "ineligible"
        value = compute()
        validate(value)
        if disposition == "miss" and self._buffers.acquire(blocking=False):
            try:
                self._publish(scope, owner, scope_key, key, value, revision)
            finally:
                self._buffers.release()
        else:
            with closing(self.database.connect()) as con:
                con.execute("BEGIN")
                self._require_scope(con, scope, owner, scope_key)
        return CacheResult(value, disposition)

    def _publish(self, scope, owner, scope_key, key, value, revision):
        raw = self._encode(value)
        body_hash = sha256(raw).hexdigest() if raw is not None else None
        with self.database.transaction() as con:
            self._require_scope(con, scope, owner, scope_key)
            state = self._settings(con, owner)
            if (raw is None or not state["enabled"] or state["revision"] != revision
                    or len(raw) > state["max_bytes"]):
                return
            con.execute("""
                INSERT INTO computation_cache_entries(owner_id,cache_key,payload,byte_count,body_sha256)
                VALUES (?,?,?,?,?) ON CONFLICT(owner_id,cache_key) DO NOTHING
            """, (owner, key, raw, len(raw), body_hash))
            self._trim(con, owner=owner, max_entries=OWNER_ENTRIES, max_bytes=state["max_bytes"])
            self._trim(con, owner=None, max_entries=TOTAL_ENTRIES, max_bytes=TOTAL_BYTES)
            self._require_scope(con, scope, owner, scope_key)
