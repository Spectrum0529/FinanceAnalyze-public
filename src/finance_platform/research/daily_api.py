"""Separate typed daily-simulation family, ordinary session/Origin/CSRF gates."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from typing import Literal
from finance_platform.core.errors import PlatformError
from finance_platform.data.contracts import DataError
from .daily import DailyError
from .daily_service import DailyBacktestService


class DailyBacktestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    @model_validator(mode="before")
    @classmethod
    def reject_legacy_slippage(cls, value):
        if type(value) is dict and "slippage_bps" in value:
            raise ValueError("DAILY_LEGACY_SLIPPAGE_UNSUPPORTED: use explicitly labeled spread_bps for the new engine")
        return value

    ticker: str = Field(min_length=1, max_length=18)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    strategy: Literal["buy_hold", "sma20", "sma_cross_5_20"]
    provider: Literal["yfinance", "akshare"] = "yfinance"
    initial_cash: str = Field(default="10000.00", max_length=32)
    fee_per_share: str = Field(default="0.005", max_length=32)
    minimum_fee: str = Field(default="1.00", max_length=32)
    spread_bps: str = Field(default="0", max_length=32)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    history_policy: Literal["reuse_or_download","refresh"] = "reuse_or_download"
    history_id: str | None = Field(default=None,min_length=1,max_length=128)
    history_version: StrictInt | None = Field(default=None,ge=1,le=2147483647)
    cache_policy: Literal["use", "bypass"] = "use"


class LifecycleBody(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    action:Literal["rename","delete","restore"]
    expected_revision:StrictInt=Field(ge=0,le=2147483647)
    idempotency_key:str=Field(min_length=1,max_length=128)
    name:str|None=Field(default=None,min_length=1,max_length=120)


def create_router(runtime, *, service=None):
    router = APIRouter(prefix="/research/daily", tags=["daily-assumption-simulation"])
    service = service or DailyBacktestService(runtime)

    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (DailyError, DataError) as error:
            raise PlatformError(error.code,404 if error.code in {"RESOURCE_NOT_FOUND","HISTORY_NOT_FOUND"} else 422) from None

    @router.get("/catalog")
    def catalog(request: Request, query:str=""):
        return invoke(service.catalog, runtime.require_principal(request),query)

    @router.post("/backtests")
    def submit(request: Request, body: DailyBacktestBody):
        principal = runtime.require_principal(request, write=True)
        value = body.model_dump(exclude={"idempotency_key", "cache_policy"})
        if body.cache_policy == "bypass":
            return invoke(service.submit, principal, value, idempotency_key=body.idempotency_key, cache_policy="bypass")
        return invoke(service.submit, principal, value, idempotency_key=body.idempotency_key)

    @router.get("/results")
    def results(request: Request, limit: int = 50, offset: int = 0,state:Literal["active","deleted","all"]="active"):
        return invoke(service.list_results, runtime.require_principal(request), limit, offset,state)

    @router.get("/submissions/{idempotency_key}")
    def submission(request: Request, idempotency_key: str):
        return invoke(service.submission, runtime.require_principal(request), idempotency_key)

    @router.get("/results/{run_id}")
    def result(request: Request, run_id: str,include_deleted:bool=False):
        return invoke(service.result, runtime.require_principal(request), run_id,include_deleted=include_deleted)

    @router.get("/results/{run_id}/series")
    def series(request:Request,run_id:str,kind:Literal["equity_curve","trades","decisions","orders","rejections","unsubmitted_outputs"]="equity_curve",limit:int=500,offset:int=0,include_deleted:bool=False):
        return invoke(service.series,runtime.require_principal(request),run_id,kind,limit,offset,include_deleted=include_deleted)

    @router.post("/results/{run_id}/lifecycle")
    def lifecycle(request:Request,run_id:str,body:LifecycleBody):
        return invoke(service.lifecycle,runtime.require_principal(request,write=True),run_id,**body.model_dump())

    return router
