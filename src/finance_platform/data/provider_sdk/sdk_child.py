"""Initial admission gate executes before site packages, SDK or app imports."""
import sys
if sys.stdin.buffer.readline(4) != b"GO\n":
    raise SystemExit(2)
# Only stdlib before the second parent admission. Exact held Job handle is
# duplicated into this actual base interpreter while it waits on its first pipe.
import os
import json
import ctypes as C
from ctypes import wintypes as W
try:
    admission_raw=sys.stdin.buffer.readline(65537)
    if len(admission_raw)>65536:raise ValueError()
    config=json.loads(admission_raw)
    k=C.WinDLL('kernel32',use_last_error=True)
    k.GetCurrentProcess.restype=W.HANDLE
    k.IsProcessInJob.argtypes=[W.HANDLE,W.HANDLE,C.POINTER(W.BOOL)]
    k.CloseHandle.argtypes=[W.HANDLE]
    k.GetModuleFileNameW.argtypes=[W.HANDLE,W.LPWSTR,W.DWORD]
    k.GetModuleHandleW.argtypes=[W.LPCWSTR];k.GetModuleHandleW.restype=W.HANDLE
    member=W.BOOL();handle=W.HANDLE(config['admission_handle']);image=C.create_unicode_buffer(32768)
    if not k.IsProcessInJob(k.GetCurrentProcess(),handle,C.byref(member)) or not member.value:raise ValueError()
    if not k.GetModuleFileNameW(None,image,len(image)):raise ValueError()
    dll=C.create_unicode_buffer(32768)
    if not k.GetModuleFileNameW(k.GetModuleHandleW('python312.dll'),dll,len(dll)):raise ValueError()
    if not k.CloseHandle(handle):raise ValueError()
    admission={'schema_version':'sdk-admission-1','binding':config['binding'],'actual_pid':os.getpid(),
               'actual_image':image.value,'actual_self_in_held_job':bool(member.value),
               'duplicated_job_handle_closed':True,
               'sdk_modules_loaded':any(n.split('.')[0] in {'yfinance','akshare','curl_cffi','openbb','openbb_core','openbb_cboe'} for n in sys.modules),
               'python_hexversion':sys.hexversion,'version_info':list(sys.version_info[:3]),'actual_python_dll':dll.value}
    sys.stdout.buffer.write(json.dumps(admission,separators=(',',':')).encode()+b'\n');sys.stdout.buffer.flush()
    if sys.stdin.buffer.readline(6)!=b'EXEC\n':raise SystemExit(3)
    goal_token=None
    if config['call'].get('provider')=='tushare' and config['fixture'] is None:
        token_line=sys.stdin.buffer.readline(514)
        if not token_line.endswith(b'\n') or not 1<len(token_line)<=513:raise ValueError()
        goal_token=token_line[:-1].decode('ascii')
except BaseException:
    raise SystemExit(3)
# Parent uses -I -S: no user site, .pth/sitecustomize or SDK before Job assignment.
from pathlib import Path
sys.path.insert(0,sys.argv[1])  # Server-owned fixed source root, never a request field.
sys.path.insert(0,sys.argv[2])  # The exact FA010-approved candidate site-packages.
package=sys.argv[3]
if package=='provider_sdk':sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
elif package!='finance_platform.data.provider_sdk':raise SystemExit(3)
import importlib
import hashlib
import contextlib
import io
import json
import os
import time

from finance_platform.data.contracts import DataError
LIMITS=importlib.import_module(package+'.contracts').LIMITS
artifacts_module=importlib.import_module(package+'.artifacts')
encoded,write_artifact=artifacts_module.encoded,artifacts_module.write_artifact
Budget=importlib.import_module(package+'.network_gate').Budget
artifact_limit=importlib.import_module(package+'.history_contracts').artifact_limit


class Suppressed:
    def __init__(self):self.count=0
    def write(self,value):
        self.count+=len(value.encode("utf-8",errors="replace"))
        if self.count>LIMITS["stderr_bytes"]:
            raise DataError("SDK_LOG_LIMIT_EXCEEDED")
        return len(value)
    def flush(self):pass


