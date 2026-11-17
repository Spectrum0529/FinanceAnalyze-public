"""Bridge-private held Windows helper: cap2/2GiB/0x2208, never breakaway.

CreateProcessW/explicit HANDLE_LIST/retained Job and thread admission follow
FA012's actual r17 EOF runner; layouts are the unchanged FA010 frozen r04 ones.
Does not alter strategy, TA or Qlib profiles. Native execution belongs to FA010.
"""
import ctypes as C
from ctypes import wintypes as W
import msvcrt
import hashlib
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from .autonomous_job_structs import Limits,Members,Accounting,SI,PI
from .errors import PlatformError

CAP=2
MEMORY=2147483648
FLAGS=0x2208
PACKAGED_WSL=r'C:\Program Files\WSL\wsl.exe'
PACKAGED_WSL_SHA256='9708903a1e0646d68a2007d62384030e831558b475484e83dff3f32e6e99623a'
PACKAGED_WSL_BYTES=4291936
WSLHOST=r'C:\Program Files\WSL\wslhost.exe'
WSLHOST_SHA256='ad41985218e1a872ad676e06ea00a7efdeada65fb1d22996482927a2213cfb84'
WSLHOST_BYTES=3492664
KEY=0xFA013
EVENTS={3:'ACTIVE_PROCESS_LIMIT',4:'ACTIVE_PROCESS_ZERO',6:'NEW_PROCESS',7:'EXIT_PROCESS',
        8:'ABNORMAL_EXIT_PROCESS',9:'PROCESS_MEMORY_LIMIT',10:'JOB_MEMORY_LIMIT',11:'NOTIFICATION_LIMIT'}
ROOT=Path(__file__).resolve().parents[3]

def fixed_argv(root, paths_sha256, *, qa_run=None):
    from .autonomous_paths import read_paths
    from .autonomous_qa_scope_contract import HOST_VENV, linux_qa_base, valid_run
    try:
        linux_root = read_paths(Path(root), paths_sha256)
    except (OSError, ValueError):
        raise PlatformError('EXECUTOR_PATHS_UNAVAILABLE', 503) from None
    home = linux_root + '/runtime/home'
    python = linux_root + '/.venv/bin/python'
    if qa_run is not None:
        require(valid_run(qa_run), 'QA_SCOPE_ARGUMENT_INVALID')
        linux_root = linux_qa_base(linux_root) + '/' + qa_run
        home = linux_root + '/daemon-home'
        python = linux_root + '/' + HOST_VENV + '/bin/python'
    return [PACKAGED_WSL,'--distribution','Ubuntu-24.04','--cd',linux_root,'--exec',
        '/usr/bin/env','-i','HOME='+home,'LANG=C.UTF-8',
        'XDG_RUNTIME_DIR=/run/user/1000','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
        'PATH=/usr/bin:/bin','LITELLM_LOCAL_MODEL_COST_MAP=True','PYTHON_DOTENV_DISABLED=1',
        'LANGSMITH_TRACING=false','LANGCHAIN_TRACING_V2=false','OTEL_SDK_DISABLED=true',
        'OMP_NUM_THREADS=1','OPENBLAS_NUM_THREADS=1',python,'-I','-B','-m','fa_research_executor.entrypoint']


def require(ok,code):
    if not ok:raise PlatformError(code,409)


def held_target(deployment, cwd):
    from .autonomous_qa_deployment import QABridgeDeployment
    if type(deployment) is QABridgeDeployment:
        require(Path(cwd) == deployment.root, 'BRIDGE_FIXED_TARGET_REQUIRED')
        scope = deployment.verified_scope()
        argv = fixed_argv(deployment.root, deployment.paths_sha256, qa_run=scope.run_id)
        return [*argv, scope.run_id, scope.sha256], deployment.root
    require(Path(cwd).resolve()==ROOT.resolve() and Path(deployment.root).resolve()==ROOT.resolve(),
            'BRIDGE_FIXED_TARGET_REQUIRED')
    argv=deployment.argv()
    require(argv==fixed_argv(ROOT, deployment.paths_sha256),'BRIDGE_FIXED_ARGV_REQUIRED')
    return argv, ROOT


