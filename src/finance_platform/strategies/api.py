"""Package/lifecycle router: no uploaded code runs in this API process."""

import base64
import binascii
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from finance_platform.core.auth import AccountDelegation
from finance_platform.core.contracts import RunMode, payload
from finance_platform.core.errors import PlatformError
from .contracts import StrategyError
from finance_platform.research.data import ResearchError
from finance_platform.trading.accounting import TradingError
from .external import ExternalSignalGate
from .service import StrategyService
from .templates import template
from .sessions import ExternalSessions
from .simulation import LocalSimulation
from .restore import resume_after_restore
from contextlib import closing


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateBody(StrictBody):
    strategy_id: str = Field(min_length=1, max_length=64)
    style: Literal["event", "batch", "target", "model", "external"]
    trusted: StrictBool = False
    model_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class PackageImportBody(StrictBody):
    archive_base64: str = Field(min_length=1, max_length=1_400_000)
    trusted: StrictBool = False


class ConfigBody(StrictBody):
    strategy_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    parameters: dict
    mode: Literal["BACKTEST", "LOCAL_SIMULATION", "BROKER_PAPER", "LIVE"] = "BACKTEST"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class InstanceBody(StrictBody):
    config_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class ExternalStartBody(StrictBody):
    account_id: str = Field(min_length=1, max_length=128)
    sample_reference: dict
    clock_mode: Literal["wall", "replay"] = "wall"
    idempotency_key: str | None = Field(default=None, max_length=128)


class LifecycleBody(StrictBody):
    cancel_orders: StrictBool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class SimulationStartBody(StrictBody):
    account_id: str = Field(min_length=1,max_length=128)
    sample_reference: dict
    model_id: str | None = Field(default=None,max_length=128)
    idempotency_key: str | None = Field(default=None,max_length=128)


class RestoreResumeBody(StrictBody):
    restore_id: str = Field(min_length=1,max_length=128)


