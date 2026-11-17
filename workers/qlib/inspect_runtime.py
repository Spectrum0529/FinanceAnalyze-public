"""Future released worker identity capture only; no application/package import."""
import ctypes
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys


def pin(path):
    path=Path(path).resolve();data=path.read_bytes()
    return {"path":str(path),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}


worker=Path(__file__).resolve().parent
venv=worker/".venv"
if sys.platform!="win32" or sys.version_info[:3]!=(3,12,14) or ctypes.sizeof(ctypes.c_void_p)!=8:
    raise RuntimeError("EXACT_WINDOWS_PYTHON_31214_X64_REQUIRED")
if Path(sys.prefix).resolve()!=venv.resolve() or Path(sys.executable).resolve()!=(venv/"Scripts/python.exe").resolve():
    raise RuntimeError("THIS_WORKER_LOCAL_INTERPRETER_REQUIRED_NO_FALLBACK")
spec=json.loads((worker/"worker_spec.json").read_text(encoding="utf-8"))
for relative,expected in spec["control_file_sha256"].items():
    target=(worker/relative).resolve()
    if not target.is_relative_to(worker) or pin(target)["sha256"]!=expected:
        raise RuntimeError("WORKER_CONTROL_FILE_IDENTITY_MISMATCH")
for row in spec["local_wheels"]:
    target=(worker/row["path"]).resolve()
    if not target.is_relative_to(worker):raise RuntimeError("WORKER_WHEEL_OUTSIDE_PROJECT")
    actual=pin(target)
    if actual["bytes"]!=row["bytes"] or actual["sha256"]!=row["sha256"]:
        raise RuntimeError("WORKER_WHEEL_IDENTITY_MISMATCH")
site=venv/"Lib/site-packages"
def normalize(name):return name.lower().replace("_","-").replace(".","-")
versions={normalize(dist.metadata["Name"]):dist.version for dist in importlib.metadata.distributions(path=[str(site)])}
if versions!=spec["expected_installed_distribution_versions"]:
    raise RuntimeError("WORKER_INSTALLED_VERSIONS_DIFFER_FROM_APPROVED_LOCK_ENVIRONMENT")
installed_sources=[]
for row in spec["primary_distribution_source_files"]:
    target=(site/row["path"]).resolve()
    if not target.is_relative_to(site.resolve()):raise RuntimeError("WORKER_SOURCE_OUTSIDE_LOCAL_SITE")
    actual=pin(target)
    if actual["bytes"]!=row["bytes"] or actual["sha256"]!=row["sha256"]:
        raise RuntimeError("PRIMARY_DISTRIBUTION_SOURCE_IDENTITY_MISMATCH")
    installed_sources.append({key:row[key] for key in ("path","bytes","sha256")})

# Same loaded-Python-DLL lookup used by the already recorded candidate probes.
k=ctypes.WinDLL("kernel32",use_last_error=True)
k.GetModuleHandleW.argtypes=[ctypes.c_wchar_p];k.GetModuleHandleW.restype=ctypes.c_void_p
k.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint];k.GetModuleFileNameW.restype=ctypes.c_uint
buffer=ctypes.create_unicode_buffer(32768)
if not k.GetModuleFileNameW(k.GetModuleHandleW("python312.dll"),buffer,len(buffer)):
    raise RuntimeError("ACTUAL_LOADED_PYTHON_DLL_IDENTITY_UNAVAILABLE")
source_digest=hashlib.sha256(json.dumps(installed_sources,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")).hexdigest()
print(json.dumps({
    "schema":"FINANCE_ANALYZE_WORKER_ACTUAL_RUNTIME_IDENTITY_1",
    "status":"MEASURED_IDENTITY_ONLY_NOT_BUSINESS_ACTIVATION",
    "worker_kind":spec["worker_kind"],"worker_root":str(worker),
    "version":list(sys.version_info[:3]),"platform":sys.platform,"pointer_bits":ctypes.sizeof(ctypes.c_void_p)*8,
    "python":pin(sys.executable),"venv_prefix":str(Path(sys.prefix).resolve()),"site_root":str(site.resolve()),
    "venv_config":pin(venv/"pyvenv.cfg"),"base_prefix":str(Path(sys.base_prefix).resolve()),
    "base_python":pin(sys._base_executable),"actual_loaded_python_DLL":pin(buffer.value),
    "manifest":pin(worker/"pyproject.toml"),"lock":pin(worker/"uv.lock"),"worker_spec":pin(worker/"worker_spec.json"),
    "identity_probe":pin(__file__),"console_entrypoint_image_not_executed":pin(worker/spec["console_executable_relative"]),
    "installed_distribution_versions":dict(sorted(versions.items())),
    "primary_distribution_source_files":installed_sources,"primary_distribution_source_digest":source_digest,
    "identity_limit":"Pinned primary wheel code/native payload and installed versions only; no all-transitive-DLL or native-code isolation claim.",
    "business_or_auth_import_or_execution":False,"reports_venv_fallback_allowed":False
},ensure_ascii=False))
