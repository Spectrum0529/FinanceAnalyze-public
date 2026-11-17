"""Registration in the existing queue; no automatic repeated SDK acquisition."""
from finance_platform.core.contracts import RunMode, VersionRef
from finance_platform.core.errors import PlatformError
from finance_platform.data.contracts import DataError
from .daily import DailyError
from .daily_service import DailyBacktestService


def validate(parameters):
    if (type(parameters) is not dict
            or set(parameters) - {"cache_policy"} != {"request", "strategy_reference"}
            or parameters.get("cache_policy", "use") not in ("use", "bypass")):
        raise PlatformError("INVALID_INPUT", 422)
    try:
        VersionRef(**parameters["strategy_reference"])
    except (ValueError, TypeError):
        raise PlatformError("INVALID_INPUT", 422) from None


def register_handlers(registry, runtime, *, service=None):
    service = service or DailyBacktestService(runtime)

    def execute(context, parameters):
        try:
            return service.execute(context, parameters)
        except (DailyError, DataError) as error:
            raise PlatformError(error.code, 422) from None

    registry.register("research.daily_backtest", execute, validate=validate, recoverable=False, auto_recovery_limit=0,
                      allowed_modes=(RunMode.BACKTEST,), resolve_versions=service.resolve_bindings)