def create_router(runtime):
    router = APIRouter(tags=["strategies"])
    service = StrategyService(runtime)
    sessions = ExternalSessions(runtime)
    simulation = LocalSimulation(runtime)
    staging = runtime.root/".local/platform/strategy-staging"
    staging.mkdir(parents=True, exist_ok=True)

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PlatformError:
            raise
        except (StrategyError, ResearchError, TradingError) as exc:
            code = getattr(exc, "code", str(exc))
            raise PlatformError(code, 403 if code in {"ACCOUNT_FORBIDDEN", "LIVE_DISABLED", "TRADING_MODE_DISABLED"} else 422) from None
        except (ValueError, TypeError, KeyError, OSError):
            raise PlatformError("INVALID_INPUT", 422) from None

    @router.get("/strategies")
    def strategies(request: Request, limit: int = 50, offset: int = 0):
        return invoke(service.list_versions, runtime.require_principal(request), limit, offset)

    @router.post("/strategies/template")
    async def create_template(request: Request, input: TemplateBody):
        principal = runtime.require_principal(request, write=True)
        body = input.model_dump(exclude_unset=True)
        if isinstance(principal, AccountDelegation):
            if (request.scope.get("state", {}).get("mcp_grant_scope") not in {"workbench", "external"}
                    or "trusted" in body or "model_id" in body):
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            return invoke(service.register_builtin, principal, input.strategy_id, input.style,
                          idempotency_key=input.idempotency_key)
        model_reference = None
        if body.get("model_id"):
            from finance_platform.research.service import ResearchService
            model = ResearchService(runtime).model(principal, body["model_id"])
            model_reference = {"artifact_id": model["model_id"], "sha256": model["model_hash"], "train_cutoff": model["train_cutoff"], "version": model["version"]}
        with tempfile.TemporaryDirectory(dir=staging) as directory:
            source = Path(directory)/"package"
            invoke(template, source, body["strategy_id"], body["style"], model=model_reference)
            return invoke(service.register, principal, source, trusted=body.get("trusted", False))

    @router.post("/strategies/import")
    async def import_package(request: Request, input: PackageImportBody):
        principal = runtime.require_principal(request, write=True)
        if isinstance(principal, AccountDelegation):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        body = input.model_dump(exclude_unset=True)
        if not isinstance(body, dict) or not {"archive_base64"} <= set(body) or set(body)-{"archive_base64", "trusted"} or not isinstance(body["archive_base64"], str) or len(body["archive_base64"]) > 1_400_000:
            raise PlatformError("INVALID_INPUT", 422)
        try:
            raw = base64.b64decode(body["archive_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise PlatformError("INVALID_INPUT", 422) from None
        with tempfile.TemporaryDirectory(dir=staging) as directory:
            archive = Path(directory)/"upload.zip"
            archive.write_bytes(raw)
            return invoke(service.register, principal, archive, trusted=body.get("trusted", False))

    @router.post("/strategies/configs")
    async def configure(request: Request, input: ConfigBody):
        principal = runtime.require_principal(request, write=True)
        body = input.model_dump(exclude_unset=True)
        try:
            mode = RunMode(body.get("mode", "BACKTEST"))
        except ValueError:
            raise PlatformError("INVALID_INPUT", 422) from None
        return invoke(service.configure, principal, body["strategy_id"], body["version"], body["parameters"],
                      mode=mode, idempotency_key=input.idempotency_key)

    @router.get("/strategies/configs")
    def configs(request: Request, limit: int = 50, offset: int = 0):
        return invoke(service.list_configs, runtime.require_principal(request), limit, offset)

    @router.get("/strategies/configs/{config_id}")
    def config(request: Request, config_id: str):
        return invoke(service.config, runtime.require_principal(request), config_id)

    @router.post("/strategies/instances")
    async def create_instance(request: Request, input: InstanceBody):
        principal = runtime.require_principal(request, write=True)
        return invoke(service.create_instance, principal, input.config_id, idempotency_key=input.idempotency_key)

    @router.get("/strategies/instances")
    def instances(request: Request, limit: int = 50, offset: int = 0):
        return invoke(service.list_instances, runtime.require_principal(request), limit, offset)

    @router.get("/strategies/instances/{instance_id}")
    def instance(request: Request, instance_id: str):
        return invoke(service.instance, runtime.require_principal(request), instance_id)

    @router.post("/strategies/instances/{instance_id}/start-external")
    def start_external(request: Request, instance_id: str, input: ExternalStartBody):
        principal = runtime.require_principal(request, write=True)
        return invoke(sessions.start, principal, instance_id, **input.model_dump())

    @router.post("/strategies/instances/{instance_id}/start-simulation")
    @router.post("/strategies/instances/{instance_id}/resume-simulation")
    def start_simulation(request: Request,instance_id: str,input: SimulationStartBody):
        return invoke(simulation.start,runtime.require_principal(request,write=True),instance_id,**input.model_dump())

    @router.get("/strategies/instances/{instance_id}/simulation")
    def simulation_status(request:Request,instance_id:str):
        return invoke(simulation.session,runtime.require_principal(request),instance_id)

    @router.post("/strategies/resume-after-restore")
    def resume_restored(request:Request,input:RestoreResumeBody):
        return resume_after_restore(runtime,runtime.require_principal(request,write=True),input.restore_id)

    @router.get("/signals/{instance_id}/messages")
    def messages(request: Request, instance_id: str, limit: int = 50, offset: int = 0):
        return invoke(sessions.list_messages, runtime.require_principal(request), instance_id, limit, offset)

    @router.post("/strategies/instances/{instance_id}/{action}")
    def transition(request: Request, instance_id: str, action: str, input: LifecycleBody = LifecycleBody()):
        principal = runtime.require_principal(request, write=True)
        target = {"validate": "VALIDATED", "pause": "PAUSED", "stop": "STOPPED", "archive": "ARCHIVED"}.get(action)
        if target is None:
            raise PlatformError("INVALID_INPUT", 422)
        if isinstance(principal, AccountDelegation) and (target != "VALIDATED" or input.cancel_orders):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        with closing(runtime.database.connect()) as con:
            bound = con.execute("SELECT 1 FROM strategy_signal_bindings WHERE instance_id=?", (instance_id,)).fetchone()
            local = con.execute("SELECT 1 FROM strategy_simulation_sessions WHERE instance_id=?", (instance_id,)).fetchone()
        if bound and target in {"PAUSED", "STOPPED", "ARCHIVED"}:
            return invoke(sessions.stop, principal, instance_id, target, cancel_orders=input.cancel_orders)
        if local and target in {"PAUSED","STOPPED","ARCHIVED"}:
            return invoke(simulation.stop,principal,instance_id,target,cancel_orders=input.cancel_orders)
        return invoke(service.transition, principal, instance_id, target, idempotency_key=input.idempotency_key)

    # Exact HTTP signal route declared separately from generic lifecycle action.
    @router.post("/signals/{instance_id}")
    async def external(request: Request, instance_id: str):
        principal = runtime.require_principal(request, write=True)
        message = await request.json()
        return invoke(sessions.accept, principal, instance_id, message, request.headers.get("x-signal-signature", ""))

    return router
