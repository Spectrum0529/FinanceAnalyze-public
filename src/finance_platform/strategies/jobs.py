"""Public external session handlers on the same core registry and durable queue."""
from finance_platform.core.contracts import VersionRef, RunMode
from finance_platform.core.errors import PlatformError
from finance_platform.trading.accounting import TradingError
from .contracts import StrategyError
from .sessions import ExternalSessions
from .simulation import LocalSimulation
from finance_platform.research.data import ResearchError


def register_handlers(registry, runtime):
    sessions = ExternalSessions(runtime)
    simulation = LocalSimulation(runtime)

    def validate_start(params):
        if not isinstance(params, dict) or set(params) != {"instance_id", "account_id", "sample_reference", "strategy_reference", "clock_mode", "requested_generation","expected_instance_updated_at"}:
            raise PlatformError("INVALID_INPUT", 422)
        try:
            VersionRef(**params["sample_reference"])
            VersionRef(**params["strategy_reference"])
        except (ValueError, TypeError):
            raise PlatformError("INVALID_INPUT", 422) from None

    def validate_consume(params):
        if not isinstance(params, dict) or set(params) != {"instance_id", "message_hash"} or not isinstance(params["message_hash"], str) or len(params["message_hash"]) != 64:
            raise PlatformError("INVALID_INPUT", 422)

    def activate(context, parameters):
        try:
            return sessions.activate(context, parameters)
        except (StrategyError,ResearchError) as error:
            raise PlatformError(str(error), 422) from None
        except TradingError as error:
            raise PlatformError(error.code, 422) from None

    def consume(context, parameters):
        try:
            return sessions.consume(context, parameters)
        except (StrategyError,ResearchError) as error:
            raise PlatformError(str(error), 422) from None
        except TradingError as error:
            raise PlatformError(error.code, 422) from None

    def validate_simulation(parameters):
        if not isinstance(parameters,dict) or set(parameters)!={"instance_id","account_id","sample_reference","strategy_reference","model_id","requested_generation","expected_instance_updated_at"}:
            raise PlatformError("INVALID_INPUT",422)

    def simulate(context,parameters):
        try:
            return simulation.execute(context,parameters)
        except (StrategyError,ResearchError) as error:
            raise PlatformError(str(error),422) from None
        except TradingError as error:
            raise PlatformError(error.code,422) from None

    registry.register("strategy.external.activate", activate, validate=validate_start, resolve_versions=sessions.resolve_activation,
                      recoverable=False, allowed_modes=(RunMode.LOCAL_SIMULATION,))
    registry.register("strategy.external.consume", consume, validate=validate_consume, resolve_versions=sessions.resolve_consumption,
                      recoverable=False, allowed_modes=(RunMode.LOCAL_SIMULATION,))
    registry.register("strategy.simulation",simulate,validate=validate_simulation,resolve_versions=simulation.resolve,
                      recoverable=False,allowed_modes=(RunMode.LOCAL_SIMULATION,))
    sessions.recover_staged()
