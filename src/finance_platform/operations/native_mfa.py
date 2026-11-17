"""Explicitly provision the Windows operator's local account MFA key."""
from contextlib import closing
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile

from finance_platform.core.errors import PlatformError
from finance_platform.core.source_binding import BindingError, checked_regular_file
from .paths import protect_private
from .provider_credentials import _dpapi


KEY_RELATIVE = ".local/platform/account-security/mfa-key.dpapi"
ENTROPY = b"FinanceAnalyze/native-account-mfa/v1"


def _unavailable():
    return PlatformError("MFA_UNAVAILABLE", 503)


def _key_path(root):
    path = root
    for name in KEY_RELATIVE.split("/"):
        path /= name
        try:
            info = path.lstat()
        except FileNotFoundError:
            return root / KEY_RELATIVE
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _unavailable()
        if path != root / KEY_RELATIVE and not stat.S_ISDIR(info.st_mode):
            raise _unavailable()
    return path


def read_native_mfa_key(root):
    root = Path(root).resolve()
    try:
        if not _key_path(root).exists():
            return None
        path = checked_regular_file(root, KEY_RELATIVE)
        with path.open("rb") as stream:
            protected = stream.read(4097)
        if not 0 < len(protected) <= 4096:
            raise _unavailable()
        key = _dpapi(protected, ENTROPY, decrypt=True)
        if len(key) != 32:
            raise _unavailable()
        return key
    except (OSError, BindingError, PlatformError):
        raise _unavailable() from None


def _existing_factor(root, database):
    try:
        database.lstat()
    except FileNotFoundError:
        return False
    path = checked_regular_file(root, database.relative_to(root).as_posix())
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_totp'").fetchone()
        return bool(has_table and connection.execute(
            "SELECT 1 FROM auth_totp WHERE active_secret IS NOT NULL OR pending_secret IS NOT NULL LIMIT 1").fetchone())


def prepare_native_mfa_key(root, database):
    """Only the operator command calls this. Repeated calls preserve the key."""
    root = Path(root).resolve()
    try:
        path = _key_path(root)
        if read_native_mfa_key(root) is not None:
            return "already_configured"
        if os.name != "nt" or _existing_factor(root, database):
            raise _unavailable()
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        _key_path(root)
        # An existing directory without a key may indicate key loss. An operator
        # must recover the old key or clear factors through local recovery.
        path.parent.mkdir()
        protect_private(path.parent)
        protected = _dpapi(secrets.token_bytes(32), ENTROPY)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows rename refuses to replace a file written by another action.
            temporary.rename(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if read_native_mfa_key(root) is None:
            raise _unavailable()
        return "prepared"
    except (OSError, ValueError, BindingError, PlatformError, sqlite3.Error):
        raise _unavailable() from None