def verify_packaged_wsl():
    # The one recorded current-Job identity, exact Microsoft-signed deployment.
    # No basename/directory wildcard, path discovery or executable invocation.
    try:
        path=Path(PACKAGED_WSL)
        require(path.is_file() and not path.is_symlink() and not path.is_junction()
                and path.stat().st_size==PACKAGED_WSL_BYTES,'BRIDGE_PACKAGED_WSL_CHANGED')
        require(hashlib.sha256(path.read_bytes()).hexdigest()==PACKAGED_WSL_SHA256,'BRIDGE_PACKAGED_WSL_CHANGED')
    except OSError:
        raise PlatformError('BRIDGE_PACKAGED_WSL_CHANGED',409) from None
    return PACKAGED_WSL_SHA256


def verify_wslhost():
    # Exact observed current-Job image; current static deployment pin only.
    try:
        path=Path(WSLHOST)
        require(path.is_file() and not path.is_symlink() and not path.is_junction()
                and path.stat().st_size==WSLHOST_BYTES,'BRIDGE_WSLHOST_CHANGED')
        require(hashlib.sha256(path.read_bytes()).hexdigest()==WSLHOST_SHA256,'BRIDGE_WSLHOST_CHANGED')
    except OSError:
        raise PlatformError('BRIDGE_WSLHOST_CHANGED',409) from None
    return WSLHOST_SHA256


class SA(C.Structure):
    _fields_=[('length',W.DWORD),('descriptor',C.c_void_p),('inherit',W.BOOL)]
class SX(C.Structure):
    _fields_=[('si',SI),('attributes',C.c_void_p)]
class Port(C.Structure):
    _fields_=[('key',C.c_void_p),('port',W.HANDLE)]


def kernel():
    k=C.WinDLL('kernel32',use_last_error=True)
    signatures={
      'CreateJobObjectW':([C.c_void_p,W.LPCWSTR],W.HANDLE),
      'SetInformationJobObject':([W.HANDLE,C.c_int,C.c_void_p,W.DWORD],W.BOOL),
      'QueryInformationJobObject':([W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.c_void_p],W.BOOL),
      'CreateProcessW':([W.LPCWSTR,W.LPWSTR,C.c_void_p,C.c_void_p,W.BOOL,W.DWORD,C.c_void_p,W.LPCWSTR,C.POINTER(SX),C.POINTER(PI)],W.BOOL),
      'AssignProcessToJobObject':([W.HANDLE,W.HANDLE],W.BOOL),'ResumeThread':([W.HANDLE],W.DWORD),
      'WaitForSingleObject':([W.HANDLE,W.DWORD],W.DWORD),'GetExitCodeProcess':([W.HANDLE,C.POINTER(W.DWORD)],W.BOOL),
      'TerminateJobObject':([W.HANDLE,W.UINT],W.BOOL),'TerminateProcess':([W.HANDLE,W.UINT],W.BOOL),
      'CloseHandle':([W.HANDLE],W.BOOL),
      'CreatePipe':([C.POINTER(W.HANDLE),C.POINTER(W.HANDLE),C.POINTER(SA),W.DWORD],W.BOOL),
      'SetHandleInformation':([W.HANDLE,W.DWORD,W.DWORD],W.BOOL),
      'InitializeProcThreadAttributeList':([C.c_void_p,W.DWORD,W.DWORD,C.POINTER(C.c_size_t)],W.BOOL),
      'UpdateProcThreadAttribute':([C.c_void_p,W.DWORD,C.c_size_t,C.c_void_p,C.c_size_t,C.c_void_p,C.c_void_p],W.BOOL),
      'DeleteProcThreadAttributeList':([C.c_void_p],None),
      'CreateIoCompletionPort':([W.HANDLE,W.HANDLE,C.c_size_t,W.DWORD],W.HANDLE),
      'GetQueuedCompletionStatus':([W.HANDLE,C.POINTER(W.DWORD),C.POINTER(C.c_size_t),C.POINTER(C.c_void_p),W.DWORD],W.BOOL),
      'OpenProcess':([W.DWORD,W.BOOL,W.DWORD],W.HANDLE),'IsProcessInJob':([W.HANDLE,W.HANDLE,C.POINTER(W.BOOL)],W.BOOL),
      'GetProcessTimes':([W.HANDLE]+[C.POINTER(W.FILETIME)]*4,W.BOOL),
      'QueryFullProcessImageNameW':([W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)],W.BOOL)}
    for name,(args,result) in signatures.items():
        fn=getattr(k,name);fn.argtypes=args;fn.restype=result
    return k


