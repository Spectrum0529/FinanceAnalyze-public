"""Independent transaction store, additive migrations and consistent backups."""
from contextlib import contextmanager, closing
import hashlib
from pathlib import Path
import re
import sqlite3
from time import monotonic, sleep
from predictor.research.append_guards import install as install_identity_guards

from .errors import PlatformError
from .mcp_request_context import require_transaction_approval
from .security import now_text, safe_json

CORE_MIGRATIONS = ("""
CREATE TABLE platform_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO platform_meta VALUES ('database_kind','finance_platform');
CREATE TABLE users(user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','researcher','viewer')), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), created_at TEXT NOT NULL);
CREATE TABLE setup(singleton INTEGER PRIMARY KEY CHECK(singleton=1), token_hash TEXT NOT NULL, expires_at REAL NOT NULL, consumed_at TEXT);
CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id), csrf_token TEXT NOT NULL,
 expires_at REAL NOT NULL, revoked_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE rate_events(event_id INTEGER PRIMARY KEY, bucket_hash TEXT NOT NULL, recorded_at REAL NOT NULL);
CREATE INDEX rate_lookup ON rate_events(bucket_hash,recorded_at);
CREATE TABLE audit_events(event_id INTEGER PRIMARY KEY, actor_id TEXT REFERENCES users(user_id), action TEXT NOT NULL,
 resource_id TEXT, recorded_at TEXT NOT NULL, details_json TEXT NOT NULL);
CREATE TABLE resource_versions(resource_id TEXT NOT NULL, version TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(user_id),
 kind TEXT NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(resource_id,version));
CREATE TABLE runs(run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id), handler TEXT NOT NULL,
 spec_json TEXT NOT NULL, spec_hash TEXT NOT NULL, parent_run_id TEXT REFERENCES runs(run_id), created_at TEXT NOT NULL);
CREATE TABLE jobs(job_id TEXT PRIMARY KEY, run_id TEXT UNIQUE NOT NULL REFERENCES runs(run_id), owner_id TEXT NOT NULL REFERENCES users(user_id),
 handler TEXT NOT NULL, parameters_json TEXT NOT NULL, parameters_hash TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('BACKTEST','LOCAL_SIMULATION','BROKER_PAPER')),
 status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')), progress INTEGER NOT NULL CHECK(progress BETWEEN 0 AND 100),
 lease_owner TEXT, lease_until REAL, fencing_token INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
 error_code TEXT, result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX queue_lookup ON jobs(status,created_at);
CREATE TABLE job_events(event_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id), run_id TEXT NOT NULL REFERENCES runs(run_id),
 kind TEXT NOT NULL, recorded_at TEXT NOT NULL, metadata_json TEXT NOT NULL);
CREATE INDEX event_lookup ON job_events(job_id,event_id);
CREATE TABLE job_keys(owner_id TEXT NOT NULL REFERENCES users(user_id), idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES jobs(job_id), PRIMARY KEY(owner_id,idempotency_key));
""", """
CREATE TABLE schedules(schedule_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id), name TEXT NOT NULL,
 handler TEXT NOT NULL, parameters_json TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('BACKTEST','LOCAL_SIMULATION','BROKER_PAPER')),
 rule TEXT NOT NULL CHECK(rule IN ('ONCE','INTERVAL')), interval_seconds INTEGER, next_due_at REAL,
 paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)), archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX schedules_due ON schedules(paused,archived,next_due_at);
CREATE TABLE schedule_events(event_id INTEGER PRIMARY KEY, schedule_id TEXT NOT NULL REFERENCES schedules(schedule_id),
 kind TEXT NOT NULL, recorded_at TEXT NOT NULL, details_json TEXT NOT NULL);
CREATE TABLE schedule_occurrences(event_id INTEGER PRIMARY KEY, schedule_id TEXT NOT NULL REFERENCES schedules(schedule_id),
 occurrence_key TEXT NOT NULL UNIQUE, due_at REAL NOT NULL, job_id TEXT REFERENCES jobs(job_id), status TEXT NOT NULL,
 error_code TEXT, recorded_at TEXT NOT NULL);
""")


def _statements(script):
    buffer = ""
    for character in script:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            yield buffer.strip()
            buffer = ""
    if buffer.strip():
        raise PlatformError("MIGRATION_REJECTED")


