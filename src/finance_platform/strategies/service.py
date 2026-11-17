"""Strategy version/config/lifecycle metadata in the shared core platform database."""

from contextlib import closing
import json
from pathlib import Path
import tempfile
import uuid

from finance_platform.core.auth import AccountDelegation
from finance_platform.core.contracts import RunMode, identifier
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import now_text
from .contracts import StrategyError, canonical, digest
from .packages import PackageStore
from .builtin_templates import APPROVAL_SOURCE, STYLES, approved_package_hash


MIGRATIONS = ("""
CREATE TABLE strategy_versions (
 owner_id TEXT NOT NULL, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
 package_hash TEXT NOT NULL, manifest_json TEXT NOT NULL, trusted INTEGER NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(owner_id,strategy_id,version)
);
CREATE TABLE strategy_configs (
 config_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
 package_hash TEXT NOT NULL, parameters_json TEXT NOT NULL, config_hash TEXT NOT NULL,
 mode TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(owner_id,strategy_id,version) REFERENCES strategy_versions(owner_id,strategy_id,version)
);
CREATE TABLE strategy_instances (
 instance_id TEXT PRIMARY KEY, config_id TEXT NOT NULL REFERENCES strategy_configs(config_id),
 owner_id TEXT NOT NULL, status TEXT NOT NULL, checkpoint_json TEXT NOT NULL, active_run_id TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE strategy_events (
 event_id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES strategy_instances(instance_id),
 kind TEXT NOT NULL, body_json TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE strategy_external_messages (
 instance_id TEXT NOT NULL REFERENCES strategy_instances(instance_id), source_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, message_hash TEXT NOT NULL, body_json TEXT NOT NULL,
 received_at TEXT NOT NULL, PRIMARY KEY(instance_id,source_id,sequence)
);
""", """
CREATE TABLE strategy_preparation_commands(
 owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 response_json TEXT NOT NULL, PRIMARY KEY(owner_id,idempotency_key)
);
CREATE TABLE strategy_builtin_template_approvals(
 owner_id TEXT NOT NULL, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
 package_hash TEXT NOT NULL, style TEXT NOT NULL, approval_source TEXT NOT NULL,
 PRIMARY KEY(owner_id,strategy_id,version),
 FOREIGN KEY(owner_id,strategy_id,version) REFERENCES strategy_versions(owner_id,strategy_id,version)
);
""", """
CREATE TABLE strategy_migrated_approvals(
 owner_id TEXT NOT NULL, strategy_id TEXT NOT NULL, version TEXT NOT NULL,
 package_hash TEXT NOT NULL, approved_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,strategy_id,version),
 FOREIGN KEY(owner_id,strategy_id,version) REFERENCES strategy_versions(owner_id,strategy_id,version)
);
CREATE VIEW strategy_runtime_versions AS
 SELECT v.owner_id,v.strategy_id,v.version,v.package_hash,v.manifest_json,
        (v.trusted OR a.owner_id IS NOT NULL) AS trusted,v.created_at
 FROM strategy_versions v
 LEFT JOIN strategy_migrated_approvals a USING(owner_id,strategy_id,version,package_hash);
""")

TRANSITIONS = {"CREATED": {"VALIDATED", "ARCHIVED"}, "VALIDATED": {"RUNNING", "PAUSED", "STOPPED", "ARCHIVED"},
               "RUNNING": {"PAUSED", "STOPPED", "FAILED"}, "PAUSED": {"RUNNING", "STOPPED", "ARCHIVED"},
               "STOPPED": {"RUNNING", "ARCHIVED"}, "FAILED": {"RUNNING", "ARCHIVED"}, "ARCHIVED": set()}


