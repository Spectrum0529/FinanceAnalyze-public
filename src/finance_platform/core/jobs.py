"""Persistent queue: immutable runs, explicit handlers, cancellation and fencing."""
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
import re
import sqlite3
from pathlib import Path
import time
from types import MappingProxyType
import uuid

from .auth import AccountDelegation, Principal, require_owner, require_writer
from .contracts import RunMode, RunSpec, VersionRef, canonical, identifier, parse_instant, payload
from .database import Database
from .errors import PlatformError
from .pagination import validate_pagination
from .pending_writes import require_open_key
from .security import now_text, safe_json
from .source_binding import BindingError, deployment_digest


@dataclass(frozen=True)
class HandlerDefinition:
    name: str
    handler: object
    validate: object
    recoverable: bool
    allowed_modes: tuple[RunMode, ...]
    code_hash: str
    resolve_versions: object = None
    auto_recovery_limit: int = 2
    internal_only: bool = False


class HandlerRegistry:
    def __init__(self, root=None, *, disabled=()):
        self._disabled = frozenset(disabled)
        self.root = Path(root).resolve() if root else Path(__file__).resolve().parents[3]
        self._handlers = {}

    def register(self, name, handler, *, validate=None, recoverable=False,
                 allowed_modes=(RunMode.BACKTEST, RunMode.LOCAL_SIMULATION), resolve_versions=None,
                 auto_recovery_limit=2, internal_only=False):
        identifier(name, "handler")
        if self.is_disabled(name):
            return
        if name in self._handlers or not callable(handler) or validate is not None and not callable(validate):
            raise PlatformError("CONFLICT", 409)
        try:
            modes = tuple(RunMode(m) for m in allowed_modes)
        except ValueError:
            raise PlatformError("INVALID_INPUT") from None
        if (not modes or RunMode.LIVE in modes or type(recoverable) is not bool
                or type(internal_only) is not bool or internal_only and recoverable):
            raise PlatformError("INVALID_INPUT")
        try:
            code = Path(inspect.getfile(handler)).read_bytes()
        except (TypeError, OSError):
            raise PlatformError("INVALID_INPUT") from None
        if resolve_versions is not None and not callable(resolve_versions):
            raise PlatformError("INVALID_INPUT")
        if type(auto_recovery_limit) is not int or not 0 <= auto_recovery_limit <= 10:
            raise PlatformError("INVALID_INPUT")
        self._handlers[name] = HandlerDefinition(name, handler, validate, recoverable, modes,
                                                 self.current_code_hash(handler), resolve_versions,
                                                 auto_recovery_limit, internal_only)

    def current_code_hash(self, handler):
        try:
            return deployment_digest(self.root,external_handler=Path(inspect.getfile(handler)))
        except (BindingError,OSError,TypeError):
            raise PlatformError("INVALID_INPUT") from None

    def is_disabled(self, name):
        return name in self._disabled

    def require_enabled(self, name):
        if self.is_disabled(name):
            raise PlatformError("HOSTED_OPERATION_DISABLED", 403)

    def get(self, name):
        self.require_enabled(name)
        definition = self._handlers.get(name)
        if not definition:
            raise PlatformError("HANDLER_UNAVAILABLE", 422)
        return definition

    def is_internal(self, name):
        definition = self._handlers.get(name)
        return definition is not None and definition.internal_only

    def list(self):
        return [{"name": d.name, "recoverable": d.recoverable, "auto_recovery_limit": d.auto_recovery_limit, "modes": [m.value for m in d.allowed_modes], "code_hash": d.code_hash}
                for d in sorted(self._handlers.values(), key=lambda d: d.name)]


