"""One direct-base Qlib bootstrap. No package/.pth/Qlib import before EXEC.

Parent launches exact Qlib base Python with -I -S -B -X utf8, assigns its Job,
then sends one bounded private GO packet. No inherited user environment lookup,
venv copying, provider SDK imports or arbitrary execution command is supported.
"""
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
from pathlib import Path
import runpy
import sys

MAX_PACKET = 524284


def fail():
    raise RuntimeError("QLIB_BOOTSTRAP_REJECTED")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def checked_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail()
            result[key] = value
        return result
    def constant(value):
        fail()
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def read_line(limit):
    raw = sys.stdin.buffer.readline(limit + 1)
    if not raw.endswith(b"\n") or len(raw) > limit:
        fail()
    return raw


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


def validate_packet(packet):
    required = {"schema", "request", "attempt_root", "expected_pid", "job_handle",
                "worker_python", "base_python", "base_dll", "bootstrap", "entry",
                "contract", "site_root", "venv_prefix", "binding"}
    if type(packet) is not dict or set(packet) != required or packet["schema"] != "qlib-admission-2":
        fail()
    binding = packet["binding"]
    body = {key: value for key, value in packet.items() if key != "binding"}
    if hashlib.sha256(encoded(body)).hexdigest() != binding:
        fail()
    if (type(packet["expected_pid"]) is not int or packet["expected_pid"] <= 0
            or type(packet["job_handle"]) is not int or packet["job_handle"] <= 0
            or type(packet["request"]) is not dict):
        fail()
    return packet


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


def main():
    if sys.version_info[:3] != (3, 12, 14) or not sys.flags.isolated or not sys.flags.no_site:
        fail()
    if read_line(3) != b"GO\n":
        fail()
    packet = validate_packet(checked_json(read_line(MAX_PACKET)))
    paths = {name: verify_pin(packet[name])
             for name in ("worker_python", "base_python", "base_dll", "bootstrap", "entry", "contract")}
    if (Path(__file__).resolve() != paths["bootstrap"].resolve()
            or paths["entry"].name != "qlib_child.py"
            or paths["contract"].name != "qlib_workflow.py"
            or paths["entry"].parent != paths["contract"].parent):
        fail()
    attempt = Path(packet["attempt_root"]).resolve()
    if attempt != Path.cwd().resolve() or not attempt.is_dir():
        fail()
    site, prefix = Path(packet["site_root"]).resolve(), Path(packet["venv_prefix"]).resolve()
    if site != prefix / "Lib/site-packages" or not site.is_dir():
        fail()
    for path in (site, prefix):
        for item in (path, *path.parents):
            if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
                fail()
    identity = own_identity(packet["job_handle"])
    if (identity["actual_pid"] != packet["expected_pid"]
            or Path(identity["actual_image"]).resolve() != paths["base_python"].resolve()
            or Path(identity["actual_python_dll"]).resolve() != paths["base_dll"].resolve()):
        fail()
    if any(name.split(".", 1)[0] in {"qlib", "numpy", "pandas", "sklearn", "mlflow", "lightgbm"}
           for name in sys.modules):
        fail()
    # This is the Qlib worker's explicit prefix/site, not phase1's environment.
    # Do not execute .pth files or silently call site.addsitedir.
    sys.path.insert(0, str(site))
    sys.prefix = sys.exec_prefix = str(prefix)
    ready = {"schema": "qlib-ready-2", "binding": packet["binding"], **identity,
             "actual_python_dll_sha256": packet["base_dll"]["sha256"],
             "version": list(sys.version_info[:3]), "site_root": str(site),
             "prefix": str(prefix), "actual_base_prefix": str(Path(sys.base_prefix).resolve()),
             "qlib_imported": False, "automatic_site_startup": False}
    print(json.dumps(ready), flush=True)
    if read_line(5) != b"EXEC\n":
        fail()
    # A mutable external file is never accepted just because READY once passed.
    for key in ("bootstrap", "entry", "contract"):
        verify_pin(packet[key])
    original_stdin, original_argv = sys.stdin, sys.argv
    request = encoded(packet["request"])
    if len(request) > 524288:
        fail()
    sys.stdin = io.TextIOWrapper(io.BytesIO(request), encoding="utf-8")
    sys.argv = [str(paths["entry"]), "--mode", "native", "--attempt-root", str(attempt)]
    try:
        runpy.run_path(str(paths["entry"]), run_name="__main__")
    finally:
        sys.stdin.close()
        sys.stdin, sys.argv = original_stdin, original_argv


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"status":"QLIB_BOOTSTRAP_FAILED"}', flush=True)
        raise SystemExit(71)
