"""MCP grants backed by the product account session and a client public key."""
from contextlib import closing
from dataclasses import dataclass
from typing import Literal
import secrets
import unicodedata
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .account_secrets import timestamp
from .auth import AccountDelegation
from .errors import PlatformError
from .mcp_proof import (CLIENT_ID, CLIENT_NAMES, GRANT_ID, REQUEST_WINDOW, decode, encode,
                        pairing_id, pairing_message, request_message)
from .pagination import validate_pagination

PAIRING_SECONDS = 120
GRANT_SECONDS = 8 * 60 * 60
MAX_ACTIVE_GRANTS = 20
GrantScope = Literal["read", "research", "browse", "manage", "workbench", "external"]
SCOPE_LABELS = {
    "read": "基础读取（原有）",
    "research": "原有回测与读取",
    "browse": "工作台读取",
    "manage": "管理本人资料与设置",
    "workbench": "执行工作台任务与公开行情下载",
    "external": "工作台与允许的有预算外部任务",
}
_CAPABILITIES = {
    "read": frozenset({"read"}),
    "research": frozenset({"read", "research"}),
    "browse": frozenset({"read", "browse"}),
    "manage": frozenset({"read", "browse", "manage"}),
    "workbench": frozenset({"read", "browse", "manage", "research", "workbench"}),
    "external": frozenset({"read", "browse", "manage", "research", "workbench", "external"}),
}


@dataclass(frozen=True)
class McpDelegation(AccountDelegation):
    mcp_scope: GrantScope


def permits(scope, capability):
    return capability in _CAPABILITIES.get(scope, ())

MIGRATIONS = ("""
CREATE TABLE mcp_pairings(
 pairing_id TEXT PRIMARY KEY, pairing_public BLOB NOT NULL,
 session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
 client_id TEXT NOT NULL, client_name TEXT NOT NULL, scope TEXT NOT NULL,
 origin TEXT NOT NULL, approved_role TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE INDEX mcp_pairing_expiry ON mcp_pairings(expires_at);
CREATE TABLE mcp_grants(
 grant_id TEXT PRIMARY KEY, public_key BLOB NOT NULL,
 user_id TEXT NOT NULL REFERENCES users(user_id),
 session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
 client_id TEXT NOT NULL, client_name TEXT NOT NULL, scope TEXT NOT NULL,
 origin TEXT NOT NULL, approved_role TEXT NOT NULL, created_at REAL NOT NULL,
 expires_at REAL NOT NULL, revoked_at REAL);
CREATE INDEX mcp_grants_owner ON mcp_grants(user_id, created_at);
CREATE TABLE mcp_nonces(
 grant_id TEXT NOT NULL REFERENCES mcp_grants(grant_id) ON DELETE CASCADE,
 nonce TEXT NOT NULL, expires_at REAL NOT NULL, PRIMARY KEY(grant_id, nonce));
CREATE INDEX mcp_nonce_expiry ON mcp_nonces(expires_at);
""", """
CREATE TABLE mcp_tool_discovery(
 grant_id TEXT PRIMARY KEY REFERENCES mcp_grants(grant_id) ON DELETE CASCADE,
 confirmed_at REAL NOT NULL);
""")


def scopes_for(role):
    return ["read", "browse"] if role == "viewer" else list(SCOPE_LABELS)


def client_label(client_id, display=None):
    if type(client_id) is not str or not CLIENT_ID.fullmatch(client_id):
        raise PlatformError("INVALID_INPUT", 422)
    if display is None:
        return CLIENT_NAMES.get(client_id, client_id)
    if (type(display) is not str or not 1 <= len(display.strip()) <= 48
            or any(unicodedata.category(character).startswith("C") for character in display)):
        raise PlatformError("INVALID_INPUT", 422)
    return display.strip()


