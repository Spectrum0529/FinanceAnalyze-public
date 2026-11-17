"""Real event scheduling and NumPy batch signal computation, shared execution semantics."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
import hashlib
import numpy as np

from finance_platform.core.contracts import (StrategyContext, StrategyOutput, RunMode, OutputKind,
                                            payload, fingerprint)
from finance_platform.strategies.contracts import digest
from finance_platform.strategies.templates import SOURCES
from .data import ResearchError


SEMANTICS = {"version": "next-observed-print-3", "bar_decision": "available_at>=bar_end",
             "submission": "first_regular_session_with_fresh_market",
             "fill": "separate_observed_tick_strictly_after_effective_and_actual_acceptance",
             "daily_liquidity": "explicit_tick_volume_never_full_daily_bar_volume_at_open",
             "settlement": "shared_trading_T+1", "fee": "shared_trading_per_order_minimum",
             "actions":"before_same_clock_prints_via_shared_trading_bridge",
             "processing_clock":"shared_account_execution_1_no_unknown_past_effects"}


class EventBackend:
    name, version = "platform-event", "1.0"

    def run(self, sample, boundary, worker, package_hash, parameters, *, timers=(), state=None,
            model=None, job_context=None, checkpoint=None, mode=RunMode.BACKTEST, resume=None,actions=()):
        result = self._replay(sample, boundary, lambda context, event: worker.execute(
                            package_hash, context, event_type=event, trusted=True), parameters,
                            timers=timers, state=state, model=model, job_context=job_context, checkpoint=checkpoint, mode=mode, resume=resume,actions=actions)
        result.update({"strategy_hash": package_hash, "parameters_hash": fingerprint(parameters)})
        return result

    def _replay(self, sample, boundary, provider, parameters, *, timers=(), state=None,
                model=None, job_context=None, checkpoint=None, mode=RunMode.BACKTEST, resume=None,actions=()):
        from .actions import dispatch_at,apply
        timeline = defaultdict(lambda: {"ticks": [], "bars": [], "timers": 0,"actions":[]})
        for action in actions:
            timeline[dispatch_at(action)]["actions"].append(action)
        for tick in sample.ticks:
            # Observation latency is honest: the broker still requires exchange
            # print time > order effective time, so delayed old prints cannot fill.
            timeline[tick.available_at]["ticks"].append(tick)
        for bar in sample.bars:
            timeline[bar.available_at]["bars"].append(bar)
        for timer in timers:
            timeline[timer]["timers"] += 1
        visible, pending, state = {}, {}, dict(state or {})
        resumed_at = None
        if resume and resume.get("processed_at"):
            from finance_platform.core.contracts import parse_instant, parse_decimal
            resumed_at = parse_instant(resume["processed_at"])
            state = dict(resume.get("state", state))
            for bar in sorted(sample.bars, key=lambda b: b.available_at):
                if bar.available_at <= resumed_at:
                    visible[bar.instrument_id] = bar
            for raw in resume.get("pending_outputs", []):
                item = dict(raw)
                item["kind"], item["mode"] = OutputKind(item["kind"]), RunMode(item["mode"])
                item["value"] = parse_decimal(item["value"])
                item["limit_price"] = None if item["limit_price"] is None else parse_decimal(item["limit_price"])
                for field in ("produced_at", "effective_at", "expires_at"):
                    item[field] = parse_instant(item[field])
                output = StrategyOutput(**item)
                pending[output.instrument_id] = output
        curves, decisions, timer_count, skips,action_receipts = [], [], 0, [],[]
        ordered_times = sorted(timeline)
        for index, now in enumerate(ordered_times):
            if resumed_at is not None and now <= resumed_at:
                continue
            if job_context:
                job_context.require_active()
            events = timeline[now]
            for action in sorted(events["actions"],key=lambda a:(a["metadata"]["effective_at"],a["metadata"]["record_id"])):
                receipt=apply(boundary,action,now)
                action_receipts.append({"at":payload(now),"record_hash":fingerprint(action),"receipt":receipt})
            for tick in sorted(events["ticks"], key=lambda x: (x.event_at,x.instrument_id,x.event_id)):
                boundary.on_tick(tick, now)
                ready = [o for o in pending.values() if o.instrument_id == tick.instrument_id and boundary.can_submit((o,), now)]
                for output in ready:
                    if now >= output.expires_at:
                        skips.append({"output_id": output.output_id, "code": "OUTPUT_EXPIRED"})
                    else:
                        boundary.route((output,), now)
                    pending.pop(output.instrument_id, None)
            for bar in events["bars"]:
                current = visible.get(bar.instrument_id)
                if current is None or bar.end > current.end:
                    visible[bar.instrument_id] = bar
            types = (["bar"] if events["bars"] else []) + ["timer"]*events["timers"]
            for event_type in types:
                timer_count += event_type == "timer"
                rows = [visible[i].view() for i in sorted(visible)]
                view = {"rows": rows}
                if model is not None:
                    view["model"] = model
                context = StrategyContext(now, mode, parameters, view, boundary.snapshot(now),
                                          state, tuple(sorted(boundary.instruments)), sample.scope)
                outputs, state = provider(context, event_type)
                for output in outputs:
                    context.validate_output(output)
                    decisions.append(payload(output))
                    if output.instrument_id in pending:
                        skips.append({"output_id": pending[output.instrument_id].output_id, "code": "SUPERSEDED_BEFORE_SUBMISSION"})
                        pending.pop(output.instrument_id)
                if outputs and boundary.can_submit(outputs, now):
                    boundary.route(outputs, now)
                else:
                    pending.update({o.instrument_id: o for o in outputs})
            snapshot = boundary.snapshot(now)
            curves.append({"at": payload(now), "equity": snapshot["equity"], "cash": snapshot["cash"],
                           "fees": snapshot["fees"], "positions": snapshot["positions"]})
            if checkpoint:
                checkpoint({"state": state, "processed_at": payload(now), "processed_events": index+1,
                            "sample_hash": sample.sha256, "pending_outputs": payload(tuple(pending.values()))})
            if job_context:
                job_context.checkpoint(min(99, (index+1)*99//len(ordered_times)), {"processed_events": index+1})
        return {"backend": self.name, "backend_version": self.version, "semantics": SEMANTICS,
                "sample_hash": sample.sha256, "scope": sample.scope.value, "mode": mode.value,
                "execution_mode": "LOCAL_SIMULATION", "decisions": decisions, "orders": boundary.orders,
                "fills": boundary.fills, "ledger": boundary.ledger(), "equity_curve": curves,
                "final": boundary.snapshot(ordered_times[-1]), "state": state,
                "timer_events": timer_count, "processed_events": len(ordered_times),
                "rejections": boundary.rejections, "skips": skips, "unsubmitted_outputs": payload(tuple(pending.values())),
                "action_hash":fingerprint(actions),"actions":action_receipts,
                "execution_skips":boundary.execution_skips,
                "assumptions": {"participation": str(boundary.simulator.participation), "slippage_bps": str(boundary.simulator.slippage_bps),
                                "fees": boundary.fee_assumption, "initial_cash": boundary.initial_cash, "gross_weight": str(boundary.gross_weight),
                                "cost_status": "SCENARIO_ASSUMPTION", "no_alpha_claim": True}}


class NumpyBatchBackend(EventBackend):
    name, version = "numpy-batch", "1.0"

    def run(self, sample, boundary, worker, package_hash, parameters, **kwargs):
        manifest = worker.packages.verify(package_hash)
        source = (worker.packages.path(package_hash)/"strategy.py").read_text(encoding="utf-8")
        if manifest.raw["style"] != "event" or source != SOURCES["event"] or kwargs.get("timers"):
            raise ResearchError("BACKEND_STRATEGY_NOT_COMPATIBLE")
        threshold = parameters.get("threshold")
        if not isinstance(threshold, str):
            raise ResearchError("BATCH_THRESHOLD_REQUIRED")
        # Decimal comparisons are the exact supported semantics. NumPy runs the
        # entire object array computation in one batch; no binary price rounding.
        closes = np.asarray([bar.close for bar in sample.bars], dtype=object)
        values = np.greater_equal(closes, Decimal(threshold)).astype(np.int8)
        lookup = {(bar.instrument_id, payload(bar.end)): str(int(value)) for bar, value in zip(sample.bars, values)}

        def provider(context, event_type):
            request_clock = payload(context.as_of)
            outputs = []
            for i, row in enumerate(payload(context.data_view)["rows"]):
                value = lookup[(row["instrument_id"], row["bar_end"])]
                raw = {"kind": "signal", "instrument_id": row["instrument_id"], "value": value, "decision_at": request_clock}
                outputs.append(StrategyOutput("out-"+digest({"package": package_hash, "clock": request_clock, "index": i, "output": raw})[:32],
                               row["instrument_id"], OutputKind.SIGNAL, Decimal(value), "direction", context.as_of, context.as_of,
                               context.as_of+timedelta(days=7), RunMode.BACKTEST))
            state = payload(context.state)
            state["events"] = state.get("events", 0)+1
            state.setdefault("timers", 0)
            return tuple(outputs), state

        result = self._replay(sample, boundary, provider, parameters, **kwargs)
        result.update({"strategy_hash": package_hash, "parameters_hash": fingerprint(parameters)})
        result["batch_execution"] = {"numpy_version": np.__version__, "rows": len(values), "positive_signals": int(values.sum()),
                                     "algorithm": "numpy.greater_equal Decimal object array"}
        return result

    @staticmethod
    def cross_section(sample):
        """Actual dense numerical factor calculation, isolated from cash accounting."""
        groups = defaultdict(list)
        for bar in sample.bars:
            groups[bar.available_at].append(bar)
        results = []
        for at, bars in sorted(groups.items()):
            values = np.asarray([float(x.close) for x in bars], dtype=np.float64)
            mean, scale = values.mean(), values.std()
            scores = (values-mean)/(scale if scale else 1)
            results.extend({"instrument_id": b.instrument_id, "at": payload(at), "score": format(float(s), ".12f")} for b, s in zip(bars, scores))
        return results


def comparable(left, right):
    if any(left.get(k) != right.get(k) for k in ("semantics", "sample_hash", "assumptions", "strategy_hash", "parameters_hash", "scope", "mode","action_hash")):
        raise ResearchError("BACKEND_RESULTS_NOT_COMPARABLE")
    def normalized(value):
        if isinstance(value, dict):
            return {k: normalized(v) for k, v in value.items() if k not in {"account_id", "client_order_id", "order_id", "event_id", "created_at", "updated_at"}}
        if isinstance(value, list):
            return [normalized(x) for x in value]
        return value
    dimensions = {k: normalized(left[k]) == normalized(right[k]) for k in ("decisions", "orders", "fills", "ledger", "equity_curve", "rejections")}
    dimensions["actions"]=normalized(left.get("actions",[]))==normalized(right.get("actions",[]))
    dimensions["execution_skips"]=normalized(left.get("execution_skips",[]))==normalized(right.get("execution_skips",[]))
    final_keys = ("cash", "available_cash", "unsettled_cash", "fees", "positions")
    dimensions.update({key: left["final"][key] == right["final"][key] for key in final_keys})
    return {"comparable": True, "equal": all(dimensions.values()), "dimensions": dimensions,
            "basis": "same sample, cost, execution, and compatible threshold strategy; no general return-equivalence claim"}
