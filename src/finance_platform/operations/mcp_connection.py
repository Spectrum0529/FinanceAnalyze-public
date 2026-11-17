"""Public local MCP client configuration, derived from trusted ASGI listener.

Never accept a client-supplied command, URL, path, port or environment payload.
This describes the existing installation; it does not launch another service.
"""
from importlib import metadata
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fastapi import APIRouter, Request
from pydantic import Field
from finance_platform.core.errors import PlatformError
from finance_platform.core.mcp_grants import GrantScope, SCOPE_LABELS, client_label, scopes_for
from finance_platform.core.mcp_proof import listener_origin
from finance_platform.core.team_profiles import Strict
from finance_platform.operations.paths import contained


def _installation_status(root):
    required = ('.venv/Scripts/python.exe', 'tools/platform_ops/mcp_stdio.py',
                'src/finance_platform/core/mcp_server.py')
    for relative in required:
        path = contained(root, root / relative)
        if not path.is_file() or path.stat().st_nlink != 1:
            return False, ['MCP_INSTALLATION_INCOMPLETE']
    try:
        distribution = metadata.distribution('mcp')
        # Core contract uses the official SDK 2.x MCPServer API, not FastMCP.
        if distribution.version != '2.3.0':
            return False, ['MCP_SDK_INCOMPATIBLE']
    except metadata.PackageNotFoundError:
        return False, ['MCP_SDK_NOT_INSTALLED']
    return True, []


class Pairing(Strict):
    client_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    client_name: str | None = Field(default=None, min_length=1, max_length=48, repr=False)
    scope: GrantScope


class Claim(Strict):
    pairing_id: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    client_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    public_key: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)
    signature: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$", repr=False)
    key_proof: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$", repr=False)


class EasyConnect(Strict):
    client_id: Literal["codex", "deepseek"]
    access: Literal["browse", "workbench"]
    allow_model_budget: bool = False


def _client_command(root, port, client_id):
    return (str(root / ".venv/Scripts/python.exe"),
            ("-I", "-B", str(root / "tools/platform_ops/mcp_stdio.py"),
             "--port", str(port), "--client", client_id))


@lru_cache(maxsize=4)
def _source_identity(root):
    from .source_binding import inspect_source
    return inspect_source(root)


@contextmanager
def _easy_listener(runtime, request, client_id):
    from .launch import _auth_database_binding
    from .mcp_keystore import McpKeyStore
    from .mcp_peer import WindowsPeer

    origin = listener_origin(runtime.allowed_origins, request.scope)
    if runtime.hosted_accounts or runtime.access_mode != "accounts" or request.scope["server"][0] != "127.0.0.1":
        raise PlatformError("MCP_MANAGED_LISTENER_REQUIRED", 503)
    root = Path(runtime.root).resolve()
    port = request.scope["server"][1]
    try:
        available, reasons = _installation_status(root)
        if not available:
            raise PlatformError(reasons[0], 503)
        expected = {**_source_identity(root), "auth_database_binding": _auth_database_binding(root, runtime.database.path)}
    except (OSError, ValueError) as error:
        if isinstance(error, PlatformError):
            raise
        raise PlatformError("MCP_MANAGED_LISTENER_REQUIRED", 503) from None
    with WindowsPeer(root, expected, port).verified(caller=request.scope["client"]):
        yield origin, McpKeyStore(root, port, client_id)


def _status(grants, principal, client_id, origin, store):
    try:
        status = grants.local_status(principal.user_id, client_id, origin, store.read())
    except PlatformError as error:
        if error.code not in {"MCP_AUTHORIZATION_REQUIRED", "MCP_SCOPE_REQUIRED"}:
            raise
        return _empty_status(client_id, "authorization_required", "MCP_AUTHORIZATION_REQUIRED")
    confirmed = status["client_confirmed_at"] is not None
    return {"client_id": client_id, "state": "connected" if confirmed else "paired_waiting_for_client",
            "reason_code": None if confirmed else "MCP_CLIENT_DISCOVERY_PENDING", **status}


def _empty_status(client_id, state, reason):
    return {"client_id": client_id, "state": state, "scope": None, "reason_code": reason,
            "expires_at": None, "client_confirmed_at": None}


def _ensure_client(runtime, client_id, port, configure_client):
    install = configure_client or getattr(runtime, "mcp_easy_configure_client", None)
    if install is None:
        try:
            from .mcp_client_config import ensure_client_config
        except ImportError:
            raise PlatformError("MCP_CLIENT_CONFIG_UNAVAILABLE", 503) from None
        install = ensure_client_config
    result = install(Path(runtime.root).resolve(), client_id, port)
    if result["status"] in {"configured", "already_configured"}:
        return
    reason = result.get("reason_code") or "MCP_CLIENT_CONFIG_UNAVAILABLE"
    status = 409 if reason in {"MCP_CLIENT_CONFIG_CONFLICT", "MCP_CLIENT_CONFIG_BUSY"} else 503
    raise PlatformError(reason, status)


