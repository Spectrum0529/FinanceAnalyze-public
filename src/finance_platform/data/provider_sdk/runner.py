"""One owned child under the existing core job, with admission before SDK import."""
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from uuid import uuid4

from finance_platform.core.contracts import fingerprint
from finance_platform.core.contracts import identifier
from finance_platform.core.errors import PlatformError
from finance_platform.data.contracts import DataError
from finance_platform.operations.paths import contained, protect_private
from finance_platform.strategies.worker import _WindowsJob
from .contracts import LIMITS, checked_json
from .artifacts import cleanup_cache, read_artifacts
from .job_evidence import Evidence
from .network_gate import safe_transport_summary


def protect_directory(path):
    """Authoritative operations helper owns executable selection and ACL policy."""
    try:protect_private(path)
    except PlatformError:raise
    except (OSError,subprocess.SubprocessError) as error:
        raise DataError('SDK_PRIVATE_DIRECTORY_PROTECTION_FAILED') from error


class SDKRunner:
    def __init__(self, policy, *, python, pins, source_root, fixture=None, fixture_module=None, admission_delay=0, runtime_binding=None):
        self.policy, self.python, self.pins = policy, Path(python).resolve(), pins
        self.source_root = Path(source_root).resolve()
        self.site_root = self.python.parents[1]/"Lib/site-packages"
        self.fixture = fixture  # Trusted fixture constructor seam, not an API/source field.
        self.fixture_module=None
        if fixture is not None:
            if fixture_module is None:raise DataError('SDK_FIXTURE_MODULE_REQUIRED')
            fixture_path=Path(fixture_module).absolute()
            from .artifacts import regular_owned
            fixture_path=regular_owned(fixture_path.parent,fixture_path.name)
            self.fixture_module={'path':str(fixture_path),'sha256':hashlib.sha256(fixture_path.read_bytes()).hexdigest()}
        elif fixture_module is not None:raise DataError('SDK_FIXTURE_MODULE_FORBIDDEN')
        if type(admission_delay) not in {int,float} or not 0<=admission_delay<=1 or (admission_delay and fixture is None):
            raise DataError('SDK_ADMISSION_INVALID')
        self.admission_delay=admission_delay
        if os.name != "nt" or self.python.name.lower() != "python.exe" or not self.python.is_file() or not self.site_root.is_dir():
            raise DataError("SDK_RUNTIME_UNAVAILABLE")
        runtime=runtime_binding if runtime_binding is not None else json.loads(Path(__file__).with_name('runtime_binding.json').read_text(encoding='utf-8'))
        if type(runtime)is not dict or runtime.get('schema_version')!='sdk-direct-base-runtime-binding-1' or runtime.get('expected_version')!=[3,12,14]:raise DataError('SDK_RUNTIME_BINDING_CHANGED')
        self.runtime_binding=runtime
        # Bind the supplied venv to its exact recorded base, bypass its redirector.
        for field,path in [('venv_python',self.python),('venv_config',self.python.parents[1]/'pyvenv.cfg')]:
            if str(path.resolve()).casefold()!=runtime[field]['path'].casefold() or hashlib.sha256(path.read_bytes()).hexdigest()!=runtime[field]['sha256']:
                raise DataError('SDK_RUNTIME_BINDING_CHANGED')
        self.base_python=Path(runtime['base_python']['path'])
        if hashlib.sha256(self.base_python.read_bytes()).hexdigest()!=runtime['base_python']['sha256'] or str(self.site_root.resolve()).casefold()!=runtime['site_root'].casefold():
            raise DataError('SDK_RUNTIME_BINDING_CHANGED')
        if hashlib.sha256(Path(runtime['base_dll']['path']).read_bytes()).hexdigest()!=runtime['base_dll']['sha256']:
            raise DataError('SDK_RUNTIME_BINDING_CHANGED')

    def run(self, context, owner_id, call, *, whole_deadline, goal_token=None):
        secret_required=call.get('provider')=='tushare' and self.fixture is None
        if secret_required:
            if type(goal_token) is not str or not 1<=len(goal_token)<=512 or not all(33<=ord(c)<=126 for c in goal_token):
                raise DataError('GOAL_DATA_CREDENTIAL_REQUIRED')
        elif goal_token is not None:raise DataError('SDK_INPUT_INVALID')
        identifier(owner_id)
        context.require_active()
        if time.monotonic() >= whole_deadline:
            raise DataError("SDK_JOB_TIMEOUT")
        parent = self.policy.partitions["development"]/"platform"/owner_id/"sdk-private"
        # AccessPolicy checks the own development boundary before child writes.
        parent = contained(self.policy.partitions["development"], parent)
        self.policy.authorize(parent)
        parent.mkdir(parents=True, exist_ok=True)
        protect_directory(parent)
        work = parent/("unit-"+uuid4().hex)
        work.mkdir(exist_ok=False)
        protect_directory(work)
        for name in ("output","cache"):
            (work/name).mkdir(exist_ok=False)
        deadline = min(whole_deadline, time.monotonic()+LIMITS["seconds"])
        binding = fingerprint({"owner":owner_id,"run":context.run.run_id,"call":call,"pins":self.pins,"runtime_binding":self.runtime_binding,"fixture_module":self.fixture_module})
        packet = {"schema_version":"sdk-child-call-1","call":call,"work":str(work),"pins":self.pins,
                  "binding":binding,"deadline":deadline,"fixture":self.fixture,"fixture_module":self.fixture_module}
        child = Path(__file__).with_name("sdk_child.py")
        argv = [str(self.base_python),"-I","-S","-B","-X","utf8","-u",str(child),str(self.source_root),str(self.site_root),__package__]
        # No inherited provider/broker/proxy/CA/PYTHONPATH/user cache environment.
        env = {"SYSTEMROOT":os.environ.get("SYSTEMROOT","C:\\Windows"),"TEMP":str(work),"TMP":str(work),
               "OPENBLAS_NUM_THREADS":"1","OMP_NUM_THREADS":"1","MKL_NUM_THREADS":"1",
               "NUMEXPR_NUM_THREADS":"1","VECLIB_MAXIMUM_THREADS":"1"}
        if call.get('provider') in {'openbb_cboe','openbb_nasdaq','tushare','akshare_cn'}:
            # Explicit private home: official provider helpers never see personal settings/cache.
            env.update(HOME=str(work/'cache'),USERPROFILE=str(work/'cache'),OPENBB_HOME=str(work/'cache'))
        process, job = None,None
        evidence=None;admission=None;before_exec=None;final_proof=None
        overflow=threading.Event()
        buffers=[bytearray(),bytearray()]
        counts=[0,0]
        admitted=False
        outcome="FAILED"
        readers=[]
        writer=None
        writer_error=threading.Event()
        transport_diagnostics=[]

        def send_packet(encoded):
            try:
                process.stdin.write(b"GO\n"+encoded)
                process.stdin.flush()
            except (OSError, ValueError):
                writer_error.set()

        def drain(pipe, index):
            while True:
                chunk=pipe.read(4096)
                if not chunk:break
                counts[index]+=len(chunk)
                if counts[index]>LIMITS["stdout_bytes" if index==0 else "stderr_bytes"]:
                    overflow.set()
                    with suppress(Exception):process.kill()
                    break
                # Never persist SDK stderr or exception text. stdout must be exact safe manifest.
                if index==0:buffers[index].extend(chunk)
            pipe.close()

        try:
            context.require_active()
            if len(str(work).encode('utf-16-le'))//2>258:raise DataError('SDK_WORKDIR_PATH_TOO_LONG')
            process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                cwd=work,env=env,creationflags=subprocess.DETACHED_PROCESS)
            # Child is blocked on its initial GO read and -S prevents site/.pth startup.
            job=_WindowsJob(process,LIMITS["memory_bytes"])
            evidence=Evidence(job)
            if not evidence.contains(process):raise DataError('SDK_JOB_MEMBERSHIP_UNPROVEN')
            packet['admission_handle']=evidence.duplicate_into(process)
            encoded=json.dumps(packet,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode()+b'\n'
            if len(encoded)>65536:raise DataError('SDK_INPUT_LIMIT_EXCEEDED')
            context.require_active()
            writer=threading.Thread(target=send_packet,args=(encoded,),daemon=True)
            writer.start()
            admitted=True
            handshake=[]
            def read_admission():handshake.append(process.stdout.readline(4097))
            admission_reader=threading.Thread(target=read_admission,daemon=True);readers.append(admission_reader);admission_reader.start()
            while admission_reader.is_alive():
                context.require_active()
                if time.monotonic()>=deadline or writer_error.is_set():raise DataError('SDK_ADMISSION_TIMEOUT')
                time.sleep(.02)
            if not handshake or not 0<len(handshake[0])<=4096:raise DataError('SDK_ADMISSION_INVALID')
            admission=json.loads(handshake[0])
            if admission.get('schema_version')!='sdk-admission-1' or admission.get('binding')!=binding or admission.get('actual_pid')!=process.pid or admission.get('actual_self_in_held_job')is not True or admission.get('sdk_modules_loaded')is not False or Path(admission.get('actual_image','')).resolve()!=self.base_python.resolve():
                raise DataError('SDK_JOB_MEMBERSHIP_UNPROVEN')
            if admission.get('version_info')!=[3,12,14] or Path(admission.get('actual_python_dll','')).resolve()!=Path(self.runtime_binding['base_dll']['path']).resolve():
                raise DataError('SDK_RUNTIME_BINDING_CHANGED')
            if admission.get('duplicated_job_handle_closed')is not True:raise DataError('SDK_JOB_MEMBERSHIP_UNPROVEN')
            until=time.monotonic()+self.admission_delay
            while time.monotonic()<until:
                context.require_active()
                if process.poll()is not None or not evidence.contains(process):raise DataError('SDK_JOB_MEMBERSHIP_UNPROVEN')
                time.sleep(.02)
            before_exec=evidence.snapshot()
            if before_exec['pids']!=[process.pid] or before_exec['active_processes']!=1 or not evidence.contains(process):raise DataError('SDK_JOB_MEMBERSHIP_UNPROVEN')
            writer.join(timeout=2)
            if writer.is_alive() or writer_error.is_set():raise DataError('SDK_CHILD_INPUT_FAILED')
            context.require_active()
            begin = getattr(context,'begin_provider_request',None)
            attempt = begin() if begin is not None else None
            process.stdin.write(b'EXEC\n')
            if secret_required:process.stdin.write(goal_token.encode('ascii')+b'\n')
            process.stdin.close()
            for index,pipe in enumerate((process.stdout,process.stderr)):
                thread=threading.Thread(target=drain,args=(pipe,index),daemon=True);readers.append(thread);thread.start()
            while process.poll() is None:
                context.require_active()
                if overflow.is_set():raise DataError("SDK_OUTPUT_LIMIT_EXCEEDED")
                if writer_error.is_set():raise DataError("SDK_CHILD_INPUT_FAILED")
                if time.monotonic()>=deadline:raise DataError("SDK_TIMEOUT")
                time.sleep(0.05)
            for thread in readers:thread.join(timeout=2)
            writer.join(timeout=2)
            context.require_active()
            if process.returncode or overflow.is_set() or writer_error.is_set() or writer.is_alive() or any(t.is_alive() for t in readers):
                raise DataError("SDK_CHILD_FAILED")
            # Parsing is bounded, only after process exit/reap; no late child commit.
            result=checked_json(bytes(buffers[0]),limit=LIMITS["manifest_bytes"])
            artifacts=read_artifacts(self.policy,work,result,binding,call=call)
            # Parent-side allowlist only; never persist child exception/header/
            # body/URL/query/cookie/crumb text, including on a failed SDK unit.
            stats=result.get("stats")
            transport_diagnostics=safe_transport_summary(stats.get("attempts") if type(stats)is dict else None,call)
            if attempt is not None:
                if result['status']=='SUCCEEDED':
                    context.finish_provider_request(attempt,'RESPONSE_RECEIVED')
                elif type(stats) is dict:
                    attempts=stats.get('physical_attempts')
                    if attempts==0 or (attempts==len(transport_diagnostics) and all(
                            row['stage']=='DNS' for row in transport_diagnostics)):
                        context.finish_provider_request(attempt,'NOT_SENT')
                    elif attempts==len(transport_diagnostics) and all(row['stage'] in {'COMPLETE','DNS'}
                                                                     for row in transport_diagnostics):
                        context.finish_provider_request(attempt,'RESPONSE_RECEIVED')
            if result["status"]!="SUCCEEDED":
                code=result.get("error_code")
                # Whitelisted prefix plus strict syntax, not arbitrary SDK text.
                if type(code)is not str or len(code)>80 or not code.replace("_","").isalnum() or not code.isupper():
                    code="SDK_EXECUTION_FAILED"
                raise DataError(code)
            if not {"sdk-frame.json","observations.json"}<=set(artifacts):
                raise DataError("SDK_OUTPUT_INVALID")
            context.require_active()
            final_proof=evidence.final_empty()
            try:cleanup_cache(work)
            except Exception:raise DataError('SDK_CACHE_CLEANUP_FAILED') from None
            outcome="SUCCEEDED"
            return result,artifacts,{"work_ref":work.name,"binding":binding,"admitted_after_job_assignment":admitted,
                "child_reaped":True,"stdout_bytes":counts[0],"stderr_suppressed_bytes":counts[1],
                "actual_sdk_pid":admission['actual_pid'],"kernel_membership_proven":True,"kernel_final_empty":final_proof['kernel_final_empty'],
                "network_class":"OFFLINE_SYNTHETIC" if self.fixture is not None else "BOUNDED_PUBLIC_SDK"}
        finally:
            primary=getattr(sys.exc_info()[1],'code',None)
            if type(primary)is not str or len(primary)>80 or not primary.replace('_','').isalnum() or not primary.isupper():primary=None
            containment_error=False
            if evidence is not None:
                try:final_proof=evidence.final_empty(terminate=process.poll()is None)
                except Exception:containment_error=True
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                with suppress(Exception):process.wait(timeout=5)
            if job is not None:job.close()
            if writer is not None:writer.join(timeout=2)
            for thread in readers:thread.join(timeout=2)
            cleanup_error=False
            if primary=='SDK_CACHE_CLEANUP_FAILED':cleanup_error=True
            else:
                try:cleanup_cache(work)
                except Exception:cleanup_error=True
            # Own private failure metadata only. No URI/body/cookie/stdout/stderr copied.
            audit={"outcome":outcome,"binding":binding,"admission_sent":admitted,
                   "process_reaped":process is not None and process.poll() is not None,
                   "process_exit_code":None if process is None else process.returncode,
                   "stdout_bytes":counts[0],"stderr_suppressed_bytes":counts[1],"cache_removed":not(work/"cache").exists(),"cache_cleanup_failed":cleanup_error}
            audit.update(actual_admission=admission,kernel_before_exec=before_exec,kernel_final=final_proof,containment_failure=containment_error,launched_base_python=str(self.base_python),configured_venv_python=str(self.python))
            audit['primary_error_code']=primary
            audit['safe_transport_diagnostics']=transport_diagnostics
            with (work/"parent-outcome.json").open("x",encoding="utf-8") as stream:
                json.dump(audit,stream,indent=2)
            if containment_error:raise DataError('SDK_JOB_FINAL_EMPTY_UNPROVEN')
