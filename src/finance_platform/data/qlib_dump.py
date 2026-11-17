"""Owned r04 fixed dump envelope. Server-owned deployment and original Job/AccessPolicy controls."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import sys
import threading
import time

HERE=Path(__file__).resolve().parent
from uuid import uuid4
from finance_platform.core.contracts import canonical
from finance_platform.core.errors import PlatformError
from .contracts import DataError
from .qlib_dump_deployment import QlibDumpDeployment
from .qlib_dump_deadline import Deadline
def pin(path):
    raw=path.read_bytes();return {'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
def write(path,obj):
    with path.open('x',encoding='utf-8') as out:json.dump(obj,out,indent=2)
def check_inputs(binding,*,bridge,context):
    # Existing catalog commits this current consumer audit before all19 reads.
    bridge._active(context)
    if bridge.policy.root!=Path(binding['runtime_root']):raise DataError('QLIB_DUMP_ROOT_CHANGED')
    reader=bridge.catalog.reader(bridge.policy)
    if len(bridge.catalog.document['source_files'])!=19:raise DataError('QLIB_FIXTURE_CATALOG_INVALID')
    for entry in bridge.catalog.document['source_files']:
        bridge._active(context);reader(entry['relative'])
    return {'fixture_bytes':'VERIFIED_THROUGH_EXISTING_CATALOG_ACCESS_POLICY','verified_files':19,
        'audit_path':str(bridge.policy.audit),'job_id':context.job_id,'owner_id':context.run.owner_id,
        'run_id':context.run.run_id,'fencing_token':context.fencing_token}

def environment(run):
    for directory in ['home','tmp','home/AppData/Roaming','home/AppData/Local','home/mpl']:(run/directory).mkdir(parents=True,exist_ok=True)
    return {'SystemRoot':'C:\\Windows','WINDIR':'C:\\Windows','SystemDrive':'C:',
        'COMSPEC':'C:\\Windows\\System32\\cmd.exe','PATH':'C:\\Windows\\System32',
        'HOME':str(run/'home'),'USERPROFILE':str(run/'home'),'APPDATA':str(run/'home/AppData/Roaming'),
        'LOCALAPPDATA':str(run/'home/AppData/Local'),'TEMP':str(run/'tmp'),'TMP':str(run/'tmp'),
        'MPLCONFIGDIR':str(run/'home/mpl'),'MLFLOW_TRACKING_URI':(run/'home/mlruns').as_uri(),
        'MLFLOW_DISABLE_TELEMETRY':'true','GIT_PYTHON_REFRESH':'quiet',
        'OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','OMP_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1','PYTHONDONTWRITEBYTECODE':'1'}
def launch_process(command,run,owned,record,binding,runtime_root,attempt_id):
    from . import qlib_dump_domain as launch
    if pin(Path(launch.__file__))['sha256']!=binding['launch_contract']['sha256']:raise RuntimeError('LAUNCH_CONTRACT_CHANGED')
    cwd=launch.select_launch_cwd(runtime_root,run,owned,attempt_id)
    record['launch_cwd']={'path':str(cwd),'utf16_characters':len(str(cwd).encode('utf-16-le'))//2,'existing_real_directory':True}
    env=environment(run);env[launch.ATTEMPT_ENV]=str(run);env[launch.ROOT_ENV]=str(runtime_root)
    env['FA014_DUMP_BINDING_SHA256']=record['binding_sha256']
    process=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.DETACHED_PROCESS)
    record['launch_stage']='POPEN_RETURNED'
    return process

def run_owned(context,bridge,export_id,*,deployment):
    deployment.verify();binding=deployment.document();REPO=deployment.root
    binding_raw=canonical(binding).encode();bound=hashlib.sha256(binding_raw).hexdigest()
    context.require_active();guarded_before=check_inputs(binding,bridge=bridge,context=context)
    owned=bridge.owned_input(context,export_id)
    if owned['owner_id']!=context.run.owner_id or owned['run_id']!=context.run.run_id:raise RuntimeError('OWNER_RUN_EQUALITY_REQUIRED')
    attempt_id='qlib-native-'+uuid4().hex
    attempt=bridge.begin_attempt(context,export_id,attempt_id,bound);published_directory=Path(attempt['directory'])
    from . import qlib_dump_domain as launch
    run=launch.work_directory(REPO,owned,attempt_id)
    for node in (run,*run.parents):
        if node.exists() and (node.is_symlink() or getattr(node.lstat(),'st_file_attributes',0)&0x400):raise RuntimeError('WORK_ALIAS_DENIED')
        if node==REPO:break
    if bridge.policy.authorize(run)!=run:raise RuntimeError('WORK_ALIAS_DENIED')
    run.mkdir(parents=True,exist_ok=False)
    def read_owned(path):return bridge.policy.read_bytes(bridge._regular(path))
    def write_owned(path,obj):return bridge._write(path.parent,path.name,json.dumps(obj,indent=2).encode())
    def pin_owned(path):
        raw=read_owned(path);return {'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    bridge._write(run,'dump_binding.json',binding_raw)
    # Each new attempt owns one exclusive marker; failed attempts never retry.
    write_owned(run/'execution_started.json',{'binding_sha256':bound,'attempt_id':attempt_id,'review_sha256':deployment.execution_review.sha256})
    request={'attempt_id':attempt_id,'binding_sha256':bound,'owner_id':owned['owner_id'],'run_id':owned['run_id'],'owned_input':owned}
    if len(json.dumps(request).encode())>524288:raise RuntimeError('REQUEST_BUDGET')
    write_owned(run/'request.json',request);request_hash=pin_owned(run/'request.json')['sha256']
    profile=REPO/binding['profile_source']['path']
    from finance_platform.core import qlib_process as module
    if Path(module.__file__).resolve()!=profile.resolve():raise RuntimeError('QLIB_PROCESS_SOURCE_MISMATCH')
    command=[binding['base_python']['path'],'-I','-S','-B','-X','utf8',str(HERE/'qlib_dump_child.py')]
    process=job=deadline=None;threads=[];errors=[];counts={};snapshots=[];first=queue.Queue(maxsize=1);started=time.monotonic()
    record={'binding_sha256':bound,'owner_manifest_sha256':owned['owner_manifest_sha256'],'attempt_id':attempt_id,
        'guarded_fixture_before':guarded_before}
    def drain(stream,name):
        count=0
        try:
            with bridge.policy.authorize(run/(name+'.log')).open('xb') as out:
                if name=='stdout':
                    line=stream.readline(65537);count+=len(line);out.write(line)
                    if count>65536 or not line.endswith(b'\n'):errors.append('READY_LINE_BUDGET')
                    first.put_nowait(line)
                while chunk:=stream.read(4096):
                    count+=len(chunk)
                    if count>65536:errors.append('PIPE_BUDGET');return
                    out.write(chunk)
        except BaseException:errors.append('PIPE_DRAIN_FAILURE')
        finally:counts[name]=count;stream.close()
    def snapshot():
        proof=job.snapshot()
        if proof['total_processes_started']>3:raise RuntimeError('TOTAL_PROCESS_BUDGET')
        if not snapshots or proof!=snapshots[-1]:snapshots.append(proof)
        return proof
    def inspect_files():
        native=run/'native';files=[]
        for path in [native,*(native.rglob('*') if native.exists() else [])]:
            if not path.exists():continue
            info=path.lstat()
            if path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:raise RuntimeError('OUTPUT_ALIAS')
            if path.is_file():
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise RuntimeError('OUTPUT_ALIAS')
                files.append(path)
        if len(files)>65 or sum(p.stat().st_size for p in files)>8388608:raise RuntimeError('OUTPUT_BUDGET')
        audits=list(run.glob('audit-*.jsonl'));identities=list(run.glob('identity-*.json'));phases=list(run.glob('phase-*.jsonl'));proofs=list(run.glob('probe-refusal-*.jsonl'))
        if len(audits)>3 or len(identities)>3 or len(phases)>3 or len(proofs)>3 or any(p.stat().st_size>8192 for p in phases+proofs) or sum(p.stat().st_size for p in audits+identities+phases+proofs)>131072:raise RuntimeError('AUDIT_BUDGET')
        if any('"DENIED"' in read_owned(p).decode('utf-8') for p in audits):raise RuntimeError('DENIED_ATTEMPT')
        return files
    try:
        process=launch_process(command,run,owned,record,binding,REPO,attempt_id)
        job=module.QlibOwnedJob(process,module.QlibProcessProfile.OFFICIAL_DUMP)
        record['admission']=job.verify_before_admission();context.require_active()
        deadline=Deadline(process,job,started);deadline.start()
        for stream,name in [(process.stdout,'stdout'),(process.stderr,'stderr')]:
            thread=threading.Thread(target=drain,args=(stream,name),daemon=True);thread.start();threads.append(thread)
        go={'phase':'QUERY','job_handle':job.duplicate_into(process),'expected_pid':process.pid,'request_sha256':request_hash}
        process.stdin.write(json.dumps(go).encode()+b'\n');process.stdin.flush()
        while first.empty():
            context.require_active();snapshot()
            if time.monotonic()-started>15 or process.poll() is not None or errors:raise RuntimeError('READY_NOT_PROVEN')
            time.sleep(.02)
        ready=json.loads(first.get_nowait());record['ready']=ready
        if (ready.get('phase')!='READY' or ready.get('actual_pid')!=process.pid
            or ready.get('request_sha256')!=request_hash or ready.get('actual_self_in_held_job') is not True
            or ready.get('duplicated_job_handle_closed') is not True or ready.get('qlib_imported') is not False
            or Path(ready.get('actual_image','')).resolve()!=Path(binding['base_python']['path']).resolve()
            or Path(ready.get('actual_python_dll','')).resolve()!=Path(binding['python_dll']['path']).resolve()
            or ready.get('image_sha256')!=binding['base_python']['sha256'] or ready.get('dll_sha256')!=binding['python_dll']['sha256']
            or Path(ready.get('site_root','')).resolve()!=Path(binding['site_root']).resolve()
            or Path(ready.get('prefix','')).resolve()!=Path(binding['venv_prefix']).resolve()):raise RuntimeError('READY_IDENTITY_MISMATCH')
        job.verify_before_admission();context.require_active()
        process.stdin.write(b'EXEC\n');process.stdin.flush();process.stdin.close()
        while process.poll() is None:
            context.require_active();snapshot();inspect_files()
            if time.monotonic()-started>120:raise TimeoutError('NATIVE_WALL_BUDGET')
            if errors:raise RuntimeError(errors[0])
            time.sleep(.02)
        process.wait(timeout=5)
        if time.monotonic()-started>120:raise TimeoutError('NATIVE_WALL_BUDGET')
        for thread in threads:thread.join(timeout=2)
        if any(t.is_alive() for t in threads) or errors:raise RuntimeError('PIPE_JOIN_OR_BUDGET_FAILED')
        record['final_empty']=job.require_empty(deadline_seconds=5)
        if record['final_empty']['total_processes_started']!=3:raise RuntimeError('EXACT_TOTAL_PROCESS_WITNESS_REQUIRED')
        context.require_active();record['guarded_fixture_after']=check_inputs(binding,bridge=bridge,context=context)
        if bridge.owned_input(context,export_id)!=owned:raise RuntimeError('OWNED_INPUT_CHANGED')
        files=inspect_files()
        expected={'calendars/day.txt','instruments/all.txt'}|{f'features/{entry["symbol"].lower()}/{field}.day.bin' for entry in owned['CSV_files'] for field in ['open','close','high','low','volume','vwap','factor']}
        if {p.relative_to(run/'native').as_posix() for p in files}!=expected:raise RuntimeError('OUTPUT_SET_INCOMPLETE')
        identities=[json.loads(read_owned(p)) for p in run.glob('identity-*.json')]
        observed={pid for proof in snapshots for pid in proof['pids']}
        if len(identities)!=3 or len({v['pid'] for v in identities})!=3 or max(s['active_processes'] for s in snapshots)!=2:raise RuntimeError('POOL_WITNESS_INCOMPLETE')
        if ([v['pid'] for v in identities if v['role']=='root']!=[process.pid]
            or sum(v['role']=='official_pool_helper' for v in identities)!=2):raise RuntimeError('POOL_ROLES_MISMATCH')
        for identity in identities:
            if (identity['pid'] not in observed or Path(identity['image']).resolve()!=Path(binding['base_python']['path']).resolve()
                or Path(identity['dll']).resolve()!=Path(binding['python_dll']['path']).resolve()
                or identity['image_sha256']!=binding['base_python']['sha256'] or identity['dll_sha256']!=binding['python_dll']['sha256']
                or Path(identity['site_root']).resolve()!=Path(binding['site_root']).resolve()):raise RuntimeError('POOL_IDENTITY_MISMATCH')
        proofs={}
        for path in run.glob('probe-refusal-*.jsonl'):
            rows=[json.loads(line) for line in read_owned(path).decode('utf-8').splitlines()]
            if len(rows)!=2:raise RuntimeError('OPTIONAL_PROBE_PROOF_REQUIRED')
            pid=rows[0].get('pid');role='root' if pid==process.pid else 'official_pool_helper'
            if type(pid) is not int or path.name!=f'probe-refusal-{pid}.jsonl' or pid in proofs:raise RuntimeError('OPTIONAL_PROBE_PID_INVALID')
            if any(row.get('pid')!=pid or row.get('role')!=role for row in rows):raise RuntimeError('OPTIONAL_PROBE_ROLE_INVALID')
            if (rows[0].get('event')!='OPTIONAL_IPV6_CONSTRUCTOR_REFUSED'
                or [rows[0].get(k) for k in ['attempt','family','type','proto']]!=[1,23,1,0]
                or rows[0].get('source_sha256')!=binding['optional_probe_sources']['connection']['sha256']
                or rows[0].get('OS_constructor_permission') is not False
                or rows[1]!={'pid':pid,'role':role,'event':'ORIGINAL_OPTIONAL_PROBE_RETURN','returned_false':True,'return_count':1}):
                raise RuntimeError('OPTIONAL_PROBE_NATIVE_FALLBACK_UNPROVEN')
            proofs[pid]=rows
        if set(proofs)!={identity['pid'] for identity in identities} or len(proofs)!=3:raise RuntimeError('OPTIONAL_PROBE_PROCESS_WITNESS_INCOMPLETE')
        record['optional_probe_proofs']=[{'pid':pid,'records':rows} for pid,rows in sorted(proofs.items())]
        verification_path=run/'native_verification.json'
        verification=json.loads(read_owned(verification_path))
        if (verification['status']!='PASS_OFFICIAL_LOCAL_PROVIDER_READ' or verification['feature_reads']!=63
            or verification['original_spawn_requests']!=2 or verification['input_owner_manifest_sha256']!=owned['owner_manifest_sha256']):raise RuntimeError('LOCAL_PROVIDER_VERIFICATION_REQUIRED')
        if process.returncode:raise RuntimeError('CHILD_FAILED')
        record.update(status='VERIFIED_OFFICIAL_DUMP_UNPUBLISHED',identities=identities,
            provider_verification_sha256=pin_owned(verification_path)['sha256'],
            outputs=[{'relative':p.relative_to(run/'native').as_posix(),'bytes':p.stat().st_size,'sha256':pin_owned(p)['sha256']} for p in sorted(files)])
    except BaseException as error:
        record.update(status='FAILED_OR_INTERRUPTED_PRESERVED',error_type=type(error).__name__,platform_error_code=error.code if isinstance(error,PlatformError) else None,returncode_before_termination=process.poll() if process else None,native_returncode=process.poll() if process else None)
        if job:job.terminate()
        if process:
            if process.poll() is None and not job:process.kill()
            process.wait(timeout=10)
        for thread in threads:thread.join(timeout=2)
        if job:record['final_empty']=job.require_empty(deadline_seconds=5)
    finally:
        joined=deadline.stop() if deadline else True
        if deadline:
            errors.extend(deadline.errors)
            record['independent_deadline']={'fired':deadline.fired,'joined':joined,'errors':deadline.errors}
        if job and joined:job.close()
        elif job:errors.append('OWNED_DEADLINE_JOIN_FAILED')
        if process:
            # Never abandon unstarted drains after an early admission failure.
            # Already-started drains own their streams until joined; no blocking
            # close against a live reader is claimed as a hard OS deadline.
            for stream in [process.stdin,*([process.stdout,process.stderr] if all(not t.is_alive() for t in threads) else [])]:
                if stream is not None and not stream.closed:
                    try:stream.close()
                    except OSError:errors.append('PIPE_CLOSE_FAILED')
        record.update(native_returncode=process.returncode if process else None,elapsed_seconds=time.monotonic()-started,command=command,snapshots=snapshots,
            child_handle_acquired=process is not None,job_handle_acquired=job is not None,
            root_reaped=process is not None and process.poll() is not None,drains_joined=all(not t.is_alive() for t in threads),pipe_bytes=counts,
            pipes_closed=process is not None and all(s is None or s.closed for s in [process.stdin,process.stdout,process.stderr]),control_errors=errors,
            provider_uri=None,published=False,scope='Fixed synthetic V2 official dump and local provider reader only; no workflow/model/API/auth acceptance.')
        if record['status']=='VERIFIED_OFFICIAL_DUMP_UNPUBLISHED' and (errors or not record['root_reaped'] or not record['drains_joined'] or not record['pipes_closed'] or deadline is not None and deadline.fired):
            record['status']='FAILED_OR_INTERRUPTED_PRESERVED'
        write_owned(run/'controller_result.json',record)
    if record['status']=='VERIFIED_OFFICIAL_DUMP_UNPUBLISHED':
        context.require_active()
        copies=[(run/'native_verification.json',published_directory/'native_verification.json'),(run/'controller_result.json',published_directory/'controller_result.json')]
        copies += [(run/'native'/item['relative'],published_directory/'native'/item['relative']) for item in record['outputs']]
        if len(copies)!=67:raise RuntimeError('PUBLICATION_COUNT_REQUIRED')
        for source,target in copies:
            context.require_active();raw=read_owned(source)
            if len(raw)>8388608:raise RuntimeError('PUBLICATION_BYTES_BOUND')
            bridge.policy.authorize(target.parent).mkdir(parents=True,exist_ok=True)
            bridge._write(target.parent,target.name,raw)
    return record

class QlibOfficialDumpRunner:
    def __init__(self,runtime,deployment=None):
        if deployment is not None and (type(deployment) is not QlibDumpDeployment or deployment.root!=runtime.root.resolve()):
            raise DataError('QLIB_DUMP_COMPOSITION_INVALID')
        self.runtime,self.deployment=runtime,deployment
    def require_configured(self):
        if self.deployment is None:raise DataError('QLIB_DUMP_EXECUTION_UNCONFIGURED')
        self.deployment.verify()
    def run(self,context,bridge,export_id):
        self.require_configured()
        if context.service.database is not self.runtime.database or bridge.db is not self.runtime.database:raise DataError('QLIB_DUMP_CONTEXT_INVALID')
        return run_owned(context,bridge,export_id,deployment=self.deployment)
