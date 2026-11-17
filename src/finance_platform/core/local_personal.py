"""Explicit loopback-only personal mode using existing sessions and CSRF."""
from contextlib import closing
from ipaddress import ip_address
import secrets
from urllib.parse import urlsplit

from .auth import AuthService, LOCAL_OWNER, LOCAL_PASSWORD_MARKER, Principal
from .errors import PlatformError
from .security import now_text, token_digest


def validate_mode(mode, origins):
    if mode not in {'accounts', 'local-personal'}:
        raise PlatformError('INVALID_INPUT', 422)
    if mode == 'local-personal' and (not origins or any(urlsplit(origin).hostname not in {'127.0.0.1', 'localhost'} for origin in origins)):
        raise PlatformError('ORIGIN_REJECTED', 403)


def require_local_scope(scope, allowed_origins):
    """Do not trust Host or forwarded client-IP claims as proof of locality."""
    try:
        if not scope.get('client') or not ip_address(scope['client'][0]).is_loopback:
            raise ValueError
        pairs = scope.get('headers', [])
        if any(key.lower() in {b'forwarded', b'x-real-ip'} or key.lower().startswith(b'x-forwarded-') for key, _ in pairs):
            raise ValueError
        hosts = [v.decode('ascii').lower() for k, v in pairs if k.lower() == b'host']
        origins = [v.decode('ascii') for k, v in pairs if k.lower() == b'origin']
        if len(hosts) != 1 or len(origins) > 1:
            raise ValueError
        authorities = {urlsplit(v).netloc.lower() for v in allowed_origins}
        if hosts[0] not in authorities:
            raise ValueError
        if scope.get('method') in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            if len(origins) != 1 or origins[0] not in allowed_origins:
                raise ValueError
            origin = urlsplit(origins[0])
            if origin.netloc.lower() != hosts[0] or origin.scheme != scope.get('scheme'):
                raise ValueError
    except (ValueError, UnicodeError, TypeError):
        raise PlatformError('ORIGIN_REJECTED', 403) from None


def require_direct_local_scope(scope, allowed_origins):
    """Require the request authority to name this actual loopback listener."""
    require_local_scope(scope, allowed_origins)
    try:
        server = scope.get("server")
        origin = urlsplit(next(value.decode("ascii") for key, value in scope["headers"]
                               if key.lower() == b"origin"))
        port = origin.port or (443 if origin.scheme == "https" else 80)
        if not server or not ip_address(server[0]).is_loopback or server[1] != port:
            raise ValueError
    except (StopIteration, ValueError, TypeError, UnicodeError):
        raise PlatformError("ORIGIN_REJECTED", 403) from None


class PersonalAuthService(AuthService):
    def initialized(self):
        return True

    def session(self, token):
        with closing(self.database.connect()) as con:
            row = self._session_row(con, token, personal=True)
            if self._setup_kind(con) != "upgrade_personal" or row["user_id"] != LOCAL_OWNER:
                raise PlatformError("NOT_AUTHENTICATED", 401)
        return Principal(row["user_id"], row["username"], row["role"]), row["csrf_token"]

    def logout(self, token):
        return super().logout(token, personal=True)

    def local_session(self, token=None):
        # Reuse this browser's valid local session; never adopt legacy identity.
        if token:
            try:
                principal, csrf = self.session(token)
                return {'session_token': token, 'csrf_token': csrf, 'user': principal.record()}
            except PlatformError as error:
                if error.code != 'NOT_AUTHENTICATED':
                    raise
        self.rate_limit('local-session', limit=30, window=60)
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.database.transaction() as con:
            kind = self._setup_kind(con)
            if kind is None:
                raise PlatformError("CONFLICT", 409)
            if kind == "first_admin":
                con.execute("INSERT INTO users VALUES (?,?,?,?,1,?)",
                            (LOCAL_OWNER, LOCAL_OWNER, LOCAL_PASSWORD_MARKER, "researcher", now_text()))
            con.execute("""INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at)
                           VALUES(?,?,?,?,?)""",
                        (token_digest(session), LOCAL_OWNER, csrf, self.clock() + self.session_seconds, now_text()))
            self.database.audit(LOCAL_OWNER, 'auth.local_session', LOCAL_OWNER, connection=con)
        return {'session_token': session, 'csrf_token': csrf, 'user': {'user_id': LOCAL_OWNER, 'username': LOCAL_OWNER, 'role': 'researcher'}}
