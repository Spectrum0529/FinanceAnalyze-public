"""Owner-bound HTTP routes shared by the native console and typed MCP."""
from fastapi import Request

from finance_platform.core.auth import AccountDelegation
from finance_platform.core.errors import PlatformError
from .contracts import DataError
from .market_lifecycle import change_connection, connections, require_credential
from .market_management import MarketManagement, approval


def mount_market_routes(router, runtime, library, *, service=None):
    service = service or MarketManagement(library)

    def actor(request, *, write=False):
        principal = runtime.require_principal(request, write=write)
        runtime.require_owner(principal, principal.user_id, write=write)
        return principal

    def call(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except DataError as error:
            if error.code == "RESOURCE_NOT_FOUND":
                raise PlatformError("NOT_FOUND", 404) from None
            raise
        except PlatformError:
            raise
        except (TypeError, KeyError, ValueError):
            raise DataError("INVALID_INPUT") from None

    @router.get("/managed")
    def managed(request: Request, q: str = "", source: str = "all", state: str = "active",
                kind: str = "all", provider: str | None = None, limit: int = 50, offset: int = 0):
        return call(service.list, actor(request), q=q, source=source, state=state, kind=kind,
                    provider=provider, limit=limit, offset=offset)

    @router.get("/managed/{source}/{ident}")
    def detail(request: Request, source: str, ident: str, version: int | None = None,
               include_trash: bool = False, version_limit: int = 20, version_offset: int = 0):
        return call(service.detail, actor(request), source, ident, version=version,
                    include_trash=include_trash, version_limit=version_limit, version_offset=version_offset)

    @router.post("/managed/{source}/{ident}/lifecycle")
    def lifecycle(request: Request, source: str, ident: str, body: dict):
        return call(service.lifecycle, actor(request, write=True), source, ident, body)

    @router.post("/managed/reuse")
    def reuse(request: Request, body: dict):
        return call(service.reuse, actor(request, write=True), body)

    @router.post("/managed/merge-preview")
    def merge_preview(request: Request, body: dict):
        return call(service.preview, actor(request), body)

    @router.post("/managed/merge")
    def merge(request: Request, body: dict):
        return call(service.merge, actor(request, write=True), body)

    @router.get("/connections/manage")
    def managed_connections(request: Request, q: str = "", state: str = "active",
                            limit: int = 50, offset: int = 0):
        principal = actor(request)
        return call(connections, library, principal.user_id, q=q, state=state, limit=limit, offset=offset,
                    delegated=isinstance(principal, AccountDelegation), publication_guard=approval(principal))

    @router.post("/connections/{ident}/manage")
    def managed_connection(request: Request, ident: str, body: dict):
        principal = actor(request, write=True)
        return call(change_connection, library, principal.user_id, ident, body,
                    publication_guard=approval(principal), check_reference=lambda *args: require_credential(runtime, *args),
                    delegated=isinstance(principal, AccountDelegation))
