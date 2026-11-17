"""Persistent accounts and transaction-bound session security."""
from contextlib import closing
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import hmac
import secrets
import time
import uuid

from .account_secrets import Authenticator, password_hash, timestamp, verify_password
from .contracts import identifier
from .database import Database
from .errors import PlatformError
from .pagination import validate_pagination
from .security import hash_password as legacy_password_hash, now_text, token_digest

ROLES = {"admin", "researcher", "viewer"}
LOCAL_OWNER = "local-personal"
LOCAL_PASSWORD_MARKER = "UNUSABLE_LOCAL_PERSONAL_NO_LOGIN_V1"
PERSONAL_SOURCE = (LOCAL_OWNER, LOCAL_OWNER, LOCAL_PASSWORD_MARKER, "researcher", 1)
RECENT_SECONDS = 600
RECOVERY_SECONDS = 90 * 86400
OPERATOR_RECOVERY_SECONDS = 900
TRUSTED_SESSION_SECONDS = 7 * 86400

MIGRATIONS = ("""
ALTER TABLE setup ADD COLUMN kind TEXT;
ALTER TABLE sessions ADD COLUMN session_id TEXT;
ALTER TABLE sessions ADD COLUMN authenticated_at REAL;
CREATE UNIQUE INDEX auth_session_public_id ON sessions(session_id);
CREATE INDEX auth_sessions_owner ON sessions(user_id, expires_at) WHERE revoked_at IS NULL;
CREATE TABLE auth_recovery_codes(
    code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
    expires_at REAL NOT NULL
);
CREATE INDEX auth_recovery_codes_owner ON auth_recovery_codes(user_id,expires_at);
CREATE TABLE auth_operator_recovery(
    user_id TEXT PRIMARY KEY REFERENCES users(user_id), code_hash TEXT NOT NULL UNIQUE,
    password_binding TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE auth_totp(
    user_id TEXT PRIMARY KEY REFERENCES users(user_id),
    active_secret BLOB, accepted_step INTEGER NOT NULL DEFAULT -1,
    pending_secret BLOB, pending_token_hash TEXT REFERENCES sessions(token_hash),
    pending_expires REAL
);
CREATE INDEX auth_rate_expiration ON rate_events(recorded_at);
""", """
ALTER TABLE sessions ADD COLUMN persistent INTEGER NOT NULL DEFAULT 0 CHECK(persistent IN (0,1));
""",)


@lru_cache(maxsize=1)
def _dummy_hashes():
    return password_hash(secrets.token_urlsafe(24)), legacy_password_hash(secrets.token_urlsafe(24))


def clear_auth_proofs(con, *, strip_sessions=False, clear_rates=False):
    """Clear ephemeral proofs in current and historical schemas, preserving active MFA."""
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "auth_totp" in tables:
        con.execute("""UPDATE auth_totp SET pending_secret=NULL,pending_token_hash=NULL,pending_expires=NULL
                       WHERE pending_token_hash IS NOT NULL OR pending_secret IS NOT NULL OR pending_expires IS NOT NULL""")
        con.execute("DELETE FROM auth_totp WHERE active_secret IS NULL")
    for name in ("mcp_nonces", "mcp_grants", "mcp_pairings"):
        if name in tables:
            con.execute(f"DELETE FROM {name}")
    if strip_sessions:
        count = con.execute("DELETE FROM sessions").rowcount
    else:
        count = con.execute("UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL", (now_text(),)).rowcount
    con.execute("DELETE FROM setup")
    if "auth_operator_recovery" in tables:
        con.execute("DELETE FROM auth_operator_recovery")
    if "auth_recovery_codes" in tables:
        con.execute("DELETE FROM auth_recovery_codes")
    if clear_rates:
        con.execute("DELETE FROM rate_events")
    return count


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    username: str
    role: str

    def record(self):
        return {"user_id": self.user_id, "username": self.username, "role": self.role}


