"""Exact account summaries with explicit insufficient-data metric reasons."""

from decimal import Decimal
import math

from .data import ResearchError
from finance_platform.trading.accounting import decimal, money, ZERO


def report(result, *, benchmark=None):
    curve = result["equity_curve"]
    valued = [decimal(p["equity"]) for p in curve if p["equity"] is not None]
    if not valued or valued[0] <= 0:
        raise ResearchError("REPORT_EQUITY_UNAVAILABLE")
    high, drawdown = valued[0], ZERO
    for equity in valued:
        high = max(high, equity)
        drawdown = max(drawdown, 1-equity/high)
    gross = sum((money(decimal(f["quantity"])*decimal(f["price"])) for f in result["fills"]), ZERO)
    average = sum(valued)/len(valued)
    fees = decimal(result["final"]["fees"])
    final_equity = result["final"]["equity"]
    return_value = None if final_equity is None else decimal(final_equity)/valued[0]-1
    missing = len(curve)-len(valued)
    dates = {p["at"][:10] for p in curve}
    metric_reason = "Synthetic/development engineering sample; fewer than 20 sessions or no approved statistical evaluation protocol"
    benchmark_value = None
    if benchmark is not None:
        if benchmark["sample_hash"] != result["sample_hash"] or benchmark["semantics"] != result["semantics"] or benchmark["assumptions"] != result["assumptions"] or benchmark.get("action_hash")!=result.get("action_hash"):
            raise ResearchError("BENCHMARK_NOT_COMPARABLE")
        benchmark_curve = [decimal(p["equity"]) for p in benchmark["equity_curve"] if p["equity"] is not None]
        benchmark_return=None if benchmark["final"]["equity"] is None or not benchmark_curve else str(decimal(benchmark["final"]["equity"])/benchmark_curve[0]-1)
        benchmark_value = {"name": "fixed initial 10% allocation then hold; remaining cash", "net_return":benchmark_return,
                           "fees": benchmark["final"]["fees"], "fill_count": len(benchmark["fills"]),
                           "cost_status": "SAME_SCENARIO_ASSUMPTIONS","valuation_status":"UNKNOWN" if benchmark_return is None else "AVAILABLE"}
    return {"status": "INSUFFICIENT_EVIDENCE", "mode": "BACKTEST", "scope": result["scope"],
            "sample_hash": result["sample_hash"], "net_return": None if return_value is None else str(return_value),
            "max_drawdown": str(drawdown), "turnover": str(gross/average), "gross_traded_notional": str(gross),
            "fees": str(fees), "trade_count": len(result["fills"]), "order_count": len(result["orders"]),
            "risk": {"annualized_volatility": None, "sharpe": None, "alpha": None, "reason": metric_reason},
            "coverage": {"sessions": len(dates), "valuation_points": len(curve), "missing_valuations": missing,
                         "rejections": len(result["rejections"]), "unsubmitted_outputs": len(result["unsubmitted_outputs"]),
                         "pending_orders": sum(o["status"] not in {"filled", "cancelled", "expired", "rejected", "replaced"} for o in result["final"]["orders"]),
                         "market_coverage": "NOT_CERTIFIED_SYNTHETIC_OR_DEVELOPMENT"},
            "execution_skips":result.get("execution_skips",[]),
            "benchmark": benchmark_value, "assumptions": result["assumptions"],
            "equity_curve": curve, "trades": result["fills"], "rejections": result["rejections"],
            "decision": "工程路径已运行；这些数据不足以证明可靠增量或批准实盘。"}
