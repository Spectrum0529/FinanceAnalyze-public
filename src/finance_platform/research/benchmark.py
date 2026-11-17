"""Fixed initial allocation buy-and-hold, actually replayed with identical costs."""

from datetime import timedelta
from decimal import Decimal
from finance_platform.core.contracts import StrategyOutput, OutputKind, RunMode, payload
from finance_platform.trading.accounting import floor_step, decimal
from .backends import EventBackend


def execute_benchmark(sample, boundary, *, job_context=None,actions=()):
    def provider(context, event_type):
        state = payload(context.state)
        if state.get("issued") or not context.data_view["rows"]:
            return (), state
        outputs = []
        rows = context.data_view["rows"]
        for i, row in enumerate(rows):
            price = decimal(row["close"])
            quantity = floor_step(decimal(boundary.initial_cash)*Decimal("0.10")/len(rows)/price,
                                  boundary.instruments[row["instrument_id"]].lot_size)
            outputs.append(StrategyOutput("benchmark-"+str(i), row["instrument_id"], OutputKind.TARGET_QUANTITY,
                           quantity, "shares", context.as_of, context.as_of, context.as_of+timedelta(days=7), RunMode.BACKTEST))
        return tuple(outputs), {"issued": True}

    class ActiveOnly:
        def require_active(self):
            job_context.require_active()
        def checkpoint(self, progress, metadata=None):
            job_context.require_active()

    result = EventBackend()._replay(sample, boundary, provider, {}, job_context=ActiveOnly() if job_context else None,actions=actions)
    result["benchmark_policy"] = "fixed first-decision 10% allocation; remaining cash; no rebalance"
    return result
