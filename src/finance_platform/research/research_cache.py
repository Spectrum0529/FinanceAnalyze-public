"""Eligibility and immutable identities for trusted, already-authorized research tools."""
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
import platform

from finance_platform.core.contracts import fingerprint
from finance_platform.core.source_binding import file_digest
from .daily import DailyError, ENGINE, RESULT_FAMILY
from .portfolio import FAMILY, digest, fail
from .task_plans import FACTORS, execute_plan


BACKTEST_STRATEGIES = frozenset(("buy_hold", "sma20", "sma_cross_5_20"))


def validate_policy(value):
    if type(value) is not str or value not in ("use", "bypass"):
        fail("RESEARCH_CACHE_POLICY_INVALID")
    return value


def _engine_identity(*modules, backtest):
    packages = ["pandas", "numpy"]
    sources = {}
    for name in ("research_cache", *modules):
        spec = find_spec(f"{__package__}.{name}")
        sources[name] = file_digest(Path(spec.origin))
    return digest({
        "python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
        "libraries": {name: version(name) for name in packages},
        "execution": ENGINE if backtest else None,
        "sources": sources,
    })


def _run(runtime, context, *, kind, policy, identify, compute, validate, eligible=True):
    context.require_active()
    cache = getattr(runtime, "computation_cache", None)
    if not eligible:
        return compute(), "ineligible"
    if cache is None:
        return compute(), "disabled"
    identity = identify()
    if identity is None:
        return compute(), "ineligible"
    result = cache.run_manual(context, kind=kind, identity=identity, compute=compute,
                              validate=validate, cache_policy=policy)
    context.require_active()
    return result.value, result.disposition


def run_daily(runtime, context, series, policy, compute):
    request = series.request
    eligible = (request.history_id is not None and request.history_version is not None
                and request.history_policy == "reuse_or_download"
                and request.strategy in BACKTEST_STRATEGIES and bool(series.history))
    sample_hash = fingerprint(series.payload()) if eligible else None

    def identify():
        return {
            "input_sha256": sample_hash,
            "computation_sha256": digest({"strategy": request.payload(), "engine": ENGINE,
                                         "seed": 0, "fills": False, "window": "FIXED_OBSERVED_DAILY"}),
            "engine_sha256": _engine_identity("daily", "daily_backtesting", "daily_price_policy",
                                              backtest=True),
        }

    def validate(value):
        if (type(value) is not dict or value.get("family") != RESULT_FAMILY
                or value.get("request") != request.payload()
                or type(value.get("report")) is not dict or value["report"].get("sample_hash") != sample_hash
                or type(value.get("equity_curve")) is not list or len(value["equity_curve"]) != len(series.bars)
                or type(value.get("trades")) is not list or type(value.get("full_accounting")) is not dict
                or type(value.get("final")) is not dict):
            raise DailyError("DAILY_CACHE_RESULT_BINDING_MISMATCH")

    return _run(runtime, context, kind="exact_backtest", policy=validate_policy(policy), identify=identify,
                compute=compute, validate=validate, eligible=eligible)


def plan_eligible(kind, parameters):
    return (kind == "factor_validation" and parameters.get("factor") in FACTORS
            or kind == "strategy_validation" and parameters.get("strategy") in BACKTEST_STRATEGIES
            and not parameters.get("benchmark_id"))


def run_plan(runtime, context, dataset, plan, progress):
    def compute():
        return execute_plan(dataset, plan, active=context.require_active, progress=progress)

    if not plan_eligible(plan["kind"], plan["parameters"]) or "benchmark_reference" in plan:
        return compute(), "ineligible"
    reference = {name: dataset[name] for name in ("dataset_id", "version", "content_hash")}
    if (reference != plan.get("dataset_reference")
            or digest({k: v for k, v in plan.items() if k != "plan_hash"}) != plan.get("plan_hash")):
        fail("TASK_PLAN_BINDING_MISMATCH")

    backtest = plan["kind"] == "strategy_validation"
    parameters = plan["parameters"]

    def identify():
        # Explicit allowlist excludes run/job, storage locators, irrelevant
        # acceptance refs and every model/context field.
        rows = dataset["rows"]
        if len(rows) > 100000:
            return None
        evidence = {name: dataset.get(name) for name in (
            "scope", "source", "metadata", "universe", "start", "end", "window",
            "available_at", "published_at", "pit_status", "input_content_hash",
            "goal_data_provenance", "security_metadata", "dividends", "splits",
        )}
        from .adjusted_portfolio import basis_key

        evidence.update(reference=reference, rows_sha256=digest(rows), adjusted_basis=basis_key(dataset))
        if dataset.get("history_views"):
            evidence["fixed_history_views"] = {
                ticker: {name: view.get(name) for name in (
                    "history_id", "version", "history_content_hash", "content_hash", "price_policy",
                    "corporate_action_basis", "price_transform", "quantity_basis", "availability_basis",
                    "observed_received_at", "source_ref", "data_quality",
                )} for ticker, view in dataset["history_views"].items()
            }
        return {"input_sha256": digest(evidence),
                "computation_sha256": digest({"kind": plan["kind"], "parameters": parameters,
                                             "scope": "OBSERVED_DEVELOPMENT", "seed": 0}),
                "engine_sha256": _engine_identity("task_plans", "portfolio", "daily", "daily_backtesting",
                                                  "adjusted_portfolio", "daily_price_policy", backtest=backtest)}

    def validate(value):
        if type(value) is not dict:
            fail("RESEARCH_CACHE_RESULT_BINDING_MISMATCH")
        if backtest:
            if (value.get("family") != FAMILY or value.get("dataset_reference") != reference
                    or value.get("request") != parameters or type(value.get("reconciliation")) is not dict
                    or any(type(value.get(name)) is not list for name in ("equity_curve", "fills", "engine_runs"))):
                fail("RESEARCH_CACHE_RESULT_BINDING_MISMATCH")
        elif (value.get("family") != "factor-validation-result-1" or value.get("factor") != parameters["factor"]
              or type(value.get("rows")) is not list or type(value.get("rank_ic")) is not list
              or type(value.get("sample_rows")) is not int or value["sample_rows"] < len(value["rows"])
              or value.get("tool") != "factor_validation"):
            fail("RESEARCH_CACHE_RESULT_BINDING_MISMATCH")

    return _run(runtime, context, kind="exact_backtest" if backtest else "trusted_indicator",
                policy=validate_policy(plan.get("cache_policy", "use")), identify=identify,
                compute=compute, validate=validate)
