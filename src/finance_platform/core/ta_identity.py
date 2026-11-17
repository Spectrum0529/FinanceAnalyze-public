"""Pinned low-level bootstrap helpers reused from Qlib r02; no main call."""
import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path

def fail():raise RuntimeError("TA_BOOTSTRAP_REJECTED")

def verify_pin(pin):
    if (type(pin) is not dict or set(pin) != {"path", "bytes", "sha256"}
            or type(pin["path"]) is not str or not Path(pin["path"]).is_absolute()
            or type(pin["bytes"]) is not int or not 0 < pin["bytes"] <= 64 * 1024**2):
        fail()
    path = Path(pin["path"]).absolute()
    for item in (path, *path.parents):
        stat = item.lstat()
        if item.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            fail()
        if item.is_file() and stat.st_nlink != 1:
            fail()
    if not path.is_file() or path.stat().st_size != pin["bytes"]:
        fail()
    raw = path.read_bytes()
    if len(raw) != pin["bytes"] or hashlib.sha256(raw).hexdigest() != pin["sha256"]:
        fail()
    return path

def own_identity(query_handle):
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x00000800)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcessId.argtypes = []
    kernel.GetCurrentProcessId.restype = wintypes.DWORD
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HANDLE
    kernel.GetModuleFileNameW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    def filename(handle):
        target = ctypes.create_unicode_buffer(32768)
        count = kernel.GetModuleFileNameW(handle, target, len(target))
        if not 0 < count < len(target):
            fail()
        return str(Path(target.value).resolve())
    contained = wintypes.BOOL()
    try:
        if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), wintypes.HANDLE(query_handle),
                                     ctypes.byref(contained)) or not contained.value:
            fail()
        dll = kernel.GetModuleHandleW("python312.dll")
        if not dll:
            fail()
        result = {"actual_pid": int(kernel.GetCurrentProcessId()),
                  "actual_image": filename(None), "actual_python_dll": filename(dll),
                  "actual_self_in_held_job": True}
    finally:
        if not kernel.CloseHandle(wintypes.HANDLE(query_handle)):
            fail()
    result["duplicated_job_handle_closed"] = True
    return result

def verify_source(row, site):
    """Exact installed uv source identity; never an arbitrary alias allowance.

    uv's existing package .py files can be hardlinked into its cache. Preserve
    that installation. Only fixed TA-site .py rows bind dev/ino/nlink+SHA; all
    executables/control/data/checkpoint paths retain nlink1 verification.
    """
    if not row.get("installed_dependency"):
        return verify_pin({key:row[key] for key in ("path","bytes","sha256")})
    path=Path(row["path"]).absolute()
    site=Path(site).absolute()
    if path.suffix!=".py" or not path.is_relative_to(site):fail()
    for item in (path,*path.parents):
        info=item.lstat()
        if item.is_symlink() or getattr(info,"st_file_attributes",0)&0x400:fail()
    before=path.lstat()
    identity={"st_dev":before.st_dev,"st_ino":before.st_ino,"st_nlink":before.st_nlink}
    if identity!=row["installed_file_identity"] or before.st_nlink<1 or not path.is_file():fail()
    raw=path.read_bytes()
    after=path.lstat()
    if (len(raw)!=row["bytes"] or hashlib.sha256(raw).hexdigest()!=row["sha256"]
            or (before.st_dev,before.st_ino,before.st_nlink,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_nlink,after.st_size,after.st_mtime_ns)):fail()
    return path
