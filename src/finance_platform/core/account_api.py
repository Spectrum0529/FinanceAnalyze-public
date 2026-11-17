"""Browser account routes and same-origin cookie lifecycle."""
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .errors import PlatformError
from .local_personal import require_direct_local_scope
from .pagination import MAX_LIMIT, MAX_OFFSET

COOKIE = "finance_session"
COOKIE_PATH = "/api/v1"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128, repr=False)


class Setup(Credentials):
    setup_token: str = Field(min_length=20, max_length=128, repr=False)


class Login(Credentials):
    remember: bool = False
    otp: str | None = Field(default=None, max_length=8, repr=False)


class CreateUser(Credentials):
    role: Literal["admin", "researcher", "viewer"]


class UpdateUser(BaseModel):
    model_config = Credentials.model_config
    role: Literal["admin", "researcher", "viewer"] | None = None
    enabled: bool | None = None


class PasswordProof(BaseModel):
    model_config = Credentials.model_config
    password: str = Field(min_length=1, max_length=128, repr=False)
    otp: str | None = Field(default=None, max_length=8, repr=False)


class PasswordUpdate(BaseModel):
    model_config = Credentials.model_config
    current_password: str = Field(min_length=1, max_length=128, repr=False)
    new_password: str = Field(min_length=1, max_length=128, repr=False)
    otp: str | None = Field(default=None, max_length=8, repr=False)


class Recovery(BaseModel):
    model_config = Credentials.model_config
    username: str = Field(min_length=1, max_length=64)
    recovery_code: str = Field(min_length=1, max_length=128, repr=False)
    new_password: str = Field(min_length=1, max_length=128, repr=False)


class Revoke(BaseModel):
    model_config = Credentials.model_config
    scope: Literal["others", "all"]


class FactorProof(BaseModel):
    model_config = Credentials.model_config
    otp: str = Field(min_length=1, max_length=8, repr=False)