class VersionService:
    def __init__(self, database):
        self.database = database
        self.storage = None

    def attach_storage(self, storage):
        self.database.apply_module_migrations('product_version_storage', ('''
CREATE TABLE resource_version_payload_refs(
 resource_id TEXT NOT NULL,version TEXT NOT NULL,owner_id TEXT NOT NULL,
 category TEXT NOT NULL,reference_json TEXT NOT NULL,
 PRIMARY KEY(resource_id,version),
 FOREIGN KEY(resource_id,version) REFERENCES resource_versions(resource_id,version));
''',))
        from .database import immutable
        with self.database.transaction() as con:
            immutable(con, 'resource_version_payload_refs', ('resource_id', 'version'))
        self.storage = storage

    def freeze(self, principal, resource_id, version, kind, value, *, storage_binding=None):
        require_writer(principal)
        identifier(resource_id)
        identifier(version)
        identifier(kind)
        if storage_binding is not None and self.storage is None:
            raise PlatformError('STORAGE_UNAVAILABLE', 503)
        encoded = safe_json(value)
        sha = hashlib.sha256(encoded.encode()).hexdigest()
        with self.database.transaction() as con:
            if isinstance(principal, AccountDelegation):
                principal.check_approval(con)
            # Only explicit version payloads use this route. Compact fallback
            # run/config/handler metadata in _insert retains its original form.
            category = {'data': 'market_data', 'model': 'artifacts'}.get(kind)
            self._store(con, principal.user_id, resource_id, version, kind, encoded, sha,
                        storage_category=category, storage_binding=storage_binding)
        return VersionRef(resource_id, version, sha)

    def _store(self, con, owner_id, resource_id, version, kind, encoded, sha, *,
               storage_category=None, storage_binding=None):
        old = con.execute("SELECT * FROM resource_versions WHERE resource_id=? AND version=?", (resource_id, version)).fetchone()
        if old:
            if old["content_hash"] != sha or old["owner_id"] != owner_id or old["kind"] != kind:
                raise PlatformError("CONFLICT", 409)
            return
        stored = encoded
        reference = None
        if self.storage is not None and storage_category is not None:
            binding = storage_binding
            if binding is None:
                key = 'version:' + hashlib.sha256(safe_json([resource_id, version]).encode()).hexdigest()
                binding = self.storage.bind(owner_id, key, categories=(storage_category,), connection=con)
            if type(binding) is not dict or binding.get('owner_id') != owner_id:
                raise PlatformError('STORAGE_REFERENCE_INVALID', 409)
            reference = self.storage.put(binding, storage_category, encoded.encode('utf-8'), media_type='application/json', connection=con)
            # Authority is the immutable server-written sidecar, never a magic
            # schema in user-supplied JSON. Logical hash still covers original.
            stored = 'null'
        con.execute("INSERT INTO resource_versions VALUES (?,?,?,?,?,?,?)", (resource_id, version, owner_id, kind, sha, stored, now_text()))
        if reference is not None:
            con.execute('INSERT INTO resource_version_payload_refs VALUES(?,?,?,?,?)',
                        (resource_id, version, owner_id, storage_category, safe_json(reference)))
        self.database.audit(owner_id, "version.frozen", resource_id, {"version": version, "kind": kind, "content_hash": sha}, connection=con)

    def get(self, principal, reference):
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM resource_versions WHERE resource_id=? AND version=?", (reference.resource_id, reference.version)).fetchone()
            # Old immutable DB images need no schema mutation or sqlite_master
            # enumeration. Only the exact absent additive table is legacy;
            # permission denials and every other database error remain errors.
            try:
                link = con.execute('SELECT * FROM resource_version_payload_refs WHERE resource_id=? AND version=?',
                                   (reference.resource_id, reference.version)).fetchone()
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_ERROR or str(error) != 'no such table: resource_version_payload_refs':
                    raise
                link = None
        if not row:
            raise PlatformError("NOT_FOUND", 404)
        require_owner(principal, row["owner_id"])
        if row["content_hash"] != reference.content_hash:
            raise PlatformError("CONFLICT", 409)
        encoded = row['payload_json'].encode('utf-8')
        if link is not None:
            if self.storage is None:
                raise PlatformError('STORAGE_UNAVAILABLE', 503)
            if link['owner_id'] != row['owner_id'] or link['category'] != {'data':'market_data','model':'artifacts'}.get(row['kind']):
                raise PlatformError('STORAGE_REFERENCE_INVALID', 409)
            encoded = self.storage.read(row['owner_id'], json.loads(link['reference_json']),
                                        category=link['category'], maximum_bytes=2*1024*1024)
        if hashlib.sha256(encoded).hexdigest() != row['content_hash']:
            raise PlatformError('STORAGE_HASH_MISMATCH', 409)
        return json.loads(encoded)


def parse_run(record):
    values = json.loads(record) if isinstance(record, str) else dict(record)
    values["mode"] = RunMode(values["mode"])
    values["created_at"] = parse_instant(values["created_at"])
    for name in ("strategy", "config", "data", "model"):
        if values[name] is not None:
            values[name] = VersionRef(**values[name])
    return RunSpec(**values)


def retry_command_hash(job_id, idempotency_key, intent):
    if intent not in {"retry", "resume"}:
        raise PlatformError("INVALID_INPUT", 422)
    if idempotency_key is not None:
        try:
            identifier(idempotency_key, "idempotency key")
        except ValueError:
            raise PlatformError("INVALID_INPUT", 422) from None
    return hashlib.sha256(safe_json({"action": intent, "job_id": job_id}).encode()).hexdigest()


