"""Persistent authenticated HTTP signal sessions and worker consumption.

Only LOCAL_SIMULATION. All order/cancel/fill effects use shared trading services.
"""
from contextlib import closing
from datetime import datetime, timezone
import json

from finance_platform.core.auth import Principal
from finance_platform.core.contracts import (VersionRef, RunMode, DataScope, OutputKind, StrategyOutput,
                                            parse_decimal, parse_instant, payload, fingerprint)
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import now_text
from finance_platform.research.data import DevelopmentSample, Tick, sample_from_version
from finance_platform.research.execution import TradeBoundary
from finance_platform.research.actions import action_records,dispatch_at,apply as apply_action
from finance_platform.research.replay import timeline,market_event,preflight_outputs,preflight_action
from finance_platform.trading.service import TradingService
from finance_platform.trading.accounting import TradingError
from finance_platform.trading.simulation import SimulationBroker
from .contracts import StrategyError, canonical, digest
from .external import ExternalSignalGate
from .service import StrategyService, TRANSITIONS
from .restore import require_execution_enabled
from .risk_binding import validate_risk_binding


MIGRATIONS = ("""
CREATE TABLE strategy_signal_bindings (
 instance_id TEXT PRIMARY KEY REFERENCES strategy_instances(instance_id), account_id TEXT NOT NULL,
 sample_reference_json TEXT NOT NULL, strategy_reference_json TEXT NOT NULL, clock_mode TEXT NOT NULL,
 generation INTEGER NOT NULL, activation_run_id TEXT NOT NULL, status TEXT NOT NULL,
 cursor_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE strategy_signal_acceptances (
 message_hash TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES strategy_instances(instance_id),
 generation INTEGER NOT NULL, account_id TEXT NOT NULL, sample_reference_json TEXT NOT NULL,
 strategy_reference_json TEXT NOT NULL, clock_mode TEXT NOT NULL, accepted_at TEXT NOT NULL
);
CREATE TABLE strategy_signal_work (
 message_hash TEXT PRIMARY KEY REFERENCES strategy_signal_acceptances(message_hash),
 instance_id TEXT NOT NULL REFERENCES strategy_instances(instance_id), job_id TEXT,
 status TEXT NOT NULL, error_code TEXT, result_json TEXT, updated_at TEXT NOT NULL
);
""",)


def retained_cursor(runtime,principal,old,parameters):
    if old and old["account_id"]==parameters["account_id"] and old["clock_mode"]==parameters["clock_mode"] and json.loads(old["sample_reference_json"])==parameters["sample_reference"]:
        prior=runtime.versions.get(principal,VersionRef(**json.loads(old["strategy_reference_json"])))
        proposed=runtime.versions.get(principal,VersionRef(**parameters["strategy_reference"]))
        # A new generation may freeze new risk limits without forgetting the
        # exact unchanged package/config/input processing prefix. Each message
        # still has its own original immutable risk/effect identities.
        if all(fingerprint(prior[k])==fingerprint(proposed[k]) for k in ("package_hash","manifest","config")):
            return old["cursor_at"]
    return None


def message_outputs(message):
    units={"target_quantity":"shares","target_weight":"decimal_weight","score":"dimensionless","signal":"direction"}
    return tuple(StrategyOutput("ext-"+digest({"message":message,"i":i})[:32],item["instrument_id"],OutputKind(item["kind"]),parse_decimal(item["value"]),units[item["kind"]],
                 parse_instant(message["produced_at"]),parse_instant(message["effective_at"]),parse_instant(message["expires_at"]),RunMode.LOCAL_SIMULATION) for i,item in enumerate(message["outputs"]))


