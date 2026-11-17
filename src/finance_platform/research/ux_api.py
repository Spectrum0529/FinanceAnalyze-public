"""Product endpoints; MCP delegates these exact service operations."""
from fastapi import APIRouter,Request
from finance_platform.core.auth import AccountDelegation
from finance_platform.core.errors import PlatformError
from .portfolio import PortfolioError
from .daily import DailyError
from finance_platform.data.contracts import DataError
from .ux_service import ResearchTaskService


def create_router(runtime,*,service=None):
    service=service or ResearchTaskService(runtime);router=APIRouter(prefix="/research",tags=["dataset-research"])
    def call(fn,*args):
        try:return fn(*args)
        except (PortfolioError,DailyError,DataError) as error:raise PlatformError(error.code,422)from None

    def scope(request):
        return request.scope.get("state", {}).get("mcp_grant_scope")

    @router.get("/portfolio/catalog")
    def catalog(request:Request):
        principal = runtime.require_principal(request)
        value = call(service.catalog, principal)
        if isinstance(principal, AccountDelegation) and scope(request) in {"read", "research"}:
            return {**value, "sdk_instances":[]}
        return value

    @router.post("/portfolio/backtests")
    def submit(request:Request,body:dict):
        principal = runtime.require_principal(request, write=True)
        if (isinstance(principal, AccountDelegation) and body.keys() & {"instance_id", "benchmarks"}
                and scope(request) not in {"workbench", "external"}):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        return call(service.submit_portfolio, principal, body)
    @router.get("/portfolio/results")
    def results(request:Request,limit:int=50,offset:int=0):return call(service.list_results,runtime.require_principal(request),limit,offset)
    @router.get("/portfolio/results/{run_id}")
    def result(request:Request,run_id:str):
        principal = runtime.require_principal(request)
        value = call(service.result, principal, run_id)
        if isinstance(principal, AccountDelegation):
            service.require_mcp_report(principal, run_id, value)
        return value
    @router.post("/tasks/plan")
    def plan(request:Request,body:dict):return call(service.plan,runtime.require_principal(request,write=True),body)
    @router.post("/tasks/start")
    def start(request:Request,body:dict):return call(service.start,runtime.require_principal(request,write=True),body)
    @router.get("/tasks/{job_id}")
    def status(request:Request,job_id:str):return call(service.status,runtime.require_principal(request),job_id)
    @router.get('/tasks/{job_id}/search-evidence')
    def search_evidence(request:Request,job_id:str,limit:int=20,offset:int=0):return call(service.search_events,runtime.require_principal(request),job_id,limit,offset)
    @router.get('/tasks/{job_id}/search-evidence/{sequence}/series')
    def search_evidence_section(request:Request,job_id:str,sequence:int,section:str='',limit:int=500,offset:int=0,expected_sha256:str|None=None):
        return call(service.search_event_section,runtime.require_principal(request),job_id,sequence,section,limit,offset,expected_sha256)
    @router.post("/tasks/{job_id}/cancel")
    def cancel(request:Request,job_id:str):return call(service.cancel,runtime.require_principal(request,write=True),job_id)
    @router.post("/tasks/{job_id}/resume")
    def resume(request:Request,job_id:str,body:dict):return call(service.resume,runtime.require_principal(request,write=True),job_id,body)
    return router
