"""Readonly replay preflight; the shared trading service owns the clock/effects."""
from collections import defaultdict
from contextlib import closing
import json
from finance_platform.core.contracts import Provenance,payload,fingerprint,parse_instant
from finance_platform.trading.models import MarketEvent,OrderIntent
from finance_platform.trading.accounting import LedgerEvent,TradingError
from .actions import dispatch_at


def market_event(sample,tick):
    meta=Provenance(tick.event_id,sample.source_id,sample.license_id,fingerprint(tick),1,sample.scope,
                    tick.event_at,tick.available_at,tick.available_at)
    return MarketEvent(tick.event_id,tick.instrument_id,tick.event_at,tick.available_at,
                       tick.price,tick.volume,meta,tick.tradable)


def timeline(sample,actions=()):
    events=defaultdict(lambda:{"ticks":[],"bars":[],"actions":[]})
    for tick in sample.ticks:events[tick.available_at]["ticks"].append(tick)
    for bar in sample.bars:events[bar.available_at]["bars"].append(bar)
    for action in actions:events[dispatch_at(action)]["actions"].append(action)
    return events


def journal_key(sample,package_hash,parameters,at,event,state):
    return fingerprint({"sample":sample.sha256,"package":package_hash,"parameters":payload(parameters),
                        "at":payload(at),"event":event,"state":payload(state)})


def preflight_action(service,principal,account_id,record,now):
    """A past action needs both its exact full receipt identity and ledger body."""
    from finance_platform.trading.research_actions import parse_action
    action=parse_action(record)
    with closing(service.database.connect()) as con:
        prior=con.execute("SELECT body FROM trading_research_actions WHERE account_id=? AND event_id=?",(account_id,action.event_id)).fetchone()
        effect=con.execute("SELECT body FROM trading_ledger WHERE account_id=? AND event_id=?",(account_id,action.event_id)).fetchone()
    if prior and prior["body"]==action.envelope_json and effect:
        event=LedgerEvent(**json.loads(effect["body"]))
        return service.preflight_execution(principal,account_id,now,event=event)
    return service.preflight_execution(principal,account_id,now)


def preflight_outputs(service,principal,account_id,sample,outputs,now,*,require_existing=False):
    """Only exact stored output plans can identify old committed intent retries."""
    for output in outputs:
        with closing(service.database.connect()) as con:
            installed=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_order_plans'").fetchone()
            plan=con.execute("SELECT * FROM research_order_plans WHERE account_id=? AND output_id=?",(account_id,output.output_id)).fetchone() if installed else None
        if plan is None:
            if require_existing:
                # A fully journaled completed prefix may contain expired or
                # superseded outputs which never reached the order boundary.
                # Prove absence of their stable intent identity; copying the
                # completed checkpoint will never execute such outputs anew.
                client_id="ord-"+fingerprint({"account":account_id,"output_id":output.output_id})[:32]
                with closing(service.database.connect()) as con:
                    intent=con.execute("SELECT 1 FROM trading_intents WHERE client_order_id=?",(client_id,)).fetchone()
                if intent:raise TradingError("SIMULATION_BINDING_NOT_FULLY_COMMITTED")
            else:
                service.preflight_execution(principal,account_id,now)
            continue
        if plan["sample_hash"]!=sample.sha256 or plan["output_hash"]!=fingerprint(output):
            raise TradingError("STRATEGY_ORDER_PLAN_CONFLICT")
        if plan["intent_json"] is not None:
            result=service.preflight_execution(principal,account_id,now,intent=OrderIntent.from_dict(json.loads(plan["intent_json"])))
            if require_existing and not result["retry"]:raise TradingError("SIMULATION_BINDING_NOT_FULLY_COMMITTED")


def preflight_local(service,principal,account_id,sample,actions,*,session=None,instance_id=None,
                    package_hash=None,parameters=None,decode_outputs=None,require_existing=False):
    events=timeline(sample,actions)
    if session is None:
        return service.preflight_execution(principal,account_id,min(events))
    resume=json.loads(session["checkpoint_json"])
    if resume.get("sample_hash") not in {None,sample.sha256}:
        raise TradingError("SIMULATION_CHECKPOINT_SAMPLE_CONFLICT")
    clock=service.execution_clock(principal,account_id)
    watermark=parse_instant(clock["watermark"])
    service.preflight_execution(principal,account_id,watermark)
    processed=None if not resume.get("processed_at") else parse_instant(resume["processed_at"])
    state=dict(resume.get("state",{}))
    remaining=[at for at in sorted(events) if processed is None or at>processed]
    if not remaining:
        return preflight_local(service,principal,account_id,sample,actions,session={"checkpoint_json":"{}"},
                instance_id=resume.get("committed_binding_origin",instance_id),package_hash=package_hash,
                parameters=parameters,decode_outputs=decode_outputs,require_existing=True)
    for at in remaining:
        if at>=watermark and not require_existing:
            return service.preflight_execution(principal,account_id,at)
        items=events[at]
        for tick in items["ticks"]:
            result=service.preflight_execution(principal,account_id,at,market=market_event(sample,tick))
            if require_existing and not result["retry"]:raise TradingError("SIMULATION_BINDING_NOT_FULLY_COMMITTED")
        for action in items["actions"]:
            result=preflight_action(service,principal,account_id,action,at)
            if require_existing and not result["retry"]:raise TradingError("SIMULATION_BINDING_NOT_FULLY_COMMITTED")
        if items["bars"]:
            key=journal_key(sample,package_hash,parameters,at,"bar",state)
            with closing(service.database.connect()) as con:
                recorded=con.execute("SELECT outputs_json,state_json FROM strategy_simulation_journal WHERE instance_id=? AND event_key=?",(instance_id,key)).fetchone()
            if recorded is None:
                service.preflight_execution(principal,account_id,at)
                if require_existing:raise TradingError("SIMULATION_BINDING_NOT_FULLY_COMMITTED")
            else:
                preflight_outputs(service,principal,account_id,sample,decode_outputs(json.loads(recorded["outputs_json"])),at,require_existing=require_existing)
                state=json.loads(recorded["state_json"])
    return clock