def execute():
    if set(config)!={"schema_version","call","work","pins","binding","deadline","fixture","fixture_module","admission_handle"} or config["schema_version"]!="sdk-child-call-1":
        raise DataError("SDK_INPUT_INVALID")
    work=Path(config["work"]).resolve(strict=True)
    if Path.cwd().resolve()!=work or any(os.environ.get(k)!="1" for k in ("OPENBLAS_NUM_THREADS","OMP_NUM_THREADS","MKL_NUM_THREADS")):
        raise DataError("SDK_ENVIRONMENT_INVALID")
    budget=Budget(config["call"],deadline=config["deadline"])
    output=work/"output"
    result={"schema_version":"sdk-child-output-1","binding":config["binding"],"status":"FAILED","error_code":"SDK_EXECUTION_FAILED","files":[],"entities":[],"stats":{},"sdk_ref":None}
    try:
        # SDK imports, fixtures and execution are all AFTER parent admission.
        if config["fixture"] is not None and config["fixture"].get("case")=="block_import":
            (work/"phase-marker.json").write_text('{"phase":"import"}',encoding="utf-8")
            time.sleep(60)  # Trusted fixture only, parent must cancel/reap.
        sdk_calls=importlib.import_module(package+'.sdk_calls')
        yahoo,akshare=sdk_calls.yahoo,sdk_calls.akshare
        exchanges={}
        if config["fixture"] is not None:
            # Offline code lives in tests/, not the product provider package.
            reference=config['fixture_module']
            if type(reference)is not dict or set(reference)!={'path','sha256'}:raise DataError('SDK_FIXTURE_MODULE_REQUIRED')
            path=Path(reference['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=reference['sha256']:raise DataError('SDK_FIXTURE_MODULE_CHANGED')
            from importlib import util as import_util
            spec=import_util.spec_from_file_location('trusted_sdk_offline_fixture',path)
            module=import_util.module_from_spec(spec);spec.loader.exec_module(module)
            exchanges=module.fixture_exchanges(config["fixture"],budget)
        elif config['fixture_module'] is not None:raise DataError('SDK_FIXTURE_MODULE_FORBIDDEN')
        function=yahoo if config["call"]["provider"]=="yfinance" else akshare
        with contextlib.redirect_stdout(Suppressed()),contextlib.redirect_stderr(Suppressed()):
            if config["call"]["provider"] in {"akshare_cn","openbb_nasdaq","tushare"}:
                from finance_platform.data.goal_data.goal_sdk import collect_child
                sdk,snapshot,rows=collect_child(config["call"],budget,config["pins"],
                    fixture_exchange=exchanges.get("goal_fixture_exchange"),token=goal_token)
            elif config["call"]["provider"]=="openbb_cboe":
                if config['fixture'] is not None:raise DataError('OPENBB_FIXTURE_UNSUPPORTED')
                cboe=importlib.import_module(package+'.openbb_cboe_runtime')
                sdk,snapshot,rows=cboe.collect(config['call'],budget,config['pins'])
            elif config["call"]["provider"]=="yfinance":
                sdk,snapshot,rows=function(config["call"],budget,work/"cache",config["pins"],**exchanges)
            else:
                sdk,snapshot,rows=function(config["call"],budget,config["pins"],**exchanges)
        if config["fixture"] is not None and config["fixture"].get("case")=="block_parse":
            (work/"phase-marker.json").write_text('{"phase":"parse"}',encoding="utf-8")
            time.sleep(60)
        budget.check()
        result["sdk_ref"]=sdk
        result["files"].append(write_artifact(output,"sdk-frame.json",encoded(snapshot,artifact_limit("sdk-frame.json",config["call"])),call=config["call"]))
        result["files"].append(write_artifact(output,"observations.json",encoded(rows,artifact_limit("observations.json",config["call"])),call=config["call"]))
        result.update(status="SUCCEEDED",error_code=None)
    except DataError as error:
        result["error_code"]=budget.fatal or error.code
    except BaseException:
        result["error_code"]=budget.fatal or "SDK_EXECUTION_FAILED"
    finally:
        try:
            for i,entity in enumerate(budget.entities):
                entry=write_artifact(output,"entity-"+str(i)+".json",entity["body"])
                result["files"].append(entry)
                result["entities"].append({**{k:v for k,v in entity.items() if k!="body"},"artifact":entry["path"]})
            # Parent is authoritative after actual process exit/kernel-empty;
            # live yfinance SQLite managers may still hold Windows cache handles.
        except BaseException:
            result.update(status="FAILED",error_code=budget.fatal or "SDK_OUTPUT_OR_CLEANUP_FAILED")
        result["stats"]={"physical_attempts":budget.requests,"response_bytes":budget.bytes,"header_bytes":budget.header_bytes,
                         "attempts":budget.attempts,"late_or_partial_success_accepted":False}
    return encoded(result,LIMITS["manifest_bytes"])


try:
    sys.stdout.buffer.write(execute()+b"\n")
except BaseException:
    # Never publish SDK exception, traceback, URI, body, cookie or crumb text.
    sys.stdout.buffer.write(b'{"status":"FAILED","error_code":"SDK_CHILD_PROTOCOL_FAILED"}\n')