class McpGrants:
    def __init__(self, auth, installation, *, clock=None):
        self.auth = auth
        self.database = auth.database
        self.clock = clock or auth.clock
        self.installation = installation
        self.database.apply_module_migrations("account_mcp", MIGRATIONS)

    def issue(self, token, csrf, *, client_id, scope, display, origin):
        self.auth.require_csrf(token, csrf, require_write=False)
        name = client_label(client_id, display)
        seed = secrets.token_bytes(32)
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
        with self.database.transaction() as con:
            row = self.auth._recent(con, token)
            if scope not in scopes_for(row["role"]):
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            now = self.clock()
            expires = min(row["expires_at"], now + PAIRING_SECONDS)
            con.execute("DELETE FROM mcp_pairings WHERE expires_at<=? OR (session_id=? AND client_id=?)",
                        (now, row["session_id"], client_id))
            con.execute("INSERT INTO mcp_pairings VALUES(?,?,?,?,?,?,?,?,?)",
                        (pairing_id(public), public, row["session_id"], client_id, name, scope,
                         origin, row["role"], expires))
            self.database.audit(row["user_id"], "mcp.pairing_issued", connection=con)
        return {"pairing_code": encode(seed), "expires_at": timestamp(expires), "client_id": client_id,
                "client_name": name, "scope": scope}

    def claim(self, *, identifier, client_id, public_key, signature, key_proof, origin, peer):
        self.auth.rate_limit("mcp-pairing-peer:" + peer, limit=30, window=300)
        rejected = PlatformError("MCP_PAIRING_REJECTED", 401)
        try:
            public = decode(public_key, 32)
            proof = decode(signature, 64)
            possession = decode(key_proof, 64)
        except ValueError:
            raise rejected from None
        with self.database.transaction() as con:
            now = self.clock()
            row = con.execute("""
                SELECT p.*, s.user_id, s.revoked_at, s.expires_at AS session_expiry, u.enabled, u.role
                FROM mcp_pairings p JOIN sessions s USING(session_id) JOIN users u USING(user_id)
                WHERE p.pairing_id=?""", (identifier,)).fetchone()
            if (not row or row["client_id"] != client_id or row["origin"] != origin or row["expires_at"] <= now
                    or row["session_expiry"] <= now or row["revoked_at"] is not None or not row["enabled"]
                    or row["role"] != row["approved_role"]):
                raise rejected
            try:
                message = pairing_message(self.installation, origin, client_id, identifier, public)
                Ed25519PublicKey.from_public_bytes(row["pairing_public"]).verify(proof, message)
                Ed25519PublicKey.from_public_bytes(public).verify(possession, b"client\n" + message)
            except (InvalidSignature, ValueError):
                raise rejected from None
            grant_id = uuid.uuid4().hex
            con.execute("DELETE FROM mcp_pairings WHERE pairing_id=?", (identifier,))
            expiry = self._create_grant(con, grant_id, public, row["user_id"], row["session_id"],
                                        row["session_expiry"], client_id, row["client_name"], row["scope"],
                                        origin, row["role"], now)
        return {"grant_id": grant_id, "client_id": client_id, "scope": row["scope"], "expires_at": timestamp(expiry)}

    def _create_grant(self, con, grant_id, public, owner_id, session_id, session_expiry,
                      client_id, name, scope, origin, role, now):
        if self._active_count(con, owner_id, now) >= MAX_ACTIVE_GRANTS:
            raise PlatformError("MCP_GRANT_LIMIT", 409)
        expiry = min(session_expiry, now + GRANT_SECONDS)
        con.execute("""INSERT INTO mcp_grants(grant_id,public_key,user_id,session_id,client_id,client_name,scope,
                                              origin,approved_role,created_at,expires_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (grant_id, public, owner_id, session_id, client_id, name, scope, origin, role, now, expiry))
        self.database.audit(owner_id, "mcp.grant_created", grant_id, connection=con)
        return expiry

    @staticmethod
    def _active_count(con, user_id, now):
        return con.execute("""
            SELECT count(*) FROM mcp_grants g JOIN sessions s USING(session_id)
            WHERE g.user_id=? AND g.revoked_at IS NULL AND g.expires_at>?
                AND s.revoked_at IS NULL AND s.expires_at>?""", (user_id, now, now)).fetchone()[0]

    def list(self, principal, *, limit, offset):
        validate_pagination(limit, offset)
        now = self.clock()
        with closing(self.database.connect()) as con:
            total = con.execute("SELECT count(*) FROM mcp_grants WHERE user_id=?", (principal.user_id,)).fetchone()[0]
            rows = con.execute("""
                SELECT g.*,s.expires_at AS session_expiry,s.revoked_at AS session_revoked
                FROM mcp_grants g JOIN sessions s USING(session_id) WHERE g.user_id=?
                ORDER BY g.created_at DESC, g.grant_id LIMIT ? OFFSET ?""",
                               (principal.user_id, limit, offset)).fetchall()
        return {"items": [self._record(row, principal.role, now) for row in rows],
                "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _record(row, role, now):
        status = "active"
        if row["revoked_at"] is not None or row["session_revoked"] is not None or role != row["approved_role"]:
            status = "revoked"
        elif min(row["expires_at"], row["session_expiry"]) <= now:
            status = "expired"
        return {key: row[key] for key in ("grant_id", "client_id", "client_name", "scope")} | {
            "created_at": timestamp(row["created_at"]), "expires_at": timestamp(row["expires_at"]), "status": status}

    def revoke(self, token, csrf, grant_id):
        self.auth.require_csrf(token, csrf, require_write=False)
        with self.database.transaction() as con:
            principal = self.auth._session_row(con, token)
            row = con.execute("SELECT revoked_at FROM mcp_grants WHERE grant_id=? AND user_id=?",
                              (grant_id, principal["user_id"])).fetchone()
            if row is None:
                raise PlatformError("NOT_FOUND", 404)
            if row["revoked_at"] is None:
                con.execute("UPDATE mcp_grants SET revoked_at=? WHERE grant_id=?", (self.clock(), grant_id))
                self.database.audit(principal["user_id"], "mcp.grant_revoked", grant_id, connection=con)
        return {"revoked": True}

    def _local_binding(self, con, owner_id, client_id, origin, credential):
        grant_id, key = credential
        row = self._active(con, grant_id, origin, self.clock(), "read")
        if (row["user_id"] != owner_id or row["client_id"] != client_id
                or row["public_key"] != key.public_key().public_bytes_raw()):
            raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 401)
        return row

    @staticmethod
    def _require_owned_slot(con, owner_id, client_id, origin, credential):
        grant_id, key = credential
        row = con.execute("""SELECT revoked_at FROM mcp_grants
                             WHERE grant_id=? AND user_id=? AND client_id=? AND origin=? AND public_key=?""",
                          (grant_id, owner_id, client_id, origin, key.public_key().public_bytes_raw())).fetchone()
        if row is None:
            raise PlatformError("MCP_CONNECTION_SLOT_CONFLICT", 409)
        return row["revoked_at"]

    def owned_slot_revoked_at(self, owner_id, client_id, origin, credential):
        with closing(self.database.connect()) as con:
            return self._require_owned_slot(con, owner_id, client_id, origin, credential)

    def local_status(self, owner_id, client_id, origin, credential):
        with closing(self.database.connect()) as con:
            con.execute("BEGIN")
            row = self._local_binding(con, owner_id, client_id, origin, credential)
            seen = con.execute("SELECT confirmed_at FROM mcp_tool_discovery WHERE grant_id=?",
                               (row["grant_id"],)).fetchone()
        return {"scope": row["scope"], "expires_at": timestamp(min(row["expires_at"], row["session_expiry"])),
                "client_confirmed_at": None if seen is None else timestamp(seen["confirmed_at"])}

    def publish_local_credential(self, token, client_id, origin, scope, credential, previous, publish,
                                 *, previous_revoked_at):
        # Hold the same writer lock as signed authorization and revocation across the swap.
        grant_id, key = credential
        with self.database.transaction() as con:
            session = self.auth._recent(con, token)
            owner_id = session["user_id"]
            if scope not in scopes_for(session["role"]):
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            now = self.clock()
            if previous is not None:
                if self._require_owned_slot(con, owner_id, client_id, origin, previous) != previous_revoked_at:
                    raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 409)
                retired = con.execute("UPDATE mcp_grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                                      (now, previous[0])).rowcount
                if retired:
                    self.database.audit(owner_id, "mcp.grant_revoked", previous[0], connection=con)
            self._create_grant(con, grant_id, key.public_key().public_bytes_raw(), owner_id, session["session_id"],
                               session["expires_at"], client_id, client_label(client_id), scope, origin,
                               session["role"], now)
            publish()

    def _active(self, con, grant_id, origin, now, required):
        row = con.execute("""
            SELECT g.*, u.username,u.enabled,u.role,s.revoked_at AS session_revoked,s.expires_at AS session_expiry
            FROM mcp_grants g JOIN sessions s ON s.session_id=g.session_id AND s.user_id=g.user_id
                JOIN users u ON u.user_id=g.user_id WHERE g.grant_id=?""", (grant_id,)).fetchone()
        if (not row or row["origin"] != origin or not row["enabled"] or row["role"] != row["approved_role"]
                or row["revoked_at"] is not None or row["session_revoked"] is not None
                or min(row["expires_at"], row["session_expiry"]) <= now):
            raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 401)
        if row["scope"] not in scopes_for(row["role"]) or not permits(row["scope"], required):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        return row

    def authorize(self, proof, *, origin, method, target, body, required, observe_tools=False):
        try:
            grant_id, stamp, nonce, encoded_signature = proof.split(".")
            if not GRANT_ID.fullmatch(grant_id) or not stamp.isascii() or not stamp.isdecimal() or len(stamp) > 12:
                raise ValueError
            instant = int(stamp)
            if str(instant) != stamp:
                raise ValueError
            decode(nonce, 16)
            signature = decode(encoded_signature, 64)
        except (AttributeError, TypeError, ValueError):
            raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 401) from None
        with self.database.transaction() as con:
            now = self.clock()
            if abs(now - instant) > REQUEST_WINDOW:
                raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 401)
            row = self._active(con, grant_id, origin, now, required)
            try:
                message = request_message(self.installation, origin, method, target, body, grant_id, stamp, nonce)
                Ed25519PublicKey.from_public_bytes(row["public_key"]).verify(signature, message)
            except (InvalidSignature, ValueError):
                raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 401) from None
            con.execute("DELETE FROM mcp_nonces WHERE expires_at<=?", (now,))
            recorded = con.execute("INSERT OR IGNORE INTO mcp_nonces VALUES(?,?,?)",
                                   (grant_id, nonce, instant + REQUEST_WINDOW + 1)).rowcount
            if recorded != 1:
                raise PlatformError("MCP_REPLAY_REJECTED", 409)
            self.auth._rate_limit(con, "mcp-call:" + grant_id, limit=120, window=60)
            if observe_tools:
                con.execute("""INSERT INTO mcp_tool_discovery(grant_id,confirmed_at) VALUES(?,?)
                               ON CONFLICT(grant_id) DO UPDATE SET confirmed_at=excluded.confirmed_at""",
                            (grant_id, now))
        role = "researcher" if any(permits(row["scope"], cap) for cap in ("manage", "research")) else "viewer"

        def check(con):
            self._active(con, grant_id, origin, self.clock(), required)

        return McpDelegation(row["user_id"], row["username"], role, check, row["scope"]), row["scope"]