def connection_configuration(runtime, scope, principal, client_id):
    if runtime.hosted_accounts:
        raise PlatformError("FORBIDDEN", 403)
    api_url = listener_origin(runtime.allowed_origins, scope)
    name = client_label(client_id)
    root = Path(runtime.root).resolve()
    try:
        available, reasons = _installation_status(root)
    except (PlatformError, OSError):
        available, reasons = False, ['MCP_INSTALLATION_INCOMPLETE']
    command, argv = _client_command(root, scope["server"][1], client_id)
    args = list(argv)
    personal = runtime.access_mode == 'local-personal'
    if personal:
        args += ['--access-mode', 'local-personal']
    configuration = {'mcpServers': {'financeanalyze': {'command': command, 'args': args}}}
    scopes = [] if personal else scopes_for(principal.role)
    return {'available': available, 'reason_codes': reasons,
            'message': '复制配置到 MCP 客户端，保持当前程序运行。' if available else 'MCP 组件尚未安装完整，请完成本地安装。',
            'transport': 'stdio', 'api_url': api_url,
            'availability_basis': 'LOCAL_INSTALLATION_ONLY_NOT_PROTOCOL_ACCEPTANCE',
            'client_id': client_id, 'client_name': name, 'scopes': scopes,
            'scope_labels': {scope: SCOPE_LABELS[scope] for scope in scopes},
            'client_config': configuration if available else None,
            'pairing_command': {'command': command, 'args': args + ['--pair']} if available and not personal else None}


def create_router(runtime, grants=None, *, configure_client=None):
    router = APIRouter(prefix='/operations/mcp-connection', tags=['mcp-connection'])

    @router.get('')
    def configuration(request: Request, client_id: str = 'codex'):
        principal = runtime.require_principal(request)
        return connection_configuration(runtime, request.scope, principal, client_id)

    if grants is None:
        return router

    @router.get("/status")
    def easy_status(request: Request, client_id: Literal["codex", "deepseek"] = "codex"):
        principal = runtime.require_principal(request)
        try:
            with _easy_listener(runtime, request, client_id) as (origin, store):
                return _status(grants, principal, client_id, origin, store)
        except PlatformError as error:
            if error.status != 503:
                raise
            return _empty_status(client_id, "unavailable", error.code)

    @router.post("/connect")
    def easy_connect(body: EasyConnect, request: Request):
        token = request.cookies.get("finance_session")
        csrf = request.headers.get("x-csrf-token")
        principal = grants.auth.require_csrf(token, csrf, require_write=False)
        if body.allow_model_budget and body.access != "workbench":
            raise PlatformError("INVALID_INPUT", 422)
        scope = "external" if body.allow_model_budget else body.access
        if scope not in scopes_for(principal.role):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        grants.auth.require_recent(token)
        with _easy_listener(runtime, request, body.client_id) as (origin, store), store.locked():
            grants.auth.require_recent(token)
            previous = store.read_for_connect()
            previous_revoked_at = None
            if previous is not None:
                previous_revoked_at = grants.owned_slot_revoked_at(principal.user_id, body.client_id, origin, previous)
            _ensure_client(runtime, body.client_id, request.scope["server"][1], configure_client)
            current = _status(grants, principal, body.client_id, origin, store)
            if current["scope"] == scope:
                return current
            credential = uuid.uuid4().hex, Ed25519PrivateKey.generate()
            with store.prepare(*credential, restore_on_error=True) as publish:
                grants.publish_local_credential(token, body.client_id, origin, scope, credential, previous, publish,
                                                previous_revoked_at=previous_revoked_at)
            return _status(grants, principal, body.client_id, origin, store)

    @router.post('/pairings', status_code=201)
    def issue(body: Pairing, request: Request):
        origin = listener_origin(runtime.allowed_origins, request.scope)
        return grants.issue(request.cookies.get("finance_session"), request.headers.get("x-csrf-token"),
                            client_id=body.client_id, scope=body.scope, display=body.client_name, origin=origin)

    @router.post('/pairings/claim', status_code=201)
    def claim(body: Claim, request: Request):
        origin = listener_origin(runtime.allowed_origins, request.scope, bridge=True)
        if any(key in request.headers for key in ("cookie", "authorization", "x-csrf-token", "x-financeanalyze-mcp")):
            raise PlatformError("MCP_PAIRING_REJECTED", 401)
        return grants.claim(identifier=body.pairing_id, client_id=body.client_id, public_key=body.public_key,
                            signature=body.signature, key_proof=body.key_proof, origin=origin, peer=request.client.host)

    @router.get('/grants')
    def list_grants(request: Request, limit: int = 20, offset: int = 0):
        listener_origin(runtime.allowed_origins, request.scope)
        return grants.list(runtime.require_principal(request), limit=limit, offset=offset)

    @router.delete('/grants/{grant_id}')
    def revoke(grant_id: str, request: Request):
        listener_origin(runtime.allowed_origins, request.scope)
        return grants.revoke(request.cookies.get("finance_session"), request.headers.get("x-csrf-token"), grant_id)

    @router.get('/session')
    def session(request: Request):
        return {"scope": request.state.mcp_grant_scope}

    return router
