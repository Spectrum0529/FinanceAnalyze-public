"""Transactional, logically append-only records and reproducible JSON views."""

from contextlib import contextmanager, closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from predictor.logging import utc_timestamp
from predictor.secrets import reject_credentials
from predictor.research.run_contract import (RUN_REQUIRED, metadata_valid, result_valid,
    instant as run_instant, configuration_hash as run_configuration_hash)
from predictor.research.run_guards import statements as run_guard_statements
from predictor.research.append_guards import install as install_identity_guards


class LedgerError(ValueError):
    """A rejected ledger operation; no old event is changed."""


class LedgerIntegrityError(LedgerError):
    """Existing history is invalid; no effective success is exposed."""


DIMENSIONS = {
    "engineering_checks": {"NOT_RUN", "PASSED", "FAILED"},
    "data_certification": {"NOT_RUN", "UNVERIFIED", "CERTIFIED"},
    "model_evaluation": {"NOT_RUN", "DEVELOPMENT_ONLY", "HISTORICAL_CHECK", "PROSPECTIVE_EVIDENCE"},
}
PLATFORM_DIMENSIONS = {
    "implementation": {"NOT_STARTED", "IN_PROGRESS", "IMPLEMENTED"},
    "verification": {"NOT_RUN", "FAILED", "LOCAL_VERIFIED", "PROVIDER_VERIFIED", "TARGET_ENV_VERIFIED"},
    "external_dependency": {"NONE", "WAITING_SECRET", "WAITING_ACCOUNT", "WAITING_DATA_RIGHTS",
                            "WAITING_HOST", "WAITING_USER_DECISION", "WAITING_OBSERVATION"},
}
PLATFORM_INITIAL = {"implementation": "NOT_STARTED", "verification": "NOT_RUN", "external_dependency": "NONE"}
TRANSITIONS = {
    "not_started": {"in_progress", "blocked", "deferred"},
    "in_progress": {"done", "blocked"},
    "blocked": {"in_progress", "deferred"},
    "done": set(), "deferred": set(),
}
EVIDENCE_ROLES = {"changes", "actual_runs", "acceptance", "limitations", "handoff"}



