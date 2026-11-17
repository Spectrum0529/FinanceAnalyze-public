"""Ordinary SDK LOCAL_SIMULATION using the shared event/risk/OMS backend."""
from contextlib import closing
import json

from finance_platform.core.contracts import (RunMode, VersionRef, OutputKind, StrategyOutput, parse_instant, parse_decimal, payload, fingerprint)
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import now_text
from finance_platform.research.backends import EventBackend
from finance_platform.research.data import sample_from_version, Tick
from finance_platform.research.execution import TradeBoundary
from finance_platform.research.actions import action_records
from finance_platform.research.replay import preflight_local,journal_key,timeline
from finance_platform.trading.service import TradingService
from .contracts import StrategyError, canonical, digest
from .service import StrategyService, TRANSITIONS
from .worker import TrustedWorker
from .restore import require_execution_enabled
from .risk_binding import validate_risk_binding

MIGRATIONS = ("""
CREATE TABLE strategy_simulation_sessions (
 instance_id TEXT PRIMARY KEY REFERENCES strategy_instances(instance_id), account_id TEXT NOT NULL,
 sample_reference_json TEXT NOT NULL,strategy_reference_json TEXT NOT NULL,model_id TEXT,
 generation INTEGER NOT NULL,job_id TEXT,status TEXT NOT NULL,checkpoint_json TEXT NOT NULL,
 cursor_at TEXT,updated_at TEXT NOT NULL
);
CREATE TABLE strategy_simulation_journal (
 instance_id TEXT NOT NULL REFERENCES strategy_instances(instance_id),event_key TEXT NOT NULL,
 outputs_json TEXT NOT NULL,state_json TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(instance_id,event_key)
);
""",)


def outputs_from_payload(values):
    result = []
    for raw in values:
        item = dict(raw)
        item["kind"], item["mode"] = OutputKind(item["kind"]), RunMode(item["mode"])
        item["value"] = parse_decimal(item["value"])
        item["limit_price"] = None if item["limit_price"] is None else parse_decimal(item["limit_price"])
        for field in ("produced_at", "effective_at", "expires_at"):
            item[field] = parse_instant(item[field])
        result.append(StrategyOutput(**item))
    return tuple(result)


