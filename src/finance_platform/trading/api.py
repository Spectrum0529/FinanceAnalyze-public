"""Authenticated module router composed by core; no alternate HTTP server."""
from functools import wraps
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from finance_platform.core.contracts import ContractError, Instrument, RunMode, identifier
from finance_platform.core.errors import PlatformError
from .accounting import FeeSchedule, TradingError, decimal
from .models import MarketEvent, OrderIntent
from .risk import RiskLimits
from .service import TradingService
from .simulation import SimulationBroker


def guarded(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PlatformError:
            raise
        except TradingError as exc:
            if exc.code == "ACCOUNT_ALREADY_EXISTS":
                raise PlatformError(exc.code, 409) from None
            raise PlatformError(exc.code, 403 if exc.code in {"ACCOUNT_FORBIDDEN", "LIVE_DISABLED", "TRADING_MODE_DISABLED"} else 422) from None
        except (ContractError, TypeError, KeyError, ValueError):
            raise PlatformError("INVALID_INPUT", 422) from None
    return wrapper


def exact(body, allowed, required=()):
    if type(body) is not dict or set(body) - set(allowed) or not set(required) <= set(body):
        raise PlatformError("INVALID_INPUT", 422)


def decimal_string(value):
    if type(value) is not str:
        raise PlatformError("INVALID_INPUT", 422)
    return decimal(value)


def create_router(runtime):
    router = APIRouter(prefix="/trading", tags=["交易与账户"])
    service = getattr(runtime, "trading", None) or TradingService(runtime.database, authorize=runtime.require_owner)
    broker = SimulationBroker(service)

    @router.get("/capabilities")
    def capabilities(request: Request):
        runtime.require_principal(request)
        from .settlement import declaration
        return {"implementation": "IMPLEMENTED", "verification": "NOT_RUN", "external_dependency": "WAITING_ACCOUNT",
                "modes": ["LOCAL_SIMULATION", "BROKER_PAPER"], "live": "DISABLED", "currency": "USD", "asset_classes": ["US_EQUITY", "US_ETF"],
                "account_type": "cash", "settlement": "T+1_FROM_2024_05_28", "settlement_policy": declaration(), "order_types": ["market", "limit"],
                "time_in_force": ["day"], "replace": "CANCEL_CONFIRM_NEW", "sender_topology": "SINGLE_HOST_MUTEX_PLUS_DB_LEASE",
                "online_paper": "NOT_VERIFIED"}

    @router.get("/accounts")
    @guarded
    def accounts(request: Request, limit: int = 50, offset: int = 0):
        return service.list_accounts(runtime.require_principal(request), limit, offset)

    @router.post("/accounts")
    @guarded
    def create_account(request: Request, body: dict):
        principal = runtime.require_principal(request, write=True)
        exact(body, {"account_id", "initial_cash", "mode", "fees", "limits", "currency", "account_type", "leverage", "allow_short", "simulation_started_at"}, {"account_id", "initial_cash"})
        if "simulation_started_at" in body and type(body["simulation_started_at"]) is not str:
            raise PlatformError("INVALID_INPUT", 422)
        decimal_string(body["initial_cash"])
        for value in body.get("fees", {}).values():
            decimal_string(value)
        for key, value in body.get("limits", {}).items():
            if key not in {"max_orders_per_minute", "max_data_age_seconds", "reset_timezone"}:
                decimal_string(value)
        return service.create_account(principal, body["account_id"], body["initial_cash"], mode=RunMode(body.get("mode", "LOCAL_SIMULATION")),
            fees=FeeSchedule(**body.get("fees", {})), limits=RiskLimits.from_dict(body.get("limits", {})),
            currency=body.get("currency", "USD"), account_type=body.get("account_type", "cash"),
            leverage=body.get("leverage", "1"), allow_short=body.get("allow_short", False),
            simulation_started_at=body.get("simulation_started_at"))

    @router.get("/accounts/{account_id}")
    @guarded
    def account(request: Request, account_id: str):
        return service.snapshot(runtime.require_principal(request), account_id)

    @router.get("/accounts/{account_id}/positions")
    @guarded
    def positions(request: Request, account_id: str, limit: int = 50, offset: int = 0):
        service._paging(limit, offset)
        rows = service.snapshot(runtime.require_principal(request), account_id)["positions"]
        return {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}

    @router.get("/accounts/{account_id}/orders")
    @guarded
    def orders(request: Request, account_id: str, limit: int = 50, offset: int = 0):
        return service.list_orders(runtime.require_principal(request), account_id, limit, offset)

    @router.get("/accounts/{account_id}/fills")
    @guarded
    def fills(request: Request, account_id: str, limit: int = 50, offset: int = 0):
        return service.list_ledger(runtime.require_principal(request), account_id, fills_only=True, limit=limit, offset=offset)

    @router.get("/accounts/{account_id}/ledger")
    @guarded
    def ledger(request: Request, account_id: str, limit: int = 50, offset: int = 0):
        return service.list_ledger(runtime.require_principal(request), account_id, limit=limit, offset=offset)

    @router.get("/accounts/{account_id}/risk")
    @guarded
    def risk(request: Request, account_id: str):
        return service.risk_configuration(runtime.require_principal(request), account_id)

    @router.get("/accounts/{account_id}/risk/versions")
    @guarded
    def risk_versions(request: Request, account_id: str, limit: int = 50, offset: int = 0):
        return service.risk_versions(runtime.require_principal(request), account_id, limit, offset)

    @router.post("/accounts/{account_id}/risk")
    @guarded
    def update_risk(request: Request, account_id: str, body: dict):
        exact(body, {"version", "expected_version", "limits"}, {"version", "expected_version", "limits"})
        from dataclasses import fields
        exact(body["limits"], {f.name for f in fields(RiskLimits)}, {f.name for f in fields(RiskLimits)})
        for key, value in body["limits"].items():
            if key not in {"max_orders_per_minute", "max_data_age_seconds", "reset_timezone"}:
                decimal_string(value)
        return service.update_risk(runtime.require_principal(request, write=True), account_id, body["version"], body["expected_version"], RiskLimits.from_dict(body["limits"]))

    @router.post("/accounts/{account_id}/reconcile")
    @guarded
    def reconcile_local(request: Request, account_id: str, body: dict = {}):
        exact(body, set())
        return service.reconcile_local(runtime.require_principal(request, write=True), account_id)

    @router.post("/accounts/{account_id}/instruments")
    @guarded
    def add_instrument(request: Request, account_id: str, body: dict):
        service.add_instrument(runtime.require_principal(request, write=True), account_id, Instrument.from_mapping(body))
        return {"registered": True}

    @router.post("/accounts/{account_id}/orders")
    @guarded
    def submit(request: Request, account_id: str, body: dict):
        order = OrderIntent.from_dict(body)
        if order.account_id != account_id:
            raise PlatformError("INVALID_INPUT", 422)
        return {"order_id": service.submit(runtime.require_principal(request, write=True), order), "status": "queued"}

    @router.post("/accounts/{account_id}/orders/{order_id}/cancel")
    @guarded
    def cancel(request: Request, account_id: str, order_id: str):
        return {"status": service.cancel(runtime.require_principal(request, write=True), account_id, order_id)}

    @router.post("/accounts/{account_id}/orders/{order_id}/replace")
    @guarded
    def replace(request: Request, account_id: str, order_id: str, body: dict):
        replacement = OrderIntent.from_dict(body)
        return {"order_id": service.replace(runtime.require_principal(request, write=True), account_id, order_id, replacement), "status": "queued"}

    @router.post("/accounts/{account_id}/emergency-stop")
    @guarded
    def emergency(request: Request, account_id: str, body: dict):
        exact(body, {"enabled"}, {"enabled"})
        service.emergency_stop(runtime.require_principal(request, write=True), account_id, body["enabled"])
        return {"emergency_stop": body["enabled"]}

    @router.post("/accounts/{account_id}/targets")
    @guarded
    def targets(request: Request, account_id: str, body: dict):
        exact(body, {"request_id", "budgets", "weights"}, {"request_id", "budgets", "weights"})
        return service.submit_targets(runtime.require_principal(request, write=True), account_id, body["request_id"],
            {s: decimal_string(v) for s, v in body["budgets"].items()},
            {s: {i: decimal_string(v) for i, v in weights.items()} for s, weights in body["weights"].items()})

    @router.post("/accounts/{account_id}/cash")
    @guarded
    def cash(request: Request, account_id: str, body: dict):
        exact(body, {"event_id", "amount"}, {"event_id", "amount"})
        service.cash_event(runtime.require_principal(request, write=True), account_id, body["event_id"], decimal_string(body["amount"]))
        return service.snapshot(runtime.require_principal(request), account_id)

    @router.post("/accounts/{account_id}/corporate-actions")
    @guarded
    def corporate_action(request: Request, account_id: str, body: dict):
        from .accounting import LedgerEvent
        principal = runtime.require_principal(request, write=True)
        for key in {"quantity", "price", "fee", "amount"} & set(body):
            decimal_string(body[key])
        service.corporate_action(principal, account_id, LedgerEvent(**body))
        return service.snapshot(principal, account_id)

    @router.post("/accounts/{account_id}/corporate-actions/{event_id}/resolve")
    @guarded
    def resolve_corporate_action(request: Request, account_id: str, event_id: str):
        principal = runtime.require_principal(request, write=True)
        service.resolve_corporate_action(principal, account_id, event_id)
        return service.snapshot(principal, account_id)

    @router.get("/accounts/{account_id}/reconciliations")
    @guarded
    def reconciliations(request: Request, account_id: str, limit: int = 50, offset: int = 0):
        from contextlib import closing
        import json
        principal = runtime.require_principal(request)
        service._paging(limit, offset)
        with closing(service.database.connect()) as con:
            service._account(con, account_id, principal)
            total = con.execute("SELECT count(*) FROM trading_reconciliations WHERE account_id=?", (account_id,)).fetchone()[0]
            rows = con.execute("SELECT at,body FROM trading_reconciliations WHERE account_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?", (account_id, limit, offset))
            return {"items": [{"at": r["at"], **json.loads(r["body"])} for r in rows], "total": total, "limit": limit, "offset": offset}

    @router.post("/accounts/{account_id}/simulation")
    @guarded
    def simulate(request: Request, account_id: str, body: dict):
        principal = runtime.require_principal(request, write=True)
        exact(body, {"events", "intents", "idempotency_key"}, {"events", "intents", "idempotency_key"})
        identifier(body["idempotency_key"])
        service.snapshot(principal, account_id)
        from .jobs import prepare_replay, prior_submission
        original = {"account_id": account_id, "events": body["events"], "intents": body["intents"]}
        previous = prior_submission(runtime, principal, "trading.simulate", original, body["idempotency_key"])
        if previous is not None:
            return previous
        prepared = prepare_replay(runtime, service, principal, original)
        return runtime.jobs.submit(principal, "trading.simulate", prepared,
            mode=RunMode.LOCAL_SIMULATION, idempotency_key=body["idempotency_key"])

    @router.get("/accounts/{account_id}/paper")
    @guarded
    def paper_status(request: Request, account_id: str):
        from .paper import status
        return status(service, runtime.require_principal(request), account_id)

    @router.post("/accounts/{account_id}/paper/config")
    @guarded
    def paper_config(request: Request, account_id: str, body: dict):
        from .paper import configure, status
        from .alpaca import AlpacaPaperConfig
        principal = runtime.require_principal(request, write=True)
        exact(body, {"broker_account_id", "key_env", "secret_env"}, {"broker_account_id", "key_env", "secret_env"})
        configure(service, principal, account_id, AlpacaPaperConfig(account_id, **body))
        return status(service, principal, account_id)

    @router.post("/accounts/{account_id}/paper/{action}")
    @guarded
    def paper_action(request: Request, account_id: str, action: str, body: dict):
        principal = runtime.require_principal(request, write=True)
        if action not in {"probe", "recover", "dispatch", "stream"}:
            raise PlatformError("UNSUPPORTED_CAPABILITY", 422)
        exact(body, {"idempotency_key", "seconds"}, {"idempotency_key"})
        identifier(body["idempotency_key"])
        from .paper import load_config
        load_config(service, principal, account_id)
        parameters = {"account_id": account_id, "action": action}
        if "seconds" in body:
            parameters["seconds"] = body["seconds"]
        from .jobs import prepare_paper, prior_submission
        previous = prior_submission(runtime, principal, "trading.paper", parameters, body["idempotency_key"])
        if previous is not None:
            return previous
        return runtime.jobs.submit(principal, "trading.paper", prepare_paper(runtime, service, principal, parameters),
            mode=RunMode.BROKER_PAPER, idempotency_key=body["idempotency_key"])

    return router
