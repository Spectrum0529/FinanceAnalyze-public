"""Version-bound research submissions on core auth/versions/jobs, never S03 ledger."""
from .payload_storage import persist as store_payload, hydrate as load_payload, task_binding

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import uuid

from finance_platform.core.auth import Principal
from finance_platform.core.contracts import (RunMode, VersionRef, DataScope, payload, fingerprint,
                                            StrategyOutput, OutputKind, parse_instant, parse_decimal)
from finance_platform.core.database import immutable
from finance_platform.core.security import now_text
from finance_platform.strategies.service import StrategyService
from finance_platform.strategies.worker import TrustedWorker
from finance_platform.trading.service import TradingService
from finance_platform.trading.accounting import FeeSchedule, floor_step, decimal
from .backends import EventBackend, NumpyBatchBackend, comparable
from .data import DevelopmentSample, Bar, Tick, ResearchError, synthetic_sample, sample_from_version
from .execution import TradeBoundary
from .experiments import ExperimentArchive
from .model import LinearPipeline, ModelRow, TrainingWindow
from .reporting import report
from .benchmark import execute_benchmark
from .experiments import parameter_search
from .actions import action_records
from finance_platform.strategies.contracts import digest

EXPORT_MIGRATIONS = ("""
CREATE TABLE research_data_export_bindings (
 resource_id TEXT NOT NULL, version TEXT NOT NULL, owner_id TEXT NOT NULL,
 policy_json TEXT NOT NULL, PRIMARY KEY(resource_id,version)
);
""",)


MIGRATIONS = ("""
CREATE TABLE research_results (run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, job_id TEXT NOT NULL,
 sample_hash TEXT NOT NULL, strategy_hash TEXT NOT NULL, result_json TEXT NOT NULL, result_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE research_models (model_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, run_id TEXT NOT NULL,
 version TEXT NOT NULL, model_hash TEXT NOT NULL, train_cutoff TEXT NOT NULL, artifact_json TEXT NOT NULL,
 reference_json TEXT NOT NULL, created_at TEXT NOT NULL);
""",)


