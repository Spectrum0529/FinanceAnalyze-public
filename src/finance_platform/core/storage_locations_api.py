"""Three storage settings only; all filesystem behavior stays in StorageService."""
from typing import Literal
from fastapi import APIRouter, Request
from pydantic import Field
from .team_profiles import Strict
from .errors import PlatformError

StorageCategory = Literal['market_data', 'reports', 'artifacts']


class StorageLocationCheck(Strict):
    directory: str | None = Field(max_length=4096)


class StorageLocationSave(StorageLocationCheck):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128,
                                pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


def create_router(runtime):
    router = APIRouter(prefix='/storage-locations', tags=['存储位置'])

    def principal(request, *, write=False):
        value = runtime.require_principal(request, write=write)
        if write and getattr(runtime, "hosted_accounts", False):
            raise PlatformError("FORBIDDEN", 403)
        mode = runtime.access_mode
        if (mode not in {'accounts', 'local-personal'}
                or mode == 'local-personal' and value.user_id != 'local-personal'
                or write and mode == 'accounts' and value.role != 'admin'):
            raise PlatformError('FORBIDDEN', 403)
        runtime.require_owner(value, value.user_id, write=write)
        return value

    def service():
        # Resolve after authentication. Never construct a storage/probe service
        # while importing the router or on an unauthenticated request.
        from .storage_locations import get_storage
        return get_storage(runtime)

    @router.get('')
    def settings(request: Request):
        owner = principal(request)
        return service().get_settings(owner)

    @router.post('/{category}/check')
    def check(category: StorageCategory, body: StorageLocationCheck, request: Request):
        owner = principal(request, write=True)
        return service().check_location(owner, category, body.directory)

    @router.put('/{category}')
    def save(category: StorageCategory, body: StorageLocationSave, request: Request):
        owner = principal(request, write=True)
        return service().save_location(owner, category, body.directory,
                                       expected_revision=body.expected_revision,
                                       idempotency_key=body.idempotency_key)

    return router
