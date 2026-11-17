"""Research router using the common runtime authentication and persistent jobs."""

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from typing import Literal
from finance_platform.core.errors import PlatformError
from .backends import comparable
from .data import ResearchError
from .jobs import submit_training_example
from .service import ResearchService


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReferenceBody(StrictBody):
    resource_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SampleBody(StrictBody):
    frequency: Literal["1d", "1m"] = "1d"
    count: StrictInt = Field(default=8, ge=2, le=8)
    two_assets: StrictBool = False
    snapshot_id: str | None = Field(default=None, max_length=128)


class BacktestBody(StrictBody):
    instance_id: str = Field(min_length=1, max_length=128)
    sample_reference: ReferenceBody
    backend: Literal["event", "batch"] = "event"
    model_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)


class CompareBody(StrictBody):
    left_run_id: str = Field(min_length=1, max_length=128)
    right_run_id: str = Field(min_length=1, max_length=128)


class TrainBody(StrictBody):
    kind: Literal["rolling", "expanding"] = "expanding"
    seed: StrictInt = Field(default=0, ge=0, lt=2**32)


class QlibBody(StrictBody):
    export_reference: ReferenceBody
    workflow: dict
    idempotency_key: str | None = Field(default=None,max_length=128)


class QlibInputPrepareBody(StrictBody):
    profile_id: Literal["FA014-QLIB-US-OFFLINE-V2"] = "FA014-QLIB-US-OFFLINE-V2"
    idempotency_key: str | None = Field(default=None,max_length=128)


class QlibInputRegisterBody(StrictBody):
    idempotency_key: str | None = Field(default=None,max_length=128)


class DebateOptions(StrictBody):
    debate_rounds: StrictInt = Field(default=1,ge=1,le=2)
    risk_rounds: StrictInt = Field(default=1,ge=1,le=2)
    tool_rounds: StrictInt = Field(default=1,ge=1,le=2)


class EvidenceResultBody(StrictBody):
    kind: Literal["backtesting","qlib"]
    run_id: str = Field(min_length=1,max_length=128)


class DebateBody(StrictBody):
    agent_team_snapshot_id: str | None = Field(default=None,min_length=1,max_length=128)
    data_reference: ReferenceBody
    ticker: str = Field(min_length=1,max_length=18)
    trade_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    evidence_results: list[EvidenceResultBody] = Field(default_factory=list,max_length=4)
    options: DebateOptions = Field(default_factory=DebateOptions)
    idempotency_key: str | None = Field(default=None,max_length=128)


class SearchBody(StrictBody):
    instance_id: str = Field(min_length=1, max_length=128)
    sample_reference: ReferenceBody
    space: list[dict] = Field(min_length=1, max_length=16)
    budget: StrictInt = Field(ge=1, le=16)


