"""Handlers registered in core's API/worker registry; no private scheduler."""
from .payload_storage import persist as store_payload, hydrate as load_payload, task_binding

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from finance_platform.core.contracts import VersionRef, payload, parse_instant, parse_decimal, RunMode
from finance_platform.core.errors import PlatformError
from finance_platform.strategies.contracts import StrategyError
from finance_platform.trading.accounting import TradingError
from .data import ResearchError
from .model import LinearPipeline, ModelRow, TrainingWindow
from .service import ResearchService


def _validate_backtest(params):
    if not isinstance(params, dict) or set(params) != {"instance_id", "sample_reference", "backend", "model_id", "strategy_reference"} or params["backend"] not in {"event", "batch"}:
        raise PlatformError("INVALID_INPUT", 422)
    try:
        VersionRef(**params["sample_reference"])
        VersionRef(**params["strategy_reference"])
    except (ValueError, TypeError):
        raise PlatformError("INVALID_INPUT", 422) from None


def _validate_train(params):
    if not isinstance(params, dict) or set(params) != {"rows_reference", "window", "seed"}:
        raise PlatformError("INVALID_INPUT", 422)
    try:
        VersionRef(**params["rows_reference"])
        TrainingWindow(params["window"]["kind"], *(parse_instant(params["window"][k]) for k in ("train_start", "train_cutoff", "prediction_start", "prediction_end")))
    except (KeyError, ValueError, TypeError):
        raise PlatformError("INVALID_INPUT", 422) from None
    if type(params["seed"]) is not int or not 0 <= params["seed"] < 2**32:
        raise PlatformError("INVALID_INPUT", 422)


def register_handlers(registry, runtime):
    from .daily_jobs import register_handlers as register_daily_handlers
    from .daily_service import DailyBacktestService
    daily=DailyBacktestService(runtime)
    runtime.debate_daily_result_store=daily.store
    runtime.debate_history_gateway=daily.gateway
    register_daily_handlers(registry, runtime,service=daily)
    service = ResearchService(runtime)

    def backtest(context, parameters):
        try:
            return service.execute(context, parameters)
        except (ResearchError, StrategyError) as exc:
            raise PlatformError(str(exc), 422) from None
        except TradingError as exc:
            raise PlatformError(exc.code, 422) from None

    def train(context, parameters):
        principal = service.principal(context.run.owner_id)
        if payload(context.run.data) != parameters["rows_reference"] or runtime.versions.get(principal, context.run.config) != parameters:
            raise ResearchError("MODEL_RUN_BINDING_MISMATCH")
        raw = runtime.versions.get(principal, context.run.data)
        if set(raw) != {"scope", "rows"} or raw["scope"] not in {"SYNTHETIC", "DEVELOPMENT"}:
            raise ResearchError("MODEL_DATA_SCOPE_INVALID")
        context.require_active()
        service.archive.begin(context.run.run_id, context.run, storage_binding=task_binding(context), data=raw, configuration=parameters,
                              code_paths=sorted((runtime.root/"src").rglob("*.py")), dependency_bytes=(runtime.root/"uv.lock").read_bytes())
        try:
            return fit_and_publish(context, parameters, principal, raw)
        except Exception as error:
            code = error.code if isinstance(error, PlatformError) else str(error) if isinstance(error, ResearchError) else "MODEL_TRAINING_FAILED"
            service.archive.fail(context.run.run_id, code)
            raise

    def fit_and_publish(context, parameters, principal, raw):
        rows = tuple(ModelRow(r["row_id"], *(parse_instant(r[k]) for k in ("prediction_at", "feature_available_at", "label_mature_at")),
                              tuple(parse_decimal(x) for x in r["features"]), None if r["label"] is None else parse_decimal(r["label"]), r["scope"], r["role"]) for r in raw["rows"])
        window = TrainingWindow(parameters["window"]["kind"], *(parse_instant(parameters["window"][k]) for k in ("train_start", "train_cutoff", "prediction_start", "prediction_end")))
        artifact = LinearPipeline().fit(rows, window, seed=parameters["seed"])
        context.checkpoint(80, {"training_rows": len(artifact["training_rows"]), "pipeline": artifact["pipeline"]})
        # The actual numerical inference path executes before publishing the model.
        inference_rows = tuple(r for r in rows if window.prediction_start <= r.prediction_at <= window.prediction_end)
        if not inference_rows:
            raise ResearchError("MODEL_INFERENCE_SAMPLE_REQUIRED")
        predictions = LinearPipeline().infer(artifact, inference_rows)
        context.require_active()
        saved = service.save_model(principal, artifact, context.run.run_id, storage_binding=task_binding(context))
        result = {"model_id": saved["model_id"], "reference": saved["reference"], "model_hash": saved["model_hash"],
                "train_cutoff": saved["train_cutoff"], "pipeline": artifact["pipeline"], "predictions": predictions,
                "scope": "DEVELOPMENT_ONLY", "status": "INSUFFICIENT_EVIDENCE"}
        service.archive.finish(context.run.run_id, {"result": result, "artifact": artifact, "lineage": payload(context.run)})
        return result

    def resolve_train(principal, parameters):
        raw = runtime.versions.get(principal, VersionRef(**parameters["rows_reference"]))
        if not isinstance(raw, dict) or set(raw) != {"scope", "rows"} or raw["scope"] not in {"SYNTHETIC", "DEVELOPMENT"}:
            raise ResearchError("MODEL_DATA_SCOPE_INVALID")
        return {"data": parameters["rows_reference"], "seed": parameters["seed"], "model_reason": "Training a new deterministic development model"}

    def validate_search(parameters):
        if not isinstance(parameters, dict) or set(parameters) != {"instance_id", "sample_reference", "strategy_reference", "space", "budget"} or not isinstance(parameters["space"], list) or type(parameters["budget"]) is not int or not 1 <= len(parameters["space"]) <= parameters["budget"] <= 16:
            raise PlatformError("INVALID_INPUT", 422)

    def search(context, parameters):
        try:
            return service.execute_search(context, parameters)
        except ResearchError as error:
            raise PlatformError(str(error), 422) from None

    registry.register("research.backtest", backtest, validate=_validate_backtest, recoverable=True, allowed_modes=(RunMode.BACKTEST,), resolve_versions=service.resolve_bindings)
    registry.register("research.train", train, validate=_validate_train, recoverable=True, allowed_modes=(RunMode.BACKTEST,), resolve_versions=resolve_train)
    registry.register("research.search", search, validate=validate_search, recoverable=True, allowed_modes=(RunMode.BACKTEST,), resolve_versions=service.resolve_search)
    register_native_handlers(registry,runtime,service=service)