class ResearchService:
    def __init__(self, runtime):
        self.runtime, self.database = runtime, runtime.database
        self.strategies = StrategyService(runtime)
        from .archive_storage import StoredExperimentArchive
        self.archive = StoredExperimentArchive(runtime, ExperimentArchive(runtime.root/".local/platform/experiments"))
        self.database.apply_module_migrations("research", MIGRATIONS)
        self.database.apply_module_migrations("research_export", EXPORT_MIGRATIONS)
        with self.database.transaction() as con:
            immutable(con, "research_results", ("run_id",))
            immutable(con, "research_models", ("model_id",))
            immutable(con, "research_data_export_bindings", ("resource_id", "version"))
        from .inputs import InputAuthorizer
        self.inputs=InputAuthorizer(runtime)

    def create_sample(self, principal, *, frequency="1d", count=8, two_assets=False):
        self.runtime.require_owner(principal, principal.user_id, write=True)
        if type(count) is not int or not 2 <= count <= 8 or type(two_assets) is not bool:
            raise ResearchError("SYNTHETIC_SAMPLE_CONFIGURATION_INVALID")
        sample = synthetic_sample(frequency, count, two_assets=two_assets)
        import hashlib
        from . import data as data_module
        origin = {"schema_version": "platform-generated-origin-1", "origin_kind": "generated", "generator": "platform-synthetic-v1",
                  "generator_code_hash": hashlib.sha256(Path(data_module.__file__).read_bytes()).hexdigest(), "sample_hash": sample.sha256,
                  "scope": "SYNTHETIC", "use_allowed": ["development", "export"]}
        reference = self.runtime.versions.freeze(principal, "research-sample:"+sample.sha256[:20]+":"+principal.user_id,
                                                 "2", "data", {"schema_version": "research-input-2", "sample": payload(sample), "origin": origin})
        self._export_binding(principal, reference, {"kind": "PLATFORM_GENERATED_SYNTHETIC", "allowed": True})
        return {"reference": payload(reference), "sample_hash": sample.sha256, "scope": "SYNTHETIC", "bars": len(sample.bars), "ticks": len(sample.ticks)}

    def submit_qlib(self,principal,export_reference,workflow,*,idempotency_key=None):
        from .qlib_service import QlibResearchService
        return QlibResearchService(self).submit(principal,export_reference,workflow,idempotency_key=idempotency_key)

    def resolve_qlib(self,principal,parameters):
        from .qlib_service import QlibResearchService
        return QlibResearchService(self).resolve(principal,parameters)

    def execute_qlib(self,context,parameters):
        from .qlib_service import QlibResearchService
        return QlibResearchService(self).execute(context,parameters)

    def debate_service(self):
        from .debate_service import DebateResearchService
        return DebateResearchService(self)

    def from_snapshot(self, principal, snapshot_id):
        """Freeze authorized bar decisions plus independent execution prints."""
        from predictor.access import AccessPolicy
        from finance_platform.data.provider_sdk.service import ProviderDataService as DataService
        data = DataService(self.database, AccessPolicy(self.runtime.root))
        snapshot = data.get_snapshot(principal.user_id, snapshot_id, require_quality=True)
        origin = data.authorize_snapshot(principal.user_id, snapshot_id, purpose="development")
        dataset = data.dataset(principal.user_id, snapshot["dataset_id"])
        source = data.source(principal.user_id, dataset["source_id"], dataset["source_version"])
        data._rights(source, dataset["scope"], "development")
        from .snapshot import snapshot_sample
        sample,execution=snapshot_sample(snapshot,dataset,source)
        content = {"schema_version": "research-input-3", "sample": payload(sample), "origin": origin,"execution":execution,"source_snapshot":snapshot}
        reference = self.runtime.versions.freeze(principal, "research-snapshot:"+snapshot_id+":"+principal.user_id, "3", "data", content)
        self._export_binding(principal, reference, {"kind": "DATA_SNAPSHOT", "snapshot_id": snapshot_id,
                             "source_id": dataset["source_id"], "source_version": dataset["source_version"],
                             "source_policy_hash": fingerprint(source), "scope": dataset["scope"]})
        return {"reference": payload(reference), "sample_hash": sample.sha256, "snapshot_id": snapshot_id,
                "scope": sample.scope.value, "execution":execution,"execution_ticks":len(sample.ticks)}

    def sample(self, principal, reference):
        return self.inputs.validate(principal,reference)[1]

    def _export_binding(self, principal, reference, policy):
        with self.database.transaction() as con:
            old = con.execute("SELECT policy_json FROM research_data_export_bindings WHERE resource_id=? AND version=?", (reference.resource_id, reference.version)).fetchone()
            raw = json.dumps(policy, sort_keys=True, separators=(",", ":"))
            if old:
                if old["policy_json"] != raw:
                    raise ResearchError("DATA_EXPORT_POLICY_CONFLICT")
                return
            con.execute("INSERT INTO research_data_export_bindings VALUES(?,?,?,?)", (reference.resource_id, reference.version, principal.user_id, raw))

    def export_result(self, principal, run_id):
        result = self.result(principal, run_id)
        reference = self.require_export_rights(principal, run_id, result)
        self.runtime.audit(principal, "research.export", run_id, {"data_hash": reference["content_hash"], "format": "json"})
        return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def require_export_rights(self, principal, run_id, result):
        if result.get("family")=="tradingagents-native-report-1":
            raise ResearchError("TA_RESULT_EXPORT_REQUIRES_SOURCE_RIGHTS")
        reference = result["lineage"]["data"]
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM research_data_export_bindings WHERE resource_id=? AND version=?", (reference["resource_id"], reference["version"])).fetchone()
            job = con.execute("SELECT status FROM jobs WHERE run_id=?", (run_id,)).fetchone()
        if job is None or job["status"] != "SUCCEEDED":
            raise ResearchError("RESULT_EXPORT_NOT_APPROVED")
        value = self.runtime.versions.get(principal, VersionRef(**reference))
        origin = value.get("origin") if value.get("schema_version") in {"research-input-2","research-input-3"} else None
        if origin is None:
            raise ResearchError("EXPORT_ORIGIN_UNAVAILABLE")
        if origin.get("origin_kind") == "data_snapshot":
            from predictor.access import AccessPolicy
            from finance_platform.data.provider_sdk.service import ProviderDataService as DataService
            data = DataService(self.database, AccessPolicy(self.runtime.root))
            with closing(self.database.connect()) as con:
                resource_owner = con.execute("SELECT owner_id FROM resource_versions WHERE resource_id=? AND version=?", (reference["resource_id"], reference["version"])).fetchone()[0]
            data.authorize_snapshot(resource_owner, origin["snapshot_id"], purpose="export", expected_origin=origin)
        elif row is None or json.loads(row["policy_json"])!={"kind":"PLATFORM_GENERATED_SYNTHETIC","allowed":True} or origin.get("schema_version") != "platform-generated-origin-1" or origin.get("generator") != "platform-synthetic-v1" or origin.get("scope") != "SYNTHETIC" or origin.get("sample_hash") != fingerprint(value["sample"]) or "export" not in origin.get("use_allowed", []):
            raise ResearchError("RESULT_EXPORT_NOT_APPROVED")
        return reference

    def submit_search(self, principal, instance_id, sample_reference, space, budget):
        instance = self.strategies.instance(principal, instance_id, write=True)
        config = self.strategies.config(principal, instance["config_id"])
        registered = self.strategies.version(principal, config["strategy_id"], config["version"], owner_id=config["owner_id"])
        if not isinstance(space, list) or type(budget) is not int or not 1 <= len(space) <= budget <= 16:
            raise ResearchError("SEARCH_SCOPE_OR_BUDGET_INVALID")
        reference = self.runtime.versions.freeze(principal, "strategy-config:"+config["config_id"]+":"+principal.user_id, "1", "strategy", {"package_hash": config["package_hash"], "manifest": registered["manifest"], "config": config})
        return self.runtime.jobs.submit(principal, "research.search", {"instance_id": instance_id, "sample_reference": sample_reference,
                    "strategy_reference": payload(reference), "space": space, "budget": budget}, mode=RunMode.BACKTEST)

    def resolve_search(self, principal, parameters):
        return self.resolve_bindings(principal, {"instance_id": parameters["instance_id"], "sample_reference": parameters["sample_reference"],
                    "strategy_reference": parameters["strategy_reference"], "backend": "batch", "model_id": None})

    def execute_search(self, context, parameters):
        principal = self.principal(context.run.owner_id)
        sample = self.sample(principal, parameters["sample_reference"])
        declaration = self.runtime.versions.get(principal, context.run.strategy)
        if payload(context.run.data) != parameters["sample_reference"] or declaration["config"]["mode"] != "BACKTEST":
            raise ResearchError("SEARCH_RUN_BINDING_MISMATCH")
        self.archive.begin(context.run.run_id, context.run, storage_binding=task_binding(context), sample=sample, configuration=parameters,
                           data=self.runtime.versions.get(principal,context.run.data),code_paths=sorted((self.runtime.root/"src").rglob("*.py")), dependency_bytes=(self.runtime.root/"uv.lock").read_bytes())
        try:
            base = declaration["config"]
            def evaluate(values, attempt):
                context.require_active()
                config = self.strategies.configure(principal, base["strategy_id"], base["version"], values)
                instance = self.strategies.create_instance(principal, config["config_id"])
                job = self.submit(principal, instance["instance_id"], parameters["sample_reference"], backend="batch")
                return {"job_id": job["job_id"], "run_id": job["run_id"], "stage": "BACKTEST_QUEUED"}
            def record_attempt(attempt):
                self.archive.append_attempt(context.run.run_id, attempt)
            result = parameter_search(sample, parameters["space"], budget=parameters["budget"], evaluate=evaluate,
                         before_attempt=context.require_active,record_attempt=record_attempt)
            for attempt in result["attempts"]:
                attempt["attempt_id"] = context.run.run_id+":"+str(attempt["attempt"])
                if attempt["status"] == "SUCCEEDED":
                    attempt["status"] = "QUEUED"
                else:
                    attempt["stage"] = "PRE_SUBMIT_PARAMETER_VALIDATION_FAILED"
            result.update({"search_run_id": context.run.run_id, "submission_status": "CHILDREN_QUEUED", "lineage": payload(context.run)})
            self.archive.finish(context.run.run_id, result)
            return result
        except Exception:
            self.archive.fail(context.run.run_id, "SEARCH_FAILED")
            raise

    def search_status(self, principal, job_id):
        parent = self.runtime.jobs.get(principal, job_id)
        if parent["handler"] != "research.search":
            raise ResearchError("SEARCH_JOB_REQUIRED")
        if parent["result"] is None:
            return {"job": parent, "status": parent["status"]}
        result = dict(parent["result"])
        attempts = []
        for attempt in result["attempts"]:
            item = dict(attempt)
            if item["status"] != "FAILED":
                child = self.runtime.jobs.get(principal, item["result"]["job_id"])
                item.update({"status": child["status"], "error_code": child["error_code"], "backtest_result": child["result"]})
            attempts.append(item)
        result["attempts"] = attempts
        result["failure_count"] = sum(a["status"] in {"FAILED", "CANCELLED"} for a in attempts)
        result["status"] = "RUNNING" if any(a["status"] in {"QUEUED", "RUNNING"} for a in attempts) else "COMPLETED_WITH_ATTEMPT_FAILURES" if result["failure_count"] else "COMPLETED"
        return result

    def submit(self, principal, instance_id, sample_reference, *, backend="event", model_id=None, idempotency_key=None):
        instance = self.strategies.instance(principal, instance_id, write=True)
        if instance["status"] == "CREATED":
            self.strategies.transition(principal, instance_id, "VALIDATED")
            instance = self.strategies.instance(principal, instance_id)
        if instance["status"] not in {"VALIDATED", "STOPPED", "PAUSED", "FAILED"}:
            raise ResearchError("STRATEGY_INSTANCE_NOT_SUBMITTABLE")
        config = self.strategies.config(principal, instance["config_id"])
        registered = self.strategies.version(principal, config["strategy_id"], config["version"], owner_id=config["owner_id"])
        if not registered["trusted"] or backend not in {"event", "batch"} or config["mode"] != "BACKTEST":
            raise ResearchError("TRUSTED_STRATEGY_OR_BACKEND_REQUIRED")
        sample = self.sample(principal, sample_reference)
        strategy_ref = self.runtime.versions.freeze(principal, "strategy-config:"+config["config_id"]+":"+principal.user_id, "1", "strategy",
                         {"package_hash": config["package_hash"], "manifest": registered["manifest"], "config": config})
        model = self.model(principal, model_id) if model_id else None
        if registered["manifest"]["model"] is not None and (model is None or registered["manifest"]["model"]["sha256"] != model["model_hash"]):
            raise ResearchError("MODEL_STRATEGY_REFERENCE_MISMATCH")
        return self.runtime.jobs.submit(principal, "research.backtest", {"instance_id": instance_id, "sample_reference": sample_reference,
                    "backend": backend, "model_id": model_id, "strategy_reference": payload(strategy_ref)}, mode=RunMode.BACKTEST, idempotency_key=idempotency_key)

    def resolve_bindings(self, principal, parameters):
        instance = self.strategies.instance(principal, parameters["instance_id"], write=True)
        config = self.strategies.config(principal, instance["config_id"])
        registered = self.strategies.version(principal, config["strategy_id"], config["version"], owner_id=config["owner_id"])
        declaration = self.runtime.versions.get(principal, VersionRef(**parameters["strategy_reference"]))
        if not registered["trusted"] or config["mode"] != "BACKTEST" or declaration != {"package_hash": config["package_hash"], "manifest": registered["manifest"], "config": config}:
            raise ResearchError("RESEARCH_STRATEGY_BINDING_MISMATCH")
        sample = self.sample(principal, parameters["sample_reference"])
        manifest = self.strategies.packages.verify(config["package_hash"])
        if not {b.frequency for b in sample.bars} <= set(manifest.raw["data"]["frequencies"]) or not {i.asset_class.value for i in sample.instruments} <= set(manifest.raw["assets"]):
            raise ResearchError("STRATEGY_DATA_CAPABILITY_MISMATCH")
        model = self.model(principal, parameters["model_id"]) if parameters.get("model_id") else None
        if manifest.raw["model"] is not None and (model is None or manifest.raw["model"]["sha256"] != model["model_hash"]):
            raise ResearchError("MODEL_STRATEGY_REFERENCE_MISMATCH")
        return {"strategy": parameters["strategy_reference"], "data": parameters["sample_reference"], "seed": 0,
                "model": None if model is None else model["reference"], "model_reason": "No model used" if model is None else None}

    def principal(self, owner_id):
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT user_id,username,role,enabled FROM users WHERE user_id=?", (owner_id,)).fetchone()
        if row is None or not row["enabled"]:
            raise ResearchError("RESEARCH_OWNER_UNAVAILABLE")
        return Principal(row["user_id"], row["username"], row["role"])

    def execute(self, context, parameters):
        principal = self.principal(context.run.owner_id)
        instance = self.strategies.instance(principal, parameters["instance_id"], write=True)
        config = self.strategies.config(principal, instance["config_id"])
        input_value,sample,input_grant=self.inputs.validate(principal,parameters["sample_reference"])
        actions=action_records(input_value)
        # Raw /jobs submissions cannot substitute core's synthetic-none defaults.
        if payload(context.run.data) != parameters["sample_reference"] or self.runtime.versions.get(principal, context.run.config) != parameters:
            raise ResearchError("RESEARCH_RUN_BINDING_MISMATCH")
        declaration = self.runtime.versions.get(principal, context.run.strategy)
        if declaration.get("package_hash") != config["package_hash"] or declaration.get("config") != config:
            raise ResearchError("RESEARCH_STRATEGY_BINDING_MISMATCH")
        model = self.model(principal, parameters["model_id"]) if parameters.get("model_id") else None
        if model and payload(context.run.model) != model["reference"] or model is None and context.run.model is not None:
            raise ResearchError("RESEARCH_MODEL_BINDING_MISMATCH")
        context.require_active()
        run_id = context.run.run_id
        self.archive.begin(run_id, context.run, storage_binding=task_binding(context), sample=sample, code_paths=sorted((self.runtime.root/"src").rglob("*.py")),
                           data=self.runtime.versions.get(principal, context.run.data), configuration=parameters, dependency_bytes=(self.runtime.root/"uv.lock").read_bytes())
        try:
            self.strategies.transition(principal, instance["instance_id"], "RUNNING", run_id=run_id)
            account_id = "bt-"+run_id
            def authorize_research(p, owner, *, write=False):
                self.runtime.require_owner(p, owner, write=write)
                if write:
                    context.require_active()
                    with closing(self.database.connect()) as con:
                        current = con.execute("SELECT status,active_run_id FROM strategy_instances WHERE instance_id=?", (instance["instance_id"],)).fetchone()
                    if current is None or current["status"] != "RUNNING" or current["active_run_id"] != run_id:
                        raise ResearchError("RESEARCH_RUN_NO_LONGER_ACTIVE")
            trading = TradingService(self.database, authorize=authorize_research,
                         data_authorizer=lambda e: e.provenance.source_id == sample.source_id and e.provenance.license_id == sample.license_id)
            trading.create_account(principal, account_id, "1000", now=sample.ticks[0].event_at)
            for instrument in sample.instruments:
                trading.add_instrument(principal, account_id, instrument)
            boundary = TradeBoundary(trading, principal, account_id, sample, strategy_id=config["strategy_id"], slippage_bps="10")
            boundary.action_authorizer=input_grant.authorize_action
            backend = EventBackend() if parameters["backend"] == "event" else NumpyBatchBackend()
            result = backend.run(sample, boundary, TrustedWorker(self.strategies.packages, active_guard=context.require_active), config["package_hash"], config["parameters"],
                     actions=actions,
                     model=None if model is None else model["artifact"], job_context=context,
                     checkpoint=lambda state: self.strategies.checkpoint(principal, instance["instance_id"], run_id, state, job_context=context))
            benchmark_account = "bm-"+run_id
            trading.create_account(principal, benchmark_account, "1000", now=sample.ticks[0].event_at)
            for instrument in sample.instruments:
                trading.add_instrument(principal, benchmark_account, instrument)
            benchmark_boundary = TradeBoundary(trading, principal, benchmark_account, sample, strategy_id="benchmark", slippage_bps="10")
            benchmark_boundary.action_authorizer=input_grant.authorize_action
            benchmark = execute_benchmark(sample, benchmark_boundary, job_context=context,actions=actions)
            result["benchmark"] = benchmark
            result["report"] = report(result, benchmark=benchmark)
            result["run_id"] = run_id
            result["lineage"] = payload(context.run)
            result["account_id"] = account_id
            context.require_active()
            stored_report = store_payload(self.runtime, task_binding(context), "reports", json.dumps(result, sort_keys=True, separators=(",", ":")))
            with self.database.transaction() as con:
                context.assert_fenced(con)
                self.runtime.require_owner(principal, principal.user_id, write=True)
                current = con.execute("SELECT status,active_run_id FROM strategy_instances WHERE instance_id=?", (instance["instance_id"],)).fetchone()
                if current is None or current["status"] != "RUNNING" or current["active_run_id"] != run_id:
                    raise ResearchError("RESEARCH_RUN_NO_LONGER_ACTIVE")
                con.execute("INSERT INTO research_results VALUES(?,?,?,?,?,?,?,?)", (run_id, principal.user_id, context.job_id, sample.sha256,
                    config["package_hash"], stored_report, fingerprint(result), now_text()))
            self.archive.finish(run_id, result)
            self.strategies.transition(principal, instance["instance_id"], "STOPPED", run_id=run_id)
            return {"run_id": run_id, "account_id": account_id, "backend": result["backend"], "report": result["report"]}
        except Exception:
            self.archive.fail(run_id, "RESEARCH_EXECUTION_FAILED")
            current = self.strategies.instance(principal, instance["instance_id"])
            if current["status"] == "RUNNING" and current["active_run_id"] == run_id:
                self.strategies.transition(principal, instance["instance_id"], "FAILED", run_id=run_id)
            raise

    def result(self, principal, run_id):
        from finance_platform.core.result_lifecycle import active_result
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM research_results WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ResearchError("RESEARCH_RESULT_NOT_FOUND")
            self.runtime.require_owner(principal, row["owner_id"])
            if not active_result(con, row["owner_id"], "research:"+run_id):
                raise ResearchError("RESEARCH_RESULT_NOT_FOUND")
        result = load_payload(self.runtime, row["owner_id"], "reports", row["result_json"])
        from . import native_results
        if result.get("family") in native_results.FAMILIES:
            with closing(self.database.connect()) as con:
                job=con.execute("SELECT status FROM jobs WHERE job_id=? AND owner_id=?",(row["job_id"],principal.user_id)).fetchone()
            if principal.user_id!=row["owner_id"] or job is None or job["status"]!="SUCCEEDED":
                raise ResearchError("RESEARCH_RESULT_NOT_FOUND")
        actual_hash=native_results.digest(result) if result.get("family") in native_results.FAMILIES else fingerprint(result)
        if actual_hash != row["result_hash"]:
            raise ResearchError("RESEARCH_RESULT_HASH_MISMATCH")
        return result

    def list_results(self, principal, limit=50, offset=0):
        from finance_platform.core.result_lifecycle import table_exists, visible_clause
        self.runtime.jobs._paging(limit, offset)
        native_families="('qlib-workflow-result-1','tradingagents-native-report-1')"
        where, args = ("WHERE (owner_id=? OR coalesce(json_extract(result_json,'$.family'),'') NOT IN "+native_families+")", (principal.user_id,)) if principal.role == "admin" else ("WHERE owner_id=?", (principal.user_id,))
        where += " AND (coalesce(json_extract(result_json,'$.family'),'') NOT IN "+native_families+" OR EXISTS (SELECT 1 FROM jobs j WHERE j.job_id=research_results.job_id AND j.owner_id=research_results.owner_id AND j.status='SUCCEEDED'))"
        with closing(self.database.connect()) as con:
            name = "NULL"
            if table_exists(con, "result_lifecycle"):
                where += " AND " + visible_clause("research", "research_results.run_id", "research_results.owner_id")
                name = ("(SELECT l.name FROM result_lifecycle l WHERE l.owner_id=research_results.owner_id "
                        "AND l.result_id='research:'||research_results.run_id)")
            count = con.execute("SELECT count(*) FROM research_results "+where, args).fetchone()[0]
            rows = con.execute("SELECT run_id,owner_id,job_id,sample_hash,strategy_hash,created_at,"
                               "json_extract(result_json,'$.family') AS family," + name + " AS name FROM research_results "
                               +where+" ORDER BY created_at DESC,run_id LIMIT ? OFFSET ?", args+(limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": count, "limit": limit, "offset": offset}

    def save_model(self, principal, artifact, run_id, *, storage_binding):
        model_id = "model-"+uuid.uuid4().hex
        reference = self.runtime.versions.freeze(principal, model_id, artifact["version"], "model", artifact, storage_binding=storage_binding)
        model_hash = fingerprint(artifact)
        stored_model = store_payload(self.runtime, storage_binding, "artifacts", json.dumps(artifact, sort_keys=True, separators=(",", ":")))
        with self.database.transaction() as con:
            con.execute("INSERT INTO research_models VALUES(?,?,?,?,?,?,?,?,?)", (model_id, principal.user_id, run_id, artifact["version"], model_hash,
                         artifact["train_cutoff"], stored_model, json.dumps(payload(reference)), now_text()))
        return self._model(principal, model_id, allow_in_progress=True)

    def model(self, principal, model_id):
        return self._model(principal,model_id)

    def _model(self, principal, model_id, *, allow_in_progress=False):
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM research_models WHERE model_id=?", (model_id,)).fetchone()
            job = con.execute("SELECT status FROM jobs WHERE run_id=?",(row["run_id"],)).fetchone() if row else None
        if row is None:
            raise ResearchError("MODEL_NOT_FOUND")
        self.runtime.require_owner(principal, row["owner_id"])
        if job is None or job["status"] not in ({"RUNNING","SUCCEEDED"} if allow_in_progress else {"SUCCEEDED"}):
            raise ResearchError("MODEL_SOURCE_RUN_NOT_SUCCEEDED")
        result = dict(row)
        result["artifact"] = load_payload(self.runtime, row["owner_id"], "artifacts", result.pop("artifact_json"))
        result["reference"] = json.loads(result.pop("reference_json"))
        if fingerprint(result["artifact"]) != result["model_hash"]:
            raise ResearchError("MODEL_ARTIFACT_CHANGED")
        return result

    def list_models(self, principal, limit=50, offset=0):
        self.runtime.jobs._paging(limit, offset)
        where, args = ("WHERE j.status='SUCCEEDED'", ()) if principal.role == "admin" else ("WHERE j.status='SUCCEEDED' AND m.owner_id=?", (principal.user_id,))
        with closing(self.database.connect()) as con:
            count = con.execute("SELECT count(*) FROM research_models m JOIN jobs j ON j.run_id=m.run_id "+where, args).fetchone()[0]
            rows = con.execute("SELECT m.model_id,m.owner_id,m.run_id,m.version,m.model_hash,m.train_cutoff,m.created_at FROM research_models m JOIN jobs j ON j.run_id=m.run_id "+where+" ORDER BY m.created_at DESC LIMIT ? OFFSET ?", args+(limit, offset)).fetchall()
        return {"items": [dict(r) for r in rows], "total": count, "limit": limit, "offset": offset}
