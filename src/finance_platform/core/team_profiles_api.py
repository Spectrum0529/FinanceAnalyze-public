"""Thin FastAPI routes over the owner-scoped preset service."""
from fastapi import APIRouter, Path, Query, Request
from pydantic import Field
from .contracts import ContractError, identifier
from .errors import PlatformError
from .team_profiles import Strict, TeamConfiguration


class ConfigurationBody(Strict):
    configuration: TeamConfiguration


class PresetBody(ConfigurationBody):
    name: str = Field(min_length=1, max_length=80)
    idempotency_key:str|None=Field(default=None,min_length=1,max_length=128)


class UpdateBody(PresetBody):
    expected_revision: int = Field(ge=1)


class SnapshotBody(Strict):
    preset_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    idempotency_key:str|None=Field(default=None,min_length=1,max_length=128)

class CopyBody(Strict):
    name:str=Field(min_length=1,max_length=80)
    revision:int=Field(ge=1)
    idempotency_key:str=Field(min_length=1,max_length=128)


_TEAM_EXPECTED_OWNER = 'X-Team-Expected-Owner'


def _require_team_write(runtime, request):
    principal = runtime.require_principal(request, write=True)
    supplied = request.headers.getlist(_TEAM_EXPECTED_OWNER)
    if not supplied:
        return principal
    try:
        if len(supplied) != 1:
            raise ContractError('Multiple expected owner headers')
        expected_owner = identifier(supplied[0], 'expected owner')
    except ContractError:
        raise PlatformError('TEAM_WRITE_EXPECTED_OWNER_INVALID', 422) from None
    if expected_owner != principal.user_id:
        raise PlatformError('TEAM_WRITE_OWNER_MISMATCH', 409)
    return principal


def create_router(runtime):
    router = APIRouter(prefix='/agent-teams', tags=['agent teams'])
    service = runtime.agent_teams

    @router.get('/capabilities')
    def capabilities(request: Request):
        runtime.require_principal(request)
        return service.capabilities()

    @router.post('/preview')
    def preview(body: ConfigurationBody, request: Request):
        principal=runtime.require_principal(request, write=True)
        return service.preview(body.configuration.model_dump(),principal=principal)

    @router.get('/commands/{idempotency_key}')
    def command_receipt(request: Request, idempotency_key: str = Path(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')):
        principal = runtime.require_principal(request)
        return service.command_receipt(principal, idempotency_key)

    @router.get('/presets')
    def presets(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        return service.list(runtime.require_principal(request), limit, offset)

    @router.post('/presets', status_code=201)
    def create(body: PresetBody, request: Request):
        return service.save(_require_team_write(runtime, request), body.name, body.configuration.model_dump(),idempotency_key=body.idempotency_key)

    @router.get('/presets/{preset_id}')
    def get(preset_id: str, request: Request):
        return service.get(runtime.require_principal(request), preset_id)

    @router.put('/presets/{preset_id}')
    def update(preset_id: str, body: UpdateBody, request: Request):
        return service.save(_require_team_write(runtime, request), body.name, body.configuration.model_dump(),
                            preset_id=preset_id, expected_revision=body.expected_revision,idempotency_key=body.idempotency_key)

    @router.post('/presets/{preset_id}/copy',status_code=201)
    def copy(preset_id:str,body:CopyBody,request:Request):
        return service.copy(runtime.require_principal(request,write=True),preset_id,body.revision,body.name,idempotency_key=body.idempotency_key)

    @router.delete('/presets/{preset_id}')
    def delete(preset_id: str, request: Request, expected_revision: int = Query(ge=1),idempotency_key:str|None=Query(default=None,min_length=1,max_length=128)):
        return service.delete(runtime.require_principal(request, write=True), preset_id, expected_revision,idempotency_key=idempotency_key)

    @router.post('/snapshots', status_code=201)
    def snapshot(body: SnapshotBody, request: Request):
        return service.create_snapshot(_require_team_write(runtime, request), body.preset_id, body.revision,idempotency_key=body.idempotency_key)

    @router.get('/snapshots/{snapshot_id}')
    def get_snapshot(snapshot_id: str, request: Request):
        return service.get_snapshot(runtime.require_principal(request), snapshot_id)

    return router