def immutable(connection, table, keys):
    """Reject update/delete and conflict replacement (including OR REPLACE)."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table) or any(not re.fullmatch(r"[a-z_][a-z0-9_]*", k) for k in keys):
        raise PlatformError("INVALID_INPUT")
    for action in ("UPDATE", "DELETE"):
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable platform record'); END")
    equality = " AND ".join(f"{k}=NEW.{k}" for k in keys)
    connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_replace BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {equality}) BEGIN SELECT RAISE(ABORT,'immutable platform identity'); END")
    # Proven original helper covers explicit rowid and every PK/UNIQUE identity,
    # even on native connections with recursive_triggers disabled.
    install_identity_guards(connection, (table,), prefix="platform_identity_v3")


def install_immutable_guards(connection, tables):
    """Public module helper; immutable rowid/PK/UNIQUE plus UPDATE/DELETE guards."""
    for table in tables:
        if not re.fullmatch(r"[a-z_]+", table):
            raise PlatformError("INVALID_INPUT")
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        keys = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
        if not keys:
            raise PlatformError("INVALID_INPUT")
        immutable(connection, table, keys)


class Database:
    def __init__(self, path: Path, *, timeout: float = 10):
        self.path = Path(path).resolve()
        if self.path.name.lower() in {"research_ledger.sqlite3", "access.sqlite3"}:
            raise PlatformError("INVALID_INPUT")
        self.timeout = timeout

    def connect(self):
        busy_timeout_ms = int(self.timeout * 1000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = monotonic() + self.timeout
        delay = 0.01
        while True:
            con = None
            try:
                # SQLite can skip its busy handler during WAL conversion. Use
                # one deadline for setup and release any lock before retrying.
                con = sqlite3.connect(self.path, timeout=0, isolation_level=None)
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("PRAGMA synchronous=FULL")
                # Recursive triggers also apply to conflict DELETE; identity guard is independent.
                con.execute("PRAGMA recursive_triggers=ON")
                con.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                return con
            except BaseException as error:
                if con is not None:
                    con.close()
                if not isinstance(error, sqlite3.Error):
                    raise
                primary_code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                if primary_code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise
                sleep(min(delay, remaining))
                if monotonic() >= deadline:
                    raise
                delay = min(delay * 2, 0.1)

    @contextmanager
    def transaction(self):
        with closing(self.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                require_transaction_approval(self, con)
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def migrate(self):
        with self.transaction() as con:
            # Never adopt a nonempty unrelated SQLite database.
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "schema_migrations" not in tables:
                raise PlatformError("MIGRATION_REJECTED")
            con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(namespace TEXT NOT NULL, revision INTEGER NOT NULL, script_hash TEXT NOT NULL, applied_at TEXT NOT NULL, PRIMARY KEY(namespace,revision))")
            known = con.execute("SELECT revision,script_hash FROM schema_migrations WHERE namespace='core' ORDER BY revision").fetchall()
            if [r[0] for r in known] != list(range(1, len(known) + 1)) or len(known) > len(CORE_MIGRATIONS):
                raise PlatformError("MIGRATION_REJECTED")
            for revision, script in enumerate(CORE_MIGRATIONS, 1):
                sha = hashlib.sha256(script.encode()).hexdigest()
                if revision <= len(known):
                    if known[revision - 1][1] != sha:
                        raise PlatformError("MIGRATION_REJECTED")
                    continue
                for statement in _statements(script):
                    con.execute(statement)
                con.execute("INSERT INTO schema_migrations VALUES ('core',?,?,?)", (revision, sha, now_text()))
            for table, keys in (("schema_migrations", ("namespace", "revision")), ("platform_meta", ("key",)),
                                ("resource_versions", ("resource_id", "version")), ("runs", ("run_id",)),
                                ("audit_events", ("event_id",)), ("job_events", ("event_id",)), ("job_keys", ("owner_id", "idempotency_key"))):
                immutable(con, table, keys)
            install_immutable_guards(con, ("schedule_events", "schedule_occurrences"))
            if con.execute("SELECT value FROM platform_meta WHERE key='database_kind'").fetchone()[0] != "finance_platform":
                raise PlatformError("MIGRATION_REJECTED")

    def apply_module_migrations(self, namespace, migrations: tuple[str, ...]):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", namespace) or namespace == "core":
            raise PlatformError("MIGRATION_REJECTED")
        with self.transaction() as con:
            known = con.execute("SELECT revision,script_hash FROM schema_migrations WHERE namespace=? ORDER BY revision", (namespace,)).fetchall()
            if [r[0] for r in known] != list(range(1, len(known) + 1)) or len(known) > len(migrations):
                raise PlatformError("MIGRATION_REJECTED")
            for revision, script in enumerate(migrations, 1):
                sha = hashlib.sha256(script.encode()).hexdigest()
                if revision <= len(known):
                    if known[revision - 1][1] != sha:
                        raise PlatformError("MIGRATION_REJECTED")
                    continue
                for statement in _statements(script):
                    if not re.match(r"(?is)^(CREATE\s+(?:TABLE|INDEX|UNIQUE\s+INDEX|TRIGGER|VIEW)\b|ALTER\s+TABLE\s+\w+\s+ADD\s+)", statement):
                        raise PlatformError("MIGRATION_REJECTED")
                    con.execute(statement)
                con.execute("INSERT INTO schema_migrations VALUES (?,?,?,?)", (namespace, revision, sha, now_text()))

    def backup(self, destination: Path):
        target = Path(destination).resolve()
        if target == self.path or target.exists():
            raise PlatformError("CONFLICT", 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation forbids overwriting another backup or active database.
        with target.open("xb"):
            pass
        with closing(self.connect()) as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or copy.execute("PRAGMA foreign_key_check").fetchall():
                raise PlatformError("DATABASE_UNAVAILABLE", 503)
            actual_revision = copy.execute("SELECT max(revision) FROM schema_migrations WHERE namespace='core'").fetchone()[0]
        return {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "bytes": target.stat().st_size,
                "schema_revision": actual_revision}

    def health(self):
        with closing(self.connect()) as con:
            ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            revision = con.execute("SELECT max(revision) FROM schema_migrations WHERE namespace='core'").fetchone()[0]
            return {"database": "ok" if ok else "failed", "schema_revision": revision}

    def audit(self, actor_id, action, resource_id=None, details=None, *, connection=None):
        from .contracts import identifier
        identifier(action)
        if resource_id is not None:
            identifier(resource_id)
        encoded = safe_json(details or {})
        def write(con):
            return con.execute("INSERT INTO audit_events(actor_id,action,resource_id,recorded_at,details_json) VALUES (?,?,?,?,?)",
                               (actor_id, action, resource_id, now_text(), encoded)).lastrowid
        if connection is not None:
            return write(connection)
        with self.transaction() as con:
            return write(con)