@dataclass(frozen=True, slots=True)
class AccountDelegation(Principal):
    """Restricted principal for one approved MCP request."""
    check_approval: Callable


def require_owner(principal: Principal, owner_id: str, *, write=False):
    if write and principal.role == "viewer":
        raise PlatformError("FORBIDDEN", 403)
    if principal.role != "admin" and principal.user_id != owner_id:
        raise PlatformError("NOT_FOUND", 404)


def require_writer(principal: Principal):
    if principal.role not in {"admin", "researcher"}:
        raise PlatformError("FORBIDDEN", 403)


class AuthService:
    def __init__(self, database: Database, *, clock=time.time, session_seconds=8 * 3600, mfa_key=None):
        self.database, self.clock, self.session_seconds = database, clock, session_seconds
        self.authenticator = Authenticator(mfa_key)
        self._dummy_argon, self._dummy_scrypt = _dummy_hashes()
        self.database.apply_module_migrations("account_security", MIGRATIONS)

    @staticmethod
    def _setup_kind(con):
        users = con.execute("SELECT user_id,username,password_hash,role,enabled FROM users LIMIT 2").fetchall()
        if not users:
            return "first_admin"
        if len(users) == 1 and tuple(users[0]) == PERSONAL_SOURCE:
            return "upgrade_personal"
        return None

    def setup_status(self):
        with closing(self.database.connect()) as con:
            con.execute("BEGIN")
            kind = self._setup_kind(con)
            admin = con.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1 LIMIT 1").fetchone()
            personal = con.execute("SELECT 1 FROM users WHERE password_hash=? LIMIT 1",
                                   (LOCAL_PASSWORD_MARKER,)).fetchone()
        state = {"first_admin": "empty", "upgrade_personal": "legacy_personal"}.get(kind)
        if state is None:
            state = "ready" if admin and not personal else "ambiguous"
        return {"initialized": admin is not None, "setup_kind": kind, "account_state": state}

    def setup_kind(self):
        return self.setup_status()["setup_kind"]

    def initialized(self):
        with closing(self.database.connect()) as con:
            return con.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1 LIMIT 1").fetchone() is not None

    def prepare_setup(self, *, ttl=1800):
        if type(ttl) is not int or not 60 <= ttl <= 3600:
            raise PlatformError("INVALID_INPUT", 422)
        token = secrets.token_urlsafe(32)
        with self.database.transaction() as con:
            kind = self._setup_kind(con)
            if kind is None:
                raise PlatformError("CONFLICT", 409)
            con.execute("""INSERT INTO setup(singleton,token_hash,expires_at,consumed_at,kind)
                           VALUES(1,?,?,NULL,?) ON CONFLICT(singleton) DO UPDATE SET
                           token_hash=excluded.token_hash,expires_at=excluded.expires_at,
                           consumed_at=NULL,kind=excluded.kind""", (token_digest(token), self.clock() + ttl, kind))
            self.database.audit(None, "auth.setup_prepared", details={"ttl_seconds": ttl, "kind": kind}, connection=con)
        return token

    @staticmethod
    def _identity(username):
        try:
            identifier(username, "username")
        except ValueError:
            raise PlatformError("INVALID_INPUT", 422) from None
        if len(username) > 64:
            raise PlatformError("INVALID_INPUT", 422)
        return username.lower()

    def _setup_proof(self, con, proof):
        row = con.execute("SELECT token_hash,expires_at,consumed_at,kind FROM setup WHERE singleton=1").fetchone()
        kind = self._setup_kind(con)
        if (not kind or not row or row["kind"] != kind or row["consumed_at"] or row["expires_at"] <= self.clock()
                or not hmac.compare_digest(row["token_hash"], proof)):
            raise PlatformError("SETUP_REJECTED", 403)
        return kind

    def initialize(self, setup_token, username, password, *, peer="local"):
        self.rate_limit("setup:" + peer, limit=5, window=60)
        username = self._identity(username)
        proof = token_digest(setup_token)
        with closing(self.database.connect()) as con:
            self._setup_proof(con, proof)
        encoded = password_hash(password)
        with self.database.transaction() as con:
            kind = self._setup_proof(con, proof)
            if kind == "upgrade_personal":
                user_id = LOCAL_OWNER
                con.execute("UPDATE users SET username=?,password_hash=?,role='admin' WHERE user_id=?",
                            (username, encoded, user_id))
                self._revoke(con, user_id)
            else:
                user_id = str(uuid.uuid4())
                con.execute("INSERT INTO users VALUES (?,?,?,'admin',1,?)", (user_id, username, encoded, now_text()))
            con.execute("UPDATE setup SET consumed_at=? WHERE singleton=1", (now_text(),))
            self.database.audit(user_id, "auth.initialized", user_id, {"kind": kind}, connection=con)
        return Principal(user_id, username, "admin")

    def rate_limit(self, bucket, *, limit, window):
        with self.database.transaction() as con:
            self._rate_limit(con, bucket, limit=limit, window=window)

    def _rate_limit(self, con, bucket, *, limit, window):
        bucket_hash = hashlib.sha256(bucket.encode()).hexdigest()
        now = self.clock()
        count = con.execute("SELECT count(*) FROM rate_events WHERE bucket_hash=? AND recorded_at>?",
                            (bucket_hash, now - window)).fetchone()[0]
        if count >= limit:
            raise PlatformError("RATE_LIMITED", 429)
        con.execute("INSERT INTO rate_events(bucket_hash,recorded_at) VALUES (?,?)", (bucket_hash, now))
        con.execute("""DELETE FROM rate_events WHERE event_id IN
                       (SELECT event_id FROM rate_events WHERE recorded_at<? LIMIT 32)""", (now - 86400,))

    def _public_identity(self, username, *, code, status):
        try:
            return self._identity(username)
        except PlatformError:
            raise PlatformError(code, status) from None

    @staticmethod
    def _factor(con, user_id):
        return con.execute("SELECT * FROM auth_totp WHERE user_id=?", (user_id,)).fetchone()

    def _confirm_factor(self, con, user_id, otp):
        row = self._factor(con, user_id)
        if row is None or row["active_secret"] is None:
            return
        self.authenticator.require_available()
        if otp is None:
            raise PlatformError("MFA_REQUIRED", 401)
        step = self.authenticator.step(user_id, row["active_secret"], otp, self.clock(), row["accepted_step"])
        con.execute("UPDATE auth_totp SET accepted_step=? WHERE user_id=?", (step, user_id))

    def login(self, username, password, *, peer="local", remember=False, otp=None):
        self.rate_limit("login-peer:" + peer, limit=10, window=300)
        username = self._public_identity(username, code="BAD_CREDENTIALS", status=401)
        self.rate_limit("login-user:" + username, limit=10, window=300)
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        active = row is not None and row["enabled"] and row["password_hash"] != LOCAL_PASSWORD_MARKER
        encoded = row["password_hash"] if active else self._dummy_argon
        legacy = encoded.startswith("scrypt-v1$")
        valid = verify_password(password, encoded)
        if not active or not valid:
            # Run one bounded verification of each algorithm on normal failures
            # while both supported hash formats coexist. No timing guarantee.
            verify_password(password, self._dummy_argon if legacy else self._dummy_scrypt)
            self.database.audit(None, "auth.login_failed")
            raise PlatformError("BAD_CREDENTIALS", 401)
        updated = password_hash(password, policy=False) if encoded.startswith("scrypt-v1$") else None
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.clock()
        lifetime = TRUSTED_SESSION_SECONDS if remember else self.session_seconds
        with self.database.transaction() as con:
            current = con.execute("SELECT * FROM users WHERE user_id=?", (row["user_id"],)).fetchone()
            if not current or not current["enabled"] or current["password_hash"] != encoded:
                raise PlatformError("BAD_CREDENTIALS", 401)
            self._confirm_factor(con, current["user_id"], otp)
            if updated is not None:
                con.execute("UPDATE users SET password_hash=? WHERE user_id=?", (updated, current["user_id"]))
            con.execute("""INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at,session_id,authenticated_at,persistent)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (token_digest(session), current["user_id"], csrf, now + lifetime, now_text(), uuid.uuid4().hex, now, remember))
            self.database.audit(current["user_id"], "auth.login", current["user_id"], connection=con)
        return {"session_token": session, "max_age": lifetime, "csrf_token": csrf,
                "user": Principal(current["user_id"], current["username"], current["role"]).record(),
                "expires_at": timestamp(now + lifetime), "recent_until": timestamp(now + RECENT_SECONDS)}

    def _session_row(self, con, token, *, personal=False):
        row = None
        if token:
            row = con.execute("""SELECT s.*,u.username,u.role,u.enabled,u.password_hash FROM sessions s
                                 JOIN users u ON s.user_id=u.user_id WHERE token_hash=?""", (token_digest(token),)).fetchone()
        if (not row or row["revoked_at"] or row["expires_at"] <= self.clock() or not row["enabled"]
                or personal and (row["session_id"] is not None or row["password_hash"] != LOCAL_PASSWORD_MARKER)
                or not personal and (row["session_id"] is None or row["password_hash"] == LOCAL_PASSWORD_MARKER)):
            raise PlatformError("NOT_AUTHENTICATED", 401)
        return row

    def session(self, token):
        with closing(self.database.connect()) as con:
            row = self._session_row(con, token)
        return Principal(row["user_id"], row["username"], row["role"]), row["csrf_token"]

    def session_details(self, token):
        with closing(self.database.connect()) as con:
            row = self._session_row(con, token)
        return {"user": Principal(row["user_id"], row["username"], row["role"]).record(),
                "csrf_token": row["csrf_token"], "expires_at": timestamp(row["expires_at"]),
                "recent_until": (timestamp(min(row["expires_at"], row["authenticated_at"] + RECENT_SECONDS))
                                 if row["authenticated_at"] is not None else None)}

    def require_csrf(self, token, supplied, *, require_write=True):
        principal, expected = self.session(token)
        if (type(supplied) is not str or len(supplied) > 128 or not supplied.isascii()
                or not hmac.compare_digest(expected, supplied)):
            raise PlatformError("CSRF_REJECTED", 403)
        if require_write:
            require_writer(principal)
        self.rate_limit("write:" + principal.user_id, limit=120, window=60)
        return principal

    def logout(self, token, *, personal=False):
        with self.database.transaction() as con:
            row = self._session_row(con, token, personal=personal)
            con.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (now_text(), row["token_hash"]))
            self.database.audit(row["user_id"], "auth.logout", row["user_id"], connection=con)

    def _recent(self, con, token, *, admin=False):
        row = self._session_row(con, token)
        if admin and row["role"] != "admin":
            raise PlatformError("FORBIDDEN", 403)
        if row["authenticated_at"] is None or row["authenticated_at"] + RECENT_SECONDS <= self.clock():
            raise PlatformError("REAUTH_REQUIRED", 403)
        return row

    def require_recent(self, token, *, admin=False, connection=None):
        if connection is not None:
            return self._recent(connection, token, admin=admin)["user_id"]
        with closing(self.database.connect()) as con:
            return self._recent(con, token, admin=admin)["user_id"]

    def _password_proof(self, token, password, *, peer):
        self.rate_limit("reauth-peer:" + peer, limit=20, window=300)
        with closing(self.database.connect()) as con:
            row = self._session_row(con, token)
        self.rate_limit("reauth-user:" + row["user_id"], limit=10, window=300)
        if not verify_password(password, row["password_hash"]):
            raise PlatformError("BAD_CREDENTIALS", 401)
        return row["password_hash"]

    def _confirm_proof(self, con, token, expected, otp):
        row = self._session_row(con, token)
        if row["password_hash"] != expected:
            raise PlatformError("BAD_CREDENTIALS", 401)
        self._confirm_factor(con, row["user_id"], otp)
        return row

    def _rotate(self, con, row):
        now = self.clock()
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        replacement = token_digest(session)
        con.execute("""INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at,
                                            session_id,authenticated_at,persistent) VALUES (?,?,?,?,?,?,?,?)""",
                    (replacement, row["user_id"], csrf, row["expires_at"], row["created_at"],
                     uuid.uuid4().hex, now, row["persistent"]))
        con.execute("UPDATE auth_totp SET pending_token_hash=? WHERE pending_token_hash=?",
                    (replacement, row["token_hash"]))
        con.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (now_text(), row["token_hash"]))
        return {"session_token": session, "csrf_token": csrf, "persistent": bool(row["persistent"]),
                "max_age": max(0, int(row["expires_at"] - now)), "expires_at": timestamp(row["expires_at"]),
                "recent_until": timestamp(min(row["expires_at"], now + RECENT_SECONDS))}

    def reauthenticate(self, token, password, *, otp=None, peer="local"):
        expected = self._password_proof(token, password, peer=peer)
        with self.database.transaction() as con:
            row = self._confirm_proof(con, token, expected, otp)
            result = self._rotate(con, row)
            self.database.audit(row["user_id"], "auth.reauthenticated", connection=con)
        return result

    def _revoke(self, con, user, *, keep=None):
        return con.execute("""UPDATE sessions SET revoked_at=?
                              WHERE user_id=? AND revoked_at IS NULL AND expires_at>?
                              AND (? IS NULL OR token_hash<>?)""",
                           (now_text(), user, self.clock(), keep, keep)).rowcount

    def change_password(self, token, current_password, new_password, *, otp=None, peer="local"):
        expected = self._password_proof(token, current_password, peer=peer)
        encoded = password_hash(new_password)
        with self.database.transaction() as con:
            row = self._confirm_proof(con, token, expected, otp)
            user_id = row["user_id"]
            con.execute("UPDATE users SET password_hash=? WHERE user_id=?", (encoded, user_id))
            self._revoke(con, user_id)
            con.execute("DELETE FROM auth_recovery_codes WHERE user_id=?", (user_id,))
            self._revoke_operator_proof(con, user_id)
            self.database.audit(user_id, "auth.password_changed", connection=con)
        return {"changed": True}

    def list_sessions(self, token, *, limit=50, offset=0):
        validate_pagination(limit, offset)
        with closing(self.database.connect()) as con:
            con.execute("BEGIN")
            current = self._session_row(con, token)
            where = "user_id=? AND session_id IS NOT NULL AND revoked_at IS NULL AND expires_at>?"
            args = (current["user_id"], self.clock())
            total = con.execute("SELECT count(*) FROM sessions WHERE " + where, args).fetchone()[0]
            rows = con.execute("SELECT session_id,created_at,expires_at FROM sessions WHERE " + where
                               + " ORDER BY created_at DESC,session_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"items": [{"session_id": row["session_id"], "created_at": row["created_at"],
                           "expires_at": timestamp(row["expires_at"]), "current": row["session_id"] == current["session_id"]}
                          for row in rows], "total": total, "limit": limit, "offset": offset}

    def revoke_sessions(self, token, *, scope=None, session_id=None):
        if (session_id is None and scope not in {"all", "others"}
                or session_id is not None and scope is not None):
            raise PlatformError("INVALID_INPUT", 422)
        with self.database.transaction() as con:
            row = self._recent(con, token)
            user_id = row["user_id"]
            if session_id is not None:
                count = con.execute("""UPDATE sessions SET revoked_at=? WHERE user_id=? AND session_id=?
                                       AND revoked_at IS NULL AND expires_at>?""",
                                    (now_text(), user_id, session_id, self.clock())).rowcount
                current = count > 0 and row["session_id"] == session_id
            else:
                current = scope == "all"
                count = self._revoke(con, user_id, keep=None if current else row["token_hash"])
            if count:
                self.database.audit(user_id, "auth.sessions_revoked", details={"count": count}, connection=con)
        return {"revoked": count, "current_revoked": current}

    def recovery_status(self, token):
        with closing(self.database.connect()) as con:
            row = self._session_row(con, token)
            remaining, expires = con.execute("""SELECT count(*),max(expires_at) FROM auth_recovery_codes
                                                WHERE user_id=? AND expires_at>?""",
                                             (row["user_id"], self.clock())).fetchone()
        return {"remaining": remaining, "expires_at": timestamp(expires) if expires is not None else None}

    def issue_recovery_codes(self, token):
        codes = [secrets.token_urlsafe(24) for _ in range(10)]
        expires = self.clock() + RECOVERY_SECONDS
        with self.database.transaction() as con:
            user_id = self._recent(con, token)["user_id"]
            con.execute("DELETE FROM auth_recovery_codes WHERE user_id=?", (user_id,))
            con.executemany("INSERT INTO auth_recovery_codes VALUES (?,?,?)",
                            ((token_digest(code), user_id, expires) for code in codes))
            self.database.audit(user_id, "auth.recovery_codes_issued", connection=con)
        return {"codes": codes, "expires_at": timestamp(expires)}

    def _recovery_match(self, con, username, proof):
        row = con.execute("""SELECT u.user_id FROM auth_recovery_codes r JOIN users u ON u.user_id=r.user_id
                             WHERE r.code_hash=? AND u.username=? AND u.enabled=1 AND r.expires_at>?""",
                          (proof, username, self.clock())).fetchone()
        if not row:
            raise PlatformError("RECOVERY_REJECTED", 400)
        return row["user_id"]

    def recover(self, username, recovery_code, new_password, *, peer="local"):
        return self._redeem_password(username, recovery_code, new_password, peer=peer, operator=False)

    def _redeem_password(self, username, recovery_code, new_password, *, peer, operator):
        self.rate_limit("recover-peer:" + peer, limit=10, window=300)
        username = self._public_identity(username, code="RECOVERY_REJECTED", status=400)
        self.rate_limit("recover-user:" + username, limit=10, window=300)
        proof = token_digest(recovery_code)
        match = self._operator_match if operator else self._recovery_match
        with closing(self.database.connect()) as con:
            match(con, username, proof)
        encoded = password_hash(new_password)
        with self.database.transaction() as con:
            user_id = match(con, username, proof)
            con.execute("UPDATE users SET password_hash=? WHERE user_id=?", (encoded, user_id))
            con.execute("DELETE FROM auth_recovery_codes WHERE user_id=?", (user_id,))
            self._revoke(con, user_id)
            self._revoke_operator_proof(con, user_id)
            if operator:
                con.execute("DELETE FROM auth_totp WHERE user_id=?", (user_id,))
            action = "auth.operator_password_recovered" if operator else "auth.password_recovered"
            self.database.audit(user_id, action, connection=con)
        return {"recovered": True}

    @staticmethod
    def _revoke_operator_proof(con, user_id):
        con.execute("DELETE FROM auth_operator_recovery WHERE user_id=?", (user_id,))

    def invalidate_restored_auth(self):
        """Use on an offline restored copy before any profile can serve it."""
        with self.database.transaction() as con:
            count = clear_auth_proofs(con, strip_sessions=True, clear_rates=True)
            self.database.audit(None, "auth.restored_proofs_invalidated", details={"sessions": count}, connection=con)
        return {"sessions_revoked": count, "account_state": self.setup_status()["account_state"]}

    @staticmethod
    def _password_binding(encoded):
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def prepare_operator_recovery(self, username):
        username = self._identity(username)
        code = secrets.token_urlsafe(32)
        with self.database.transaction() as con:
            user = con.execute("""SELECT user_id,password_hash FROM users
                                  WHERE username=? AND role='admin' AND enabled=1""", (username,)).fetchone()
            if user is None:
                raise PlatformError("RECOVERY_REJECTED", 400)
            con.execute("""INSERT INTO auth_operator_recovery VALUES(?,?,?,?)
                           ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash,
                           password_binding=excluded.password_binding,expires_at=excluded.expires_at""",
                        (user["user_id"], token_digest(code), self._password_binding(user["password_hash"]),
                         self.clock() + OPERATOR_RECOVERY_SECONDS))
            self.database.audit(None, "auth.operator_recovery_prepared", user["user_id"], connection=con)
        return code

    def _operator_match(self, con, username, proof):
        row = con.execute("""SELECT u.user_id,u.password_hash,r.password_binding FROM auth_operator_recovery r
                             JOIN users u ON u.user_id=r.user_id WHERE r.code_hash=? AND u.username=?
                             AND u.role='admin' AND u.enabled=1 AND r.expires_at>?""",
                          (proof, username, self.clock())).fetchone()
        if row is None or not hmac.compare_digest(row["password_binding"], self._password_binding(row["password_hash"])):
            raise PlatformError("RECOVERY_REJECTED", 400)
        return row["user_id"]

    def recover_operator(self, username, recovery_code, new_password, *, peer="local"):
        return self._redeem_password(username, recovery_code, new_password, peer=peer, operator=True)

    def mfa_status(self, token):
        with closing(self.database.connect()) as con:
            user = self._session_row(con, token)["user_id"]
            factor = self._factor(con, user)
        return {"available": self.authenticator.available, "enabled": bool(factor and factor["active_secret"])}

    def enroll_mfa(self, token):
        with self.database.transaction() as con:
            row = self._recent(con, token)
            factor = self._factor(con, row["user_id"])
            if factor and factor["active_secret"] is not None:
                raise PlatformError("CONFLICT", 409)
            secret, uri, encrypted = self.authenticator.generate(row["user_id"], row["username"])
            expiry = self.clock() + RECENT_SECONDS
            con.execute("""INSERT INTO auth_totp(user_id,pending_secret,pending_token_hash,pending_expires)
                           VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET pending_secret=excluded.pending_secret,
                           pending_token_hash=excluded.pending_token_hash,pending_expires=excluded.pending_expires""",
                        (row["user_id"], encrypted, row["token_hash"], expiry))
            self._revoke_operator_proof(con, row["user_id"])
            self.database.audit(row["user_id"], "auth.mfa_enrollment_started", connection=con)
        return {"setup_secret": secret, "provisioning_uri": uri, "expires_at": timestamp(expiry)}

    def confirm_mfa(self, token, otp):
        user_id = self.require_recent(token)
        self.rate_limit("enroll-mfa:" + user_id, limit=6, window=300)
        with self.database.transaction() as con:
            row = self._recent(con, token)
            user = row["user_id"]
            factor = self._factor(con, user)
            if (not factor or factor["active_secret"] is not None or factor["pending_secret"] is None
                    or factor["pending_token_hash"] != row["token_hash"] or factor["pending_expires"] <= self.clock()):
                raise PlatformError("MFA_INVALID", 401)
            step = self.authenticator.step(user, factor["pending_secret"], otp, self.clock(), -1)
            con.execute("""UPDATE auth_totp SET active_secret=pending_secret,accepted_step=?,
                           pending_secret=NULL,pending_token_hash=NULL,pending_expires=NULL WHERE user_id=?""", (step, user))
            self._revoke(con, user, keep=row["token_hash"])
            self._revoke_operator_proof(con, user)
            replacement = self._rotate(con, row)
            self.database.audit(user, "auth.mfa_enabled", connection=con)
        return {"enabled": True, **replacement}

    def disable_mfa(self, token):
        with self.database.transaction() as con:
            row = self._recent(con, token)
            user = row["user_id"]
            con.execute("DELETE FROM auth_totp WHERE user_id=?", (user,))
            self._revoke(con, user, keep=row["token_hash"])
            self._revoke_operator_proof(con, user)
            con.execute("UPDATE sessions SET authenticated_at=NULL WHERE token_hash=?", (row["token_hash"],))
            self.database.audit(user, "auth.mfa_disabled", connection=con)
        return {"enabled": False}

    def create_user(self, principal, username, password, role, *, token=None):
        if principal.role != "admin":
            raise PlatformError("FORBIDDEN", 403)
        if role not in ROLES:
            raise PlatformError("INVALID_INPUT", 422)
        username = self._identity(username)
        if username == LOCAL_OWNER:
            raise PlatformError("CONFLICT", 409)
        if token is not None:
            with closing(self.database.connect()) as con:
                self._recent(con, token, admin=True)
        encoded = password_hash(password)
        user_id = str(uuid.uuid4())
        with self.database.transaction() as con:
            if token is not None and self._recent(con, token, admin=True)["user_id"] != principal.user_id:
                raise PlatformError("FORBIDDEN", 403)
            if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise PlatformError("CONFLICT", 409)
            con.execute("INSERT INTO users VALUES (?,?,?,?,1,?)", (user_id, username, encoded, role, now_text()))
            self.database.audit(principal.user_id, "auth.user_created", user_id, {"role": role}, connection=con)
        return Principal(user_id, username, role)

    def update_user(self, token, user_id, *, role=None, enabled=None):
        if (role is None and enabled is None or role is not None and role not in ROLES
                or enabled is not None and type(enabled) is not bool):
            raise PlatformError("INVALID_INPUT", 422)
        with self.database.transaction() as con:
            actor = self._recent(con, token, admin=True)
            row = con.execute("SELECT user_id,username,role,enabled,created_at FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise PlatformError("NOT_FOUND", 404)
            new_role = row["role"] if role is None else role
            new_enabled = bool(row["enabled"]) if enabled is None else enabled
            if row["role"] == "admin" and row["enabled"] and (new_role != "admin" or not new_enabled):
                other = con.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1 AND user_id<>? LIMIT 1",
                                    (user_id,)).fetchone()
                if not other:
                    raise PlatformError("LAST_ADMIN_REQUIRED", 409)
            if new_role != row["role"] or new_enabled != bool(row["enabled"]):
                con.execute("UPDATE users SET role=?,enabled=? WHERE user_id=?", (new_role, new_enabled, user_id))
                self._revoke(con, user_id)
                self._revoke_operator_proof(con, user_id)
                con.execute("DELETE FROM auth_recovery_codes WHERE user_id=?", (user_id,))
                self.database.audit(actor["user_id"], "auth.user_updated", user_id,
                                    {"role": new_role, "enabled": new_enabled}, connection=con)
        return dict(row) | {"role": new_role, "enabled": new_enabled}

    def list_users(self, principal, *, limit=50, offset=0):
        if principal.role != "admin":
            raise PlatformError("FORBIDDEN", 403)
        validate_pagination(limit, offset)
        with closing(self.database.connect()) as con:
            total = con.execute("SELECT count(*) FROM users").fetchone()[0]
            rows = con.execute("SELECT user_id,username,role,enabled,created_at FROM users ORDER BY created_at,user_id LIMIT ? OFFSET ?",
                               (limit, offset)).fetchall()
        return {"items": [dict(row) | {"enabled": bool(row["enabled"])} for row in rows],
                "total": total, "limit": limit, "offset": offset}