class JobService:
    def __init__(self, database: Database, registry: HandlerRegistry, *, dependency_hash: str, clock=time.time):
        self.database, self.registry, self.clock = database, registry, clock
        self.dependency_hash = dependency_hash
        self.versions = VersionService(database)
        # Trusted runtime composition only; clients cannot inject storage roots.
        self.storage = None

    def _validate(self, principal, handler, parameters, mode, versions, *, internal=False):
        require_writer(principal)
        try:
            mode = RunMode(mode)
        except (ValueError, TypeError):
            raise PlatformError("INVALID_INPUT", 422) from None
        if mode is RunMode.LIVE:
            raise PlatformError("LIVE_DISABLED", 403)
        definition = self.registry.get(handler)
        if definition.internal_only != internal:
            raise PlatformError("INTERNAL_HANDLER_REQUIRED", 403)
        if self.registry.current_code_hash(definition.handler) != definition.code_hash:
            raise PlatformError("CONFLICT", 409)
        lock = self.registry.root / "uv.lock"
        if not lock.is_file() or hashlib.sha256(lock.read_bytes()).hexdigest() != self.dependency_hash:
            raise PlatformError("CONFLICT", 409)
        if mode not in definition.allowed_modes:
            raise PlatformError("UNSUPPORTED_CAPABILITY", 422)
        if type(parameters) is not dict:
            raise PlatformError("INVALID_INPUT", 422)
        parameters_json = safe_json(parameters)
        parameters = json.loads(parameters_json)
        if definition.validate:
            try:
                definition.validate(parameters)
            except PlatformError:
                raise
            except ValueError as error:
                raise PlatformError(getattr(error, "code", "INVALID_INPUT"), 422) from None
        if versions is not None:
            # Public callers cannot supply provenance declarations. Feature
            # handlers resolve actual immutable resources from validated params.
            raise PlatformError("INVALID_INPUT", 422)
        return mode, definition, parameters_json

    def _resolved_versions(self, principal, definition, parameters):
        if definition.resolve_versions is None:
            return {}
        try:
            resolved = definition.resolve_versions(principal, parameters)
        except PlatformError:
            raise
        except ValueError as error:
            raise PlatformError(getattr(error, "code", "INVALID_INPUT"), 422) from None
        if type(resolved) is not dict or set(resolved) - {"strategy", "data", "model", "model_reason", "seed", "profile_id"}:
            raise PlatformError("INVALID_INPUT", 422)
        result = {}
        for name, value in resolved.items():
            result[name] = payload(value) if isinstance(value, VersionRef) else value
        safe_json(result)
        if result.get("profile_id"):
            raise PlatformError("PROFILE_BLOCKED", 403)
        for name in ("strategy", "data", "model"):
            if result.get(name) is not None:
                try:
                    ref = VersionRef(**result[name])
                except (ValueError, TypeError):
                    raise PlatformError("INVALID_INPUT", 422) from None
                self.versions.get(principal, ref)
        return result

    def submit(self, principal, handler, parameters, *, mode=RunMode.BACKTEST, idempotency_key=None, versions=None, storage_binding=None, goal_link=None):
        return self._submit(principal, handler, parameters, mode=mode, idempotency_key=idempotency_key,
                            versions=versions, storage_binding=storage_binding, goal_link=goal_link, origin=None)

    def submit_internal(self, principal, handler, parameters, *, mode=RunMode.BACKTEST,
                        idempotency_key=None, storage_binding=None, goal_link=None):
        origin = "goal" if goal_link is not None else "service"
        return self._submit(principal, handler, parameters, mode=mode, idempotency_key=idempotency_key,
                            versions=None, storage_binding=storage_binding, goal_link=goal_link, origin=origin)

    def _submit(self, principal, handler, parameters, *, mode, idempotency_key,
                versions, storage_binding, goal_link, origin):
        mode, definition, encoded = self._validate(principal, handler, parameters, mode, versions, internal=origin is not None)
        if idempotency_key is not None:
            identifier(idempotency_key, "idempotency key")
        request_hash = hashlib.sha256(safe_json({"handler": handler, "parameters": json.loads(encoded), "mode": mode.value, "versions": versions}).encode()).hexdigest()
        with self.database.transaction() as con:
            delegated = principal if isinstance(principal, AccountDelegation) else None
            if delegated is not None:
                delegated.check_approval(con)
            if idempotency_key:
                old = con.execute("SELECT * FROM job_keys WHERE owner_id=? AND idempotency_key=?", (principal.user_id, idempotency_key)).fetchone()
                if old:
                    if old["request_hash"] != request_hash:
                        raise PlatformError("CONFLICT", 409)
                    job = con.execute("SELECT j.*,r.parent_run_id FROM jobs j JOIN runs r USING(run_id) WHERE job_id=?",
                                      (old["job_id"],)).fetchone()
                    if origin is not None and self._origin(con, job) != origin:
                        raise PlatformError("INTERNAL_JOB_ORIGIN_REQUIRED", 409)
                    return self._record(con, job, principal)
                require_open_key(con, principal.user_id, "jobs", idempotency_key)
            job = self._insert(con, principal.user_id, definition, encoded, mode, versions,
                               storage_binding=storage_binding, internal_origin=origin, delegated=delegated)
            if goal_link is not None:
                goal_link(con, job["job_id"])
            if idempotency_key:
                con.execute("INSERT INTO job_keys VALUES (?,?,?,?)", (principal.user_id, idempotency_key, request_hash, job["job_id"]))
            return job

    def _insert(self, con, owner_id, definition, encoded, mode, versions=None, *, parent_run_id=None,
                storage_binding=None, internal_origin=None, delegated=None):
        if definition.internal_only and (internal_origin not in {"service", "goal"} or parent_run_id is not None):
            raise PlatformError("INTERNAL_JOB_ORIGIN_REQUIRED", 409)
        user = con.execute("SELECT * FROM users WHERE user_id=?", (owner_id,)).fetchone()
        principal = delegated or Principal(owner_id, user["username"], user["role"])
        resolved = self._resolved_versions(principal, definition, json.loads(encoded))
        # Internal retry/recovery retains exact references and refuses drift.
        if versions:
            if versions.get("code_hash") != definition.code_hash or versions.get("dependency_hash") != self.dependency_hash:
                raise PlatformError("CONFLICT", 409)
            for name in ("strategy", "data", "model"):
                if name in resolved and versions.get(name) != resolved[name]:
                    raise PlatformError("CONFLICT", 409)
            resolved = dict(versions)
        versions = resolved
        job_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        stamp = now_text()
        sha = hashlib.sha256(encoded.encode()).hexdigest()
        # Explicit absence of a snapshot is neither synthetic data nor acquired
        # market evidence. Only a server resolver can attach actual data lineage.
        synthetic = safe_json({"snapshot": None, "market_data_claim": "NONE", "purpose": "No data snapshot attached"})
        synthetic_hash = hashlib.sha256(synthetic.encode()).hexdigest()
        strategy_value = safe_json({"handler": definition.name, "code_hash": definition.code_hash})
        strategy_hash = hashlib.sha256(strategy_value.encode()).hexdigest()
        default_strategy = VersionRef("handler:" + strategy_hash[:16] + ":" + owner_id, definition.code_hash[:12], strategy_hash)
        default_config = VersionRef("config:" + sha[:24] + ":" + owner_id, "1", sha)
        default_data = VersionRef("data-none:" + owner_id, "1", synthetic_hash)
        for ref, kind, value in ((default_strategy, "strategy", strategy_value),
                                 (default_config, "config", encoded), (default_data, "data", synthetic)):
            self.versions._store(con, owner_id, ref.resource_id, ref.version, kind, value, ref.content_hash)
        refs = {}
        for name, fallback in (("strategy", default_strategy), ("config", default_config), ("data", default_data)):
            refs[name] = VersionRef(**versions[name]) if versions.get(name) else fallback
        model = VersionRef(**versions["model"]) if versions.get("model") else None
        try:
            spec = RunSpec(run_id, owner_id, definition.name, mode, refs["strategy"], refs["config"], refs["data"],
                           versions.get("code_hash", definition.code_hash), versions.get("dependency_hash", self.dependency_hash),
                           datetime.now(timezone.utc), versions.get("seed", 0), model=model,
                           model_reason=versions.get("model_reason", None if model else "No model used"), parent_run_id=parent_run_id)
        except (TypeError, ValueError):
            raise PlatformError("INVALID_INPUT", 422) from None
        spec_json = canonical(spec)
        con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", (run_id, owner_id, definition.name, spec_json,
                    hashlib.sha256(spec_json.encode()).hexdigest(), parent_run_id, stamp))
        con.execute("INSERT INTO jobs(job_id,run_id,owner_id,handler,parameters_json,parameters_hash,mode,status,progress,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'QUEUED',0,?,?)",
                    (job_id, run_id, owner_id, definition.name, encoded, sha, mode.value, stamp, stamp))
        if storage_binding is not None and self.storage is None:
            raise PlatformError('STORAGE_TASK_BINDING_REQUIRED', 409)
        if self.storage is not None:
            # Trusted prepared inputs retain the same original snapshot even if
            # settings changed while preparing. No HTTP/MCP input forwards this.
            if storage_binding is not None:
                if (type(storage_binding) is not dict or storage_binding.get('owner_id') != owner_id
                        or not {'market_data', 'reports', 'artifacts'} <= set(storage_binding.get('locations', {}))):
                    raise PlatformError('STORAGE_BINDING_INVALID', 409)
                self.storage.bind_snapshot(owner_id, 'job:' + job_id, storage_binding, connection=con)
            else:
                self.storage.bind(owner_id, 'job:' + job_id, connection=con)
        metadata = {"parent_run_id": parent_run_id}
        if internal_origin is not None:
            metadata.update(internal_origin=internal_origin, owner_id=owner_id, handler=definition.name,
                            parameters_hash=sha)
        self._event(con, job_id, run_id, "QUEUED", metadata)
        self.database.audit(owner_id, "job.submitted", job_id, {"run_id": run_id, "handler": definition.name, "mode": mode.value}, connection=con)
        return self._record(con, con.execute("SELECT j.*,r.parent_run_id FROM jobs j JOIN runs r USING(run_id) WHERE job_id=?", (job_id,)).fetchone())

    def _event(self, con, job_id, run_id, kind, metadata=None):
        con.execute("INSERT INTO job_events(job_id,run_id,kind,recorded_at,metadata_json) VALUES (?,?,?,?,?)", (job_id, run_id, kind, now_text(), safe_json(metadata or {})))

    @staticmethod
    def _origin(con, job):
        event = con.execute("SELECT metadata_json FROM job_events WHERE job_id=? AND run_id=? AND kind='QUEUED' ORDER BY event_id LIMIT 1",
                            (job["job_id"], job["run_id"])).fetchone()
        if event is not None:
            receipt = json.loads(event["metadata_json"])
            if (receipt.get("internal_origin") in ("service", "goal") and receipt.get("owner_id") == job["owner_id"]
                    and receipt.get("handler") == job["handler"] and receipt.get("parent_run_id") is None
                    and receipt.get("parameters_hash") == job["parameters_hash"]
                    and job["parameters_hash"] == hashlib.sha256(job["parameters_json"].encode()).hexdigest()):
                return receipt["internal_origin"]
        return None

    @staticmethod
    def _result_visible(con, row):
        from .result_lifecycle import state_for, table_exists

        handler, owner, run = row["handler"], row["owner_id"], row["run_id"]
        if handler == "autonomous_research_experiment":
            if not table_exists(con, "autonomous_attempts"):
                return True
            attempt = con.execute("""
                SELECT a.attempt_id FROM autonomous_attempts a JOIN autonomous_sessions s USING(session_id)
                WHERE a.job_id=? AND a.run_id=? AND s.owner_id=?
            """, (row["job_id"], run, owner)).fetchone()
            return attempt is None or not state_for(con, owner, "autonomous", attempt[0])["deleted"]
        if not handler.startswith("research."):
            return True
        sources = ("research", "daily", "tasks", "jobs")
        return all(not state_for(con, owner, source, run)["deleted"] for source in sources)

    def _record(self, con, row, principal=None):
        result = None
        if not isinstance(principal, AccountDelegation) and row["result_json"] and self._result_visible(con, row):
            result = json.loads(row["result_json"])
        return {"job_id": row["job_id"], "run_id": row["run_id"], "parent_run_id": row["parent_run_id"] if "parent_run_id" in row.keys() else None,
                "owner_id": row["owner_id"], "handler": row["handler"], "mode": row["mode"], "status": row["status"],
                "progress": row["progress"], "cancel_requested": bool(row["cancel_requested"]), "created_at": row["created_at"],
                "updated_at": row["updated_at"], "result": result, "error_code": row["error_code"]}

    def get(self, principal, job_id):
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT j.*,r.parent_run_id FROM jobs j JOIN runs r USING(run_id) WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise PlatformError("NOT_FOUND", 404)
            require_owner(principal, row["owner_id"])
            return self._record(con, row, principal)

    @staticmethod
    def _paging(limit, offset):
        validate_pagination(limit, offset)

    def list(self, principal, *, limit=50, offset=0):
        self._paging(limit, offset)
        predicate, args = ("1=1", ()) if principal.role == "admin" else ("j.owner_id=?", (principal.user_id,))
        with closing(self.database.connect()) as con:
            total = con.execute(f"SELECT count(*) FROM jobs j WHERE {predicate}", args).fetchone()[0]
            rows = con.execute(f"SELECT j.*,r.parent_run_id FROM jobs j JOIN runs r USING(run_id) WHERE {predicate} ORDER BY j.created_at DESC,j.job_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            return {"items": [self._record(con, r, principal) for r in rows], "total": total, "limit": limit, "offset": offset}

    def events(self, principal, job_id, *, limit=50, offset=0):
        self._paging(limit, offset)
        self.get(principal, job_id)
        with closing(self.database.connect()) as con:
            total = con.execute("SELECT count(*) FROM job_events WHERE job_id=?", (job_id,)).fetchone()[0]
            rows = con.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY event_id LIMIT ? OFFSET ?", (job_id, limit, offset)).fetchall()
        delegated = isinstance(principal, AccountDelegation)
        items = [{"event_id": r["event_id"], "job_id": r["job_id"], "run_id": r["run_id"], "kind": r["kind"], "recorded_at": r["recorded_at"],
                  "metadata": {} if delegated else json.loads(r["metadata_json"])} for r in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def cancel(self, principal, job_id, *, authorize_job=None):
        with self.database.transaction() as con:
            if isinstance(principal, AccountDelegation):
                principal.check_approval(con)
            row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise PlatformError("NOT_FOUND", 404)
            require_owner(principal, row["owner_id"], write=True)
            if authorize_job is not None:
                authorize_job(con, row)
            if row["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise PlatformError("CONFLICT", 409)
            if row["cancel_requested"]:
                return self._record(con, row, principal)
            state = "CANCELLED" if row["status"] == "QUEUED" else "RUNNING"
            con.execute("UPDATE jobs SET cancel_requested=1,status=?,updated_at=? WHERE job_id=?", (state, now_text(), job_id))
            self._event(con, job_id, row["run_id"], "CANCELLED" if state == "CANCELLED" else "CANCEL_REQUESTED")
            self.database.audit(principal.user_id, "job.cancel_requested", job_id, connection=con)
        return self.get(principal, job_id)

    def _control_row(self, con, principal, job_id, *, write=False, authorize_job=None):
        row = con.execute("""
            SELECT j.*, r.spec_json, r.parent_run_id FROM jobs j JOIN runs r USING(run_id)
            WHERE j.job_id=?
        """, (job_id,)).fetchone()
        if row is None or isinstance(principal, AccountDelegation) and row["owner_id"] != principal.user_id:
            raise PlatformError("NOT_FOUND", 404)
        require_owner(principal, row["owner_id"], write=write)
        if authorize_job is not None:
            authorize_job(con, row)
        return row

    def _retry_denial(self, con, row):
        definition = self.registry.get(row["handler"])
        if definition.internal_only:
            raise PlatformError("INTERNAL_HANDLER_REQUIRED", 403)
        if row["status"] not in {"FAILED", "CANCELLED"}:
            return "JOB_STATUS_NOT_RETRYABLE"
        original = json.loads(row["spec_json"])
        if original["code_hash"] != definition.code_hash or original["dependency_hash"] != self.dependency_hash:
            return "JOB_RUNTIME_CHANGED"
        active = con.execute("""
            SELECT 1 FROM runs r JOIN jobs j USING(run_id) WHERE r.parent_run_id=?
            AND (j.status IN ('QUEUED','RUNNING') OR (? AND j.status='SUCCEEDED'))
        """, (row["run_id"], row["handler"] == "research.debate")).fetchone()
        return "JOB_REPLACEMENT_EXISTS" if active is not None else None

    @staticmethod
    def _checkpoint_denial(principal, row, checkpoint_status):
        if row["handler"] != "research.debate" or checkpoint_status is None:
            return "VERIFIED_CHECKPOINT_REQUIRED"
        job = {key: row[key] for key in ("job_id", "run_id", "owner_id", "status", "handler")}
        reply = checkpoint_status(principal, job)
        if type(reply) is dict and reply.get("available") is True:
            return None
        reason = reply.get("reason_code") if type(reply) is dict else None
        return reason if type(reason) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason) else "VERIFIED_CHECKPOINT_REQUIRED"

    def operation_capabilities(self, principal, job_id, *, checkpoint_status=None, authorize_job=None):
        def capability(reason):
            return {"available": reason is None, "reason_code": reason}

        delegated = isinstance(principal, AccountDelegation)
        with self.database.transaction() if delegated else closing(self.database.connect()) as con:
            if delegated:
                principal.check_approval(con)
            row = self._control_row(con, principal, job_id, authorize_job=authorize_job)
            if principal.role not in {"admin", "researcher"}:
                cancel = retry = resume = "FORBIDDEN"
            else:
                cancel = None
                if row["status"] not in {"QUEUED", "RUNNING"}:
                    cancel = "JOB_ALREADY_TERMINAL"
                elif row["cancel_requested"]:
                    cancel = "JOB_CANCELLATION_REQUESTED"
                try:
                    retry = self._retry_denial(con, row)
                except PlatformError as error:
                    retry = error.code
                resume = retry or self._checkpoint_denial(principal, row, checkpoint_status)
                if retry is None and row["handler"] == "research.debate":
                    retry = "VERIFIED_CHECKPOINT_REQUIRED"
        return {"job_id": job_id, "status": row["status"], "cancel": capability(cancel),
                "retry": capability(retry), "resume": capability(resume)}

    @staticmethod
    def _retry_key(connection, principal, idempotency_key, request_hash, original):
        if idempotency_key is None:
            return None
        row = connection.execute("""
            SELECT j.*, r.parent_run_id, k.request_hash FROM job_keys k
            JOIN jobs j USING(job_id) JOIN runs r USING(run_id)
            WHERE k.owner_id=? AND k.idempotency_key=?
        """, (principal.user_id, idempotency_key)).fetchone()
        if row is None:
            return None
        if (row["request_hash"] != request_hash or row["parent_run_id"] != original["run_id"]
                or row["owner_id"] != original["owner_id"] or row["handler"] != original["handler"]):
            raise PlatformError("CONFLICT", 409)
        return row

    def retry_cached(self, principal, job_id, idempotency_key, *, intent="retry", authorize_job=None):
        request_hash = retry_command_hash(job_id, idempotency_key, intent)
        delegated = isinstance(principal, AccountDelegation)
        with self.database.transaction() if delegated else closing(self.database.connect()) as con:
            if delegated:
                principal.check_approval(con)
            original = self._control_row(con, principal, job_id, write=True, authorize_job=authorize_job)
            cached = self._retry_key(con, principal, idempotency_key, request_hash, original)
            return None if cached is None else self._record(con, cached, principal)

    def retry(self, principal, job_id, *, idempotency_key=None, intent="retry",
              authorize_job=None, checkpoint_status=None):
        request_hash = retry_command_hash(job_id, idempotency_key, intent)
        delegated = principal if isinstance(principal, AccountDelegation) else None
        with self.database.transaction() as con:
            if delegated is not None:
                delegated.check_approval(con)
            row = self._control_row(con, principal, job_id, write=True, authorize_job=authorize_job)
            cached = self._retry_key(con, principal, idempotency_key, request_hash, row)
            if cached is not None:
                return self._record(con, cached, principal)
            require_open_key(con, principal.user_id, "jobs", idempotency_key)
            if self._retry_denial(con, row):
                raise PlatformError("CONFLICT", 409)
            if (intent == "resume" or checkpoint_status is not None and row["handler"] == "research.debate"):
                if self._checkpoint_denial(principal, row, checkpoint_status):
                    raise PlatformError("VERIFIED_CHECKPOINT_REQUIRED", 409)
            definition = self.registry.get(row["handler"])
            original = json.loads(row["spec_json"])
            versions = {k: original[k] for k in ("strategy", "config", "data", "model", "model_reason", "code_hash", "dependency_hash", "seed")}
            self._validate(principal, row["handler"], json.loads(row["parameters_json"]), row["mode"], None)
            storage_binding = self.storage.lookup_binding(row["owner_id"], "job:" + row["job_id"], connection=con) if self.storage is not None else None
            result = self._insert(con, row["owner_id"], definition, row["parameters_json"], RunMode(row["mode"]),
                                  versions, parent_run_id=row["run_id"], storage_binding=storage_binding,
                                  delegated=delegated)
            if idempotency_key is not None:
                con.execute("INSERT INTO job_keys VALUES(?,?,?,?)",
                            (principal.user_id, idempotency_key, request_hash, result["job_id"]))
            return result

    def claim(self, worker_id, *, lease_seconds=30):
        identifier(worker_id)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise PlatformError("INVALID_INPUT")
        with self.database.transaction() as con:
            expired = con.execute("SELECT * FROM jobs WHERE status='RUNNING' AND lease_until<=?", (self.clock(),)).fetchall()
            for row in expired:
                code = "JOB_CANCELLED" if row["cancel_requested"] else "LEASE_EXPIRED"
                status = "CANCELLED" if row["cancel_requested"] else "FAILED"
                con.execute("UPDATE jobs SET status=?,error_code=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?", (status, code, now_text(), row["job_id"]))
                self._event(con, row["job_id"], row["run_id"], status, {"code": code})
                definition = self.registry._handlers.get(row["handler"])
                if definition and definition.recoverable and not row["cancel_requested"]:
                    spec = json.loads(con.execute("SELECT spec_json FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()[0])
                    versions = {k: spec[k] for k in ("strategy", "config", "data", "model", "model_reason", "code_hash", "dependency_hash", "seed")}
                    ancestors = con.execute("WITH RECURSIVE lineage(run_id,parent_run_id) AS (SELECT run_id,parent_run_id FROM runs WHERE run_id=? UNION ALL SELECT r.run_id,r.parent_run_id FROM runs r JOIN lineage l ON r.run_id=l.parent_run_id) SELECT count(*)-1 FROM lineage", (row["run_id"],)).fetchone()[0]
                    if ancestors >= definition.auto_recovery_limit:
                        self._event(con, row["job_id"], row["run_id"], "RECOVERY_BLOCKED", {"code": "RECOVERY_LIMIT"})
                        continue
                    # A failed resume must never roll back the terminal record of
                    # the expired original or block unrelated queued work.
                    con.execute("SAVEPOINT queue_recovery")
                    try:
                        owner = con.execute("SELECT * FROM users WHERE user_id=?", (row["owner_id"],)).fetchone()
                        if not owner or not owner["enabled"]:
                            raise PlatformError("FORBIDDEN", 403)
                        self._validate(Principal(owner["user_id"], owner["username"], owner["role"]), row["handler"], json.loads(row["parameters_json"]), row["mode"], None)
                        storage_binding = self.storage.lookup_binding(row["owner_id"], 'job:' + row["job_id"], connection=con) if self.storage is not None else None
                        self._insert(con, row["owner_id"], definition, row["parameters_json"], RunMode(row["mode"]), versions, parent_run_id=row["run_id"], storage_binding=storage_binding)
                        con.execute("RELEASE queue_recovery")
                    except PlatformError as error:
                        con.execute("ROLLBACK TO queue_recovery")
                        con.execute("RELEASE queue_recovery")
                        self._event(con, row["job_id"], row["run_id"], "RECOVERY_BLOCKED", {"code": error.code})
            while True:
                row = con.execute("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY created_at,job_id LIMIT 1").fetchone()
                if row is None:
                    return None
                if self.registry.is_disabled(row["handler"]):
                    code = "HOSTED_OPERATION_DISABLED"
                elif self.registry.is_internal(row["handler"]) and self._origin(con, row) is None:
                    code = "INTERNAL_JOB_ORIGIN_REQUIRED"
                else:
                    break
                con.execute("UPDATE jobs SET status='FAILED',error_code=?,updated_at=? WHERE job_id=?",
                            (code, now_text(), row["job_id"]))
                self._event(con, row["job_id"], row["run_id"], "FAILED", {"code":code})
            fencing = row["fencing_token"] + 1
            con.execute("UPDATE jobs SET status='RUNNING',lease_owner=?,lease_until=?,fencing_token=?,updated_at=? WHERE job_id=? AND status='QUEUED'", (worker_id, self.clock() + lease_seconds, fencing, now_text(), row["job_id"]))
            self._event(con, row["job_id"], row["run_id"], "RUNNING", {"worker_id": worker_id, "fencing_token": fencing})
            spec = parse_run(con.execute("SELECT spec_json FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()[0])
            return {"job_id": row["job_id"], "run": spec, "handler": row["handler"], "parameters": json.loads(row["parameters_json"]), "fencing_token": fencing}

    def _active(self, con, job_id, worker_id, fencing_token):
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row or row["status"] != "RUNNING" or row["lease_owner"] != worker_id or row["fencing_token"] != fencing_token or row["lease_until"] <= self.clock():
            raise PlatformError("LEASE_LOST", 409)
        return row

    def require_active(self, job_id, worker_id, fencing_token):
        with closing(self.database.connect()) as con:
            row = self._active(con, job_id, worker_id, fencing_token)
            if row["cancel_requested"]:
                raise PlatformError("JOB_CANCELLED", 409)

    def heartbeat(self, job_id, worker_id, fencing_token, *, lease_seconds=30):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise PlatformError("INVALID_INPUT")
        with self.database.transaction() as con:
            row = self._active(con, job_id, worker_id, fencing_token)
            if row["cancel_requested"]:
                raise PlatformError("JOB_CANCELLED", 409)
            con.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=?", (self.clock() + lease_seconds, now_text(), job_id))

    def checkpoint(self, job_id, worker_id, fencing_token, progress, metadata=None):
        if type(progress) is not int or not 0 <= progress <= 99:
            raise PlatformError("INVALID_INPUT")
        with self.database.transaction() as con:
            row = self._active(con, job_id, worker_id, fencing_token)
            if row["cancel_requested"]:
                raise PlatformError("JOB_CANCELLED", 409)
            if progress < row["progress"]:
                raise PlatformError("CONFLICT", 409)
            con.execute("UPDATE jobs SET progress=?,updated_at=? WHERE job_id=?", (progress, now_text(), job_id))
            self._event(con, job_id, row["run_id"], "PROGRESS", {"progress": progress, "details": metadata or {}})

    def finish(self, job_id, worker_id, fencing_token, result):
        if not isinstance(result, Mapping):
            raise PlatformError("INVALID_INPUT")
        encoded = safe_json(dict(result))
        with self.database.transaction() as con:
            row = self._active(con, job_id, worker_id, fencing_token)
            if row["cancel_requested"]:
                raise PlatformError("JOB_CANCELLED", 409)
            con.execute("UPDATE jobs SET status='SUCCEEDED',progress=100,result_json=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?", (encoded, now_text(), job_id))
            self._event(con, job_id, row["run_id"], "SUCCEEDED", {"result_hash": hashlib.sha256(encoded.encode()).hexdigest()})

    def fail(self, job_id, worker_id, fencing_token, code="HANDLER_FAILED"):
        # Never persist raw exception text. Module errors may use fixed codes.
        if not isinstance(code, str) or not code.isupper() or len(code) > 64:
            code = "HANDLER_FAILED"
        with self.database.transaction() as con:
            row = self._active(con, job_id, worker_id, fencing_token)
            state = "CANCELLED" if row["cancel_requested"] or code == "JOB_CANCELLED" else "FAILED"
            con.execute("UPDATE jobs SET status=?,error_code=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?", (state, code, now_text(), job_id))
            self._event(con, job_id, row["run_id"], state, {"code": code})


@dataclass(frozen=True)
class WorkerJobContext:
    """Trusted platform-handler control; StrategyContext is a separate value view."""
    service: JobService
    job_id: str
    worker_id: str
    fencing_token: int
    run: RunSpec

    @property
    def storage_binding(self):
        storage = self.service.storage
        lookup = getattr(storage, 'lookup_binding', None)
        if not callable(lookup):
            raise PlatformError('STORAGE_TASK_BINDING_REQUIRED', 409)
        value = lookup(self.run.owner_id, 'job:' + self.job_id)
        if (type(value) is not dict or value.get('schema') != 'storage-binding-1'
                or value.get('owner_id') != self.run.owner_id
                or value.get('binding_key') != 'job:' + self.job_id
                or not {'market_data', 'reports', 'artifacts'} <= set(value.get('locations', {}))):
            raise PlatformError('STORAGE_TASK_BINDING_REQUIRED', 409)
        return value

    def require_active(self):
        self.service.require_active(self.job_id, self.worker_id, self.fencing_token)

    def checkpoint(self, progress, metadata=None):
        self.service.checkpoint(self.job_id, self.worker_id, self.fencing_token, progress, metadata)

    def assert_fenced(self, connection):
        """Check in the same business transaction that performs platform writes."""
        row = self.service._active(connection, self.job_id, self.worker_id, self.fencing_token)
        if row["cancel_requested"]:
            raise PlatformError("JOB_CANCELLED", 409)

    def internal_origin(self, connection=None):
        if connection is None:
            with closing(self.service.database.connect()) as reader:
                return self.internal_origin(reader)
        self.assert_fenced(connection)
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (self.job_id,)).fetchone()
        if (row["run_id"] != self.run.run_id or row["owner_id"] != self.run.owner_id
                or row["handler"] != self.run.handler or self.run.parent_run_id is not None):
            raise PlatformError("INTERNAL_JOB_ORIGIN_REQUIRED", 409)
        origin = self.service._origin(connection, row)
        if origin is None:
            raise PlatformError("INTERNAL_JOB_ORIGIN_REQUIRED", 409)
        return origin
