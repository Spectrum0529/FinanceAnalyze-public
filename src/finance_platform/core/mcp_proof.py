"""Versioned Ed25519 proofs shared by the loopback API and the stdio bridge."""
import base64
import hashlib
import re
import secrets
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .errors import PlatformError
from .local_personal import require_local_scope

BASE = "/api/v1/operations/mcp-connection"
EXECUTE = BASE + "/execute"
SESSION = BASE + "/session"
PROOF_HEADER = "X-FinanceAnalyze-MCP"
CLIENT_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}", re.ASCII)
GRANT_ID = re.compile(r"[0-9a-f]{32}", re.ASCII)
CLIENT_NAMES = {"codex": "Codex", "deepseek": "DeepSeek Harness", "other": "其他 MCP 客户端"}
REQUEST_WINDOW = 30


def encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value, size):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) != (size * 8 + 5) // 6:
        raise ValueError("Invalid proof encoding")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != size or encode(decoded) != value:
        raise ValueError("Invalid proof encoding")
    return decoded


def pairing_id(pairing_public):
    return hashlib.sha256(pairing_public).hexdigest()


def installation_identity(root):
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()


def pairing_message(installation, origin, client_id, identifier, public_key):
    return f"financeanalyze-mcp-pair-v1\n{installation}\n{origin}\n{client_id}\n{identifier}\n{encode(public_key)}".encode("ascii")


def make_claim(code, installation, origin, client_id, key):
    pairing = Ed25519PrivateKey.from_private_bytes(decode(code, 32))
    identifier = pairing_id(pairing.public_key().public_bytes_raw())
    public = key.public_key().public_bytes_raw()
    message = pairing_message(installation, origin, client_id, identifier, public)
    return {"pairing_id": identifier, "client_id": client_id, "public_key": encode(public),
            "signature": encode(pairing.sign(message)), "key_proof": encode(key.sign(b"client\n" + message))}


def request_message(installation, origin, method, target, body, grant_id, timestamp, nonce):
    prefix = f"financeanalyze-mcp-request-v1\n{installation}\n{origin}\n{grant_id}\n{timestamp}\n{nonce}\n{method}\n"
    return prefix.encode("ascii") + target + b"\n" + hashlib.sha256(body).hexdigest().encode("ascii")


def request_proof(key, installation, origin, method, target, body, grant_id, timestamp):
    nonce = encode(secrets.token_bytes(16))
    signature = key.sign(request_message(installation, origin, method, target, body, grant_id, timestamp, nonce))
    return f"{grant_id}.{timestamp}.{nonce}.{encode(signature)}"


def listener_origin(allowed_origins, scope, *, bridge=False):
    """Authorize the actual local listener and its Host, never a proxy claim."""
    require_local_scope(scope, allowed_origins)
    try:
        server, port = scope["server"]
        if server not in {"127.0.0.1", "::1"} or type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError
        origin = f"http://127.0.0.1:{port}"
        host = next(value.decode("ascii").lower() for key, value in scope["headers"] if key.lower() == b"host")
        origins = [value.decode("ascii") for key, value in scope["headers"] if key.lower() == b"origin"]
        if scope["scheme"] != "http" or origin not in allowed_origins:
            raise ValueError
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise ValueError
        if origins and (origins[0] not in allowed_origins or urlsplit(origins[0]).netloc != host):
            raise ValueError
        if bridge and (host != f"127.0.0.1:{port}" or origins != [origin]):
            raise ValueError
        return origin
    except (KeyError, StopIteration, TypeError, UnicodeError, ValueError):
        raise PlatformError("ORIGIN_REJECTED", 403) from None
