"""Fail-closed private paths and native file protection."""
from pathlib import Path
import base64
import os
import subprocess

from finance_platform.core.errors import PlatformError


def contained(base, path, *, allow_base=False):
    base, path = Path(base).resolve(), Path(path)
    # Reject links/junctions before resolution as well as escapes after resolution.
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise PlatformError("INVALID_INPUT")
        if candidate == base:
            break
    result = path.resolve()
    if not result.is_relative_to(base) or (result == base and not allow_base):
        raise PlatformError("INVALID_INPUT")
    return result


def _windows_powershell():
    """Resolve Windows PowerShell via the OS, never environment or cwd search."""
    import ctypes
    from ctypes import wintypes

    # LOAD_LIBRARY_SEARCH_SYSTEM32 excludes cwd/PATH from the API DLL lookup.
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x00000800)
    query = kernel.GetSystemDirectoryW
    query.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    query.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = query(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError(ctypes.get_last_error(), "Windows system directory unavailable")
    system = Path(buffer.value)
    if not system.is_absolute():
        raise OSError("Windows system directory is not absolute")
    return str(system / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def protect_private(path):
    """Apply owner-only POSIX mode or protected NTFS ACL before writing secrets."""
    path = Path(path).resolve()
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return
    from .windows_acl import SCRIPT
    # A Python launched by PowerShell 7 may inherit its incompatible PSModulePath.
    child_environment = dict(os.environ)
    for key in tuple(child_environment):
        if key.upper() == "PSMODULEPATH":
            child_environment.pop(key)
    child_environment["FINANCE_PLATFORM_ACL_TARGET"] = str(path)
    encoded = base64.b64encode(SCRIPT.encode("utf-16le")).decode("ascii")
    result = subprocess.run([_windows_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=30, env=child_environment)
    if result.returncode:
        raise PlatformError("DATABASE_UNAVAILABLE", 503)