def register_native_handlers(registry,runtime,*,service=None):
    """Same native registrations, independently testable without SDK bootstrap."""
    service=service or ResearchService(runtime)
    from .qlib_service import validate_parameters as validate_qlib_parameters
    def qlib_workflow(context,parameters):
        try:return service.execute_qlib(context,parameters)
        except ValueError as error:raise PlatformError(getattr(error,"code","QLIB_NATIVE_FAILED"),422) from None
    registry.register("research.qlib",qlib_workflow,validate=validate_qlib_parameters,recoverable=True,
                      allowed_modes=(RunMode.BACKTEST,),resolve_versions=service.resolve_qlib)
    from .debate_service import validate_parameters as validate_debate_parameters
    def debate(context,parameters):
        try:return service.debate_service().execute(context,parameters)
        except ValueError as error:raise PlatformError(getattr(error,"code","TA_NATIVE_FAILED"),422) from None
    registry.register("research.debate",debate,validate=validate_debate_parameters,recoverable=True,
                      allowed_modes=(RunMode.BACKTEST,),resolve_versions=lambda principal,parameters:service.debate_service().resolve(principal,parameters))


def submit_training_example(service, principal, *, kind="expanding", seed=0):
    if kind not in {"rolling", "expanding"}:
        raise ResearchError("TRAINING_WINDOW_INVALID")
    at = datetime(2026, 9, 1, 14, tzinfo=timezone.utc)
    rows = [ModelRow("train-"+str(i), at+timedelta(days=i), at+timedelta(days=i)-timedelta(minutes=1),
                     at+timedelta(days=i, hours=1), (Decimal(i),), Decimal(2*i+1)) for i in range(5)]
    # Labels of inference rows are absent and cannot accidentally join fit.
    rows.append(ModelRow("infer-0", datetime(2026, 10, 1, 14, tzinfo=timezone.utc), datetime(2026, 10, 1, 13, tzinfo=timezone.utc),
                         datetime(2026, 10, 2, 14, tzinfo=timezone.utc), (Decimal(10),), None, role="TEST"))
    window = TrainingWindow(kind, rows[0].prediction_at-timedelta(seconds=1), rows[4].label_mature_at,
                            datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc), datetime(2026, 10, 20, 20, tzinfo=timezone.utc))
    data = service.runtime.versions.freeze(principal, "model-training:"+principal.user_id, "1", "data", {"scope": "SYNTHETIC", "rows": payload(rows)})
    return service.runtime.jobs.submit(principal, "research.train", {"rows_reference": payload(data), "window": payload(window), "seed": seed},
                 mode=RunMode.BACKTEST)
