"""Owner-only cache controls through the normal application session."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field, field_validator

from .computation_cache import MAX_BYTES, MIN_BYTES
from .errors import PlatformError
from .team_profiles import Strict


class CacheCommand(Strict):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class CacheSettings(CacheCommand):
    enabled: bool
    max_bytes: int = Field(ge=MIN_BYTES, le=MAX_BYTES)


class CacheClear(CacheCommand):
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("confirmation required")
        return value


def create_router(runtime):
    router = APIRouter(prefix="/computation-cache", tags=["计算缓存"])

    def access(request, *, write=False):
        principal = runtime.require_principal(request, write=write)
        runtime.require_owner(principal, principal.user_id, write=write)
        service = runtime.computation_cache
        if service is None:
            raise PlatformError("UNSUPPORTED_CAPABILITY", 503)
        return principal, service

    @router.get("")
    def get(request: Request):
        principal, service = access(request)
        return service.get(principal)

    @router.put("")
    def settings(body: CacheSettings, request: Request):
        principal, service = access(request, write=True)
        return service.settings(principal, **body.model_dump())

    @router.post("/clear")
    def clear(body: CacheClear, request: Request):
        principal, service = access(request, write=True)
        return service.clear(principal, **body.model_dump())

    return router
