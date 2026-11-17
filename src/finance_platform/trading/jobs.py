"""Synthetic replay handler on the existing persistent core worker."""
from contextlib import closing
from functools import wraps

from finance_platform.core.auth import Principal
from finance_platform.core.contracts import DataScope, RunMode, fingerprint, payload
from finance_platform.core.errors import PlatformError
from .accounting import TradingError
from .models import OrderIntent, MarketEvent
from .service import TradingService
from .simulation import SimulationBroker


def safe_trading_handler(function):
    @wraps(function)
    def wrapped(context, parameters):
        try:
            return function(context, parameters)
        except TradingError as exc:
            raise PlatformError(exc.code, 422) from None
    return wrapped


def _validate_replay(parameters, *, references_required):
    try:
        expected = {"account_id", "events", "intents"}
        if references_required:
            expected |= {"binding_reference", "data_reference"}
        if set(parameters) != expected or not isinstance(parameters["events"], list) or not isinstance(parameters["intents"], list):
            raise ValueError
        if references_required:
            from finance_platform.core.contracts import VersionRef
            VersionRef(**parameters["binding_reference"])
            VersionRef(**parameters["data_reference"])
        if not 1 <= len(parameters["events"]) <= 10000 or len(parameters["intents"]) > 1000:
            raise ValueError
        events = [MarketEvent.from_dict(e) for e in parameters["events"]]
        orders = [OrderIntent.from_dict(i) for i in parameters["intents"]]
        if any(e.provenance.scope is not DataScope.SYNTHETIC for e in events):
            raise ValueError
        if any(i.account_id != parameters["account_id"] or i.mode is not RunMode.LOCAL_SIMULATION for i in orders):
            raise ValueError
        if any(events[i].event_at >= events[i + 1].event_at for i in range(len(events) - 1)):
            raise ValueError
        if len({e.event_id for e in events}) != len(events) or len({i.client_order_id for i in orders}) != len(orders):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise PlatformError("INVALID_INPUT", 422) from None


def validate_replay(parameters):
    return _validate_replay(parameters, references_required=True)


def prepare_replay(runtime, service, principal, parameters):
    from .settlement import declaration as settlement_declaration
    _validate_replay(parameters, references_required=False)
    with closing(service.database.connect()) as con:
        account = service._account(con, parameters["account_id"], principal, True)
        for item in parameters["events"]:
            service._require_started(account, MarketEvent.from_dict(item).event_at)
        for item in parameters["intents"]:
            service._require_started(account, OrderIntent.from_dict(item).effective_at)
    risk = service.risk_configuration(principal, parameters["account_id"])
    declaration = {"handler": "trading.simulate", "account_id": parameters["account_id"],
                   "risk": {"version": risk["version"], "content_hash": risk["content_hash"]}, "settlement": settlement_declaration()}
    strategy = runtime.versions.freeze(principal, "trading-replay:" + fingerprint(declaration)[:48] + ":" + principal.user_id, "1", "strategy", declaration)
    data_value = {"scope": "SYNTHETIC", "events": parameters["events"]}
    data = runtime.versions.freeze(principal, "trading-events:" + fingerprint(data_value)[:48] + ":" + principal.user_id, "1", "data", data_value)
    prepared = dict(parameters) | {"binding_reference": payload(strategy), "data_reference": payload(data)}
    validate_replay(prepared)
    return prepared


def prepare_paper(runtime, service, principal, parameters):
    from .settlement import declaration as settlement_declaration
    from .paper import load_config
    config = load_config(service, principal, parameters["account_id"])
    risk = service.risk_configuration(principal, parameters["account_id"])
    declaration = {"handler": "trading.paper", "account_id": parameters["account_id"], "paper_config": payload(config),
                   "risk": {"version": risk["version"], "content_hash": risk["content_hash"]}, "settlement": settlement_declaration()}
    strategy = runtime.versions.freeze(principal, "trading-paper:" + fingerprint(declaration)[:48] + ":" + principal.user_id, "1", "strategy", declaration)
    return dict(parameters) | {"binding_reference": payload(strategy)}


