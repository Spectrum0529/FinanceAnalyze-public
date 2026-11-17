"""Same profile service for browser API and local MCP gateway."""
from typing import Literal
from fastapi import APIRouter,Request,Query
from pydantic import Field
from .errors import PlatformError
from .model_library import ProfileConfiguration
from .team_profiles import Strict

class SaveProfile(Strict):
    name:str=Field(min_length=1,max_length=80)
    configuration:ProfileConfiguration
    idempotency_key:str=Field(min_length=1,max_length=128)

class UpdateProfile(SaveProfile):
    expected_revision:int=Field(ge=1)

class CopyProfile(Strict):
    name:str=Field(min_length=1,max_length=80)
    revision:int=Field(ge=1)
    idempotency_key:str=Field(min_length=1,max_length=120)

class ComposeTeam(Strict):
    mode:Literal['role_team']
    selections:dict=Field(min_length=1,max_length=13)
    configuration:dict|None=None

class ModelTestRequest(Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1,max_length=128)
    allow_billable_call: bool

def create_router(runtime):
    router=APIRouter(prefix='/model-profiles',tags=['模型配置'])
    service=runtime.model_profiles
    @router.get('/capabilities')
    def capabilities(request:Request):
        runtime.require_principal(request);return service.capabilities()
    @router.post('/compose-team')
    def compose(body:ComposeTeam,request:Request):
        return service.compose_team(runtime.require_principal(request,write=True),body.selections,configuration=body.configuration,mode=body.mode)
    @router.get('')
    def profiles(request:Request,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=10000)):
        return service.list(runtime.require_principal(request),limit,offset)
    @router.post('',status_code=201)
    def create(body:SaveProfile,request:Request):
        return service.save(runtime.require_principal(request,write=True),body.name,body.configuration.model_dump(),idempotency_key=body.idempotency_key)
    if hasattr(runtime, 'model_connection_test'):
        probe = runtime.model_connection_test

        @router.get('/{profile_id}/test')
        def test_status(profile_id: str, request: Request):
            return probe.status(runtime.require_principal(request), profile_id)

        @router.post('/{profile_id}/test', status_code=202)
        def start_test(profile_id: str, body: ModelTestRequest, request: Request):
            principal = runtime.require_principal(request, write=True)
            if not body.allow_billable_call:
                raise PlatformError('MODEL_TEST_CONFIRMATION_REQUIRED', 422)
            return probe.submit(principal, profile_id, body.expected_revision, body.idempotency_key)

    @router.get('/{profile_id}/revisions/{revision}')
    def revision(profile_id:str,revision:int,request:Request):
        return service.revision(runtime.require_principal(request),profile_id,revision)
    @router.post('/{profile_id}/copy',status_code=201)
    def copy(profile_id:str,body:CopyProfile,request:Request):
        return service.copy(runtime.require_principal(request,write=True),profile_id,body.name,body.revision,idempotency_key=body.idempotency_key)
    @router.get('/{profile_id}')
    def get(profile_id:str,request:Request):return service.get(runtime.require_principal(request),profile_id)
    @router.put('/{profile_id}')
    def update(profile_id:str,body:UpdateProfile,request:Request):
        return service.save(runtime.require_principal(request,write=True),body.name,body.configuration.model_dump(),profile_id=profile_id,expected_revision=body.expected_revision,idempotency_key=body.idempotency_key)
    @router.delete('/{profile_id}')
    def delete(profile_id:str,request:Request,expected_revision:int=Query(ge=1),idempotency_key:str=Query(min_length=1,max_length=128)):
        return service.delete(runtime.require_principal(request,write=True),profile_id,expected_revision,idempotency_key=idempotency_key)
    from .model_usage_api import create_router as create_usage_router
    root=APIRouter()
    root.include_router(router)
    root.include_router(create_usage_router(runtime))
    return root