class ExternalSessions:
    def __init__(self, runtime):
        self.runtime, self.database = runtime, runtime.database
        self.strategies = StrategyService(runtime)
        self.database.apply_module_migrations("strategy_external", MIGRATIONS)
        with self.database.transaction() as con:
            immutable(con, "strategy_signal_acceptances", ("message_hash",))
        self.trading = TradingService(self.database, authorize=self.runtime.require_owner)
        from finance_platform.research.inputs import InputAuthorizer
        self.inputs=InputAuthorizer(runtime)

    def principal(self, owner_id):
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM users WHERE user_id=?", (owner_id,)).fetchone()
        if row is None or not row["enabled"] or row["role"] not in {"admin", "researcher"}:
            raise StrategyError("EXTERNAL_OWNER_UNAVAILABLE")
        return Principal(row["user_id"], row["username"], row["role"])

    def binding(self, principal, instance_id, *, write=False):
        self.strategies.instance(principal, instance_id, write=write)
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM strategy_signal_bindings WHERE instance_id=?", (instance_id,)).fetchone()
        if row is None:
            raise StrategyError("EXTERNAL_SESSION_NOT_STARTED")
        return dict(row)

    def start(self, principal, instance_id, account_id, sample_reference, *, clock_mode="wall", idempotency_key=None):
        require_execution_enabled(self.database)
        instance = self.strategies.instance(principal, instance_id, write=True)
        if instance["status"] == "CREATED":
            self.strategies.transition(principal, instance_id, "VALIDATED")
            instance=self.strategies.instance(principal,instance_id)
        config = self.strategies.config(principal, instance["config_id"])
        registered = self.strategies.version(principal, config["strategy_id"], config["version"], owner_id=config["owner_id"])
        resource_principal = self.principal(instance["owner_id"])
        self.runtime.versions.get(resource_principal, VersionRef(**sample_reference))
        risk = self.trading.risk_configuration(principal, account_id)
        reference = self.runtime.versions.freeze(resource_principal, "external-config:"+fingerprint({"config": config["config_id"], "risk": risk})[:24]+":"+instance["owner_id"], "1", "strategy",
                    {"package_hash": config["package_hash"], "manifest": registered["manifest"], "config": config, "risk": risk})
        parameters = {"instance_id": instance_id, "account_id": account_id, "sample_reference": sample_reference,
                      "strategy_reference": payload(reference), "clock_mode": clock_mode,"expected_instance_updated_at":instance["updated_at"]}
        with closing(self.database.connect()) as con:
            old = con.execute("SELECT generation FROM strategy_signal_bindings WHERE instance_id=?", (instance_id,)).fetchone()
        parameters["requested_generation"] = 1 if old is None else old["generation"]+1
        return self.runtime.jobs.submit(principal, "strategy.external.activate", parameters, mode=RunMode.LOCAL_SIMULATION, idempotency_key=idempotency_key)

    def resolve_activation(self, principal, parameters):
        require_execution_enabled(self.database)
        instance = self.strategies.instance(principal, parameters["instance_id"], write=True)
        if instance["updated_at"]!=parameters["expected_instance_updated_at"]:
            raise StrategyError("EXTERNAL_ACTIVATION_LIFECYCLE_STALE")
        config = self.strategies.config(principal, instance["config_id"])
        registered = self.strategies.version(principal, config["strategy_id"], config["version"], owner_id=config["owner_id"])
        declaration = self.runtime.versions.get(principal, VersionRef(**parameters["strategy_reference"]))
        if config["mode"] != "LOCAL_SIMULATION" or not registered["trusted"] or registered["manifest"]["style"] != "external" or any(declaration.get(k) != v for k,v in {"package_hash": config["package_hash"], "manifest": registered["manifest"], "config": config}.items()):
            raise StrategyError("EXTERNAL_TRUSTED_CONFIG_REQUIRED")
        self.strategies.packages.verify(config["package_hash"])
        input_value,sample,_=self.inputs.validate(principal,parameters["sample_reference"])
        if parameters["clock_mode"] not in {"wall", "replay"} or parameters["clock_mode"] == "replay" and sample.scope is not DataScope.SYNTHETIC:
            raise StrategyError("EXTERNAL_REPLAY_REQUIRES_SYNTHETIC")
        account = self.trading.snapshot(principal, parameters["account_id"])
        if parameters["clock_mode"] == "replay":
            self.trading.snapshot(principal, parameters["account_id"], min(t.event_at for t in sample.ticks))
        validate_risk_binding(self.trading,principal,parameters["account_id"],declaration.get("risk"))
        if account["mode"] != "LOCAL_SIMULATION":
            raise StrategyError("EXTERNAL_ACCOUNT_MODE_MISMATCH")
        with closing(self.database.connect()) as con:
            owner = con.execute("SELECT owner_id FROM trading_accounts WHERE account_id=?", (parameters["account_id"],)).fetchone()[0]
        if owner != instance["owner_id"]:
            raise StrategyError("EXTERNAL_ACCOUNT_BINDING_OWNER_MISMATCH")
        self.runtime.versions.get(self.principal(instance["owner_id"]), VersionRef(**parameters["sample_reference"]))
        with closing(self.database.connect()) as con:
            old = con.execute("SELECT * FROM strategy_signal_bindings WHERE instance_id=?", (parameters["instance_id"],)).fetchone()
        if parameters["requested_generation"] != (1 if old is None else old["generation"]+1):
            raise StrategyError("EXTERNAL_ACTIVATION_GENERATION_STALE")
        cursor=retained_cursor(self.runtime,principal,old,parameters)
        at=(parse_instant(cursor) if cursor else min(timeline(sample,action_records(input_value)))) if parameters["clock_mode"]=="replay" else datetime.now(timezone.utc)
        self.trading.preflight_execution(principal,parameters["account_id"],at)
        return {"strategy": parameters["strategy_reference"], "data": parameters["sample_reference"], "seed": 0,
                "model_reason": "Authenticated structured external signals; no model"}

    def activate(self, context, parameters):
        principal = self.principal(context.run.owner_id)
        self.resolve_activation(principal, parameters)
        if payload(context.run.data) != parameters["sample_reference"] or payload(context.run.strategy) != parameters["strategy_reference"]:
            raise StrategyError("EXTERNAL_RUN_BINDING_MISMATCH")
        sample = sample_from_version(self.runtime.versions.get(principal, context.run.data))
        trading = TradingService(self.database, authorize=self.runtime.require_owner)
        risk = self.runtime.versions.get(principal, context.run.strategy)["risk"]
        trading.bind_run_risk(principal, parameters["account_id"], context.run.run_id, risk["version"])
        context.require_active()
        for instrument in sample.instruments:
            trading.add_instrument(principal, parameters["account_id"], instrument)
        with self.database.transaction() as con:
            self._job_active(con, context)
            row = con.execute("SELECT * FROM strategy_instances WHERE instance_id=?", (parameters["instance_id"],)).fetchone()
            old = con.execute("SELECT * FROM strategy_signal_bindings WHERE instance_id=?", (parameters["instance_id"],)).fetchone()
            if row["updated_at"]!=parameters["expected_instance_updated_at"] or parameters["requested_generation"]!=(1 if old is None else old["generation"]+1):
                raise StrategyError("EXTERNAL_ACTIVATION_LIFECYCLE_STALE")
            if old and old["activation_run_id"] == context.run.run_id and old["status"] == "ACTIVE":
                return {"instance_id": parameters["instance_id"], "status": "RUNNING", "generation": old["generation"]}
            if "RUNNING" not in TRANSITIONS[row["status"]]:
                raise StrategyError("EXTERNAL_LIFECYCLE_CONFLICT")
            generation = 1 if old is None else old["generation"]+1
            values = (parameters["instance_id"], parameters["account_id"], canonical(parameters["sample_reference"]).decode(),
                      canonical(parameters["strategy_reference"]).decode(), parameters["clock_mode"], generation,
                      context.run.run_id, "ACTIVE", retained_cursor(self.runtime,principal,old,parameters), now_text())
            if old is None:
                con.execute("INSERT INTO strategy_signal_bindings VALUES(?,?,?,?,?,?,?,?,?,?)", values)
            else:
                con.execute("UPDATE strategy_signal_bindings SET account_id=?,sample_reference_json=?,strategy_reference_json=?,clock_mode=?,generation=?,activation_run_id=?,status=?,cursor_at=?,updated_at=? WHERE instance_id=?", values[1:]+values[:1])
            con.execute("UPDATE strategy_instances SET status='RUNNING',active_run_id=?,updated_at=? WHERE instance_id=?", (context.run.run_id, now_text(), parameters["instance_id"]))
            self.strategies._event(con, parameters["instance_id"], "EXTERNAL_STARTED", {"run_id": context.run.run_id, "generation": generation, "clock_mode": parameters["clock_mode"]})
        return {"instance_id": parameters["instance_id"], "account_id": parameters["account_id"], "status": "RUNNING", "mode": "LOCAL_SIMULATION", "generation": generation, "clock_mode": parameters["clock_mode"]}

    def message_clock(self, principal, binding, message):
        if binding["clock_mode"] == "wall":
            return datetime.now(timezone.utc)
        sample = sample_from_version(self.runtime.versions.get(principal, VersionRef(**json.loads(binding["sample_reference_json"]))))
        produced = parse_instant(message["produced_at"])
        if not min(t.event_at for t in sample.ticks) <= produced <= max(t.event_at for t in sample.ticks):
            raise StrategyError("EXTERNAL_REPLAY_CLOCK_OUTSIDE_SAMPLE")
        return max(produced, parse_instant(binding["cursor_at"]) if binding["cursor_at"] else produced)

    def accept(self, principal, instance_id, message, signature):
        require_execution_enabled(self.database)
        binding = self.binding(principal, instance_id, write=True)
        now = self.message_clock(principal, binding, message)
        config = self.strategies.config(principal, self.strategies.instance(principal, instance_id)["config_id"])
        manifest = self.strategies.packages.verify(config["package_hash"])
        if not isinstance(message, dict) or not isinstance(message.get("outputs"), list) or any(o.get("kind") not in manifest.raw["outputs"] for o in message["outputs"] if isinstance(o, dict)):
            raise StrategyError("EXTERNAL_OUTPUT_CAPABILITY_INVALID")

        def hook(con, sha, body):
            current = con.execute("SELECT * FROM strategy_signal_bindings WHERE instance_id=?", (instance_id,)).fetchone()
            activation = con.execute("SELECT status FROM jobs WHERE run_id=?", (current["activation_run_id"],)).fetchone() if current else None
            if current is None or current["status"] != "ACTIVE" or current["generation"] != binding["generation"] or activation is None or activation["status"] != "SUCCEEDED":
                raise StrategyError("EXTERNAL_SESSION_NOT_ACTIVE")
            # Signature/sequence validation precedes this hook; a rejection
            # rolls back the newly inserted inbox identity in this transaction.
            self.trading.preflight_execution(principal,current["account_id"],now)
            con.execute("INSERT INTO strategy_signal_acceptances VALUES(?,?,?,?,?,?,?,?)", (sha, instance_id, current["generation"], current["account_id"], current["sample_reference_json"], current["strategy_reference_json"], current["clock_mode"], now_text()))
            con.execute("INSERT INTO strategy_signal_work VALUES(?,?,NULL,'STAGED',NULL,NULL,?)", (sha, instance_id, now_text()))

        accepted = ExternalSignalGate(self.strategies).accept(principal, instance_id, message, signature, expected_source=instance_id,
                    secret_env="FINANCE_PLATFORM_SIGNAL_SECRET", now=now, acceptance_hook=hook)
        job = self.enqueue(principal, instance_id, accepted["message_hash"])
        return {"accepted": True, "duplicate": accepted["duplicate"], "message_hash": accepted["message_hash"], "job_id": job["job_id"], "run_id": job["run_id"], "status": job["status"], "order_submitted": False}

    def work(self, principal, instance_id, message_hash):
        self.strategies.instance(principal, instance_id)
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT w.*,a.generation,a.account_id,a.sample_reference_json,a.strategy_reference_json,a.clock_mode,m.body_json FROM strategy_signal_work w JOIN strategy_signal_acceptances a USING(message_hash) JOIN strategy_external_messages m ON m.message_hash=w.message_hash AND m.instance_id=w.instance_id WHERE w.instance_id=? AND w.message_hash=?", (instance_id, message_hash)).fetchone()
        if row is None:
            raise StrategyError("EXTERNAL_MESSAGE_NOT_FOUND")
        return dict(row)

    def enqueue(self, principal, instance_id, message_hash):
        work = self.work(principal, instance_id, message_hash)
        if work["job_id"]:
            return self.runtime.jobs.get(principal, work["job_id"])
        job = self.runtime.jobs.submit(principal, "strategy.external.consume", {"instance_id": instance_id, "message_hash": message_hash},
                    mode=RunMode.LOCAL_SIMULATION, idempotency_key="external:"+message_hash)
        with self.database.transaction() as con:
            con.execute("UPDATE strategy_signal_work SET job_id=?,status='QUEUED',updated_at=? WHERE message_hash=? AND job_id IS NULL", (job["job_id"], now_text(), message_hash))
        return job

    def resolve_consumption(self, principal, parameters):
        require_execution_enabled(self.database)
        work = self.work(principal, parameters["instance_id"], parameters["message_hash"])
        input_value,sample,_=self.inputs.validate(principal,VersionRef(**json.loads(work["sample_reference_json"])))
        self.runtime.versions.get(principal, VersionRef(**json.loads(work["strategy_reference_json"])))
        binding=self.binding(principal,parameters["instance_id"])
        message=json.loads(work["body_json"])
        now=self.message_clock(principal,binding,message)
        with closing(self.database.connect()) as con:
            current=self.trading._markets(con,work["account_id"],now)
        initial=[t for t in sample.ticks if t.available_at<=now and (t.instrument_id not in current or t.event_at>current[t.instrument_id].event_at)]
        actions=action_records(input_value)
        for tick in initial:self.trading.preflight_execution(principal,work["account_id"],tick.available_at,market=market_event(sample,tick))
        for action in actions:
            if dispatch_at(action)<=now:preflight_action(self.trading,principal,work["account_id"],action,dispatch_at(action))
        preflight_outputs(self.trading,principal,work["account_id"],sample,message_outputs(message),now)
        if work["clock_mode"]=="replay":
            for tick in sample.ticks:
                if now<tick.available_at<parse_instant(message["expires_at"]):
                    self.trading.preflight_execution(principal,work["account_id"],tick.available_at,market=market_event(sample,tick))
            for action in actions:
                if now<dispatch_at(action)<parse_instant(message["expires_at"]):preflight_action(self.trading,principal,work["account_id"],action,dispatch_at(action))
        return {"strategy": json.loads(work["strategy_reference_json"]), "data": json.loads(work["sample_reference_json"]),
                "seed": 0, "model_reason": "Authenticated external message bound by immutable SHA-256"}

    @staticmethod
    def _job_active(con, context):
        row = context.service._active(con, context.job_id, context.worker_id, context.fencing_token)
        if row["cancel_requested"]:
            raise PlatformError("JOB_CANCELLED", 409)

    def _guard(self, principal, owner_id, context, work, *, write=False):
        self.runtime.require_owner(principal, owner_id, write=write)
        if not write:
            return
        require_execution_enabled(self.database)
        context.require_active()
        # Called by TradingService._account inside its BEGIN IMMEDIATE transaction.
        # Stop/generation changes and order effects linearize on this same DB lock.
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT b.status,b.generation,i.status AS instance_status,w.job_id FROM strategy_signal_bindings b JOIN strategy_instances i USING(instance_id) JOIN strategy_signal_work w ON w.instance_id=i.instance_id WHERE w.message_hash=?", (work["message_hash"],)).fetchone()
        if row is None or row["status"] != "ACTIVE" or row["instance_status"] != "RUNNING" or row["generation"] != work["generation"] or row["job_id"] != context.job_id:
            raise StrategyError("EXTERNAL_SESSION_NOT_ACTIVE")

    def consume(self, context, parameters):
        principal = self.principal(context.run.owner_id)
        work = self.work(principal, parameters["instance_id"], parameters["message_hash"])
        try:
            self.resolve_consumption(principal,parameters)
            if payload(context.run.data) != json.loads(work["sample_reference_json"]) or payload(context.run.strategy) != json.loads(work["strategy_reference_json"]):
                raise StrategyError("EXTERNAL_RUN_BINDING_MISMATCH")
            with self.database.transaction() as con:
                self._job_active(con, context)
                if work["job_id"] != context.job_id:
                    previous = con.execute("SELECT run_id,status FROM jobs WHERE job_id=?", (work["job_id"],)).fetchone()
                    if previous is None or previous["status"] not in {"FAILED", "CANCELLED"} or context.run.parent_run_id != previous["run_id"]:
                        raise StrategyError("EXTERNAL_CONSUMER_FENCED")
                con.execute("UPDATE strategy_signal_work SET job_id=?,status='RUNNING',error_code=NULL,updated_at=? WHERE message_hash=?", (context.job_id, now_text(), work["message_hash"]))
            self._guard(principal, principal.user_id, context, work, write=True)
            binding = self.binding(principal, parameters["instance_id"])
            message = json.loads(work["body_json"])
            now = self.message_clock(principal, binding, message)
            if now >= parse_instant(message["expires_at"]):
                raise StrategyError("EXTERNAL_MESSAGE_EXPIRED")
            input_value,sample,input_grant=self.inputs.validate(principal,context.run.data)
            actions=action_records(self.runtime.versions.get(principal,context.run.data))
            declaration = self.runtime.versions.get(principal, context.run.strategy)
            config = declaration["config"]
            with closing(self.database.connect()) as con:
                prior = con.execute("SELECT w.status FROM strategy_signal_work w JOIN strategy_external_messages m ON m.message_hash=w.message_hash AND m.instance_id=w.instance_id WHERE w.instance_id=? AND m.sequence<?", (parameters["instance_id"], message["sequence"])).fetchall()
            if any(row["status"] in {"STAGED", "QUEUED", "RUNNING"} for row in prior):
                raise StrategyError("EXTERNAL_PREVIOUS_MESSAGE_PENDING")
            trading = TradingService(self.database, authorize=lambda p, owner, write=False: self._guard(p, owner, context, work, write=write),
                        data_authorizer=lambda e: e.provenance.source_id == sample.source_id and e.provenance.license_id == sample.license_id)
            boundary = TradeBoundary(trading, principal, work["account_id"], sample, strategy_id=config["strategy_id"])
            boundary.action_authorizer=input_grant.authorize_action
            trading.bind_run_risk(principal, work["account_id"], context.run.run_id, declaration["risk"]["version"])
            boundary.risk_version = declaration["risk"]["version"]
            with closing(self.database.connect()) as con:
                account = trading._account(con, work["account_id"], principal)
                current = trading._markets(con, work["account_id"], now)
            boundary.last_ticks = {i: Tick(e.event_id, i, e.event_at, e.available_at, e.price, e.volume, e.session_open) for i, e in current.items()}
            initial = [t for t in sample.ticks if t.available_at <= now and (t.instrument_id not in current or t.event_at > current[t.instrument_id].event_at)]
            if len(initial) > 128:
                raise StrategyError("EXTERNAL_REPLAY_BUDGET_EXCEEDED")
            initial_events=[(t.available_at,1,t) for t in initial]+[(dispatch_at(a),0,a) for a in actions if dispatch_at(a)<=now]
            for at,kind,item in sorted(initial_events,key=lambda item:(item[0],item[1])):
                context.require_active()
                if kind==0:
                    apply_action(boundary,item,at)
                else:
                    boundary.on_tick(item,at)
            outputs = message_outputs(message)
            boundary.route(outputs, now)
            context.checkpoint(50, {"message_hash": work["message_hash"], "sequence": message["sequence"]})
            cursor = now
            if work["clock_mode"] == "replay":
                following = [t for t in sample.ticks if now < t.available_at < parse_instant(message["expires_at"])]
                if len(following) > 128:
                    raise StrategyError("EXTERNAL_REPLAY_BUDGET_EXCEEDED")
                following_events=[(t.available_at,1,t) for t in following]+[(dispatch_at(a),0,a) for a in actions if now<dispatch_at(a)<parse_instant(message["expires_at"])]
                for at,kind,item in sorted(following_events,key=lambda item:(item[0],item[1])):
                    context.require_active()
                    if kind==0:
                        apply_action(boundary,item,at)
                    else:
                        boundary.on_tick(item,at)
                    cursor = max(cursor,at)
            result = {"message_hash": work["message_hash"], "sequence": message["sequence"], "account_id": work["account_id"],
                      "mode": "LOCAL_SIMULATION", "clock_mode": work["clock_mode"], "orders": boundary.orders, "fills": boundary.fills,
                      "rejections": boundary.rejections, "account": boundary.snapshot(cursor), "lineage": payload(context.run)}
            self._guard(principal, principal.user_id, context, work, write=True)
            with self.database.transaction() as con:
                self._job_active(con, context)
                con.execute("UPDATE strategy_signal_work SET status=?,error_code=?,result_json=?,updated_at=? WHERE message_hash=? AND job_id=?", ("FAILED" if boundary.rejections else "SUCCEEDED", "EXTERNAL_RISK_REJECTED" if boundary.rejections else None, canonical(result).decode(), now_text(), work["message_hash"], context.job_id))
                con.execute("UPDATE strategy_signal_bindings SET cursor_at=?,updated_at=? WHERE instance_id=? AND generation=?", (payload(cursor), now_text(), work["instance_id"], work["generation"]))
                self.strategies._event(con, work["instance_id"], "EXTERNAL_CONSUMED", {"message_hash": work["message_hash"], "job_id": context.job_id, "orders": len(boundary.orders), "fills": len(boundary.fills)})
            if boundary.rejections:
                raise StrategyError("EXTERNAL_RISK_REJECTED")
            return result
        except Exception as error:
            code = error.code if isinstance(error, (PlatformError, TradingError)) else str(error) if isinstance(error, StrategyError) else "EXTERNAL_CONSUMPTION_FAILED"
            with self.database.transaction() as con:
                con.execute("UPDATE strategy_signal_work SET status=?,error_code=?,updated_at=? WHERE message_hash=? AND job_id=?", ("EXPIRED" if "EXPIRED" in code else "CANCELLED" if code == "JOB_CANCELLED" else "FAILED", code, now_text(), work["message_hash"], context.job_id))
                self.strategies._event(con, work["instance_id"], "EXTERNAL_FAILED", {"message_hash": work["message_hash"], "code": code})
            raise

    def stop(self, principal, instance_id, target="STOPPED", *, cancel_orders=False):
        binding = self.binding(principal, instance_id, write=True)
        result = self.strategies.transition(principal, instance_id, target)
        with closing(self.database.connect()) as con:
            jobs = con.execute("SELECT job_id FROM strategy_signal_work WHERE instance_id=? AND job_id IS NOT NULL AND status IN ('QUEUED','RUNNING')", (instance_id,)).fetchall()
        for row in jobs:
            self.runtime.jobs.cancel(principal, row["job_id"])
        with self.database.transaction() as con:
            con.execute("UPDATE strategy_signal_work SET status='CANCELLED',error_code='EXTERNAL_SESSION_STOPPED',updated_at=? WHERE instance_id=? AND status IN ('STAGED','QUEUED')", (now_text(), instance_id))
        failures, cancelled = [], []
        if cancel_orders:
            trading = TradingService(self.database, authorize=self.runtime.require_owner)
            config = self.strategies.config(principal, result["config_id"])
            snapshot = trading.snapshot(principal, binding["account_id"])
            for order in snapshot["orders"]:
                if order["intent"]["strategy_id"] == config["strategy_id"] and order["status"] not in {"filled", "cancelled", "expired", "rejected", "replaced"}:
                    try:
                        trading.cancel(principal, binding["account_id"], order["client_order_id"])
                        cancelled.append(order["client_order_id"])
                    except TradingError as error:
                        failures.append({"client_order_id": order["client_order_id"], "code": error.code})
        self.runtime.audit(principal, "strategy.external.stopped", instance_id, {"cancel_failures": failures, "cancel_requested": cancelled})
        return result | {"cancel_requested": cancelled, "cancel_failures": failures}

    def recover_staged(self):
        try:
            require_execution_enabled(self.database)
        except PlatformError:
            return
        with closing(self.database.connect()) as con:
            rows = con.execute("SELECT w.instance_id,w.message_hash,i.owner_id FROM strategy_signal_work w JOIN strategy_instances i USING(instance_id) JOIN strategy_signal_bindings b USING(instance_id) JOIN strategy_signal_acceptances a USING(message_hash) WHERE w.job_id IS NULL AND w.status='STAGED' AND i.status='RUNNING' AND b.status='ACTIVE' AND a.generation=b.generation ORDER BY w.updated_at LIMIT 100").fetchall()
        for row in rows:
            try:
                self.enqueue(self.principal(row["owner_id"]), row["instance_id"], row["message_hash"])
            except (StrategyError, PlatformError):
                # Preserve staged evidence; API list exposes the incomplete enqueue.
                continue

    def list_messages(self, principal, instance_id, limit=50, offset=0):
        self.strategies.instance(principal, instance_id)
        self.runtime.jobs._paging(limit, offset)
        with closing(self.database.connect()) as con:
            count = con.execute("SELECT count(*) FROM strategy_signal_work WHERE instance_id=?", (instance_id,)).fetchone()[0]
            rows = con.execute("SELECT message_hash,job_id,status,error_code,result_json,updated_at FROM strategy_signal_work WHERE instance_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?", (instance_id, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
            items.append(item)
        return {"items": items, "total": count, "limit": limit, "offset": offset}
