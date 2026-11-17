"""Single shared portfolio -> risk -> OMS -> persistent simulation boundary."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json

from finance_platform.core.contracts import RunMode, OutputKind, Provenance, payload, fingerprint
from finance_platform.trading.accounting import PortfolioAllocator, floor_step, decimal, TradingError
from finance_platform.trading.models import MarketEvent, OrderIntent
from finance_platform.trading.risk import regular_session
from finance_platform.trading.simulation import SimulationBroker
from finance_platform.trading.risk_configuration import policy
from finance_platform.core.security import now_text
from .order_plans import install
from .replay import market_event,preflight_outputs


class TradeBoundary:
    """Research BACKTEST maps explicitly to isolated LOCAL_SIMULATION accounts.

    This adapter contains no independent cash, fee, fill or risk implementation.
    All intents go through TradingService.submit; all fills through SimulationBroker.
    """
    def __init__(self, service, principal, account_id, sample, *, strategy_id="research",
                 participation="0.10", slippage_bps="0", gross_weight="0.10"):
        self.service, self.principal, self.account_id = service, principal, account_id
        self.sample, self.strategy_id = sample, strategy_id
        self.simulator = SimulationBroker(service, participation=participation, slippage_bps=slippage_bps)
        self.gross_weight = decimal(gross_weight)
        if not 0 <= self.gross_weight <= 1:
            raise TradingError("INVALID_PORTFOLIO_WEIGHT")
        self.instruments = {x.instrument_id: x for x in sample.instruments}
        self.last_ticks, self.signals, self.orders, self.fills, self.rejections = {}, [], [], [], []
        self.execution_skips=[]
        self.risk_version = None
        install(service.database)
        with closing(service.database.connect()) as con:
            account = service._account(con, account_id, principal)
            self.fee_assumption = json.loads(account["fee_json"])
            self.initial_cash = account["initial_cash"]

    def on_tick(self, tick, now):
        event = market_event(self.sample,tick)
        checked=self.service.preflight_execution(self.principal,self.account_id,now,market=event)
        with closing(self.service.database.connect()) as con:
            self.service._account(con,self.account_id,self.principal)
            known=con.execute("SELECT 1 FROM trading_market WHERE account_id=? AND event_id=?",(self.account_id,tick.event_id)).fetchone()
            latest=self.service._markets(con,self.account_id,now).get(tick.instrument_id)
        if not known and latest and tick.event_at<=latest.event_at:
            # Keep the observed record in the frozen source/archive, but an old
            # out-of-order print neither rewinds the quote nor supplies capacity.
            self.execution_skips.append({"event_id":tick.event_id,"event_at":payload(tick.event_at),"available_at":payload(tick.available_at),"code":"OUT_OF_ORDER_OBSERVED_PRINT_NO_EXECUTION"})
            # The shared broker records this forward observation and advances
            # its clock without historical matching or quote rewind.
            self.simulator.on_market(self.principal,self.account_id,event,now=now)
            return []
        fills = self.simulator.on_market(self.principal, self.account_id, event, now=now)
        self.last_ticks[tick.instrument_id] = tick
        for fill in fills:
            self.fills.append({"fill_at": payload(now), "event_at": payload(tick.event_at), **fill})
        if not checked["retry"]:
            self.service.settle(self.principal, self.account_id, now)
        return fills

    def snapshot(self, now):
        return self.service.snapshot(self.principal, self.account_id, now=now)

    def can_submit(self, outputs, now):
        return all(x.instrument_id in self.last_ticks and regular_session(now, self.instruments[x.instrument_id].calendar_id)
                   and self.last_ticks[x.instrument_id].tradable for x in outputs)

    def route(self, outputs, now):
        outputs = tuple(outputs)
        if not outputs:
            return []
        for output in outputs:
            if output.instrument_id not in self.instruments or output.mode not in {RunMode.BACKTEST, RunMode.LOCAL_SIMULATION} or not output.produced_at <= now < output.expires_at:
                raise TradingError("STRATEGY_OUTPUT_SCOPE_OR_CLOCK")
            if any(o.instrument_id==output.instrument_id and o.output_id!=output.output_id for o in outputs):
                raise TradingError("DUPLICATE_INSTRUMENT_OUTPUT")
        preflight_outputs(self.service,self.principal,self.account_id,self.sample,outputs,now)
        with closing(self.service.database.connect()) as con:
            plans={o.output_id:con.execute("SELECT * FROM research_order_plans WHERE account_id=? AND output_id=?",(self.account_id,o.output_id)).fetchone() for o in outputs}
        new=tuple(o for o in outputs if plans[o.output_id] is None)
        if new:
            computed=self._compute_plans(new,now,ranking_outputs=outputs)
            with self.service.database.transaction() as con:
                account=self.service._account(con,self.account_id,self.principal,True)
                # Recheck while this writer owns the account database lock;
                # no unknown past plan is recorded after another writer moves.
                preflight_outputs(self.service,self.principal,self.account_id,self.sample,new,now)
                _,risk=policy(con,account,self.risk_version)
                for output,intent in computed:
                    con.execute("INSERT INTO research_order_plans VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,output_id) DO NOTHING",(self.account_id,output.output_id,fingerprint(output),self.sample.sha256,
                             None if intent is None else json.dumps(payload(intent),sort_keys=True,separators=(",",":")),risk["version"],risk["content_hash"],payload(output.produced_at),now_text()))
        submitted=[]
        for output in sorted(outputs,key=lambda o:o.instrument_id):
            self.signals.append(payload(output))
            with closing(self.service.database.connect()) as con:
                plan=con.execute("SELECT * FROM research_order_plans WHERE account_id=? AND output_id=?",(self.account_id,output.output_id)).fetchone()
            if plan["output_hash"]!=fingerprint(output) or plan["sample_hash"]!=self.sample.sha256:
                raise TradingError("STRATEGY_ORDER_PLAN_CONFLICT")
            if plan["intent_json"] is None:
                continue
            intent=OrderIntent.from_dict(json.loads(plan["intent_json"]))
            try:
                self.service.submit(self.principal,intent,now=now,risk_version=plan["risk_version"])
                record={"decision_at":plan["decision_at"],**payload(intent)}
                self.orders.append(record);submitted.append(record)
            except TradingError as exc:
                self.rejections.append({"output_id":output.output_id,"at":payload(now),"code":exc.code})
        return submitted

    def _compute_plans(self,outputs,now,*,ranking_outputs):
        snapshot = self.snapshot(now)
        prices = {i: t.price for i, t in self.last_ticks.items()}
        lots = {i: inst.lot_size for i, inst in self.instruments.items()}
        weights, quantities, direct = {}, {}, []
        ranked = [o for o in ranking_outputs if o.kind in {OutputKind.SCORE, OutputKind.SIGNAL} and o.value > 0]
        for output in outputs:
            if output.instrument_id not in self.instruments or output.mode not in {RunMode.BACKTEST, RunMode.LOCAL_SIMULATION} or not output.produced_at <= now < output.expires_at:
                raise TradingError("STRATEGY_OUTPUT_SCOPE_OR_CLOCK")
            if any(o.instrument_id == output.instrument_id and o.output_id != output.output_id for o in outputs):
                raise TradingError("DUPLICATE_INSTRUMENT_OUTPUT")
            if output.kind in {OutputKind.SCORE, OutputKind.SIGNAL}:
                weights[output.instrument_id] = self.gross_weight / len(ranked) if output.value > 0 else Decimal(0)
            elif output.kind is OutputKind.TARGET_WEIGHT:
                weights[output.instrument_id] = output.value
            elif output.kind is OutputKind.TARGET_QUANTITY:
                quantities[output.instrument_id] = output.value
            else:
                direct.append(output)
        if weights:
            if snapshot["equity"] is None:
                raise TradingError("VALUATION_UNKNOWN")
            _, contributions = PortfolioAllocator.targets(decimal(snapshot["equity"]), {self.strategy_id: Decimal(1)},
                                 {self.strategy_id: weights}, prices, lots)
            quantities.update(contributions[self.strategy_id])
        held = {p["instrument_id"]: decimal(p["quantity"]) for p in snapshot["strategy_contributions"] if p["strategy_id"] == self.strategy_id}
        pending = {}
        for order in snapshot["orders"]:
            intent = order["intent"]
            if intent["strategy_id"] == self.strategy_id and order["status"] not in {"filled", "cancelled", "rejected", "expired", "replaced"}:
                remaining = decimal(intent["quantity"])-decimal(order["filled_qty"])
                pending[intent["instrument_id"]] = pending.get(intent["instrument_id"], Decimal(0)) + remaining*(1 if intent["side"] == "buy" else -1)
        planned = []
        for output in sorted(outputs, key=lambda x: x.instrument_id):
            if output.kind is OutputKind.ORDER_INTENT:
                side, quantity, order_type, limit = output.side.lower(), output.value, output.order_type.lower(), output.limit_price
            else:
                difference = quantities[output.instrument_id]-held.get(output.instrument_id, Decimal(0))-pending.get(output.instrument_id, Decimal(0))
                if difference == 0:
                    planned.append((output,None))
                    continue
                side, quantity, order_type, limit = "buy" if difference > 0 else "sell", abs(difference), "market", None
            intent = OrderIntent("ord-"+fingerprint({"account": self.account_id, "output_id": output.output_id})[:32],
                     self.account_id, self.strategy_id, output.instrument_id, side, quantity, order_type,
                     now, RunMode.LOCAL_SIMULATION, limit_price=limit, source="external" if output.output_id.startswith("ext-") else "strategy")
            planned.append((output,intent))
        return planned

    def ledger(self):
        with closing(self.service.database.connect()) as con:
            return [{"at": row["at"], **json.loads(row["body"])} for row in con.execute(
                    "SELECT at,body FROM trading_ledger WHERE account_id=? ORDER BY sequence", (self.account_id,))]