class LocalSimulation:
    def __init__(self, runtime):
        self.runtime, self.database = runtime, runtime.database
        self.strategies = StrategyService(runtime)
        self.trading = TradingService(self.database, authorize=runtime.require_owner)
        from finance_platform.research.inputs import InputAuthorizer
        self.inputs=InputAuthorizer(runtime)
        self.database.apply_module_migrations("strategy_simulation", MIGRATIONS)
        with self.database.transaction() as con:
            immutable(con, "strategy_simulation_journal", ("instance_id", "event_key"))

    def session(self, principal, instance_id):
        self.strategies.instance(principal, instance_id)
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM strategy_simulation_sessions WHERE instance_id=?", (instance_id,)).fetchone()
        if row is None:
            raise StrategyError("SIMULATION_SESSION_NOT_FOUND")
        result = dict(row)
        result["checkpoint"] = json.loads(result.pop("checkpoint_json"))
        return result

    def start(self, principal, instance_id, account_id, sample_reference, *, model_id=None, idempotency_key=None):
        require_execution_enabled(self.database)
        instance = self.strategies.instance(principal, instance_id, write=True)
        with closing(self.database.connect()) as con:
            old = con.execute("SELECT * FROM strategy_simulation_sessions WHERE instance_id=?", (instance_id,)).fetchone()
            previous = con.execute("SELECT status FROM jobs WHERE job_id=?",(old["job_id"],)).fetchone() if old and old["job_id"] else None
        # An explicit resume may finalize the stale projection only after the
        # actual old core job has failed/cancelled; it never steals a live lease.
        if instance["status"]=="RUNNING" and previous and previous["status"] in {"FAILED","CANCELLED"}:
            self.strategies.transition(principal,instance_id,"FAILED",run_id=instance["active_run_id"])
            instance=self.strategies.instance(principal,instance_id)
            with closing(self.database.connect()) as con:
                old=con.execute("SELECT * FROM strategy_simulation_sessions WHERE instance_id=?",(instance_id,)).fetchone()
        if instance["status"] == "CREATED":
            self.strategies.transition(principal, instance_id, "VALIDATED")
            instance=self.strategies.instance(principal,instance_id)
        if instance["status"] in {"RUNNING", "ARCHIVED"}:
            raise StrategyError("SIMULATION_LIFECYCLE_CONFLICT")
        config = self.strategies.config(principal, instance["config_id"])
        registered = self.strategies.version(principal, config["strategy_id"], config["version"], owner_id=config["owner_id"])
        if old:
            reference=VersionRef(**json.loads(old["strategy_reference_json"]))
        else:
            risk = self.trading.risk_configuration(principal, account_id)
            reference = self.runtime.versions.freeze(principal, "sim-config:"+fingerprint({"config": config["config_id"], "risk": risk})[:24]+":"+principal.user_id,
                        "1", "strategy", {"package_hash": config["package_hash"], "manifest": registered["manifest"], "config": config, "risk": risk})
        if old and (json.loads(old["sample_reference_json"]) != sample_reference or old["account_id"] != account_id or old["model_id"] != model_id):
            raise StrategyError("SIMULATION_RESUME_INPUT_CHANGED_USE_NEW_INSTANCE")
        parameters = {"instance_id": instance_id, "account_id": account_id, "sample_reference": sample_reference,
                      "strategy_reference": payload(reference), "model_id": model_id, "requested_generation": 1 if old is None else old["generation"]+1,"expected_instance_updated_at":instance["updated_at"]}
        return self.runtime.jobs.submit(principal, "strategy.simulation", parameters, mode=RunMode.LOCAL_SIMULATION, idempotency_key=idempotency_key)

    def resolve(self, principal, parameters):
        require_execution_enabled(self.database)
        instance = self.strategies.instance(principal, parameters["instance_id"], write=True)
        if instance["updated_at"]!=parameters["expected_instance_updated_at"]:
            raise StrategyError("SIMULATION_LIFECYCLE_STALE")
        config = self.strategies.config(principal, instance["config_id"])
        registered = self.strategies.version(principal, config["strategy_id"], config["version"], owner_id=config["owner_id"])
        declaration = self.runtime.versions.get(principal, VersionRef(**parameters["strategy_reference"]))
        if not registered["trusted"] or config["mode"] != "LOCAL_SIMULATION" or any(declaration.get(k) != v for k,v in {"package_hash": config["package_hash"], "manifest": registered["manifest"], "config": config}.items()):
            raise StrategyError("SIMULATION_TRUSTED_CONFIG_REQUIRED")
        self.strategies.packages.verify(config["package_hash"])
        input_value,sample,_=self.inputs.validate(principal,parameters["sample_reference"])
        if not {b.frequency for b in sample.bars}<=set(registered["manifest"]["data"]["frequencies"]) or not {i.asset_class.value for i in sample.instruments}<=set(registered["manifest"]["assets"]):
            raise StrategyError("SIMULATION_DATA_CAPABILITY_MISMATCH")
        account = self.trading.snapshot(principal, parameters["account_id"])
        # The shared service rejects replay before the explicit capital start.
        self.trading.snapshot(principal, parameters["account_id"], min(t.event_at for t in sample.ticks))
        validate_risk_binding(self.trading,principal,parameters["account_id"],declaration.get("risk"))
        with closing(self.database.connect()) as con:
            owner = con.execute("SELECT owner_id FROM trading_accounts WHERE account_id=?", (parameters["account_id"],)).fetchone()[0]
            old = con.execute("SELECT * FROM strategy_simulation_sessions WHERE instance_id=?", (parameters["instance_id"],)).fetchone()
        if account["mode"] != "LOCAL_SIMULATION" or owner != instance["owner_id"] or parameters["requested_generation"] != (1 if old is None else old["generation"]+1):
            raise StrategyError("SIMULATION_MODE_OWNER_OR_GENERATION_INVALID")
        if old and (old["account_id"]!=parameters["account_id"] or json.loads(old["sample_reference_json"])!=parameters["sample_reference"]
                    or json.loads(old["strategy_reference_json"])!=parameters["strategy_reference"] or old["model_id"]!=parameters["model_id"]):
            raise StrategyError("SIMULATION_RESUME_INPUT_CHANGED_USE_NEW_INSTANCE")
        self._preflight(principal,parameters,input_value,sample,config,old)
        model = None
        if parameters["model_id"]:
            from finance_platform.research.service import ResearchService
            # Constructing this service inside a core resolver would write migrations.
            with closing(self.database.connect()) as con:
                row = con.execute("SELECT * FROM research_models WHERE model_id=?", (parameters["model_id"],)).fetchone()
                job = con.execute("SELECT status FROM jobs WHERE run_id=?",(row["run_id"],)).fetchone() if row else None
            if row is None:
                raise StrategyError("MODEL_NOT_FOUND")
            self.runtime.require_owner(principal, row["owner_id"])
            if job is None or job["status"]!="SUCCEEDED":
                raise StrategyError("MODEL_SOURCE_RUN_NOT_SUCCEEDED")
            model = json.loads(row["reference_json"])
            if registered["manifest"]["model"] is None or registered["manifest"]["model"]["sha256"] != row["model_hash"]:
                raise StrategyError("SIMULATION_MODEL_REFERENCE_MISMATCH")
        elif registered["manifest"]["model"] is not None:
            raise StrategyError("SIMULATION_MODEL_REQUIRED")
        return {"strategy": parameters["strategy_reference"], "data": parameters["sample_reference"], "model": model,
                "model_reason": "No model used" if model is None else None, "seed": 0}

    def _preflight(self,principal,parameters,input_value,sample,config,old):
        actions=action_records(input_value)
        args={"instance_id":parameters["instance_id"],"package_hash":config["package_hash"],"parameters":config["parameters"],"decode_outputs":outputs_from_payload}
        if old:
            preflight_local(self.trading,principal,parameters["account_id"],sample,actions,session=old,**args)
            return old["checkpoint_json"]
        clock=self.trading.execution_clock(principal,parameters["account_id"])
        if min(timeline(sample,actions))>=parse_instant(clock["watermark"]):
            preflight_local(self.trading,principal,parameters["account_id"],sample,actions)
            return "{}"
        # A second instance may reuse only a completed, byte-identical complete
        # binding with every old effect independently proved by shared preflight.
        with closing(self.database.connect()) as con:
            prior=con.execute("SELECT s.* FROM strategy_simulation_sessions s JOIN jobs j ON j.job_id=s.job_id WHERE s.account_id=? AND s.sample_reference_json=? AND s.strategy_reference_json=? AND s.model_id IS ? AND j.status='SUCCEEDED' AND EXISTS(SELECT 1 FROM strategy_simulation_journal x WHERE x.instance_id=s.instance_id) ORDER BY s.updated_at DESC LIMIT 1",
                (parameters["account_id"],canonical(parameters["sample_reference"]).decode(),canonical(parameters["strategy_reference"]).decode(),parameters["model_id"])).fetchone()
        if prior:
            checkpoint=json.loads(prior["checkpoint_json"])
            if checkpoint.get("sample_hash")==sample.sha256 and checkpoint.get("processed_at") and parse_instant(checkpoint["processed_at"])>=max(timeline(sample,actions)):
                preflight_local(self.trading,principal,parameters["account_id"],sample,actions,session={"checkpoint_json":"{}"},
                    **{**args,"instance_id":prior["instance_id"]},require_existing=True)
                checkpoint["committed_binding_origin"]=prior["instance_id"]
                return canonical(checkpoint).decode()
        preflight_local(self.trading,principal,parameters["account_id"],sample,actions)
        return "{}"

    def execute(self, context, parameters):
        from .sessions import ExternalSessions
        principal = ExternalSessions(self.runtime).principal(context.run.owner_id)
        self.resolve(principal, parameters)
        declaration = self.runtime.versions.get(principal, context.run.strategy)
        input_value,sample,input_grant=self.inputs.validate(principal,context.run.data)
        instance_id = parameters["instance_id"]
        old = None
        with self.database.transaction() as con:
            context.service._active(con, context.job_id, context.worker_id, context.fencing_token)
            instance = con.execute("SELECT * FROM strategy_instances WHERE instance_id=?", (instance_id,)).fetchone()
            old = con.execute("SELECT * FROM strategy_simulation_sessions WHERE instance_id=?", (instance_id,)).fetchone()
            if instance["updated_at"]!=parameters["expected_instance_updated_at"] or parameters["requested_generation"]!=(1 if old is None else old["generation"]+1):
                raise StrategyError("SIMULATION_LIFECYCLE_STALE")
            if "RUNNING" not in TRANSITIONS[instance["status"]]:
                raise StrategyError("SIMULATION_LIFECYCLE_CONFLICT")
            checkpoint = self._preflight(principal,parameters,input_value,sample,declaration["config"],old)
            values = (instance_id, parameters["account_id"], canonical(parameters["sample_reference"]).decode(), canonical(parameters["strategy_reference"]).decode(), parameters["model_id"], parameters["requested_generation"], context.job_id, "RUNNING", checkpoint, None if old is None else old["cursor_at"], now_text())
            if old is None:
                con.execute("INSERT INTO strategy_simulation_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
            else:
                con.execute("UPDATE strategy_simulation_sessions SET account_id=?,sample_reference_json=?,strategy_reference_json=?,model_id=?,generation=?,job_id=?,status=?,checkpoint_json=?,cursor_at=?,updated_at=? WHERE instance_id=?", values[1:]+values[:1])
            con.execute("UPDATE strategy_instances SET status='RUNNING',active_run_id=?,updated_at=? WHERE instance_id=?", (context.run.run_id, now_text(), instance_id))
            self.strategies._event(con, instance_id, "SIMULATION_STARTED", {"run_id": context.run.run_id, "generation": parameters["requested_generation"]})

        try:
            return self._execute_active(context,parameters,principal,declaration,sample,checkpoint,input_grant)
        except Exception:
            current = self.strategies.instance(principal,instance_id)
            if current["status"] == "RUNNING" and current["active_run_id"] == context.run.run_id:
                self.strategies.transition(principal,instance_id,"FAILED",run_id=context.run.run_id)
            raise

    def _execute_active(self,context,parameters,principal,declaration,sample,checkpoint,input_grant):
        instance_id=parameters["instance_id"]
        def guard(p, owner, *, write=False):
            self.runtime.require_owner(p, owner, write=write)
            if write:
                require_execution_enabled(self.database)
                context.require_active()
                current = self.session(p, instance_id)
                if current["status"] != "RUNNING" or current["generation"] != parameters["requested_generation"] or current["job_id"] != context.job_id:
                    raise StrategyError("SIMULATION_NO_LONGER_ACTIVE")
        def assert_session_locked(con):
            context.assert_fenced(con)
            self.runtime.require_owner(principal, principal.user_id, write=True)
            require_execution_enabled(self.database)
            current = con.execute("SELECT status,generation,job_id FROM strategy_simulation_sessions WHERE instance_id=?", (instance_id,)).fetchone()
            item = con.execute("SELECT status,active_run_id FROM strategy_instances WHERE instance_id=?", (instance_id,)).fetchone()
            if current is None or item is None or current["status"] != "RUNNING" or current["generation"] != parameters["requested_generation"] or current["job_id"] != context.job_id or item["status"] != "RUNNING" or item["active_run_id"] != context.run.run_id:
                raise StrategyError("SIMULATION_NO_LONGER_ACTIVE")
        trading = TradingService(self.database, authorize=guard, data_authorizer=lambda e: e.provenance.source_id == sample.source_id and e.provenance.license_id == sample.license_id)
        for instrument in sample.instruments:
            trading.add_instrument(principal,parameters["account_id"],instrument)
        boundary = TradeBoundary(trading, principal, parameters["account_id"], sample, strategy_id=declaration["config"]["strategy_id"])
        boundary.action_authorizer=input_grant.authorize_action
        trading.bind_run_risk(principal, parameters["account_id"], context.run.run_id, declaration["risk"]["version"])
        boundary.risk_version = declaration["risk"]["version"]
        resume = json.loads(checkpoint)
        if resume.get("processed_at"):
            with closing(self.database.connect()) as con:
                markets = trading._markets(con, parameters["account_id"], parse_instant(resume["processed_at"]))
            boundary.last_ticks = {i: Tick(e.event_id,i,e.event_at,e.available_at,e.price,e.volume,e.session_open) for i,e in markets.items()}
        actual_worker = TrustedWorker(self.strategies.packages, active_guard=lambda: guard(principal, principal.user_id, write=True))
        service = self
        class JournalWorker:
            def execute(self, package_hash, strategy_context, *, event_type, trusted):
                key = journal_key(sample,package_hash,strategy_context.config,strategy_context.as_of,event_type,strategy_context.state)
                with closing(service.database.connect()) as con:
                    recorded = con.execute("SELECT * FROM strategy_simulation_journal WHERE instance_id=? AND event_key=?", (instance_id,key)).fetchone()
                if recorded:
                    return outputs_from_payload(json.loads(recorded["outputs_json"])),json.loads(recorded["state_json"])
                outputs,state = actual_worker.execute(package_hash,strategy_context,event_type=event_type,trusted=trusted)
                guard(principal,principal.user_id,write=True)
                with service.database.transaction() as con:
                    assert_session_locked(con)
                    con.execute("INSERT INTO strategy_simulation_journal VALUES(?,?,?,?,?)", (instance_id,key,canonical(payload(outputs)).decode(),canonical(state).decode(),now_text()))
                return outputs,state
        def save(state):
            guard(principal,principal.user_id,write=True)
            raw = canonical(state).decode()
            if len(raw.encode()) > 128_000:
                raise StrategyError("STRATEGY_STATE_TOO_LARGE")
            with self.database.transaction() as con:
                assert_session_locked(con)
                con.execute("UPDATE strategy_instances SET checkpoint_json=?,updated_at=? WHERE instance_id=?", (raw,now_text(),instance_id))
                self.strategies._event(con,instance_id,"CHECKPOINT",{"run_id":context.run.run_id,"state_hash":digest(state)})
                updated = con.execute("UPDATE strategy_simulation_sessions SET checkpoint_json=?,cursor_at=?,updated_at=? WHERE instance_id=? AND generation=? AND job_id=? AND status='RUNNING'", (raw,state["processed_at"],now_text(),instance_id,parameters["requested_generation"],context.job_id))
                if updated.rowcount != 1:
                    raise StrategyError("SIMULATION_NO_LONGER_ACTIVE")
        model = None if context.run.model is None else self.runtime.versions.get(principal,context.run.model)
        result = EventBackend().run(sample,boundary,JournalWorker(),declaration["package_hash"],declaration["config"]["parameters"],
                    mode=RunMode.LOCAL_SIMULATION,model=model,resume=resume,job_context=context,checkpoint=save,
                    actions=action_records(self.runtime.versions.get(principal,context.run.data)))
        self.strategies.transition(principal,instance_id,"STOPPED",run_id=context.run.run_id)
        return {"instance_id":instance_id,"account_id":parameters["account_id"],"mode":"LOCAL_SIMULATION","status":"STOPPED_AT_SAMPLE_END",
                "orders":result["orders"],"fills":result["fills"],"account":result["final"],"lineage":payload(context.run),"resumed":bool(resume),
                "checkpoint_origin_instance_id":resume.get("committed_binding_origin",instance_id)}

    def stop(self, principal, instance_id, target="STOPPED", *, cancel_orders=False):
        session = self.session(principal,instance_id)
        result = self.strategies.transition(principal,instance_id,target)
        if session["job_id"]:
            self.runtime.jobs.cancel(principal,session["job_id"])
        cancelled,failures=[],[]
        if cancel_orders:
            config=self.strategies.config(principal,result["config_id"])
            for order in self.trading.snapshot(principal,session["account_id"])["orders"]:
                if order["intent"]["strategy_id"]==config["strategy_id"] and order["status"] not in {"filled","cancelled","expired","rejected","replaced"}:
                    try:
                        self.trading.cancel(principal,session["account_id"],order["client_order_id"])
                        cancelled.append(order["client_order_id"])
                    except Exception as error:
                        failures.append({"client_order_id":order["client_order_id"],"code":getattr(error,"code","CANCEL_FAILED")})
        self.runtime.audit(principal,"strategy.simulation.stopped",instance_id,{"cancel_failures":failures})
        return result|{"cancel_requested":cancelled,"cancel_failures":failures}