def create_router(runtime, *, secure_cookies=False, hosted=False):
    router = APIRouter(tags=["accounts"])
    auth = runtime.auth
    personal = runtime.access_mode == "local-personal"
    cookie = {"path": COOKIE_PATH, "secure": secure_cookies, "httponly": True, "samesite": "strict"}

    def account_mode():
        if personal:
            raise PlatformError("FORBIDDEN", 403)

    def local_operator(request):
        account_mode()
        if hosted:
            raise PlatformError("FORBIDDEN", 403)
        require_direct_local_scope(request.scope, runtime.allowed_origins)

    def peer(request):
        return request.client.host if request.client else "unknown"

    def token(request, *, write=False, business=False):
        value = request.cookies.get(COOKIE)
        if write:
            auth.require_csrf(value, request.headers.get("x-csrf-token"), require_write=business)
        return value

    def session_response(result, *, persistent):
        public = {key: result[key] for key in ("user", "csrf_token", "expires_at", "recent_until", "enabled") if key in result}
        response = JSONResponse(public)
        response.set_cookie(COOKIE, result["session_token"],
                            max_age=result.get("max_age", auth.session_seconds) if persistent else None, **cookie)
        return response

    def without_session(body):
        response = JSONResponse(body)
        response.delete_cookie(COOKIE, **cookie)
        return response

    @router.get("/auth/status")
    def status():
        state = auth.setup_status()
        if personal or hosted:
            state["setup_kind"] = None
        if personal:
            state["initialized"] = state["account_state"] in {"empty", "legacy_personal"}
        return {**state, "access_mode": runtime.access_mode,
                "interactive_login_required": not personal,
                "recovery_supported": not personal, "mfa_available": not personal and auth.authenticator.available}

    @router.post("/auth/initialize", status_code=201)
    def initialize(body: Setup, request: Request):
        local_operator(request)
        return auth.initialize(body.setup_token, body.username, body.password, peer=peer(request)).record()

    @router.post("/auth/login")
    def login(body: Login, request: Request):
        account_mode()
        result = auth.login(body.username, body.password, peer=peer(request), remember=body.remember, otp=body.otp)
        return session_response(result, persistent=body.remember)

    @router.post("/auth/local-session")
    def local_session(request: Request):
        if not personal:
            raise PlatformError("FORBIDDEN", 403)
        return session_response(auth.local_session(token(request)), persistent=True)

    @router.get("/auth/me")
    def me(request: Request):
        if personal:
            principal, csrf = auth.session(token(request))
            return {"user": principal.record(), "csrf_token": csrf}
        return auth.session_details(token(request))

    @router.post("/auth/logout")
    def logout(request: Request):
        try:
            auth.logout(token(request, write=True))
        except PlatformError as error:
            if error.code != "NOT_AUTHENTICATED":
                raise
        return without_session({"logged_out": True})

    @router.post("/auth/reauthenticate")
    def reauthenticate(body: PasswordProof, request: Request):
        account_mode()
        result = auth.reauthenticate(token(request, write=True), body.password, otp=body.otp, peer=peer(request))
        return session_response(result, persistent=result["persistent"])

    @router.post("/auth/password")
    def password(body: PasswordUpdate, request: Request):
        account_mode()
        return without_session(auth.change_password(token(request, write=True), body.current_password,
                                                    body.new_password, otp=body.otp, peer=peer(request)))

    @router.get("/auth/sessions")
    def sessions(request: Request, limit: int = Query(50, ge=1, le=MAX_LIMIT),
                 offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        account_mode()
        return auth.list_sessions(token(request), limit=limit, offset=offset)

    @router.delete("/auth/sessions/{session_id}")
    def revoke_session(session_id: str, request: Request):
        account_mode()
        result = auth.revoke_sessions(token(request, write=True), session_id=session_id)
        return without_session(result) if result["current_revoked"] else result

    @router.post("/auth/sessions/revoke")
    def revoke(body: Revoke, request: Request):
        account_mode()
        result = auth.revoke_sessions(token(request, write=True), scope=body.scope)
        return without_session(result) if result["current_revoked"] else result

    @router.get("/auth/recovery")
    def recovery_status(request: Request):
        account_mode()
        return auth.recovery_status(token(request))

    @router.post("/auth/recovery/codes")
    def recovery_codes(request: Request):
        account_mode()
        return auth.issue_recovery_codes(token(request, write=True))

    @router.post("/auth/recover")
    def recover(body: Recovery, request: Request):
        account_mode()
        return without_session(auth.recover(body.username, body.recovery_code, body.new_password, peer=peer(request)))

    @router.post("/auth/recover/local")
    def recover_local(body: Recovery, request: Request):
        local_operator(request)
        return without_session(auth.recover_operator(body.username, body.recovery_code, body.new_password,
                                                      peer=peer(request)))

    @router.get("/auth/mfa")
    def factor(request: Request):
        account_mode()
        return auth.mfa_status(token(request))

    @router.post("/auth/mfa/enroll")
    def enroll(request: Request):
        account_mode()
        return auth.enroll_mfa(token(request, write=True))

    @router.post("/auth/mfa/confirm")
    def confirm(body: FactorProof, request: Request):
        account_mode()
        result = auth.confirm_mfa(token(request, write=True), body.otp)
        return session_response(result, persistent=result["persistent"])

    @router.delete("/auth/mfa")
    def disable(request: Request):
        account_mode()
        return auth.disable_mfa(token(request, write=True))

    @router.get("/users")
    def users(request: Request, limit: int = Query(50, ge=1, le=MAX_LIMIT),
              offset: int = Query(0, ge=0, le=MAX_OFFSET)):
        return auth.list_users(runtime.require_principal(request), limit=limit, offset=offset)

    @router.post("/users", status_code=201)
    def add_user(body: CreateUser, request: Request):
        account_mode()
        principal = runtime.require_principal(request, write=True)
        return auth.create_user(principal, body.username, body.password, body.role, token=token(request)).record()

    @router.patch("/users/{user_id}")
    def update_user(user_id: str, body: UpdateUser, request: Request):
        account_mode()
        current = token(request, write=True, business=True)
        return auth.update_user(current, user_id, role=body.role, enabled=body.enabled)

    return router