def prior_submission(runtime, principal, handler, original, key):
    """Existing idempotent request retains its already-frozen references."""
    import json
    from finance_platform.core.contracts import canonical
    with closing(runtime.database.connect()) as con:
        row = con.execute("SELECT j.job_id,j.handler,j.parameters_json FROM job_keys k JOIN jobs j USING(job_id) WHERE k.owner_id=? AND k.idempotency_key=?", (principal.user_id, key)).fetchone()
    if row is None:
        return None
    old = json.loads(row["parameters_json"])
    old.pop("binding_reference", None)
    old.pop("data_reference", None)
    if row["handler"] != handler or canonical(old) != canonical(original):
        raise PlatformError("CONFLICT", 409)
    return runtime.jobs.get(principal, row["job_id"])


def register_handlers(registry, runtime):
    service = getattr(runtime, "trading", None) or TradingService(runtime.database, authorize=runtime.require_owner)

    def replay(context, parameters):
        if context.run.mode is not RunMode.LOCAL_SIMULATION:
            raise PlatformError("UNSUPPORTED_CAPABILITY", 422)
        with closing(runtime.database.connect()) as con:
            row = con.execute("SELECT username,role,enabled FROM users WHERE user_id=?", (context.run.owner_id,)).fetchone()
            if not row or not row["enabled"]:
                raise PlatformError("FORBIDDEN", 403)
            principal = Principal(context.run.owner_id, row["username"], row["role"])
            service._account(con, parameters["account_id"], principal, True)
        declaration = runtime.versions.get(principal, context.run.strategy)
        from .settlement import declaration as settlement_declaration
        if declaration.get("handler") != "trading.simulate" or declaration.get("account_id") != parameters["account_id"] or declaration.get("settlement") != settlement_declaration():
            raise TradingError("RUN_RISK_BINDING_MISMATCH")
        risk_ref = service.bind_run_risk(principal, parameters["account_id"], context.run.run_id, declaration["risk"]["version"])
        if risk_ref != declaration["risk"] or runtime.versions.get(principal, context.run.data) != {"scope": "SYNTHETIC", "events": parameters["events"]}:
            raise TradingError("RUN_RISK_BINDING_MISMATCH")
        events = [MarketEvent.from_dict(e) for e in parameters["events"]]
        orders = sorted([OrderIntent.from_dict(i) for i in parameters["intents"]], key=lambda i: i.effective_at)
        broker = SimulationBroker(service)
        pending, fills, rejected = list(orders), [], []
        for index, event in enumerate(events):
            context.require_active()
            # Existing available marks precede the new fill event. First event seeds the book.
            while pending and pending[0].effective_at < event.event_at:
                order = pending.pop(0)
                context.require_active()
                try:
                    service.submit(principal, order, order.effective_at, risk_version=risk_ref["version"])
                except TradingError as exc:
                    rejected.append({"order_id": order.client_order_id, "code": exc.code})
            context.require_active()
            fills.extend(broker.on_market(principal, parameters["account_id"], event, event.available_at))
            while pending and pending[0].effective_at == event.event_at:
                order = pending.pop(0)
                context.require_active()
                try:
                    service.submit(principal, order, order.effective_at, risk_version=risk_ref["version"])
                except TradingError as exc:
                    rejected.append({"order_id": order.client_order_id, "code": exc.code})
            context.checkpoint(min(99, (index + 1) * 99 // len(events)), {"market_events": index + 1, "fills": len(fills)})
        rejected.extend({"order_id": i.client_order_id, "code": "NO_FUTURE_EXECUTION_EVENT"} for i in pending)
        return {"mode": "LOCAL_SIMULATION", "scope": "SYNTHETIC", "fills": fills, "rejected": rejected,
                "risk": risk_ref,
                "snapshot": service.snapshot(principal, parameters["account_id"], events[-1].available_at)}

    def resolve_replay(principal, parameters):
        from .settlement import declaration as settlement_declaration
        from finance_platform.core.contracts import VersionRef
        service.snapshot(principal, parameters["account_id"])
        strategy = VersionRef(**parameters["binding_reference"])
        data = VersionRef(**parameters["data_reference"])
        declaration = runtime.versions.get(principal, strategy)
        if declaration.get("handler") != "trading.simulate" or declaration.get("account_id") != parameters["account_id"] or declaration.get("settlement") != settlement_declaration() or runtime.versions.get(principal, data) != {"scope": "SYNTHETIC", "events": parameters["events"]}:
            raise TradingError("RUN_RISK_BINDING_MISMATCH")
        return {"strategy": strategy, "data": data, "model_reason": "No model; deterministic synthetic intent replay"}

    registry.register("trading.simulate", safe_trading_handler(replay), validate=validate_replay, recoverable=False,
                      allowed_modes=(RunMode.LOCAL_SIMULATION,), resolve_versions=resolve_replay)

    def validate_paper(parameters):
        if set(parameters) - {"account_id", "action", "seconds", "binding_reference"} or not {"account_id", "action", "binding_reference"} <= set(parameters) or parameters["action"] not in {"probe", "recover", "dispatch", "stream"}:
            raise PlatformError("INVALID_INPUT", 422)
        if "seconds" in parameters and (parameters["action"] != "stream" or type(parameters["seconds"]) is not int or not 1 <= parameters["seconds"] <= 60):
            raise PlatformError("INVALID_INPUT", 422)

    def paper_job(context, parameters):
        from .settlement import declaration as settlement_declaration
        from .paper import load_config
        from .alpaca import AlpacaPaperBroker
        from .oms import OrderManagement
        from .authority import SenderAuthority
        if context.run.mode is not RunMode.BROKER_PAPER:
            raise PlatformError("UNSUPPORTED_CAPABILITY", 422)
        with closing(runtime.database.connect()) as con:
            row = con.execute("SELECT username,role,enabled FROM users WHERE user_id=?", (context.run.owner_id,)).fetchone()
            if row is None or not row["enabled"]:
                raise PlatformError("FORBIDDEN", 403)
            principal = Principal(context.run.owner_id, row["username"], row["role"])
        config = load_config(service, principal, parameters["account_id"])
        declaration = runtime.versions.get(principal, context.run.strategy)
        if declaration.get("handler") != "trading.paper" or declaration.get("account_id") != parameters["account_id"] or declaration.get("settlement") != settlement_declaration() or declaration.get("paper_config") != payload(config):
            raise TradingError("RUN_RISK_BINDING_MISMATCH")
        risk_ref = service.bind_run_risk(principal, parameters["account_id"], context.run.run_id, declaration["risk"]["version"])
        if risk_ref != declaration["risk"]:
            raise TradingError("RUN_RISK_BINDING_MISMATCH")
        broker = AlpacaPaperBroker(config)
        context.require_active()
        if parameters["action"] == "probe":
            return broker.probe()
        oms = OrderManagement(service, broker)
        with SenderAuthority(service, principal, parameters["account_id"], context.run.run_id) as authority:
            context.require_active()
            report = oms.recover(principal, parameters["account_id"])
            if parameters["action"] == "stream" and report["clean"]:
                import time
                deadline = time.monotonic() + parameters.get("seconds", 30)
                event_count = 0
                def receive(event):
                    nonlocal event_count
                    context.require_active()
                    authority.heartbeat()
                    oms.apply_event(principal, parameters["account_id"], event)
                    event_count += 1
                def stop():
                    context.require_active()
                    authority.heartbeat()
                    return time.monotonic() >= deadline
                broker.stream(receive, lambda: oms.freeze_disconnected(principal, parameters["account_id"]), stop=stop)
                return {"mode": "BROKER_PAPER", "trade_updates": event_count, "observation_seconds": parameters.get("seconds", 30)}
            if parameters["action"] == "dispatch" and report["clean"]:
                context.require_active()
                authority.heartbeat()
                order_id = oms.dispatch_one(authority)
                return {"reconciliation": report, "order_id": order_id, "mode": "BROKER_PAPER"}
            return report

    registry.register("trading.paper", safe_trading_handler(paper_job), validate=validate_paper, recoverable=False,
                      allowed_modes=(RunMode.BROKER_PAPER,), resolve_versions=lambda principal, parameters: resolve_paper(principal, parameters))

    def resolve_paper(principal, parameters):
        from .settlement import declaration as settlement_declaration
        from .paper import load_config
        from finance_platform.core.contracts import VersionRef
        config = load_config(service, principal, parameters["account_id"])
        strategy = VersionRef(**parameters["binding_reference"])
        declaration = runtime.versions.get(principal, strategy)
        if declaration.get("handler") != "trading.paper" or declaration.get("account_id") != parameters["account_id"] or declaration.get("settlement") != settlement_declaration() or declaration.get("paper_config") != payload(config):
            raise TradingError("RUN_RISK_BINDING_MISMATCH")
        return {"strategy": strategy, "model_reason": "No model; explicit paper operation"}
