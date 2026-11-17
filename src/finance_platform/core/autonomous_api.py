"""Fixed authenticated domain routes, installed by the shared integrator."""
from fastapi import APIRouter, Query, Request
from .autonomous_models import CreateSession, Control, PrepareSession,SessionView,CursorPageView,SaveResearchCandidate,SavedCandidateBacktest
from .errors import PlatformError
from .model_usage import ModelUsageService
from .model_usage_models import UsageSummary


def create_router(runtime, *, service, inputs=None):
    router = APIRouter(prefix='/research/autonomous', tags=['autonomous research'])
    @router.get('/sessions/{session_id}/model-usage',response_model=UsageSummary)
    def model_usage(session_id:str,request:Request,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0)):
        return ModelUsageService(runtime,sessions=service).summary(runtime.require_principal(request),'session',session_id,limit,offset)

    @router.get('/candidates')
    def candidates(request:Request,kind:str|None=Query(None,pattern='^(strategy|factor)$'),
                   limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=10000)):
        return runtime.research_candidates.list(runtime.require_principal(request),kind=kind,limit=limit,offset=offset)

    @router.get('/candidates/{candidate_id}')
    def candidate(candidate_id:str,request:Request):
        return runtime.research_candidates.get(runtime.require_principal(request),candidate_id)

    @router.post('/candidates')
    def save_candidate(body:SaveResearchCandidate,request:Request):
        p=runtime.require_principal(request,write=True)
        code=getattr(service,'unavailable_code',None)
        if code is not None:raise PlatformError(code,503)
        return runtime.research_candidates.save(p,body.session_id,body.attempt_id,body.logical_id,body.name)

    @router.post('/candidates/{candidate_id}/backtests',status_code=202)
    def saved_backtest(candidate_id:str,body:SavedCandidateBacktest,request:Request):
        p=runtime.require_principal(request,write=True)
        code=getattr(service,'unavailable_code',None)
        if code is not None:raise PlatformError(code,503)
        if inputs is None:raise PlatformError('SAVED_BACKTEST_UNAVAILABLE',503)
        return runtime.research_candidates.backtest(p,candidate_id,body.request.model_dump(),
            prepare=inputs.prepare,idempotency_key=body.idempotency_key)

    @router.get('/capabilities')
    def capabilities(request:Request):
        principal=runtime.require_principal(request)
        if inputs is None:return {'schema':'autonomous-capabilities-1','executor':{'ready':False,'reason':'TRUSTED_WINDOWS_PORTS_NOT_INSTALLED'},'evaluation':{'metrics':[]},'supports_development_only':True}
        return inputs.capabilities(principal)

    @router.post('/prepare')
    def prepare(body:PrepareSession,request:Request):
        principal=runtime.require_principal(request,write=True)
        if inputs is None:raise PlatformError('UNSUPPORTED_CAPABILITY',422)
        return inputs.prepare(principal,body.model_dump())

    @router.post('/sessions', status_code=201,response_model=SessionView,response_model_exclude_unset=True)
    def create(body: CreateSession, request: Request):
        return service.create(runtime.require_principal(request, write=True), body.spec.model_dump(), idempotency_key=body.idempotency_key)

    @router.get('/sessions')
    def listing(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        return service.list(runtime.require_principal(request), limit=limit, offset=offset)

    @router.get('/sessions/{session_id}',response_model=SessionView,response_model_exclude_unset=True)
    def read(session_id: str, request: Request):
        return service.get(runtime.require_principal(request), session_id)

    @router.get('/sessions/{session_id}/attempts/{attempt_id}/backtest/summary')
    def backtest_summary(session_id:str,attempt_id:str,request:Request):
        from finance_platform.research.autonomous_results import AutonomousBacktestResults
        value=AutonomousBacktestResults(service).summary(runtime.require_principal(request),session_id,attempt_id)
        return {**value,'section_url':'/api/v1/research/autonomous/sessions/'+session_id+'/attempts/'+attempt_id+'/backtest/series'}

    @router.get('/sessions/{session_id}/attempts/{attempt_id}/backtest/series')
    def backtest_series(session_id:str,attempt_id:str,request:Request,
                        section:str=Query('',max_length=1024),limit:int=Query(500,ge=1,le=1000),
                        offset:int=Query(0,ge=0,le=2000000),
                        expected_sha256:str|None=Query(None,pattern='^[0-9a-f]{64}$')):
        from finance_platform.research.autonomous_results import AutonomousBacktestResults
        return AutonomousBacktestResults(service).section(runtime.require_principal(request),session_id,attempt_id,
            section=section,limit=limit,offset=offset,expected_sha256=expected_sha256)

    @router.get('/sessions/{session_id}/progress')
    def progress(session_id:str,request:Request,limit:int=Query(20,ge=1,le=100),
                 before_ordinal:int|None=Query(None,ge=1)):
        from .autonomous_status import session_progress
        return session_progress(service,runtime.require_principal(request),session_id,
                                limit=limit,before_ordinal=before_ordinal)

    @router.post('/sessions/{session_id}/control',response_model=SessionView,response_model_exclude_unset=True)
    def control(session_id: str, body: Control, request: Request):
        return service.control(runtime.require_principal(request, write=True), session_id, body.model_dump())

    @router.get('/sessions/{session_id}/attempts',response_model=CursorPageView)
    def attempts(session_id: str, request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),cursor:str|None=Query(None,max_length=1024)):
        return service.page(runtime.require_principal(request), session_id, 'attempts', limit=limit, offset=offset,cursor=cursor)

    @router.get('/sessions/{session_id}/events',response_model=CursorPageView)
    def events(session_id: str, request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),cursor:str|None=Query(None,max_length=1024)):
        return service.page(runtime.require_principal(request), session_id, 'events', limit=limit, offset=offset,cursor=cursor)

    @router.get('/sessions/{session_id}/artifacts',response_model=CursorPageView)
    def artifacts(session_id: str, request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),cursor:str|None=Query(None,max_length=1024),attempt_id:str|None=Query(None,min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')):
        return service.page(runtime.require_principal(request), session_id, 'artifacts', limit=limit, offset=offset,cursor=cursor,attempt_id=attempt_id)

    @router.get('/sessions/{session_id}/artifacts/{artifact_id}')
    def artifact(session_id: str, artifact_id: str, request: Request):
        return service.artifact(runtime.require_principal(request), session_id, artifact_id)

    return router
