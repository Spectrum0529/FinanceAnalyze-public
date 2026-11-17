"""Bounded canonical metadata and standard-library password/session primitives."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import math
import os
import secrets
import re

from predictor.secrets import sensitive_field
from .errors import PlatformError


def now_text():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def scoped_values():
    return [v for k, v in os.environ.items() if v and k.startswith(("PREDICTOR_", "FINANCE_PLATFORM_", "ALPACA_", "SEC_"))
            and k.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))]


def clean(value, depth=0):
    if depth > 20:
        raise PlatformError("INVALID_INPUT")
    if isinstance(value, dict):
        if len(value) > 1000 or any(type(k) is not str or len(k) > 128 for k in value):
            raise PlatformError("INVALID_INPUT")
        if any(sensitive_field(k) and v for k, v in value.items()):
            raise PlatformError("INVALID_INPUT")
        return {k: clean(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 10000:
            raise PlatformError("INVALID_INPUT")
        return [clean(v, depth + 1) for v in value]
    if type(value) is str:
        if len(value) > 65536 or any(s in value for s in scoped_values()):
            raise PlatformError("INVALID_INPUT")
        return value
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is Decimal and value.is_finite() and len(value.as_tuple().digits) <= 36 and -12 <= value.as_tuple().exponent <= 24:
        return format(value, "f")
    if type(value) is datetime and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise PlatformError("INVALID_INPUT")


def safe_json(value):
    encoded = json.dumps(clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise PlatformError("INVALID_INPUT")
    return encoded


def token_digest(token: str) -> str:
    if type(token) is not str or len(token) > 256:
        raise PlatformError("NOT_AUTHENTICATED", 401)
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_password(password):
    if type(password) is not str or not 12 <= len(password) <= 128:
        raise PlatformError("INVALID_INPUT", 422)


def hash_password(password):
    validate_password(password)
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt-v1$32768$8$1$" + salt.hex() + "$" + key.hex()


def verify_password(password, encoded):
    # Bounded parser prevents stored parameters from causing resource exhaustion.
    if type(password) is not str or not 1 <= len(password) <= 128:
        return False
    match = re.fullmatch(r"scrypt-v1\$32768\$8\$1\$([0-9a-f]{32})\$([0-9a-f]{64})", encoded)
    if not match:
        return False
    key = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(match[1]), n=2**15, r=8, p=1,
                         maxmem=64 * 1024 * 1024, dklen=32)
    return hmac.compare_digest(key.hex(), match[2])