class NativeProcess:
    def __init__(self,owner):self.owner=owner;self.pid=int(owner.pi.pid);self.returncode=None
    def poll(self):
        state=self.owner.k.WaitForSingleObject(self.owner.pi.process,0)
        require(state in (0,258),'BRIDGE_ROOT_WAIT_FAILED')
        if state==258:return None
        code=W.DWORD();require(self.owner.k.GetExitCodeProcess(self.owner.pi.process,C.byref(code)),'BRIDGE_ROOT_EXIT_FAILED')
        self.returncode=int(code.value);return self.returncode
    def wait(self,timeout):
        until=time.monotonic()+timeout
        while self.poll()is None:
            if time.monotonic()>=until:raise subprocess.TimeoutExpired('fixed-wsl-helper',timeout)
            time.sleep(min(.01,max(0,until-time.monotonic())))
        return self.returncode


class HeldWindowsHelper:
    def __init__(self,deployment,cwd,*,_eof_before_resume=False):
        require(os.name=='nt','BRIDGE_FIXED_TARGET_REQUIRED')
        argv, launch_root = held_target(deployment, cwd)
        self.packaged_image_verified=verify_packaged_wsl()==PACKAGED_WSL_SHA256
        self.wslhost_image_verified=verify_wslhost()==WSLHOST_SHA256
        self.k=kernel();self.handles=set();self.files=[];self.pi=PI();self.job=None;self.port=None
        self.admitted=False;self.resumed=False;self.closed=False;self.forced=False;self.process=None
        self.events=[];self.identities=[];self.identity_races=[];self.seen=set();self.started=time.monotonic();self._cleanup_deadline=None;self._identity_wait_deadline=None
        self._verified_members={}
        self.lock=threading.RLock();self.admission={};self.close_receipt=None
        attrs=None;attrs_ready=False
        try:
            self.job=self.k.CreateJobObjectW(None,None);require(self.job,'BRIDGE_JOB_CREATE_FAILED');self.handles.add(self.job)
            lim=Limits();lim.basic.flags=FLAGS;lim.basic.active=CAP;lim.job_memory=MEMORY
            require(self.k.SetInformationJobObject(self.job,9,C.byref(lim),C.sizeof(lim)),'BRIDGE_JOB_LIMIT_SET_FAILED')
            self.port=self.k.CreateIoCompletionPort(W.HANDLE(-1),None,0,1);require(self.port,'BRIDGE_PORT_CREATE_FAILED');self.handles.add(self.port)
            assoc=Port(KEY,self.port)
            require(self.k.SetInformationJobObject(self.job,7,C.byref(assoc),C.sizeof(assoc)),'BRIDGE_PORT_ASSOCIATE_FAILED')
            child=[];parent=[];sa=SA(C.sizeof(SA),None,True)
            for index in range(3):
                r=W.HANDLE();w=W.HANDLE();require(self.k.CreatePipe(C.byref(r),C.byref(w),C.byref(sa),0),'BRIDGE_PIPE_CREATE_FAILED')
                self.handles.update((r.value,w.value));own,other=(w.value,r.value) if index==0 else (r.value,w.value)
                require(self.k.SetHandleInformation(own,1,0),'BRIDGE_PARENT_PIPE_INHERITANCE')
                parent.append(own);child.append(other)
            size=C.c_size_t();self.k.InitializeProcThreadAttributeList(None,1,0,C.byref(size));require(size.value>0,'BRIDGE_ATTRIBUTE_SIZE_FAILED')
            attrs=C.create_string_buffer(size.value)
            require(self.k.InitializeProcThreadAttributeList(attrs,1,0,C.byref(size)),'BRIDGE_ATTRIBUTE_INIT_FAILED');attrs_ready=True
            allowed=(W.HANDLE*3)(*child)
            require(self.k.UpdateProcThreadAttribute(attrs,0,0x20002,C.byref(allowed),C.sizeof(allowed),None,None),'BRIDGE_HANDLE_ALLOWLIST_FAILED')
            sx=SX();sx.si.cb=C.sizeof(SX);sx.si.flags=0x100;sx.si.stdin,sx.si.stdout,sx.si.stderr=child;sx.attributes=C.cast(attrs,C.c_void_p)
            env=C.create_unicode_buffer('SYSTEMROOT=C:\\Windows\0\0');command=C.create_unicode_buffer(subprocess.list2cmdline(argv))
            require(self.k.CreateProcessW(argv[0],command,None,None,True,0x4|0x400|0x80000|0x08000000,env,str(launch_root),C.byref(sx),C.byref(self.pi)),'BRIDGE_HELPER_CREATE_FAILED')
            self.handles.update((self.pi.process,self.pi.thread))
            for h in child:self._close_handle(h)
            inherited=W.BOOL();require(self.k.IsProcessInJob(self.pi.process,None,C.byref(inherited)),'BRIDGE_ANCESTOR_PRESENCE_FAILED')
            require(self.k.AssignProcessToJobObject(self.job,self.pi.process),'BRIDGE_HELD_ASSIGN_FAILED');self.admitted=True
            self.process=NativeProcess(self)
            for name,h,flags,mode in zip(('stdin','stdout','stderr'),parent,(os.O_WRONLY,os.O_RDONLY,os.O_RDONLY),('wb','rb','rb')):
                fd=msvcrt.open_osfhandle(h,flags|os.O_BINARY);self.handles.remove(h)
                try:stream=os.fdopen(fd,mode,buffering=0)
                except BaseException:os.close(fd);raise
                self.files.append(stream);setattr(self.process,name,stream)
            if _eof_before_resume:self.process.stdin.close()
            before=self.empty()
            require(before['pids']==[self.process.pid] and before['active_processes']==1 and self.process.poll()is None,'BRIDGE_HELD_MEMBERSHIP_UNPROVEN')
            self.admission={'profile':'bridge-wsl-held-cap2-2GiB-r08','held_before_resume':True,'initial':before,
                            'inherited_job_present':bool(inherited.value),'flags':FLAGS,'cap':CAP,'memory_bytes':MEMORY,
                            'stdin_eof_before_resume':_eof_before_resume}
            self.admission['packaged_wsl_sha256']=PACKAGED_WSL_SHA256
            self.admission['wslhost_sha256']=WSLHOST_SHA256
            require(self.k.ResumeThread(self.pi.thread)==1,'BRIDGE_HELD_RESUME_FAILED');self.resumed=True;self._close_handle(self.pi.thread)
        except BaseException as error:
            try:self.close()
            except BaseException as cleanup:
                cleanup.cleanup_receipt=self.close_receipt;raise cleanup from error
            error.cleanup_receipt=self.close_receipt;raise
        finally:
            if attrs_ready:self.k.DeleteProcThreadAttributeList(attrs)

    def set_cleanup_deadline(self,deadline,*,cooperative_deadline=None):
        require(type(deadline) in (int,float) and math.isfinite(deadline),'BRIDGE_CLEANUP_DEADLINE_INVALID')
        if cooperative_deadline is not None:
            require(type(cooperative_deadline) in (int,float) and math.isfinite(cooperative_deadline)
                    and cooperative_deadline<=deadline,'BRIDGE_CLEANUP_DEADLINE_INVALID')
        # Only tighten the original total and cooperative cutoffs; no new clock.
        self._cleanup_deadline=deadline if self._cleanup_deadline is None else min(self._cleanup_deadline,deadline)
        if cooperative_deadline is not None:
            old=getattr(self,'_identity_wait_deadline',None)
            self._identity_wait_deadline=min(cooperative_deadline,self._cleanup_deadline,
                cooperative_deadline if old is None else old)

    def _cleanup_timeout(self,seconds):
        if self._cleanup_deadline is None:return seconds
        return min(seconds,max(0.0,self._cleanup_deadline-time.monotonic()))

    def _cleanup_until(self,seconds):
        return time.monotonic()+self._cleanup_timeout(seconds)

    def _cleanup_pause(self,seconds):
        # The original >=50ms empty-observation separation is not weakened
        # just because the common deadline has insufficient time remaining.
        require(self._cleanup_timeout(seconds)>=seconds,'BRIDGE_CLEANUP_WALL_EXCEEDED')
        time.sleep(seconds)

    def _close_handle(self,h):
        if h in self.handles:
            require(self.k.CloseHandle(h),'BRIDGE_HANDLE_CLOSE_FAILED');self.handles.remove(h)

    def _query(self,kind,obj):
        require(self.k.QueryInformationJobObject(self.job,kind,C.byref(obj),C.sizeof(obj),None),'BRIDGE_JOB_QUERY_FAILED');return obj

    def _events(self):
        for _ in range(128):
            msg=W.DWORD();key=C.c_size_t();pid=C.c_void_p()
            ok=self.k.GetQueuedCompletionStatus(self.port,C.byref(msg),C.byref(key),C.byref(pid),0)
            if not ok:
                require(C.get_last_error()==258,'BRIDGE_PORT_READ_FAILED');return
            require(key.value==KEY and len(self.events)<512,'BRIDGE_EVENT_BUDGET')
            self.events.append({'message':int(msg.value),'name':EVENTS.get(msg.value,'OTHER'),'notification_pid_not_control_authority':pid.value})
        require(False,'BRIDGE_EVENT_DRAIN_BUDGET')

    def _image_allowed(self,image):
        if image.casefold() in {r'c:\windows\system32\wsl.exe',r'c:\windows\system32\conhost.exe'}:return True
        if image.casefold()==PACKAGED_WSL.casefold():return getattr(self,'packaged_image_verified',False)
        return image.casefold()==WSLHOST.casefold() and getattr(self,'wslhost_image_verified',False)

    def _identity_note(self,row):
        # Closed categories/numeric kernel results only; bounded and deduped.
        if row not in self.identity_races:
            require(len(self.identity_races)<64,'BRIDGE_IDENTITY_BUDGET')
            self.identity_races.append(row)

    def _member_query_failed(self,pid,h,stage,winerror,*,birth=None,known=False):
        # Capture the original failing API's error BEFORE another Win32 call.
        # A signaled handle is a lifecycle fact, not image/allowlist proof.
        wait=int(self.k.WaitForSingleObject(h,0))
        wait_error=int(C.get_last_error()) if wait==0xffffffff else None
        self._identity_note({'pid':pid,'stage':stage,'winerror':winerror,
            'wait_result':wait,'wait_winerror':wait_error,'birth_filetime':birth,
            'prior_verified_same_birth':known,
            'status':'ACCESS_DENIED_UNPROVEN' if winerror==5 else
                     'EXIT_DURING_QUERY_UNPROVEN' if wait==0 else
                     'LIVE_QUERY_FAILED' if wait==258 else 'WAIT_UNPROVEN'})
        cutoff=getattr(self,'_identity_wait_deadline',None)
        total=getattr(self,'_cleanup_deadline',None)
        if (stage=='QueryFullProcessImageNameW' and winerror==5 and wait==258
                and known is True and type(birth)is int and birth>0
                and h in self.handles and getattr(self,'admitted',False)is True
                and type(cutoff) in (int,float) and math.isfinite(cutoff)
                and type(total) in (int,float) and math.isfinite(total)):
            cutoff=min(cutoff,total)
            # Reuse the bridge's existing 10ms cooperative polling quantum;
            # this is one observation, never a new shutdown grace or retry.
            millis=int(min(.01,max(0.0,cutoff-time.monotonic()))*1000)
            if 0<millis<0xffffffff:
                # One wait on the SAME held object, only during cleanup and
                # before the original reserved-enforcement cutoff. No image
                # retry or permission-failure-as-live-identity acceptance.
                exited=int(self.k.WaitForSingleObject(h,millis))
                error=int(C.get_last_error()) if exited==0xffffffff else None
                # The requested timeout is not a host scheduling guarantee.
                # Recheck the trusted (possibly tightened) original cutoffs.
                within=time.monotonic()<min(cutoff,self._cleanup_deadline,self._identity_wait_deadline)
                self._identity_note({'pid':pid,'stage':stage,'winerror':winerror,
                    'wait_result':exited,'wait_winerror':error,'birth_filetime':birth,
                    'prior_verified_same_birth':True,'lifecycle_wait_millis':millis,
                    'lifecycle_wait_within_cutoff':within,
                    'status':'EXITED_PREVIOUSLY_VERIFIED_SAME_BIRTH' if exited==0 and within
                             else 'ACCESS_DENIED_UNPROVEN'})
                if exited==0 and within:return True
        require(False,'BRIDGE_MEMBER_QUERY_FAILED')

    def _identity(self,pids):
        for pid in pids:
            retained=self._verified_members.get(pid)
            if retained is None:
                h=self.k.OpenProcess(0x1000|0x100000,False,pid)
                if not h:
                    error=int(C.get_last_error())
                    self._identity_note({'pid':pid,'stage':'OpenProcess','winerror':error,
                        'status':'ACCESS_DENIED_UNPROVEN' if error==5 else 'OPEN_FAILED_UNPROVEN'})
                    require(False,'BRIDGE_MEMBER_OPEN_FAILED')
                self.handles.add(h)
            else:
                # An open handle pins the process object and its identifier.
                # A signalled handle proves exit for this prior verified member.
                h,verified_birth=retained
                require(h in self.handles,'BRIDGE_MEMBER_CHANGED')
                retained_wait=int(self.k.WaitForSingleObject(h,0))
                if retained_wait==0:
                    self._identity_note({'pid':pid,'stage':'WaitForSingleObject','winerror':None,
                        'wait_result':0,'birth_filetime':verified_birth,'prior_verified_same_birth':True,
                        'status':'EXITED_PREVIOUSLY_VERIFIED_SAME_BIRTH'})
                    continue
                require(retained_wait==258,'BRIDGE_MEMBER_WAIT_FAILED')
            try:
                inside=W.BOOL();ok=self.k.IsProcessInJob(h,self.job,C.byref(inside))
                if not ok or not inside.value:
                    error=int(C.get_last_error()) if not ok else None
                    self._identity_note({'pid':pid,'stage':'IsProcessInJob','winerror':error,
                        'status':'MEMBERSHIP_QUERY_FAILED' if not ok else 'NOT_CURRENT_JOB'})
                    require(False,'BRIDGE_MEMBER_CHANGED')
                times=[W.FILETIME() for _ in range(4)];image=C.create_unicode_buffer(32768);size=W.DWORD(32768)
                if not self.k.GetProcessTimes(h,*(C.byref(t) for t in times)):
                    error=int(C.get_last_error());self._member_query_failed(pid,h,'GetProcessTimes',error)
                birth=int(times[0].dwHighDateTime)<<32|int(times[0].dwLowDateTime)
                require(birth>0,'BRIDGE_MEMBER_QUERY_FAILED')
                if retained is not None:require(birth==verified_birth,'BRIDGE_MEMBER_CHANGED')
                known=any(row['pid']==pid and row['birth_filetime']==birth
                    and row.get('membership_verified')is True and row.get('handle_held_during_query')is True
                    and row.get('allowlisted')is True and row.get('rejected')is False
                    and self._image_allowed(row['image']) for row in self.identities)
                wait=int(self.k.WaitForSingleObject(h,0))
                if wait not in (0,258):
                    error=int(C.get_last_error()) if wait==0xffffffff else None
                    self._identity_note({'pid':pid,'stage':'WaitForSingleObject','winerror':error,
                        'wait_result':wait,'status':'WAIT_UNPROVEN'})
                    require(False,'BRIDGE_MEMBER_WAIT_FAILED')
                if wait==0 and known:
                    # Same held, current-Job handle and freshly read birth.
                    # Preserve the prior identity as historical only. Do not
                    # relabel a missing image query as fresh allowlist proof.
                    self._identity_note({'pid':pid,'stage':'WaitForSingleObject','winerror':None,
                        'wait_result':0,'birth_filetime':birth,'prior_verified_same_birth':True,
                        'status':'EXITED_PREVIOUSLY_VERIFIED_SAME_BIRTH'})
                    continue
                if not self.k.QueryFullProcessImageNameW(h,0,image,C.byref(size)):
                    error=int(C.get_last_error())
                    if self._member_query_failed(pid,h,'QueryFullProcessImageNameW',error,birth=birth,known=known):continue
                allowed=self._image_allowed(image.value)
                if (pid,birth) not in self.seen:
                    require(len(self.seen)<64,'BRIDGE_IDENTITY_BUDGET');self.seen.add((pid,birth))
                    self.identities.append({'pid':pid,'image':image.value,'birth_filetime':birth,'membership_verified':True,
                                            'handle_held_during_query':True,'allowlisted':allowed,'rejected':not allowed})
                    if image.value.casefold()==PACKAGED_WSL.casefold():
                        self.identities[-1]['deployment_file_sha256']=PACKAGED_WSL_SHA256 if allowed else None
                    elif image.value.casefold()==WSLHOST.casefold():
                        self.identities[-1]['deployment_file_sha256']=WSLHOST_SHA256 if allowed else None
                require(allowed,'BRIDGE_MEMBER_IMAGE_UNEXPECTED')
                self._verified_members[pid]=(h,birth)
            finally:
                if pid not in self._verified_members:self._close_handle(h)

    def empty(self):
        with self.lock:
            self._events();a=self._query(1,Accounting());p=self._query(3,Members());lim=self._query(9,Limits())
            # Console processes may appear in native accounting differently
            # from limit enforcement. Preserve raw counts; kernel cap readback
            # and positive limit events are authoritative (FA012 r18).
            require(p.assigned==p.returned and p.returned<=64,'BRIDGE_MEMBERSHIP_UNPROVEN')
            require(lim.basic.flags==FLAGS and lim.basic.active==CAP and lim.job_memory==MEMORY,'BRIDGE_JOB_LIMIT_CHANGED')
            require(a.total<=64,'BRIDGE_TOTAL_PROCESS_BUDGET')
            ids=sorted(int(p.ids[i]) for i in range(p.returned));self._identity(ids)
            return {'active_processes':int(a.active),'pids':ids,'total_processes_started':int(a.total),
                    'peak_job_memory':int(lim.peak_job),'active_process_limit':int(lim.basic.active),
                    'job_memory_bytes':int(lim.job_memory),'flags':int(lim.basic.flags)}

    def check(self):
        self.empty()
        names={e['name'] for e in self.events}
        require(not names.intersection({'ACTIVE_PROCESS_LIMIT','JOB_MEMORY_LIMIT','PROCESS_MEMORY_LIMIT'}),'BRIDGE_NATIVE_LIMIT_HIT')

    def terminate(self):
        with self.lock:
            require(self.admitted,'BRIDGE_NOT_ADMITTED');self.forced=True
            require(self.k.TerminateJobObject(self.job,238),'BRIDGE_JOB_TERMINATE_FAILED')

    def close(self):
        if self.closed:return self.close_receipt
        error=None;samples=[];root_signalled=False;verified_member_exit_count=0
        try:
            if self.admitted:
                current=self.empty()
                if current['active_processes'] or current['pids']:self.terminate()
                until=self._cleanup_until(3)
                while True:
                    row=self.empty()
                    if not row['active_processes'] and not row['pids']:break
                    require(time.monotonic()<until,'BRIDGE_WHOLE_JOB_CLOSE_UNPROVEN');time.sleep(min(.01,self._cleanup_timeout(.01)))
                for _ in range(2):
                    row=self.empty();samples.append(row);require(not row['active_processes'] and not row['pids'],'BRIDGE_JOB_EMPTY_UNPROVEN');self._cleanup_pause(.05)
            elif self.pi.process:
                # Fresh retained never-resumed CreateProcess handle only.
                require(self.k.TerminateProcess(self.pi.process,239),'BRIDGE_HELD_ROOT_TERMINATE_FAILED')
            exit_cutoff=self._cleanup_until(3)
            if self.pi.process:
                root_signalled=self.k.WaitForSingleObject(self.pi.process,int(max(0.0,exit_cutoff-time.monotonic())*1000))==0
                require(root_signalled,'BRIDGE_ROOT_CLOSE_UNPROVEN')
            # Confirm all already-verified members, including those absent
            # from the two empty Job snapshots, within the existing exit wait.
            if self._identity_wait_deadline is not None:
                exit_cutoff=min(exit_cutoff,self._identity_wait_deadline)
            for pid,(handle,birth) in self._verified_members.items():
                require(handle in self.handles,'BRIDGE_MEMBER_CHANGED')
                wait=int(self.k.WaitForSingleObject(handle,int(max(0.0,exit_cutoff-time.monotonic())*1000)))
                if wait!=0:
                    self._identity_note({'pid':pid,'stage':'WaitForSingleObject','winerror':int(C.get_last_error()) if wait==0xffffffff else None,
                        'wait_result':wait,'birth_filetime':birth,'prior_verified_same_birth':True,'status':'MEMBER_EXIT_UNPROVEN'})
                    require(False,'BRIDGE_MEMBER_EXIT_UNPROVEN')
                require(self._cleanup_deadline is None or time.monotonic()<=self._cleanup_deadline,'BRIDGE_CLEANUP_WALL_EXCEEDED')
                verified_member_exit_count+=1
        except BaseException as caught:
            error=caught
            # Cleanup still controls only the retained own Job even when
            # observation/identity validation failed. Never use a bare PID.
            try:
                if self.admitted:
                    self.forced=True;require(self.k.TerminateJobObject(self.job,239),'BRIDGE_JOB_TERMINATE_FAILED')
                    def raw_empty():
                        a=self._query(1,Accounting());p=self._query(3,Members());lim=self._query(9,Limits())
                        require(p.assigned==p.returned and p.returned<=64,'BRIDGE_MEMBERSHIP_UNPROVEN')
                        return {'active_processes':int(a.active),'pids':sorted(int(p.ids[i]) for i in range(p.returned)),
                                'active_process_limit':int(lim.basic.active),'job_memory_bytes':int(lim.job_memory),'flags':int(lim.basic.flags)}
                    until=self._cleanup_until(3)
                    while True:
                        row=raw_empty()
                        if not row['active_processes'] and not row['pids']:break
                        require(time.monotonic()<until,'BRIDGE_JOB_EMPTY_UNPROVEN');time.sleep(min(.01,self._cleanup_timeout(.01)))
                    samples=[]
                    for _ in range(2):samples.append(raw_empty());self._cleanup_pause(.05)
                elif self.pi.process:self.k.TerminateProcess(self.pi.process,239)
                if self.pi.process:root_signalled=self.k.WaitForSingleObject(self.pi.process,int(self._cleanup_timeout(3)*1000))==0
            except BaseException:pass # Original failure retained; incomplete proof stays FAIL.
        finally:
            for stream in self.files:
                try:stream.close()
                except BaseException as caught:error=error or caught
            for h in list(self.handles):
                if h not in (self.job,self.port):
                    try:self._close_handle(h)
                    except BaseException as caught:error=error or caught
            for h in (self.job,self.port):
                if h:
                    try:self._close_handle(h)
                    except BaseException as caught:error=error or caught
            self._verified_members.clear()
            self.closed=True
            self.close_receipt={'held_admission':self.admission,'forced':self.forced,'empty_samples':samples,
                'root_handle_signalled':root_signalled,'pipes_closed':all(f.closed for f in self.files),
                'handles_closed':not self.handles,'events':self.events,'member_identities':self.identities,'identity_races':self.identity_races,
                'verified_member_exit_count':verified_member_exit_count}
        if error:
            error.cleanup_receipt=self.close_receipt;raise error
        return self.close_receipt
