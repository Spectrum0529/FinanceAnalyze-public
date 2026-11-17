"""Password and OTP primitives for the account lifecycle."""
from datetime import datetime, timezone
from functools import lru_cache
import os
import re
from threading import BoundedSemaphore

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import PlatformError
from .security import verify_password as verify_legacy


_KDF_CAPACITY = BoundedSemaphore(4)
_ARGON_FORMAT = re.compile(r"\$argon2id\$v=19\$m=65536,t=3,p=1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}")
_COMMON = {"passwordpassword", "password123456789", "123456789012345", "qwertyuiopasdfgh",
           "correct horse battery staple", "letmeinletmeinletmein", "adminadminadmin",
           "financeanalyze", "financeanalyze123"}


@lru_cache(maxsize=1)
def _argon_hasher():
    # Native research workers import the shared Principal without using account crypto.
    from argon2 import PasswordHasher

    return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16)


def _bounded_kdf(call, *args):
    if not _KDF_CAPACITY.acquire(blocking=False):
        raise PlatformError("RATE_LIMITED", 429)
    try:
        return call(*args)
    finally:
        _KDF_CAPACITY.release()


def password_hash(password, *, policy=True):
    if type(password) is not str or not (15 if policy else 1) <= len(password) <= 128:
        raise PlatformError("PASSWORD_POLICY", 422)
    try:
        password.encode("utf-8")
    except UnicodeError:
        raise PlatformError("PASSWORD_POLICY", 422) from None
    folded = password.strip().casefold()
    if policy and (folded in _COMMON or len(set(folded)) < 2):
        raise PlatformError("PASSWORD_POLICY", 422)
    return _bounded_kdf(_argon_hasher().hash, password)


def verify_password(password, encoded):
    if type(password) is not str or not 1 <= len(password) <= 128 or type(encoded) is not str:
        return False
    if _ARGON_FORMAT.fullmatch(encoded):
        from argon2.exceptions import InvalidHashError, VerificationError

        try:
            return _bounded_kdf(_argon_hasher().verify, encoded, password)
        except (InvalidHashError, VerificationError, UnicodeError):
            return False
    try:
        return _bounded_kdf(verify_legacy, password, encoded)
    except UnicodeError:
        return False


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class Authenticator:
    def __init__(self, key):
        if key is not None and (type(key) is not bytes or len(key) != 32):
            raise PlatformError("MFA_UNAVAILABLE", 503)
        self.cipher = AESGCM(key) if key else None

    @property
    def available(self):
        return self.cipher is not None

    def require_available(self):
        if not self.available:
            raise PlatformError("MFA_UNAVAILABLE", 503)

    def generate(self, user_id, username):
        self.require_available()
        import pyotp

        secret = pyotp.random_base32()
        nonce = os.urandom(12)
        encrypted = nonce + self.cipher.encrypt(nonce, secret.encode("ascii"), user_id.encode("utf-8"))
        uri = pyotp.TOTP(secret).provisioning_uri(username, issuer_name="FinanceAnalyze")
        return secret, uri, encrypted

    def step(self, user_id, encrypted, otp, now, after):
        self.require_available()
        if type(otp) is not str or re.fullmatch(r"[0-9]{6}", otp) is None:
            raise PlatformError("MFA_INVALID", 401)
        import pyotp

        try:
            secret = self.cipher.decrypt(encrypted[:12], encrypted[12:], user_id.encode("utf-8")).decode("ascii")
        except (InvalidTag, UnicodeError, ValueError):
            raise PlatformError("MFA_UNAVAILABLE", 503) from None
        verifier = pyotp.TOTP(secret)
        current = int(now // verifier.interval)
        for step in (current - 1, current, current + 1):
            if step > after and verifier.verify(otp, for_time=datetime.fromtimestamp(
                    step * verifier.interval, timezone.utc)):
                return step
        raise PlatformError("MFA_INVALID", 401)