def create_router(runtime):
    router = APIRouter(tags=["research"])
    from .daily_api import create_router as create_daily_router
    router.include_router(create_daily_router(runtime))
    service = ResearchService(runtime)
    router.include_router(create_native_router(runtime,service=service))

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PlatformError:
            raise
        except ResearchError as exc:
            raise PlatformError(str(exc), 422) from None
        except (ValueError, TypeError, KeyError):
            raise PlatformError("INVALID_INPUT", 422) from None

    @router.get("/research/results")
    def results(request: Request, limit: int = 50, offset: int = 0):
        principal = runtime.require_principal(request)
        return service.list_results(principal, limit, offset)

    @router.get("/research/results/{run_id}")
    def result(request: Request, run_id: str):
        return invoke(service.result, runtime.require_principal(request), run_id)

    @router.get("/research/results/{run_id}/export")
    def export(request: Request, run_id: str):
        raw = invoke(service.export_result, runtime.require_principal(request), run_id)
        return Response(raw, media_type="application/json", headers={"Content-Disposition": 'attachment; filename="research-'+run_id+'.json"'})

    @router.post("/research/searches")
    def search(request: Request, input: SearchBody):
        return invoke(service.submit_search, runtime.require_principal(request, write=True), **input.model_dump())

    @router.get("/research/searches/{job_id}")
    def search_status(request: Request, job_id: str):
        return invoke(service.search_status, runtime.require_principal(request), job_id)

    @router.post("/research/samples")
    async def sample(request: Request, input: SampleBody):
        principal = runtime.require_principal(request, write=True)
        body = input.model_dump(exclude_unset=True)
        if not isinstance(body, dict) or set(body)-{"frequency", "count", "two_assets", "snapshot_id"}:
            raise PlatformError("INVALID_INPUT", 422)
        if "snapshot_id" in body:
            if set(body) != {"snapshot_id"}:
                raise PlatformError("INVALID_INPUT", 422)
            return invoke(service.from_snapshot, principal, body["snapshot_id"])
        return invoke(service.create_sample, principal, **body)

    @router.post("/research/backtests")
    async def submit(request: Request, input: BacktestBody):
        principal = runtime.require_principal(request, write=True)
        body = input.model_dump(exclude_unset=True)
        if not isinstance(body, dict) or not {"instance_id", "sample_reference"} <= set(body) or set(body)-{"instance_id", "sample_reference", "backend", "model_id", "idempotency_key"}:
            raise PlatformError("INVALID_INPUT", 422)
        return invoke(service.submit, principal, **body)

    @router.post("/research/compare")
    async def compare(request: Request, input: CompareBody):
        principal = runtime.require_principal(request)
        body = input.model_dump()
        if not isinstance(body, dict) or set(body) != {"left_run_id", "right_run_id"}:
            raise PlatformError("INVALID_INPUT", 422)
        left, right = (invoke(service.result, principal, body[k]) for k in ("left_run_id", "right_run_id"))
        return invoke(comparable, left, right)

    @router.get("/models")
    def models(request: Request, limit: int = 50, offset: int = 0):
        return service.list_models(runtime.require_principal(request), limit, offset)

    @router.get("/models/{model_id}")
    def model(request: Request, model_id: str):
        return invoke(service.model, runtime.require_principal(request), model_id)

    @router.post("/models/train-example")
    async def train(request: Request, input: TrainBody):
        principal = runtime.require_principal(request, write=True)
        body = input.model_dump(exclude_unset=True)
        if not isinstance(body, dict) or set(body)-{"kind", "seed"}:
            raise PlatformError("INVALID_INPUT", 422)
        return invoke(submit_training_example, service, principal, **body)

    return router


def create_native_router(runtime,*,service=None):
    """Native routes compose into the existing research router; no SDK setup."""
    router=APIRouter(tags=["research-native"]);service=service or ResearchService(runtime)
    def invoke(function,*args,**kwargs):
        try:return function(*args,**kwargs)
        except PlatformError:raise
        except ValueError as error:raise PlatformError(getattr(error,"code","INVALID_INPUT"),422) from None

    from .qlib_service import QlibResearchService
    qlib=QlibResearchService(service)
    @router.get("/research/qlib/capabilities")
    def qlib_capabilities(request:Request):return invoke(qlib.capabilities,runtime.require_principal(request))
    @router.get("/research/qlib/templates")
    def qlib_templates(request:Request):return invoke(qlib.templates,runtime.require_principal(request))
    @router.get("/research/qlib/exports")
    def qlib_exports(request:Request,limit:int=25,offset:int=0):return invoke(qlib.exports,runtime.require_principal(request),limit,offset)
    @router.post("/research/qlib/inputs/prepare")
    def qlib_prepare_input(request:Request,input:QlibInputPrepareBody):return invoke(qlib.prepare_input,runtime.require_principal(request,write=True),**input.model_dump())
    @router.post("/research/qlib/inputs/{job_id}/register")
    def qlib_register_input(request:Request,job_id:str,input:QlibInputRegisterBody):return invoke(qlib.register_input,runtime.require_principal(request,write=True),job_id,**input.model_dump())
    @router.post("/research/qlib")
    def qlib_submit(request:Request,input:QlibBody):return invoke(service.submit_qlib,runtime.require_principal(request,write=True),**input.model_dump())
    @router.get("/research/debate-capabilities")
    def debate_capabilities(request:Request):return invoke(service.debate_service().capabilities,runtime.require_principal(request))
    @router.post("/research/debates")
    def submit_debate(request:Request,input:DebateBody):
        value=input.model_dump()
        if input.agent_team_snapshot_id is not None:
            value['options']=input.options.model_dump(exclude_unset=True) if 'options' in input.model_fields_set else None
        return invoke(service.debate_service().submit,runtime.require_principal(request,write=True),**value)
    @router.get("/research/debates/{job_id}")
    def debate_status(request:Request,job_id:str):return invoke(service.debate_service().status,runtime.require_principal(request),job_id)
    @router.post("/research/debates/{job_id}/cancel")
    def debate_cancel(request:Request,job_id:str):return invoke(service.debate_service().cancel,runtime.require_principal(request,write=True),job_id)
    @router.post("/research/debates/{job_id}/retry")
    def debate_retry(request:Request,job_id:str):return invoke(service.debate_service().retry,runtime.require_principal(request,write=True),job_id)
    return router
