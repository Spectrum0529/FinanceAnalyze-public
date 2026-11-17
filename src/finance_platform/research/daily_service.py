"""One durable job: guarded provider acquisition, typed data, daily simulation."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
import json
import time

from finance_platform.core.auth import Principal
from finance_platform.core.contracts import RunMode, VersionRef, payload, fingerprint, identifier
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import now_text, safe_json
from .daily import DailyError, DailyRequest, DailySeries, DATA_FAMILY, RESULT_FAMILY, DEFAULTS, STRATEGIES, ASSUMPTIONS, ENGINE_VERSION, MAX_CALENDAR_DAYS, MAX_ROWS, simulate, ENGINE, STRATEGY_VERSION, CALCULATION_VERSION
from .daily_store import DailyResultStore
from .daily_price_policy import catalog_price_policies

MIGRATIONS = ("""
CREATE TABLE research_daily_results (
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), owner_id TEXT NOT NULL REFERENCES users(user_id),
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id), ticker TEXT NOT NULL, provider TEXT NOT NULL,
 strategy TEXT NOT NULL, result_json TEXT NOT NULL, result_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX research_daily_owner ON research_daily_results(owner_id,created_at);
""",)


class CollectionContext:
    """Keep original authority/control identity; only map monotonically progress."""
    def __init__(self, context, *, store=None, principal=None):
        self.context = context
        self.run, self.job_id = context.run, context.job_id
        self.worker_id, self.fencing_token = context.worker_id, context.fencing_token
        self.principal=principal
        self.store=store
        self._progress=5

    @property
    def storage_binding(self):
        return self.context.storage_binding

    def require_active(self):
        self.context.require_active()

    def assert_fenced(self, con):
        self.context.assert_fenced(con)

    def checkpoint(self, progress, metadata=None):
        if type(progress) is not int or not 0 <= progress <= 99:
            raise DailyError("DAILY_COLLECTION_PROGRESS_INVALID")
        self._progress=max(self._progress,5+progress*30//99)
        self.context.checkpoint(self._progress, {"stage": "COLLECTING_DATA", "collection": metadata or {}})

class DailyBacktestService:
    def __init__(self, runtime, *, adapter=None, runner=None, gateway=None):
        self.runtime, self.database = runtime, runtime.database
        if adapter is None:
            from finance_platform.data.provider_sdk.bootstrap import runtime_components
            from finance_platform.data.daily_observations import DailyObservationAdapter
            data,runner=runtime_components(runtime)
            adapter = DailyObservationAdapter(data)
        self.adapter = adapter
        self.runner = runner
        if gateway is None:
            from finance_platform.data.history_gateway import HistoryGateway
            gateway=HistoryGateway(adapter.service,runner)
        self.gateway=gateway
        self.database.apply_module_migrations("research_daily", MIGRATIONS)
        with self.database.transaction() as con:
            immutable(con, "research_daily_results", ("run_id",))
        self.store=DailyResultStore(runtime,gateway=gateway)

    def catalog(self, principal, query=""):
        self.runtime.require_owner(principal, principal.user_id)
        data = self.adapter.catalog(query=query)
        policies=catalog_price_policies()
        return {"items": data["items"], "providers": data["providers"], "strategies": list(STRATEGIES), "defaults": dict(DEFAULTS),
                "provider_labels": {**data.get("provider_labels",{"yfinance":"拆股调整价格模拟（不含分红）"}),"akshare":policies["akshare"]["display_name"]},
                "provider_price_policies":policies,
                "limits": {"max_calendar_days": min(MAX_CALENDAR_DAYS,data.get("max_calendar_days",MAX_CALENDAR_DAYS)), "max_rows": min(MAX_ROWS,data.get("max_rows",MAX_ROWS)), "start_inclusive": True, "end_inclusive": True, "earliest_start": data.get("earliest_start", "1900-01-02"), "last_allowed_end_date": (datetime.now(timezone.utc).date()-timedelta(days=1)).isoformat()},
                "data_family": DATA_FAMILY, "result_family": RESULT_FAMILY, "assumptions": list(ASSUMPTIONS.values()),
                "engine":dict(ENGINE),"strategy_version":STRATEGY_VERSION,"calculation_version":CALCULATION_VERSION,
                "cost_model":{"spread_bps":"LONG_ENTRY_PRICE_MARKUP_RAW_OPEN_EXIT","legacy_slippage":"REJECTED"},
                "identity_basis": data["identity_basis"]}

    def request(self, value):
        request = DailyRequest.parse(value)
        catalog = self.adapter.catalog()
        # Suggestions are not identity authority; data-side fixed-origin exact
        # identity validation runs in the worker for any legal typed ticker.
        if request.start_date < catalog.get("earliest_start", "1900-01-02"):
            raise DailyError("DAILY_CATALOG_WINDOW_UNSUPPORTED")
        return request

    @staticmethod
    def definition(request):
        strategy = next(s for s in STRATEGIES if s["id"] == request.strategy)
        return {"family": RESULT_FAMILY, "engine_version": ENGINE_VERSION, "engine":dict(ENGINE), "strategy_version":STRATEGY_VERSION, "calculation_version":CALCULATION_VERSION, "strategy": strategy, "assumptions": dict(ASSUMPTIONS)}

    def submit(self, principal, value, *, idempotency_key=None, cache_policy="use"):
        if cache_policy not in ("use", "bypass"):
            raise DailyError("DAILY_CACHE_POLICY_INVALID")
        self.runtime.require_owner(principal, principal.user_id, write=True)
        request = self.request(value)
        if request.history_id is not None:
            # Repeating a timeout submission without an explicit version keeps
            # its originally frozen version even if latest has since refreshed.
            if request.history_version is None and idempotency_key:
                with closing(self.database.connect()) as con:
                    old=con.execute("SELECT j.parameters_json FROM job_keys k JOIN jobs j ON j.job_id=k.job_id WHERE k.owner_id=? AND k.idempotency_key=? AND j.handler='research.daily_backtest'",(principal.user_id,idempotency_key)).fetchone()
                if old:
                    old_request=json.loads(old["parameters_json"])["request"]
                    if old_request.get("history_id")==request.history_id:
                        request=replace(request,history_version=old_request.get("history_version"))
            frozen=self.gateway.freeze_reference(principal.user_id,request.history_id,request.history_version)
            request=replace(request,history_version=frozen["version"])
        definition = self.definition(request)
        # No source/dataset/network effects here. Immutable local built-in ref
        # is stable for identical requests and cannot race with another tenant.
        ref = self.runtime.versions.freeze(principal, "daily-strategy:"+request.strategy+":"+principal.user_id, ENGINE_VERSION, "strategy", definition)
        parameters = {"request": request.payload(), "strategy_reference": payload(ref)}
        if cache_policy == "bypass":
            parameters["cache_policy"] = cache_policy
        return self.runtime.jobs.submit(principal, "research.daily_backtest", parameters, mode=RunMode.BACKTEST, idempotency_key=idempotency_key)

    def submission(self, principal, idempotency_key):
        try:
            identifier(idempotency_key)
        except ValueError:
            raise DailyError("DAILY_IDEMPOTENCY_KEY_INVALID") from None
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT k.job_id FROM job_keys k JOIN jobs j ON j.job_id=k.job_id WHERE k.owner_id=? AND k.idempotency_key=? AND j.handler='research.daily_backtest'", (principal.user_id, idempotency_key)).fetchone()
        if row is None:
            raise PlatformError("NOT_FOUND", 404)
        return self.runtime.jobs.get(principal, row["job_id"])

    def resolve_bindings(self, principal, parameters):
        request = self.request(parameters["request"])
        reference = VersionRef(**parameters["strategy_reference"])
        if self.runtime.versions.get(principal, reference) != self.definition(request):
            raise DailyError("DAILY_STRATEGY_BINDING_MISMATCH")
        if request.history_id is not None:
            if request.history_version is None:raise DailyError("DAILY_HISTORY_REFERENCE_INVALID")
            # JobService invokes this resolver inside BEGIN IMMEDIATE. The
            # gateway's freeze_reference has its own writer transaction, so
            # only its read-only active-version summary may be called here.
            # The outer writer serializes history deletion until job commit;
            # afterward data lifecycle sees the queued explicit reference.
            selected=self.gateway.summary(principal.user_id,request.history_id,request.history_version)
            if selected["version"]!=request.history_version or selected["provider"]!=request.provider or selected["ticker"]!=request.ticker:
                raise DailyError("DAILY_HISTORY_BINDING_MISMATCH")
        return {"strategy": parameters["strategy_reference"], "seed": 0, "model_reason": "Built-in deterministic daily strategy; no model"}

    def principal(self, owner_id):
        with closing(self.database.connect()) as con:
            user = con.execute("SELECT * FROM users WHERE user_id=?", (owner_id,)).fetchone()
        if user is None or not user["enabled"]:
            raise PlatformError("FORBIDDEN", 403)
        principal = Principal(user["user_id"], user["username"], user["role"])
        self.runtime.require_owner(principal, owner_id, write=True)
        return principal

    def execute(self, context, parameters):
        principal = self.principal(context.run.owner_id)
        request = self.request(parameters["request"])
        if self.runtime.versions.get(principal, context.run.config) != parameters or payload(context.run.strategy) != parameters["strategy_reference"]:
            raise DailyError("DAILY_RUN_BINDING_MISMATCH")
        self.resolve_bindings(principal, parameters)
        context.require_active()
        if context.run.parent_run_id is not None:
            # Existing SDK forbids descendant acquisition reset. An explicit
            # new user submission (new idempotency key) is required instead.
            raise DailyError("SDK_RETRY_REQUIRES_NEW_COLLECTION")
        context.checkpoint(2, {"stage": "PREPARING_DATA"})
        acquisition_context=CollectionContext(context,store=self.store,principal=principal)
        acquired=self.gateway.obtain(acquisition_context,request)
        context.require_active()
        observation=acquired
        observation_ref=acquired["observation_ref"]
        source_hash=observation_ref["content_hash"]
        if request.history_version is not None and request.history_version!=acquired["version"]:
            raise DailyError("DAILY_HISTORY_VERSION_MISMATCH")
        history_reference={"history_id":acquired["history_id"],"version":acquired["version"],"observation_hash":source_hash,
                           "history_content_hash":acquired["history_content_hash"],"selection_hash":observation["content_hash"],"observation_ref":observation_ref}
        context.checkpoint(40, {"stage": "VALIDATING_DATA"})
        from .daily_price_policy import validate_price_policy
        price_policy=validate_price_policy(acquired,request)
        series = DailySeries.from_history(observation, request)
        if series.history.get("data_quality") is not None:
            history_reference["data_quality"]=series.history["data_quality"]
        value = series.payload()
        encoded = safe_json(value)
        import hashlib
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        data_ref = VersionRef("daily-data:"+context.run.run_id+":"+principal.user_id, "1", digest)
        with self.database.transaction() as con:
            context.assert_fenced(con)
            self.principal(principal.user_id)
            self.runtime.versions._store(con, principal.user_id, data_ref.resource_id, data_ref.version, "data", encoded, digest)
        context.checkpoint(45, {"stage": "SIMULATING", "rows": len(series.bars)})
        from .research_cache import run_daily
        result, disposition = run_daily(
            self.runtime, context, series, parameters.get("cache_policy", "use"),
            lambda: simulate(series, active=context.require_active,
                             progress=lambda n: context.checkpoint(n, {"stage": "SIMULATING"})),
        )
        context.checkpoint(95, {"stage": "SAVING_RESULT"})
        result.update(run_id=context.run.run_id, job_id=context.job_id, cache_disposition=disposition,
                      data_reference=payload(data_ref),history_reference=history_reference,
                      coverage={"requested_start":request.start_date,"requested_end":request.end_date,"source":acquired["actual_coverage"],"used_rows":len(series.bars),"actual_first":series.bars[0].session_date,"actual_last":series.bars[-1].session_date},
                      price_policy=price_policy,
                      observation_reference={"result_id": observation_ref["result_id"], "content_hash": source_hash,
                        "source_ref": observation["source_ref"], "observed_received_at": observation["observed_received_at"],
                        "price_policy": observation["price_policy"], "availability_basis": observation["availability_basis"], "historical_first_available_at": None,
                        "raw_compatible": False},
                      lineage={"run": payload(context.run), "actual_data": payload(data_ref), "history":history_reference,"cache":acquired["history_cache"]})
        save_context=CollectionContext(context,store=self.store,principal=principal)
        self.store.save(save_context,result)
        # The generic worker finalizes its own job. API reads join SUCCEEDED;
        # late cancellation/lease loss therefore cannot publish this as success.
        compact = {k: v for k, v in result["report"].items() if k not in {"equity_curve", "trades", "rejections"}}
        return {"run_id": context.run.run_id, "family": RESULT_FAMILY, "result_url": "/api/v1/research/daily/results/"+context.run.run_id,
                "report": compact, "cache_disposition": disposition}

    def result(self,principal,run_id,*,include_deleted=False):
        return self.store.result(principal,run_id,include_deleted=include_deleted)

    def series(self,principal,run_id,kind="equity_curve",limit=500,offset=0,*,include_deleted=False):
        return self.store.series(principal,run_id,kind,limit,offset,include_deleted=include_deleted)

    def lifecycle(self,principal,run_id,**parameters):
        return self.store.lifecycle(principal,run_id,**parameters)

    def list_results(self,principal,limit=50,offset=0,state="active"):
        return self.store.list(principal,limit,offset,state)
