"""Store the per-client signing key only in current-user Windows DPAPI."""
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from finance_platform.core.errors import PlatformError
from finance_platform.core.mcp_proof import CLIENT_ID, GRANT_ID
from finance_platform.core.source_binding import checked_regular_file
from .paths import contained, protect_private
from .provider_credentials import _dpapi


class McpKeyStore:
    def __init__(self, root, port, client_id):
        self.root = Path(root).resolve()
        if not CLIENT_ID.fullmatch(client_id) or not 1024 <= port <= 65535:
            raise PlatformError("INVALID_INPUT", 422)
        self.relative = f".local/secrets/mcp/{port}-{client_id}.dpapi"
        label = f"financeanalyze-mcp-key-v1\n{self.root.as_posix().casefold()}\n{port}\n{client_id}"
        self.entropy = hashlib.sha256(label.encode()).digest()

    def read(self):
        try:
            path = checked_regular_file(self.root, self.relative)
            with path.open("rb") as stream:
                blob = stream.read(16385)
            if not 0 < len(blob) <= 16384:
                raise ValueError
            value = _dpapi(blob, self.entropy, decrypt=True)
            if len(value) != 48:
                raise ValueError
            return value[:16].hex(), Ed25519PrivateKey.from_private_bytes(value[16:])
        except (OSError, ValueError):
            raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 401) from None

    def read_for_connect(self):
        try:
            path = contained(self.root, self.root / self.relative)
            path.lstat()
            return self.read()
        except FileNotFoundError:
            return None
        except (PlatformError, OSError, ValueError):
            raise PlatformError("MCP_CONNECTION_SLOT_CONFLICT", 409) from None

    @contextmanager
    def locked(self):
        from .launch import _process_lock
        try:
            relative = self.relative + ".lock"
            path = contained(self.root, self.root / relative)
            if path.exists():
                checked_regular_file(self.root, relative)
            with _process_lock(path, busy_code="MCP_CONNECTION_IN_PROGRESS"):
                yield
        except PlatformError:
            raise
        except (OSError, ValueError):
            raise PlatformError("CREDENTIAL_STORE_UNAVAILABLE", 503) from None

    def revision(self):
        try:
            info = checked_regular_file(self.root, self.relative).stat()
            return info.st_ino, info.st_mtime_ns, info.st_size
        except (OSError, ValueError):
            return None

    def write(self, grant_id, key):
        with self.prepare(grant_id, key) as publish:
            publish()

    @contextmanager
    def prepare(self, grant_id, key, *, restore_on_error=False):
        if not GRANT_ID.fullmatch(grant_id):
            raise PlatformError("MCP_PRODUCT_RESPONSE_INVALID", 502)
        try:
            blob = _dpapi(bytes.fromhex(grant_id) + key.private_bytes_raw(), self.entropy)
            path = contained(self.root, self.root / self.relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            protect_private(path.parent)
            previous = checked_regular_file(self.root, self.relative) if path.exists() else None
            temporary = contained(self.root, path.parent / ("." + uuid.uuid4().hex + ".tmp"))
            backup = None
            published = False

            def publish():
                nonlocal published
                os.replace(temporary, path)
                published = True

            try:
                self._stage(temporary, blob)
                expected = bytes.fromhex(grant_id) + key.private_bytes_raw()
                if _dpapi(temporary.read_bytes(), self.entropy, decrypt=True) != expected:
                    raise ValueError
                if restore_on_error and previous is not None:
                    backup = contained(self.root, path.parent / ("." + uuid.uuid4().hex + ".tmp"))
                    with previous.open("rb") as stream:
                        original = stream.read(16385)
                    if not 0 < len(original) <= 16384:
                        raise ValueError
                    self._stage(backup, original)
                try:
                    yield publish
                except BaseException:
                    if restore_on_error and published:
                        if backup is None:
                            path.unlink()
                        else:
                            recovery, backup = backup, None
                            os.replace(recovery, path)
                    raise
            finally:
                temporary.unlink(missing_ok=True)
                if backup is not None:
                    backup.unlink(missing_ok=True)
        except (OSError, ValueError):
            raise PlatformError("CREDENTIAL_STORE_UNAVAILABLE", 503) from None

    @staticmethod
    def _stage(path, blob):
        with path.open("xb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        protect_private(path)
