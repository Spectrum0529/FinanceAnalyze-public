"""Write-only credential API; use the same principal and CSRF as the UI."""
import json
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from finance_platform.core.errors import PlatformError
from finance_platform.core.local_personal import require_direct_local_scope
from .provider_credentials import WRDS_SLOT


def create_router(runtime, store):
    router = APIRouter(tags=['provider-credentials'])

    def require_credential_owner(slot, request):
        principal = runtime.require_principal(request, write=True)
        if slot == WRDS_SLOT and runtime.access_mode != "local-personal":
            if getattr(runtime, "hosted_accounts", False):
                raise PlatformError("WRDS_CREDENTIAL_LOCAL_ONLY", 403)
            try:
                require_direct_local_scope(request.scope, runtime.allowed_origins)
            except PlatformError:
                raise PlatformError("WRDS_CREDENTIAL_LOCAL_ONLY", 403) from None
            owner = runtime.auth.require_recent(request.cookies.get("finance_session"))
            if owner != principal.user_id:
                raise PlatformError("FORBIDDEN", 403)
        return principal

    @router.get('/operations/provider-credentials/{slot}')
    def status(slot: str, request: Request):
        principal = runtime.require_principal(request)
        return store.status(principal.user_id, slot)

    @router.put('/operations/provider-credentials/{slot}')
    async def set_value(slot: str, request: Request):
        principal = require_credential_owner(slot, request)
        maximum_body = 32768 if slot == WRDS_SLOT else 8192
        data = bytearray()
        try:
            async for chunk in request.stream():
                if len(data) + len(chunk) > maximum_body:
                    raise ValueError()
                data.extend(chunk)
            value = json.loads(data)
            if not isinstance(value, dict) or set(value) != {'value','provider','endpoint_id'}:
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise PlatformError('INVALID_INPUT', 422) from None
        finally:
            data[:] = b'\0' * len(data)

        def save():
            if slot == WRDS_SLOT and runtime.access_mode != "local-personal":
                if runtime.auth.require_recent(request.cookies.get("finance_session")) != principal.user_id:
                    raise PlatformError("FORBIDDEN", 403)
            # Audit only fixed action and slot, never input/repr/exception values.
            runtime.audit(principal, 'credentials.set', store.reference(principal.user_id, slot))
            return store.set(principal.user_id, slot, value['value'], provider=value['provider'],
                             endpoint_id=value['endpoint_id'])

        return await run_in_threadpool(save)

    @router.delete('/operations/provider-credentials/{slot}')
    def delete_value(slot: str, request: Request):
        principal = require_credential_owner(slot, request)
        runtime.audit(principal, 'credentials.delete', store.reference(principal.user_id, slot))
        return store.delete(principal.user_id, slot)

    return router