class StrategyService:
    def __init__(self, runtime):
        self.runtime, self.database = runtime, runtime.database
        self.packages = PackageStore(runtime.root / ".local/platform/strategy-packages")
        self.database.apply_module_migrations("strategies", MIGRATIONS)
        from .restore import install_restore_schema
        install_restore_schema(self.database)
        with self.database.transaction() as con:
            for table, keys in (("strategy_versions", ("owner_id", "strategy_id", "version")),
                                ("strategy_configs", ("config_id",)), ("strategy_events", ("event_id",)),
                                ("strategy_external_messages", ("instance_id", "source_id", "sequence")),
                                ("strategy_preparation_commands", ("owner_id", "idempotency_key")),
                                ("strategy_builtin_template_approvals", ("owner_id", "strategy_id", "version")),
                                ("strategy_migrated_approvals", ("owner_id", "strategy_id", "version"))):
                immutable(con, table, keys)

    @staticmethod
    def _replay(con, principal, key, request):
        if isinstance(principal, AccountDelegation):
            principal.check_approval(con)
        if key is None:
            return None
        try:
            identifier(key, "idempotency key")
        except ValueError:
            raise StrategyError("STRATEGY_IDEMPOTENCY_KEY_INVALID") from None
        row = con.execute("SELECT request_hash,response_json FROM strategy_preparation_commands WHERE owner_id=? AND idempotency_key=?",
                          (principal.user_id, key)).fetchone()
        if row:
            if row["request_hash"] != digest(request):
                raise PlatformError("CONFLICT", 409)
            return json.loads(row["response_json"])
        return None

    @staticmethod
    def _receipt(con, principal, key, request, response):
        if key is not None:
            con.execute("INSERT INTO strategy_preparation_commands VALUES(?,?,?,?)",
                        (principal.user_id, key, digest(request), canonical(response).decode()))
        return response

    def register_builtin(self, principal, strategy_id, style, *, idempotency_key=None):
        """Sole delegated provenance for trusted bytes generated by the product."""
        from .templates import template
        self.runtime.require_owner(principal, principal.user_id, write=True)
        if not isinstance(principal, AccountDelegation) or type(style) is not str or style not in STYLES:
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        operation = {"action": "builtin_template", "strategy_id": strategy_id, "style": style}
        with tempfile.TemporaryDirectory(dir=self.packages.root.parent) as directory, self.database.transaction() as con:
            old = self._replay(con, principal, idempotency_key, operation)
            if old is not None:
                return old
            source = Path(directory) / "package"
            template(source, strategy_id, style)
            approved_hash = approved_package_hash(source, strategy_id, style)
            frozen = self.packages.freeze(source)
            if frozen["sha256"] != approved_hash:
                raise StrategyError("STRATEGY_BUILTIN_TEMPLATE_MISMATCH")
            row = con.execute("""
                SELECT package_hash,trusted FROM strategy_versions
                WHERE owner_id=? AND strategy_id=? AND version=?
            """, (principal.user_id, strategy_id, frozen["version"])).fetchone()
            if row is None:
                con.execute("INSERT INTO strategy_versions VALUES(?,?,?,?,?,?,?)",
                            (principal.user_id, strategy_id, frozen["version"], frozen["sha256"],
                             canonical(frozen["manifest"]).decode(), 1, now_text()))
            elif row["package_hash"] != frozen["sha256"] or not row["trusted"]:
                raise StrategyError("IMMUTABLE_STRATEGY_VERSION_CONFLICT")
            approval = (principal.user_id, strategy_id, frozen["version"], frozen["sha256"], style, APPROVAL_SOURCE)
            if con.execute("INSERT OR IGNORE INTO strategy_builtin_template_approvals VALUES(?,?,?,?,?,?)", approval).rowcount:
                self.database.audit(principal.user_id, "strategy.register_builtin", strategy_id,
                                    {"package_hash": frozen["sha256"], "approval_source": APPROVAL_SOURCE}, connection=con)
            return self._receipt(con, principal, idempotency_key, operation, {
                **frozen, "owner_id": principal.user_id, "trusted": True, "approval_source": APPROVAL_SOURCE})

    def register(self, principal, source, *, trusted=False):
        self.runtime.require_owner(principal, principal.user_id, write=True)
        if isinstance(principal, AccountDelegation):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        if type(trusted) is not bool:
            raise StrategyError("TRUST_BOOLEAN_REQUIRED")
        if trusted and principal.role != "admin":
            raise StrategyError("TRUST_APPROVAL_ADMIN_REQUIRED")
        frozen = self.packages.freeze(source)
        with self.database.transaction() as con:
            old = con.execute("SELECT * FROM strategy_versions WHERE owner_id=? AND strategy_id=? AND version=?",
                              (principal.user_id, frozen["id"], frozen["version"])).fetchone()
            if old:
                if old["package_hash"] != frozen["sha256"] or bool(old["trusted"]) != bool(trusted):
                    raise StrategyError("IMMUTABLE_STRATEGY_VERSION_CONFLICT")
                return dict(old)
            con.execute("INSERT INTO strategy_versions VALUES(?,?,?,?,?,?,?)",
                        (principal.user_id, frozen["id"], frozen["version"], frozen["sha256"],
                         canonical(frozen["manifest"]).decode(), int(trusted), now_text()))
        self.runtime.audit(principal, "strategy.register", frozen["id"], {"package_hash": frozen["sha256"], "trusted": trusted})
        return frozen | {"owner_id": principal.user_id, "trusted": trusted}

    def version(self, principal, strategy_id, version, *, owner_id=None):
        owner_id = owner_id or principal.user_id
        self._delegated_owner(principal, owner_id)
        self.runtime.require_owner(principal, owner_id)
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM strategy_runtime_versions WHERE owner_id=? AND strategy_id=? AND version=?",
                              (owner_id, strategy_id, version)).fetchone()
        if row is None:
            raise StrategyError("STRATEGY_VERSION_NOT_FOUND")
        result = dict(row)
        result["manifest"] = json.loads(result.pop("manifest_json"))
        result["trusted"] = bool(result["trusted"])
        return result

    def list_versions(self, principal, limit=50, offset=0):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= 2147483647:
            raise StrategyError("PAGINATION_INVALID")
        delegated = isinstance(principal, AccountDelegation)
        where, args = ("", ()) if principal.role == "admin" and not delegated else ("WHERE owner_id=?", (principal.user_id,))
        with closing(self.database.connect()) as con:
            count = con.execute("SELECT count(*) FROM strategy_versions " + where, args).fetchone()[0]
            rows = con.execute("SELECT * FROM strategy_runtime_versions " + where + " ORDER BY created_at LIMIT ? OFFSET ?", args+(limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["manifest"] = json.loads(item.pop("manifest_json"))
            item["trusted"] = bool(item["trusted"])
            if delegated:
                manifest = item.pop("manifest")
                item["style"] = manifest["style"]
                item["outputs"] = manifest["outputs"]
            items.append(item)
        return {"items": items, "total": count, "limit": limit, "offset": offset}

    def configure(self, principal, strategy_id, version, parameters, *, mode=RunMode.BACKTEST, idempotency_key=None):
        self.runtime.require_owner(principal, principal.user_id, write=True)
        if isinstance(principal, AccountDelegation) and mode not in (RunMode.BACKTEST, RunMode.LOCAL_SIMULATION):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        registered = self.version(principal, strategy_id, version)
        manifest = self.packages.verify(registered["package_hash"])
        if type(mode) is not RunMode or mode.value not in manifest.raw["modes"]:
            raise StrategyError("STRATEGY_MODE_UNSUPPORTED")
        params = manifest.parameters(parameters)
        config_id = "cfg-"+uuid.uuid4().hex
        config_hash = digest({"package_hash": registered["package_hash"], "parameters": params, "mode": mode.value})
        operation = {"action": "configure", "strategy_id": strategy_id, "version": version, "config_hash": config_hash}
        with self.database.transaction() as con:
            replay = self._replay(con, principal, idempotency_key, operation)
            if replay is not None:
                return replay
            con.execute("INSERT INTO strategy_configs VALUES(?,?,?,?,?,?,?,?,?)",
                        (config_id, principal.user_id, strategy_id, version, registered["package_hash"], canonical(params).decode(), config_hash, mode.value, now_text()))
            response = self._config_record(principal, con.execute("SELECT * FROM strategy_configs WHERE config_id=?", (config_id,)).fetchone())
            self._receipt(con, principal, idempotency_key, operation, response)
        self.runtime.audit(principal, "strategy.configure", config_id, {"config_hash": config_hash})
        return response

    def config(self, principal, config_id, *, write=False):
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM strategy_configs WHERE config_id=?", (config_id,)).fetchone()
        if row is None:
            raise StrategyError("STRATEGY_CONFIG_NOT_FOUND")
        self._delegated_owner(principal, row["owner_id"])
        self.runtime.require_owner(principal, row["owner_id"], write=write)
        return self._config_record(principal, row)

    @staticmethod
    def _config_record(principal, row):
        if isinstance(principal, AccountDelegation):
            fields = ("config_id", "owner_id", "strategy_id", "version", "package_hash", "config_hash", "mode", "created_at")
            return {key: row[key] for key in fields}
        result = dict(row)
        result["parameters"] = json.loads(result.pop("parameters_json"))
        return result

    def create_instance(self, principal, config_id, *, idempotency_key=None):
        self.config(principal, config_id, write=True)
        instance_id = "str-"+uuid.uuid4().hex
        operation = {"action": "create_instance", "config_id": config_id}
        with self.database.transaction() as con:
            replay = self._replay(con, principal, idempotency_key, operation)
            if replay is not None:
                return replay
            con.execute("INSERT INTO strategy_instances VALUES(?,?,?,?,?,?,?,?)",
                        (instance_id, config_id, principal.user_id, "CREATED", "{}", None, now_text(), now_text()))
            self._event(con, instance_id, "CREATED", {})
            response = self._instance_record(principal, con.execute("SELECT * FROM strategy_instances WHERE instance_id=?", (instance_id,)).fetchone())
            return self._receipt(con, principal, idempotency_key, operation, response)

    def instance(self, principal, instance_id, *, write=False):
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM strategy_instances WHERE instance_id=?", (instance_id,)).fetchone()
        if row is None:
            raise StrategyError("STRATEGY_INSTANCE_NOT_FOUND")
        self._delegated_owner(principal, row["owner_id"])
        self.runtime.require_owner(principal, row["owner_id"], write=write)
        return self._instance_record(principal, row)

    @staticmethod
    def _instance_record(principal, row):
        if isinstance(principal, AccountDelegation):
            fields = ("instance_id", "config_id", "owner_id", "status", "active_run_id", "created_at", "updated_at")
            return {key: row[key] for key in fields}
        result = dict(row)
        result["checkpoint"] = json.loads(result.pop("checkpoint_json"))
        return result

    @staticmethod
    def _delegated_owner(principal, owner_id):
        if isinstance(principal, AccountDelegation) and principal.user_id != owner_id:
            raise PlatformError("NOT_FOUND", 404)

    def list_instances(self, principal, limit=50, offset=0):
        return self._list_resources(principal, "strategy_instances", limit, offset)

    def list_configs(self, principal, limit=50, offset=0):
        return self._list_resources(principal, "strategy_configs", limit, offset)

    def _list_resources(self, principal, table, limit, offset):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= 2147483647:
            raise StrategyError("PAGINATION_INVALID")
        where, args = ("", ()) if principal.role == "admin" and not isinstance(principal, AccountDelegation) else ("WHERE owner_id=?", (principal.user_id,))
        with closing(self.database.connect()) as con:
            total = con.execute("SELECT count(*) FROM "+table+" "+where, args).fetchone()[0]
            rows = con.execute("SELECT * FROM "+table+" "+where+" ORDER BY created_at DESC LIMIT ? OFFSET ?", args+(limit, offset)).fetchall()
        project = self._config_record if table == "strategy_configs" else self._instance_record
        items = [project(principal, row) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def transition(self, principal, instance_id, target, *, run_id=None, idempotency_key=None):
        item = self.instance(principal, instance_id, write=True)
        config = self.config(principal, item["config_id"])
        delegated = isinstance(principal, AccountDelegation)
        if delegated and (target != "VALIDATED" or run_id is not None):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        if delegated and (item["owner_id"] != principal.user_id or config["owner_id"] != principal.user_id):
            raise PlatformError("NOT_FOUND", 404)
        registered = self.version(principal, config["strategy_id"], config["version"], owner_id=config["owner_id"])
        self.packages.verify(config["package_hash"])
        if target == "RUNNING" and (not registered["trusted"] or not run_id):
            raise StrategyError("TRUSTED_JOB_RUN_REQUIRED")
        operation = {"action": "transition", "instance_id": instance_id, "target": target, "run_id": run_id}
        with self.database.transaction() as con:
            replay = self._replay(con, principal, idempotency_key, operation)
            if replay is not None:
                return replay
            current = con.execute("SELECT owner_id,config_id,status,active_run_id FROM strategy_instances WHERE instance_id=?", (instance_id,)).fetchone()
            if delegated and (current is None or current["owner_id"] != principal.user_id or current["config_id"] != config["config_id"]):
                raise PlatformError("NOT_FOUND", 404)
            if target != "RUNNING" and run_id is not None and current["active_run_id"] != run_id:
                raise StrategyError("STRATEGY_RUN_NO_LONGER_ACTIVE")
            if target not in TRANSITIONS[current["status"]]:
                raise StrategyError("STRATEGY_LIFECYCLE_CONFLICT")
            con.execute("UPDATE strategy_instances SET status=?,active_run_id=?,updated_at=? WHERE instance_id=?",
                        (target, run_id if target == "RUNNING" else None, now_text(), instance_id))
            if target != "RUNNING" and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_signal_bindings'").fetchone():
                con.execute("UPDATE strategy_signal_bindings SET status='INACTIVE',generation=generation+1,updated_at=? WHERE instance_id=? AND status='ACTIVE'", (now_text(), instance_id))
            if target != "RUNNING" and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_simulation_sessions'").fetchone():
                con.execute("UPDATE strategy_simulation_sessions SET status=?,generation=generation+1,updated_at=? WHERE instance_id=? AND status='RUNNING'", (target, now_text(), instance_id))
            self._event(con, instance_id, target, {"run_id": run_id})
            response = self._instance_record(principal, con.execute("SELECT * FROM strategy_instances WHERE instance_id=?", (instance_id,)).fetchone())
            self._receipt(con, principal, idempotency_key, operation, response)
        self.runtime.audit(principal, "strategy."+target.lower(), instance_id)
        return response

    def checkpoint(self, principal, instance_id, run_id, state, *, job_context):
        self.instance(principal, instance_id, write=True)
        job_context.require_active()
        raw = canonical(state).decode()
        if len(raw.encode()) > 128_000:
            raise StrategyError("STRATEGY_STATE_TOO_LARGE")
        with self.database.transaction() as con:
            job_context.assert_fenced(con)
            self.runtime.require_owner(principal, principal.user_id, write=True)
            row = con.execute("SELECT status,active_run_id FROM strategy_instances WHERE instance_id=?", (instance_id,)).fetchone()
            if row["status"] != "RUNNING" or row["active_run_id"] != run_id:
                raise StrategyError("STRATEGY_RUN_NO_LONGER_ACTIVE")
            con.execute("UPDATE strategy_instances SET checkpoint_json=?,updated_at=? WHERE instance_id=?", (raw, now_text(), instance_id))
            self._event(con, instance_id, "CHECKPOINT", {"run_id": run_id, "state_hash": digest(state)})

    @staticmethod
    def _event(con, instance_id, kind, body):
        con.execute("INSERT INTO strategy_events(instance_id,kind,body_json,recorded_at) VALUES(?,?,?,?)", (instance_id, kind, canonical(body).decode(), now_text()))