def encode(value) -> str:
    reject_credentials(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def file_hash(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def require_hash(value, field: str):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise LedgerError(f"{field} must be a lowercase SHA-256 digest")


class Ledger:
    """SQLite is authoritative; tasks.json and decision_log.jsonl are derived views.

    Transactions prevent partial events, UNIQUE keys reject duplicate run IDs,
    and database triggers reject UPDATE/DELETE. The owner can still alter files
    or remove triggers: this is not a tamper-proof or independent audit system.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.database = self.root / "planning" / "research_ledger.sqlite3"

    @contextmanager
    def connection(self, *, create: bool = False):
        if not create and not self.database.is_file():
            raise LedgerError("Ledger is not initialized")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=DELETE")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def initialize(self, source: Path):
        raw = source.read_bytes()
        plan = json.loads(raw)
        tasks = plan.get("tasks", [])
        if not tasks or not all(isinstance(t, dict) for t in tasks):
            raise LedgerError("Source must contain task definitions")
        if any(not isinstance(t.get("id"), str) or not t["id"].strip()
               or not isinstance(t.get("dependencies"), list)
               or not all(isinstance(d, str) for d in t["dependencies"]) for t in tasks):
            raise LedgerError("Tasks require non-empty IDs and a list of dependency IDs")
        by_id = {t["id"]: t for t in tasks}
        if len(by_id) != len(tasks) or any(t.get("status") != "not_started" for t in tasks):
            raise LedgerError("Source IDs must be unique and source statuses must be not_started")
        seen, visiting = set(), set()

        def visit(tid):
            if tid not in by_id or tid in visiting:
                raise LedgerError("Source dependencies are missing or cyclic")
            if tid in seen:
                return
            visiting.add(tid)
            for dep in by_id[tid]["dependencies"]:
                visit(dep)
            visiting.remove(tid)
            seen.add(tid)

        for tid in by_id:
            visit(tid)
        source_hash = hashlib.sha256(raw).hexdigest()
        with self.connection(create=True) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, definition TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    recorded_at_utc TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_events (
                    event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    recorded_at_utc TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions (
                    event_id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE,
                    recorded_at_utc TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            for table in ("metadata", "tasks", "task_events", "runs", "run_events", "decisions"):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                        BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'Append-only ledger: mutation rejected'); END""")
        with self.transaction() as connection:
            existing = connection.execute("SELECT value FROM metadata WHERE key='source_sha256'").fetchone()
            if existing:
                if existing["value"] != source_hash:
                    raise LedgerError("Different source task list; existing ledger preserved")
            else:
                connection.executemany("INSERT INTO metadata VALUES (?,?)", [
                    ("schema_version", "1"), ("source_sha256", source_hash), ("source_plan", encode(plan)),
                ])
                for task in tasks:
                    connection.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], encode(task)))
                    connection.execute("INSERT INTO task_events(task_id,recorded_at_utc,status,payload) VALUES (?,?,?,?)",
                                       (task["id"], utc_timestamp(), "not_started", encode({
                                           "reason": "Imported planning definition; not evidence of implementation",
                                           "dimensions": {key: "NOT_RUN" for key in DIMENSIONS},
                                           "completion_evidence": None,
                                       })))

        self.migrate_run_integrity()

    def import_namespace(self, source: Path, *, namespace: str, provenance: dict) -> dict:
        """Append one frozen engineering task source using existing revision-3 tables.

        Original source metadata and research definitions/events remain untouched.
        Reimporting identical bytes is idempotent; revisions need a new namespace.
        Callers take a consistent private backup and rehearse on an isolated copy
        before using this administrative bootstrap on an existing formal ledger.
        """
        if not isinstance(namespace, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,15}", namespace):
            raise LedgerError("Invalid task namespace")
        if not isinstance(provenance, dict) or not isinstance(provenance.get("authority"), str) or not provenance["authority"].strip():
            raise LedgerError("Namespace import requires explicit authority provenance")
        encode(provenance)
        path = (self.root / source).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise LedgerError("Namespace source must be an existing repository file")
        # Metadata imports do not bypass the protected data partitions.
        from predictor.access import AccessPolicy
        raw = AccessPolicy(self.root).authorize(path, operation="config_read").read_bytes()
        try:
            plan = self._json_record(raw.decode("utf-8-sig"))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise LedgerError("Invalid namespace source JSON") from None
        if not isinstance(plan, dict) or plan.get("namespace") != namespace:
            raise LedgerError("Source namespace does not match requested namespace")
        tasks = plan.get("tasks")
        pattern = re.compile(re.escape(namespace) + r"\d{2}\.\d{2}")
        if not isinstance(tasks, list) or not tasks or any(
                not isinstance(task, dict) or not isinstance(task.get("id"), str)
                or not pattern.fullmatch(task["id"]) or task.get("status") != "not_started"
                or not isinstance(task.get("dependencies"), list)
                or not all(isinstance(dep, str) and dep.strip() for dep in task["dependencies"])
                or len(set(task["dependencies"])) != len(task["dependencies"])
                or task.get("stage", task["id"].split(".")[0]) != task["id"].split(".")[0]
                or task.get("dimensions", PLATFORM_INITIAL) != PLATFORM_INITIAL for task in tasks):
            raise LedgerError("Namespace tasks require matching unique IDs, dependencies and initial states")
        definitions = {task["id"]: task for task in tasks}
        if len(definitions) != len(tasks):
            raise LedgerError("Duplicate namespace task ID")
        encoded_plan = encode(plan)
        source_hash = hashlib.sha256(raw).hexdigest()
        prefix = f"task_namespace:{namespace}:"
        with self.transaction() as connection:
            original = json.loads(connection.execute("SELECT value FROM metadata WHERE key='source_plan'").fetchone()[0])
            if any(re.match(re.escape(namespace) + r"\d", task["id"]) for task in original["tasks"]):
                raise LedgerError("Original research namespace is reserved")
            existing = connection.execute("SELECT value FROM metadata WHERE key=?", (prefix + "source_sha256",)).fetchone()
            if existing:
                if existing["value"] != source_hash:
                    raise LedgerError("Different namespace source; existing definitions preserved")
                current_plan = connection.execute("SELECT value FROM metadata WHERE key=?", (prefix + "source_plan",)).fetchone()
                if not current_plan or current_plan[0] != encoded_plan:
                    raise LedgerIntegrityError("Namespace definition binding mismatch")
                for task in tasks:
                    stored = connection.execute("SELECT definition FROM tasks WHERE task_id=?", (task["id"],)).fetchone()
                    if not stored or stored[0] != encode(task):
                        raise LedgerIntegrityError("Namespace task definition binding mismatch")
                return {"namespace": namespace, "source_sha256": source_hash, "status": "already_imported_identical", "tasks": len(tasks)}
            combined = {row["task_id"]: json.loads(row["definition"])
                        for row in connection.execute("SELECT task_id,definition FROM tasks")}
            if set(combined) & set(definitions):
                raise LedgerError("Namespace task ID already exists")
            combined.update(definitions)
            seen, visiting = set(), set()

            def visit(task_id):
                if task_id not in combined or task_id in visiting:
                    raise LedgerError("Namespace dependencies are missing or cyclic")
                if task_id in seen:
                    return
                visiting.add(task_id)
                for dependency in combined[task_id]["dependencies"]:
                    visit(dependency)
                visiting.remove(task_id)
                seen.add(task_id)

            for task_id in combined:
                visit(task_id)
            at = utc_timestamp()
            record = {**provenance, "source": path.relative_to(self.root).as_posix(),
                      "source_sha256": source_hash, "recorded_at_utc": at,
                      "source_plan_sha256": hashlib.sha256(encoded_plan.encode("utf-8")).hexdigest()}
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [
                (prefix + "source_sha256", source_hash), (prefix + "source_plan", encoded_plan),
                (prefix + "provenance", encode(record)),
            ])
            for task in tasks:
                connection.execute("INSERT INTO tasks VALUES (?,?)", (task["id"], encode(task)))
                connection.execute("INSERT INTO task_events(task_id,recorded_at_utc,status,payload) VALUES (?,?,?,?)",
                    (task["id"], at, "not_started", encode({"reason": "Imported engineering source; not implementation evidence",
                        "dimensions": dict(PLATFORM_INITIAL), "completion_evidence": None, "task_namespace": namespace})))
        return {"namespace": namespace, "source_sha256": source_hash, "status": "imported", "tasks": len(tasks)}

    def artifact(self, path: str) -> dict:
        candidate = (self.root / path).resolve()
        if Path(path).is_absolute() or not candidate.is_relative_to(self.root) or not candidate.is_file():
            raise LedgerError("Evidence must be an existing repository-relative file")
        return {"path": candidate.relative_to(self.root).as_posix(), "sha256": file_hash(candidate)}

    def validate_evidence(self, evidence: dict):
        if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_ROLES:
            raise LedgerError("done requires changes, actual_runs, acceptance, limitations and handoff evidence")
        for role, reference in evidence.items():
            if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
                raise LedgerError(f"Invalid {role} artifact reference")
            require_hash(reference["sha256"], role)
            if self.artifact(reference["path"])["sha256"] != reference["sha256"]:
                raise LedgerError(f"Evidence hash mismatch: {role}")

    @staticmethod
    def task_state(connection, task_id):
        row = connection.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY event_id DESC LIMIT 1",
                                 (task_id,)).fetchone()
        if not row:
            raise LedgerError(f"Unknown task: {task_id}")
        return row

    def transition_task(self, task_id: str, status: str, *, reason: str,
                        evidence: dict | None = None, dimensions: dict | None = None):
        if not isinstance(reason, str) or not reason.strip():
            raise LedgerError("Task transitions require a reason")
        if status == "done":
            self.validate_evidence(evidence)
        if dimensions is not None and not isinstance(dimensions, dict):
            raise LedgerError("Status dimensions must be a JSON object")
        with self.transaction() as connection:
            previous = self.task_state(connection, task_id)
            if status not in TRANSITIONS[previous["status"]]:
                raise LedgerError(f"Invalid task transition: {previous['status']} -> {status}")
            task = json.loads(connection.execute("SELECT definition FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0])
            if status in {"in_progress", "done"}:
                for dep in task["dependencies"]:
                    if self.task_state(connection, dep)["status"] != "done":
                        raise LedgerError(f"Unfinished dependency: {dep}")
            previous_payload = json.loads(previous["payload"])
            current = previous_payload["dimensions"]
            namespace = previous_payload.get("task_namespace")
            choices = PLATFORM_DIMENSIONS if namespace else DIMENSIONS
            for key, value in (dimensions or {}).items():
                if key not in choices or value not in choices[key]:
                    raise LedgerError(f"Invalid independent status dimension: {key}")
                current[key] = value
            if namespace and status == "done" and (current["implementation"] != "IMPLEMENTED"
                    or current["verification"] in {"NOT_RUN", "FAILED"}):
                raise LedgerError("Engineering task done requires implemented and verified evidence dimensions")
            payload = {"reason": reason, "dimensions": current, "completion_evidence": evidence}
            if namespace:
                payload["task_namespace"] = namespace
            connection.execute("INSERT INTO task_events(task_id,recorded_at_utc,status,payload) VALUES (?,?,?,?)",
                               (task_id, utc_timestamp(), status, encode(payload)))

    def migrate_run_integrity(self, *, backup_to: Path | None = None):
        """Add guard revisions 2/3 atomically; preserve schema 1 and every old row.

        Call explicitly for an existing ledger, after a consistent backup and
        isolated compatibility test. Invalid history aborts the whole migration.
        """
        with self.transaction() as connection:
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise LedgerIntegrityError("Legacy foreign-key violation; migration aborted")
            if connection.execute('PRAGMA integrity_check').fetchall()[0][0] != 'ok':
                raise LedgerIntegrityError('Legacy integrity violation; migration aborted')
            for row in connection.execute("SELECT run_id FROM runs").fetchall():
                self._run_state(connection, row[0])
            if backup_to is not None:
                destination = Path(backup_to).resolve()
                if destination == self.database.resolve() or destination.exists():
                    raise LedgerError("Migration backup must be a new separate file")
                destination.parent.mkdir(parents=True, exist_ok=True)
                # BEGIN IMMEDIATE holds off writers; a separate readonly source
                # backs up the same committed preimage without backing up a live
                # write transaction on this connection.
                with closing(sqlite3.connect(self.database.as_uri()+"?mode=ro", uri=True)) as source, \
                        closing(sqlite3.connect(destination)) as snapshot:
                    source.backup(snapshot)
            connection.execute("""CREATE TABLE IF NOT EXISTS ledger_migrations (
                version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL,
                description TEXT NOT NULL)""")
            for action in ("UPDATE", "DELETE"):
                connection.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_ledger_migrations_{action}
                    BEFORE {action} ON ledger_migrations BEGIN
                    SELECT RAISE(ABORT,'Append-only migration log'); END""")
            for statement in run_guard_statements():
                connection.execute(statement)
            if not connection.execute('SELECT 1 FROM ledger_migrations WHERE version=2').fetchone():
                connection.execute("""INSERT INTO ledger_migrations VALUES
                    (2, ?, 'Add run INSERT guards and strict read validity; old rows untouched')""",
                    (utc_timestamp(),))
            connection.execute('''CREATE TABLE IF NOT EXISTS run_declarations (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id), metadata TEXT NOT NULL,
                registration_event_id INTEGER NOT NULL UNIQUE REFERENCES run_events(event_id),
                recorded_at_utc TEXT NOT NULL, provenance TEXT NOT NULL
                    CHECK(provenance IN ('registration','legacy_checkpoint')))''')
            # This is an observation at migration, never reconstructed preregistration.
            if not connection.execute('SELECT 1 FROM ledger_migrations WHERE version=3').fetchone():
                connection.execute('''INSERT INTO run_declarations
                    SELECT r.run_id,r.metadata,e.event_id,e.recorded_at_utc,'legacy_checkpoint'
                    FROM runs r JOIN run_events e ON e.run_id=r.run_id AND e.status='running'
                    WHERE NOT EXISTS(SELECT 1 FROM run_declarations d WHERE d.run_id=r.run_id)''')
            for action in ('UPDATE','DELETE'):
                connection.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_run_declarations_{action}
                    BEFORE {action} ON run_declarations BEGIN
                    SELECT RAISE(ABORT,'Append-only declaration'); END''')
            for table in ('metadata','tasks','task_events','runs','run_events','decisions'):
                for action in ('UPDATE','DELETE'):
                    connection.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                        BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT,'Append-only ledger: mutation rejected'); END''')
            install_identity_guards(connection, ('metadata','tasks','task_events','runs',
                'run_events','decisions','ledger_migrations','run_declarations'))
            connection.execute('''CREATE TRIGGER IF NOT EXISTS registration_declaration_v3
                AFTER INSERT ON run_events WHEN NEW.status='running'
                BEGIN INSERT INTO run_declarations
                    SELECT run_id,metadata,NEW.event_id,NEW.recorded_at_utc,'registration'
                    FROM runs WHERE run_id=NEW.run_id; END''')
            if not connection.execute('SELECT 1 FROM ledger_migrations WHERE version=3').fetchone():
                connection.execute("INSERT INTO ledger_migrations VALUES(3,?,?)", (utc_timestamp(),
                    'Reject conflicting INSERT/REPLACE identities; declaration checkpoints distinguish legacy'))
            if connection.execute('PRAGMA foreign_key_check').fetchone():
                raise LedgerIntegrityError('Migration foreign-key violation')

    @staticmethod
    def _json_record(raw):
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result
        def invalid_constant(_):
            raise ValueError("Nonfinite JSON constant")
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
        # Includes nonfinite numbers accepted by JSON exponent parsing.
        json.dumps(value, allow_nan=False)
        return value

    @classmethod
    def _run_state(cls, connection, run_id):
        row = connection.execute("SELECT metadata FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise LedgerError(f"Unknown run: {run_id}")
        events = connection.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY event_id", (run_id,)).fetchall()
        try:
            if not events or events[0]["status"] != "running" or len(events) > 2:
                raise ValueError("Invalid initial or duplicate terminal state")
            declaration_provenance = 'legacy_unbound'
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='run_declarations' AND type='table'").fetchone():
                declared = connection.execute('SELECT * FROM run_declarations WHERE run_id=?',(run_id,)).fetchone()
                if not declared or declared['metadata'] != row[0] or declared['registration_event_id'] != events[0]['event_id'] or declared['recorded_at_utc'] != events[0]['recorded_at_utc']:
                    raise ValueError('Registration declaration mismatch')
                declaration_provenance = declared['provenance']
            metadata = cls._json_record(row[0])
            metadata_valid(metadata, recorded_at=events[0]["recorded_at_utc"], stored=True)
            if metadata.get("experiment_id") != run_id:
                raise ValueError("Run ID mismatch")
            parent = metadata["search_parent"]
            if parent is not None and (parent == run_id or not connection.execute(
                    "SELECT 1 FROM runs WHERE run_id=?", (parent,)).fetchone()):
                raise ValueError("Invalid search parent")
            result = []
            previous_time = None
            for index, event in enumerate(events):
                if event["event_id"] < 1:
                    raise ValueError("Invalid event ID")
                at = run_instant(event["recorded_at_utc"], "recorded_at_utc")
                if previous_time is not None and at < previous_time:
                    raise ValueError("Run event time goes backwards")
                payload = cls._json_record(event["payload"])
                if index == 0:
                    if payload != {}:
                        raise ValueError("Invalid running payload")
                else:
                    result_valid(event["status"], payload)
                result.append({"event_id": event["event_id"], "recorded_at_utc": event["recorded_at_utc"],
                               "status": event["status"], "result": payload})
                previous_time = at
            return {"metadata": metadata, "status": events[-1]["status"], "events": result,
                    "declaration_provenance": declaration_provenance}
        except (ValueError, TypeError, KeyError, OverflowError):
            # No raw metadata, credentials, payload or guessed repair in diagnostics.
            raise LedgerIntegrityError("Run history is invalid; preserved for explicit legacy review") from None

    def start_run(self, metadata: dict):
        recorded = utc_timestamp()
        try:
            metadata_valid(metadata, recorded_at=recorded)
        except (ValueError, TypeError, OverflowError) as exc:
            raise LedgerError(str(exc)) from None
        stored = dict(metadata)
        stored["configuration_hash"] = run_configuration_hash(metadata["configuration"])
        encoded = encode(stored)
        run_id = metadata["experiment_id"]
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                raise LedgerError(f"Duplicate run_id: {run_id}")
            if metadata["search_parent"] is not None and not connection.execute(
                    "SELECT 1 FROM runs WHERE run_id=?", (metadata["search_parent"],)).fetchone():
                raise LedgerError("Unknown search_parent run")
            connection.execute("INSERT INTO runs VALUES (?,?)", (run_id, encoded))
            connection.execute("INSERT INTO run_events(run_id,recorded_at_utc,status,payload) VALUES (?,?,?,?)",
                               (run_id, recorded, "running", encode({})))

    def finish_run(self, run_id: str, status: str, result: dict):
        try:
            result_valid(status, result)
        except (ValueError, TypeError, OverflowError) as exc:
            raise LedgerError(str(exc)) from None
        encoded = encode(result)
        with self.transaction() as connection:
            previous = self._run_state(connection, run_id)
            if previous["status"] != "running":
                raise LedgerError("Terminal run; start a new run_id for retries")
            connection.execute("INSERT INTO run_events(run_id,recorded_at_utc,status,payload) VALUES (?,?,?,?)",
                               (run_id, utc_timestamp(), status, encoded))

    def get_run(self, run_id: str) -> dict:
        with self.connection() as connection:
            return self._run_state(connection, run_id)


    def append_decision(self, decision_id: str, payload: dict, *, precondition=None):
        required = {"motivation", "impact", "data_seen", "tasks_to_rerun", "new_test_window_required"}
        if not isinstance(payload, dict) or required - set(payload) or not decision_id.strip():
            raise LedgerError("Decision requires ID, motivation, impact, data seen, rerun tasks and test-window decision")
        if type(payload["new_test_window_required"]) is not bool:
            raise LedgerError("new_test_window_required must be boolean")
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM decisions WHERE decision_id=?", (decision_id,)).fetchone():
                raise LedgerError("Duplicate decision_id")
            # Trusted contract callers can validate history under the same write lock.
            # Raising rolls back; ordinary decisions retain their existing behavior.
            if precondition is not None:
                precondition(connection)
            connection.execute("INSERT INTO decisions(decision_id,recorded_at_utc,payload) VALUES (?,?,?)",
                               (decision_id, utc_timestamp(), encode(payload)))

    def export_views(self) -> dict:
        # Serialize exporters/writers together so an older snapshot cannot replace a newer one.
        with self.transaction() as connection:
            plan = json.loads(connection.execute("SELECT value FROM metadata WHERE key='source_plan'").fetchone()[0])
            source_hash = connection.execute("SELECT value FROM metadata WHERE key='source_sha256'").fetchone()[0]
            namespaces = []
            for row in connection.execute("SELECT key,value FROM metadata WHERE key LIKE 'task_namespace:%:source_plan' ORDER BY rowid"):
                namespace_plan = self._json_record(row["value"])
                namespace = namespace_plan["namespace"]
                prefix = f"task_namespace:{namespace}:"
                provenance = self._json_record(connection.execute("SELECT value FROM metadata WHERE key=?", (prefix + "provenance",)).fetchone()[0])
                if hashlib.sha256(row["value"].encode("utf-8")).hexdigest() != provenance["source_plan_sha256"]:
                    raise LedgerIntegrityError("Namespace source binding mismatch")
                for task in namespace_plan["tasks"]:
                    definition = connection.execute("SELECT definition FROM tasks WHERE task_id=?", (task["id"],)).fetchone()
                    if not definition or definition[0] != encode(task):
                        raise LedgerIntegrityError("Namespace task definition binding mismatch")
                # Legacy stage-gate callers index task['stage'] on every row.
                # This is derived display metadata, not a mutation of source definitions.
                plan["tasks"].extend({**task, "stage": task["id"].split(".")[0]} for task in namespace_plan["tasks"])
                namespaces.append({"namespace": namespace, **provenance})
            for task in plan["tasks"]:
                event = self.task_state(connection, task["id"])
                payload = json.loads(event["payload"])
                task.update({"status": event["status"], "status_dimensions": payload["dimensions"],
                             "completion_evidence": payload["completion_evidence"], "last_event_id": event["event_id"]})
                if payload.get("task_namespace"):
                    # Avoid exposing frozen initial dimensions beside current state.
                    task["dimensions"] = dict(payload["dimensions"])
            plan["tracking"] = {"source_sha256": source_hash, "database": "planning/research_ledger.sqlite3",
                                "view_is_derived": True, "scope": "task_tracking_no_automatic_market_claims"}
            if namespaces:
                plan["tracking"]["engineering_namespaces"] = namespaces
            decisions = [{"event_id": row["event_id"], "decision_id": row["decision_id"],
                          "recorded_at_utc": row["recorded_at_utc"], **json.loads(row["payload"])}
                         for row in connection.execute("SELECT * FROM decisions ORDER BY event_id")]
            outputs = {"planning/tasks.json": json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                       "docs/decision_log.jsonl": "".join(encode(d) + "\n" for d in decisions)}
            for relative, text in outputs.items():
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
                temporary.write_text(text, encoding="utf-8", newline="\n")
                temporary.replace(path)
            return plan
