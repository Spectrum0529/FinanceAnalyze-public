"""Fixed WSL stdio bridge and concrete trusted Windows RPC ports.

No public callable/path/command injection. The only injectable process owner is
the private fake-pipe test seam. A Windows Job is not a Linux container proof.
"""
from contextlib import contextmanager, closing
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import struct
import sqlite3
import subprocess
import threading
import time

from .errors import PlatformError
from .security import safe_json, now_text
from .database import Database
from .autonomous_models import Outcome
from .autonomous_sessions import digest
from .autonomous_windows import BrokerRequest


MAX_FRAME=64*1024*1024
MAX_STREAM=128*1024*1024
MAX_STDERR=1024*1024
MAX_RPC=8192
# FA012 r27: one total30s shutdown clock, with final6s enforcement.
CLEANUP_GRACE_SECONDS=30.0
FINAL_ENFORCEMENT_SECONDS=6.0
CLEANUP_ACQUIRE_WAIT_MS=100
MAX_CLEANUP_RPC=16
MAX_CLEANUP_BYTES=65536
MAX_CLEANUP_FRAME=16384
CLEANUP_CLOCK_VERSION=2
MAX_PROCESSING_ELAPSED_MS=624000
STOP_CODES={'BRIDGE_WALL_EXCEEDED','JOB_CANCELLED'}


def fail(code):
    raise PlatformError(code,409)


def exact(value,keys):
    if type(value)is not dict or set(value)!=set(keys):fail('BRIDGE_INPUT_INVALID')


@contextmanager
def _cleanup_transaction(service,state,*,final=False):
    # Keep the original cooperative/final cutoff and one short lock budget.
    limit=state.cleanup_deadline-(0 if final else FINAL_ENFORCEMENT_SECONDS)
    started=time.monotonic()
    if started>=limit:fail('BRIDGE_CLEANUP_WALL_EXCEEDED')
    wait_until=min(limit,started+CLEANUP_ACQUIRE_WAIT_MS/1000)

    def remaining_wait_ms():
        now=time.monotonic()
        if now>=limit:fail('BRIDGE_CLEANUP_WALL_EXCEEDED')
        return max(0,min(CLEANUP_ACQUIRE_WAIT_MS,math.floor((wait_until-now)*1000)))

    database=Database(service.database.path,timeout=0)
    timeout=remaining_wait_ms()/1000
    # Install the first lock budget before any database PRAGMA can run.
    with closing(sqlite3.connect(database.path,timeout=timeout,isolation_level=None)) as con:
        con.row_factory=sqlite3.Row
        con.set_progress_handler(lambda:int(time.monotonic()>=limit),100)

        def refresh_wait():
            con.execute(f'PRAGMA busy_timeout={remaining_wait_ms()}')
            if time.monotonic()>=limit:fail('BRIDGE_CLEANUP_WALL_EXCEEDED')

        def prepare(statement):
            refresh_wait()
            con.execute(statement)

        prepare('PRAGMA foreign_keys=ON')
        prepare('PRAGMA journal_mode=WAL')
        prepare('PRAGMA synchronous=FULL')
        prepare('PRAGMA recursive_triggers=ON')
        refresh_wait()
        if time.monotonic()>=limit:fail('BRIDGE_CLEANUP_WALL_EXCEEDED')
        con.execute('BEGIN IMMEDIATE')
        # If reset fails, closing the handle rolls back the transaction.
        con.execute('PRAGMA busy_timeout=0')
        try:
            if time.monotonic()>=limit:fail('BRIDGE_CLEANUP_WALL_EXCEEDED')
            yield con
            if time.monotonic()>=limit:fail('BRIDGE_CLEANUP_WALL_EXCEEDED')
            con.commit()
        except BaseException:
            con.rollback();raise

_SQLITE_CLEANUP_NAMES = frozenset({
    'SQLITE_ERROR', 'SQLITE_PERM', 'SQLITE_ABORT', 'SQLITE_ABORT_ROLLBACK',
    'SQLITE_BUSY', 'SQLITE_BUSY_RECOVERY', 'SQLITE_BUSY_SNAPSHOT', 'SQLITE_BUSY_TIMEOUT',
    'SQLITE_LOCKED', 'SQLITE_LOCKED_SHAREDCACHE', 'SQLITE_LOCKED_VTAB',
    'SQLITE_NOMEM', 'SQLITE_READONLY', 'SQLITE_READONLY_RECOVERY',
    'SQLITE_READONLY_CANTLOCK', 'SQLITE_READONLY_ROLLBACK', 'SQLITE_READONLY_DBMOVED',
    'SQLITE_READONLY_CANTINIT', 'SQLITE_READONLY_DIRECTORY', 'SQLITE_INTERRUPT',
    'SQLITE_IOERR', 'SQLITE_IOERR_READ', 'SQLITE_IOERR_SHORT_READ', 'SQLITE_IOERR_WRITE',
    'SQLITE_IOERR_FSYNC', 'SQLITE_IOERR_DIR_FSYNC', 'SQLITE_IOERR_TRUNCATE',
    'SQLITE_IOERR_FSTAT', 'SQLITE_IOERR_UNLOCK', 'SQLITE_IOERR_RDLOCK',
    'SQLITE_IOERR_DELETE', 'SQLITE_IOERR_ACCESS', 'SQLITE_IOERR_CHECKRESERVEDLOCK',
    'SQLITE_IOERR_LOCK', 'SQLITE_IOERR_CLOSE', 'SQLITE_IOERR_SHMOPEN',
    'SQLITE_IOERR_SHMSIZE', 'SQLITE_IOERR_SHMLOCK', 'SQLITE_IOERR_SHMMAP',
    'SQLITE_IOERR_SEEK', 'SQLITE_IOERR_MMAP', 'SQLITE_CORRUPT', 'SQLITE_FULL',
    'SQLITE_CANTOPEN', 'SQLITE_CANTOPEN_NOTEMPDIR', 'SQLITE_CANTOPEN_ISDIR',
    'SQLITE_CANTOPEN_FULLPATH', 'SQLITE_CANTOPEN_CONVPATH', 'SQLITE_CANTOPEN_SYMLINK',
    'SQLITE_PROTOCOL', 'SQLITE_SCHEMA', 'SQLITE_AUTH', 'SQLITE_NOTADB',
})


def _cleanup_sqlite_fault(error):
    from .jobs import JobService

    # Fixed offsets within the pinned functions. Match code identity before
    # using a line. No paths, frame names, SQL or exception text are emitted.
    transaction_sites = {
        16: 'CONNECT', 28: 'PRAGMA', 29: 'PRAGMA', 30: 'PRAGMA',
        31: 'PRAGMA', 32: 'PRAGMA', 36: 'PRAGMA', 34: 'BEGIN_IMMEDIATE',
        41: 'COMMIT', 43: 'ROLLBACK',
    }
    transaction_code = _cleanup_transaction.__wrapped__.__code__
    job_fence_code = JobService._active.__code__
    attempt_code = WindowsHostPorts._cleanup_attempt.__code__
    fence_code = DeadlineContext.assert_cleanup_fenced.__code__
    pipe_code = DeadlineChannel.check.__code__
    deny_code = WindowsHostPorts.active.__code__

    def describe(exc):
        # Read SQLite's instance metadata without invoking subclass accessors.
        metadata = BaseException.__dict__['__dict__'].__get__(exc)
        code = metadata.get('sqlite_errorcode')
        if type(code) is not int or not 0 < code <= 0x7fffffff:
            code = None
        name = metadata.get('sqlite_errorname')
        if (type(name) is not str or name not in _SQLITE_CLEANUP_NAMES
                or code is None or getattr(sqlite3, name, None) != code):
            name = None

        phase = site = line = caller = None
        in_job_fence = False
        trace = BaseException.__traceback__.__get__(exc)
        for _ in range(32):
            if trace is None:
                break
            frame_code = trace.tb_frame.f_code
            offset = trace.tb_lineno - frame_code.co_firstlineno
            point = None
            if frame_code is pipe_code and offset == 3:
                caller = 'PIPE_GUARD'
            elif frame_code is deny_code and offset == 4:
                caller = 'HOST_DENY'
            elif frame_code is fence_code and offset == 5:
                in_job_fence = True
            elif frame_code is transaction_code and offset in transaction_sites:
                point = (transaction_sites[offset], 'CLEANUP_TRANSACTION')
            elif frame_code is job_fence_code and offset == 1 and in_job_fence:
                point = ('FENCE_QUERY', 'JOB_FENCE')
            elif frame_code is attempt_code and offset in {2, 3, 4}:
                point = ('FENCE_QUERY', 'SESSION_FENCE' if offset == 4 else 'ATTEMPT_FENCE')
            if point is not None:
                phase, site = point
                line = trace.tb_lineno
            trace = trace.tb_next
        return dict(code=code, name=name, phase=phase, site=site, line=line, caller=caller)

    result = describe(error)
    context = BaseException.__context__.__get__(error)
    if isinstance(context, sqlite3.OperationalError) and context is not error:
        result['context'] = describe(context)
    return result


@dataclass(frozen=True)
class BridgeDeployment:
    runtime_sha256:str
    root:Path=field(default_factory=lambda:Path(__file__).resolve().parents[3])
    paths_sha256:str=""

    def __post_init__(self):
        if not re.fullmatch('[0-9a-f]{64}',self.runtime_sha256):fail('BRIDGE_DEPLOYMENT_INVALID')

    def argv(self):
        # No shell; every path/module/argument is fixed deployment source.
        from .autonomous_windows_job import fixed_argv
        return fixed_argv(self.root, self.paths_sha256)


@dataclass
class BridgeState:
    binding:dict
    runtime_sha256:str
    root:Path=field(default_factory=lambda:Path(__file__).resolve().parents[3])
    paths_sha256:str=""
    linux_closed:bool=False
    rpc_calls:int=0
    containers:dict=field(default_factory=dict)
    windows_receipt:dict|None=None
    mode:str='BUSINESS'
    stop_code:str|None=None
    cleanup_deadline:float|None=None
    cleanup_records:int=0
    cleanup_bytes:int=0
    cleanup_containers:frozenset=field(default_factory=frozenset)
    business_deadline:float|None=None
    cleanup_started:float|None=None
    cleanup_protocol_code:str|None=None
    inflight_request:str|None=None
    cleanup_pending_request:str|None=None
    cleanup_late_created:bool=False
    terminal_ack_ready:bool=False
    first_stop_at:float|None=None
    cleanup_sqlite_fault:dict|None=None
    image_id:str|None=None

    def note_stop(self):
        if self.first_stop_at is None:self.first_stop_at=time.monotonic()

    def begin_cleanup(self,error):
        code=None if error is None else getattr(error,'code','BRIDGE_FAILED')
        if self.mode=='CLOSED':fail('BRIDGE_ALREADY_CLOSED')
        if self.mode=='CLEANUP_ONLY':return False
        if self.business_deadline is None:fail('BRIDGE_CLEANUP_STATE_INVALID')
        if CLEANUP_GRACE_SECONDS<=0 or MAX_CLEANUP_RPC<=0 or MAX_CLEANUP_BYTES<=0:
            fail('BRIDGE_CLEANUP_POLICY_UNSEALED')
        if error is not None:self.note_stop()
        self.mode='CLEANUP_ONLY';self.stop_code=code
        self.cleanup_started=min(time.monotonic(),self.business_deadline,
            self.first_stop_at if self.first_stop_at is not None else float('inf'))
        self.cleanup_deadline=self.cleanup_started+CLEANUP_GRACE_SECONDS
        self.cleanup_containers=frozenset(self.containers)
        self.cleanup_pending_request=self.inflight_request
        return True

    def check_cleanup(self):
        if self.mode!='CLEANUP_ONLY':fail('BRIDGE_CLEANUP_STATE_INVALID')
        if time.monotonic()>=self.cleanup_deadline-FINAL_ENFORCEMENT_SECONDS:fail('BRIDGE_CLEANUP_WALL_EXCEEDED')

    def remaining(self):
        if self.cleanup_deadline is None:fail('BRIDGE_CLEANUP_STATE_INVALID')
        return max(0.0,self.cleanup_deadline-time.monotonic())

    def close(self):
        self.mode='CLOSED'


class DeadlineContext:
    def __init__(self,context,deadline):self.context,self.deadline=context,deadline;self._stop_observer=None
    def __getattr__(self,name):return getattr(self.context,name)
    def require_active(self):
        try:
            self.context.require_active()
            if time.monotonic()>=self.deadline:fail('BRIDGE_WALL_EXCEEDED')
        except BaseException:
            if self._stop_observer is not None:self._stop_observer()
            raise
    def assert_fenced(self,connection):
        self.require_active();return self.context.assert_fenced(connection)

    def assert_cleanup_fenced(self,connection,binding):
        # Cancellation/deadline do not grant a new lease. The original exact
        # running owner/fence/lease must still be valid in this transaction.
        expected={'job_id':self.job_id,'run_id':self.run.run_id,'fencing_token':self.fencing_token}
        if any(binding.get(k)!=v for k,v in expected.items()):fail('BRIDGE_BINDING_MISMATCH')
        row=self.context.service._active(connection,self.job_id,self.worker_id,self.fencing_token)
        if row['run_id']!=self.run.run_id:fail('LEASE_LOST')
        return row


# Bridge-private cap2 held startup; generic strategy/TA/Qlib profiles unchanged.
from .autonomous_windows_job import HeldWindowsHelper as WindowsHelperOwner


class CountedReader:
    def __init__(self,raw):self.raw,self.total=raw,0
    def read(self,n):
        value=self.raw.read(min(n,65536))
        if value:
            self.total+=len(value)
            if self.total>MAX_STREAM:fail('BRIDGE_STREAM_EXCEEDED')
        return value


class CountedWriter:
    def __init__(self,raw):self.raw,self.total=raw,0
    def write(self,value):
        if len(value)>MAX_FRAME+4 or self.total+len(value)>MAX_STREAM:fail('BRIDGE_STREAM_EXCEEDED')
        cursor=0
        while cursor<len(value):
            count=self.raw.write(value[cursor:cursor+65536])
            if not count:fail('BRIDGE_EOF')
            cursor+=count;self.total+=count
        return cursor
    def flush(self):self.raw.flush()


class DeadlineChannel:
    """One retained frame I/O, with a distinct bounded cleanup-only phase."""
    def __init__(self,process,active,deadline,*,state=None,cleanup_active=None):
        from fa_research_executor.bridge import FramedChannel
        from fa_research_executor.protocol import BOOTSTRAP_STDERR_LIMIT, strict_json
        self.process,self.active,self.deadline=process,active,deadline
        self.state=state;self.cleanup_active=cleanup_active;self._pending=None;self._cleanup_stream_start=None;self._io_lock=threading.Lock();self._cleanup_frames=0;self._cleanup_advertised_ms=None
        self._request_anchor=None;self._next_request_sequence=0
        self.reader=CountedReader(process.stdout);self.writer=CountedWriter(process.stdin)
        outer=self
        class BoundedFrames(FramedChannel):
            def receive(self):
                size=struct.unpack('>I',self._read_exact(4))[0]
                limit=MAX_CLEANUP_FRAME if outer.state is not None and outer.state.mode=='CLEANUP_ONLY' else MAX_FRAME
                if not 0<size<=limit:fail('BRIDGE_FRAME_TOO_LARGE')
                return strict_json(self._read_exact(size),limit)
        self.framed=BoundedFrames(self.reader,self.writer)

        class CleanupWriter:
            def write(self, value):
                # A queued cleanup send can expire even after the parent check.
                # Guard its first write, then let an already-started frame finish.
                outer.state.check_cleanup()
                return outer.writer.write(value)

            def flush(self):
                outer.writer.flush()

        self._cleanup_framed=FramedChannel(self.reader,CleanupWriter())
        self.threads=[];self.stderr_bytes=0;self.stderr_hash=hashlib.sha256();self.stderr_failed=threading.Event()
        self._bootstrap_stderr=bytearray()
        def stderr():
            try:
                while True:
                    block=process.stderr.read(65536)
                    if not block:break
                    self.stderr_bytes+=len(block)
                    if self.stderr_bytes>MAX_STDERR:self.stderr_failed.set();break
                    self.stderr_hash.update(block)
                    self._bootstrap_stderr.extend(block[:max(0,BOOTSTRAP_STDERR_LIMIT-len(self._bootstrap_stderr))])
            except (OSError,ValueError):self.stderr_failed.set()
        worker=threading.Thread(target=stderr,daemon=True,name='autonomous-owned-stderr')
        self.threads.append(worker);worker.start()

    def bootstrap_diagnostic(self, binding):
        from fa_research_executor.protocol import BOOTSTRAP_DIAGNOSTIC_CODES, BOOTSTRAP_DIAGNOSTIC_PREFIX
        if self.stderr_failed.is_set():
            return None
        prefix=BOOTSTRAP_DIAGNOSTIC_PREFIX+digest(binding).encode('ascii')+b' '
        # The last fragment may be unterminated or cut by the capture limit.
        records=[line[len(prefix):] for line in self._bootstrap_stderr.split(b'\n')[:-1]
                 if line.startswith(prefix)]
        if len(records)!=1:
            return None
        return next((code for code in BOOTSTRAP_DIAGNOSTIC_CODES
                     if records[0]==code.encode('ascii')),None)

    @property
    def pending_io(self):return None if self._pending is None else self._pending['kind']

    def begin_cleanup(self,error):
        if self.state is None or self.cleanup_active is None:fail('BRIDGE_CLEANUP_UNAVAILABLE')
        entered=self.state.begin_cleanup(error)
        if entered:self._cleanup_stream_start=(self.reader.total,self.writer.total)
        return entered

    def record_cleanup_error(self,error):
        if self.state is None:
            return
        if isinstance(error, sqlite3.OperationalError):
            self.state.cleanup_protocol_code='OperationalError'
            self.state.cleanup_sqlite_fault=_cleanup_sqlite_fault(error)
        else:
            self.state.cleanup_protocol_code=getattr(error,'code',type(error).__name__)
            self.state.cleanup_sqlite_fault=None

    def note_request(self,message):
        """Shared pump calls once after validation, before invoking a host port.

        One outstanding RPC, not a journal or a peer-provided timestamp. The
        same anchor survives business-to-cleanup and pending IO transitions.
        """
        if self.state is None or self.state.mode not in {'BUSINESS','CLEANUP_ONLY'} or self._request_anchor is not None:fail('BRIDGE_REQUEST_ANCHOR_INVALID')
        if (type(message)is not dict or set(message)!={'type','binding_sha256','sequence','operation','value'}
                or message['type']!='call' or message['binding_sha256']!=digest(self.state.binding)
                or type(message['sequence'])is not int or message['sequence']!=self._next_request_sequence):
            fail('BRIDGE_REQUEST_ANCHOR_INVALID')
        stamp=time.monotonic()
        if type(stamp)not in (int,float) or not math.isfinite(stamp) or stamp<0:fail('BRIDGE_REQUEST_ANCHOR_INVALID')
        self._request_anchor={'sequence':message['sequence'],'binding_sha256':message['binding_sha256'],
                              'received_at':stamp,'request_sha256':digest(message)}
        self._next_request_sequence+=1

    def _retire_reply(self,payload):
        if type(payload)is not dict or payload.get('type')not in {'return','error'}:return
        anchor=self._request_anchor
        if (anchor is None or payload.get('binding_sha256')!=anchor['binding_sha256']
                or type(payload.get('sequence'))is not int or payload['sequence']!=anchor['sequence']):
            fail('BRIDGE_REPLY_ANCHOR_INVALID')
        self._request_anchor=None

    def cleanup_remaining(self):
        if self.state is None:fail('BRIDGE_CLEANUP_UNAVAILABLE')
        return max(0.0,self.state.remaining()-FINAL_ENFORCEMENT_SECONDS)

    def check(self):
        try:
            if self.state is not None and self.state.mode=='CLEANUP_ONLY':
                self.state.check_cleanup();self.cleanup_active()
                if self._cleanup_stream_start is None:fail('BRIDGE_CLEANUP_STATE_INVALID')
                read_start,write_start=self._cleanup_stream_start
                if self.reader.total-read_start+self.writer.total-write_start>MAX_CLEANUP_BYTES:fail('BRIDGE_CLEANUP_BYTES_EXCEEDED')
            else:
                if self.state is not None and self.state.mode!='BUSINESS':fail('BRIDGE_ALREADY_CLOSED')
                self.active()
                if time.monotonic()>=self.deadline:fail('BRIDGE_WALL_EXCEEDED')
            if self.stderr_failed.is_set():fail('BRIDGE_STDERR_EXCEEDED')
        except BaseException:
            if self.state is not None and self.state.mode=='BUSINESS':self.state.note_stop()
            raise

    def _collect_pending(self):
        pending=self._pending
        try:
            while pending['result'] is None:
                self.check()
                try:pending['result']=pending['done'].get(timeout=.05)
                except queue.Empty:continue
            # Keep a completed result until guard passes; a cutoff must never
            # discard a frame or start a second pipe reader on cleanup entry.
            self.check()
            ok,value=pending['result']
            if ok and pending['kind']=='send':self._retire_reply(pending['payload'])
            self._pending=None
            if not ok:
                from fa_research_executor.protocol import Rejected
                if type(value)is Rejected and re.fullmatch('[A-Z][A-Z0-9_]{0,63}',str(value)):fail(str(value))
                raise value
            return {'kind':pending['kind'],'payload':pending['payload'],'value':value}
        finally:
            timeout=min(.05,self.cleanup_remaining()) if self.state is not None and self.state.mode=='CLEANUP_ONLY' else .05
            pending['worker'].join(timeout=timeout)
            self.threads=[t for t in self.threads if t.is_alive() or t.name=='autonomous-owned-stderr']

    def resume_pending(self):
        if self.state is None or self.state.mode!='CLEANUP_ONLY':fail('BRIDGE_CLEANUP_STATE_INVALID')
        if not self._io_lock.acquire(blocking=False):fail('BRIDGE_IO_PENDING')
        try:
            if self._pending is None:return None
            self._cleanup_frames+=1
            if self._cleanup_frames>2*MAX_CLEANUP_RPC+2:fail('BRIDGE_CLEANUP_BUDGET_EXCEEDED')
            # Finish the one already-started operation, never retransmit it.
            return self._collect_pending()
        finally:self._io_lock.release()

    def _cleanup_wire(self,value):
        # The shared pump supplies its original four-field reply. Only this
        # channel stamps version2 timing immediately before NEW IO. R and S
        # stay on Windows; only two conservative durations cross the boundary.
        if type(value)is not dict or value.get('type')not in {'error','return'}:fail('BRIDGE_CLEANUP_SEND_INVALID')
        logical={'type','binding_sha256','sequence','code' if value['type']=='error' else 'value'}
        if set(value)!=logical:fail('BRIDGE_CLEANUP_SEND_INVALID')
        anchor=self._request_anchor
        if (anchor is None or type(value)is not dict or value.get('binding_sha256')!=anchor['binding_sha256']
                or type(value.get('sequence'))is not int or value['sequence']!=anchor['sequence']):
            fail('BRIDGE_REPLY_ANCHOR_INVALID')
        sent_at=time.monotonic()
        if (type(sent_at)not in (int,float) or not math.isfinite(sent_at) or sent_at<anchor['received_at']
                or self.state is None or type(self.state.cleanup_deadline)not in (int,float)
                or not math.isfinite(self.state.cleanup_deadline)):
            fail('BRIDGE_CLEANUP_BUDGET_INVALID')
        remaining=max(0.,self.state.cleanup_deadline-FINAL_ENFORCEMENT_SECONDS-sent_at)
        if type(remaining) not in (int,float) or not math.isfinite(remaining):fail('BRIDGE_CLEANUP_BUDGET_INVALID')
        milliseconds=max(0,min(24000,math.floor(remaining*1000)))
        if self._cleanup_advertised_ms is not None:milliseconds=min(milliseconds,self._cleanup_advertised_ms)
        elapsed=math.floor((sent_at-anchor['received_at'])*1000)
        wire=dict(value,cleanup_clock_version=CLEANUP_CLOCK_VERSION,cleanup_remaining_ms=milliseconds,
                  cleanup_processing_elapsed_ms=elapsed)
        expected={'type','binding_sha256','sequence','code' if value['type']=='error' else 'value',
                  'cleanup_remaining_ms','cleanup_clock_version','cleanup_processing_elapsed_ms'}
        if (set(wire)!=expected or type(wire['cleanup_remaining_ms'])is not int or not 0<=wire['cleanup_remaining_ms']<=24000
                or type(wire['cleanup_processing_elapsed_ms'])is not int or not 0<=wire['cleanup_processing_elapsed_ms']<=MAX_PROCESSING_ELAPSED_MS):
            fail('BRIDGE_CLEANUP_SEND_INVALID')
        self._cleanup_advertised_ms=milliseconds
        return wire

    def _io(self,kind,operation,payload=None):
        if not self._io_lock.acquire(blocking=False):fail('BRIDGE_IO_PENDING')
        try:
            self.check()
            if self._pending is not None:fail('BRIDGE_IO_PENDING')
            if self.state is not None and self.state.mode=='CLEANUP_ONLY':
                # The owner check in self.check() may have used the remaining time.
                self.state.check_cleanup()
                self._cleanup_frames+=1
                if self._cleanup_frames>2*MAX_CLEANUP_RPC+2:fail('BRIDGE_CLEANUP_BUDGET_EXCEEDED')
                if kind=='send':
                    payload=self._cleanup_wire(payload)
                    operation=lambda:self._cleanup_framed.send(payload)
            done=queue.Queue(maxsize=1)
            def invoke():
                try:done.put((True,operation()))
                except BaseException as error:done.put((False,error))
            worker=threading.Thread(target=invoke,daemon=True,name='autonomous-owned-frame')
            self._pending={'kind':kind,'payload':payload,'done':done,'worker':worker,'result':None}
            self.threads.append(worker);worker.start()
            return self._collect_pending()['value']
        finally:self._io_lock.release()

    def send(self,value):
        if self.state is not None and self.state.mode=='CLEANUP_ONLY':
            if type(value)is not dict:fail('BRIDGE_CLEANUP_SEND_INVALID')
            if (value.get('binding_sha256')!=digest(self.state.binding)
                    or type(value.get('sequence'))is not int or value['sequence']<0):fail('BRIDGE_BINDING_MISMATCH')
            if value.get('type')=='error':
                if set(value)!={'type','binding_sha256','sequence','code'} or value['code']!='BRIDGE_CLEANUP_ONLY':fail('BRIDGE_CLEANUP_SEND_INVALID')
            elif (set(value)!={'type','binding_sha256','sequence','value'} or value['type']!='return'
                    or value['value']!={'recorded':True} or not self.state.terminal_ack_ready):
                fail('BRIDGE_CLEANUP_SEND_INVALID')
        result=self._io('send',lambda:self.framed.send(value),value)
        if self.state is not None:self.state.terminal_ack_ready=False
        return result
    def receive(self):return self._io('receive',self.framed.receive)


class WindowsChannelFactory:
    def __init__(self,runtime,service,deployment,state,*,_owner_factory=WindowsHelperOwner):
        self.runtime,self.service,self.deployment,self.state=runtime,service,deployment,state
        self._owner_factory=_owner_factory

    @contextmanager
    def __call__(self,context,binding):
        if binding!=self.state.binding:fail('BRIDGE_BINDING_MISMATCH')
        self.state.business_deadline=context.deadline
        context._stop_observer=self.state.note_stop
        with self.service.database.transaction() as con:
            attempt,session=self.service._attempt(con,context)
            if attempt['attempt_id']!=binding['attempt_id'] or attempt['status']!='IN_FLIGHT':fail('BRIDGE_ATTEMPT_INVALID')
            reservation=json.loads(attempt['reservation_json'])
            remaining=min(reservation['compute_seconds'],session['deadline']-self.service.clock())
        if not 0<remaining<=3600:fail('BRIDGE_WALL_EXCEEDED')
        self.state.business_deadline=context.deadline
        try:owner=self._owner_factory(self.deployment,self.runtime.root)
        except BaseException as error:
            # R28-02: even failed-start evidence consumes the SAME remaining
            # clock and zero-wait connection; never mask the original error.
            self.state.note_stop();self.state.begin_cleanup(error)
            receipt=getattr(error,'cleanup_receipt',None)
            if receipt is not None:
                try:
                    with _cleanup_transaction(self.service,self.state,final=True) as con:
                        self.service._event(con,binding['session_id'],'BRIDGE_START_FAILED',receipt)
                except BaseException as persist:
                    error.receipt_persist_code=getattr(persist,'code',type(persist).__name__)
            self.state.close()
            raise
        process=owner.process
        def active():
            context.require_active()
            if hasattr(owner,'check'):owner.check()
        def cleanup_active():
            self.state.check_cleanup()
            with _cleanup_transaction(self.service,self.state) as con:
                context.assert_cleanup_fenced(con,binding)
            self.state.check_cleanup()
            owner.set_cleanup_deadline(self.state.cleanup_deadline,
                cooperative_deadline=self.state.cleanup_deadline-FINAL_ENFORCEMENT_SECONDS)
            if hasattr(owner,'check'):owner.check()
        try:channel=DeadlineChannel(process,active,context.deadline,
                                    state=self.state,cleanup_active=cleanup_active)
        except BaseException as primary:
            close_error=None;receipt=None
            self.state.begin_cleanup(primary);owner.set_cleanup_deadline(self.state.cleanup_deadline,cooperative_deadline=self.state.cleanup_deadline-FINAL_ENFORCEMENT_SECONDS)
            try:receipt=owner.close()
            except BaseException as error:
                close_error=error;receipt=getattr(error,'cleanup_receipt',None) or getattr(owner,'close_receipt',None)
            evidence={'stage':'channel_construction','native_close':receipt,'io_joined':None,
                      'primary_code':getattr(primary,'code',type(primary).__name__),
                      'cleanup_code':None if close_error is None else getattr(close_error,'code',type(close_error).__name__)}
            primary.cleanup_receipt=evidence
            try:
                with _cleanup_transaction(self.service,self.state,final=True) as con:
                    self.service._event(con,binding['session_id'],'BRIDGE_START_FAILED',evidence)
            except BaseException as error:
                evidence['receipt_persist_code']=getattr(error,'code',type(error).__name__)
            self.state.close()
            raise
        success=False;forced=False;cleanup_error=None;primary_error=None
        try:
            yield channel
            channel.check()
            if self.state.mode!='BUSINESS':fail('BRIDGE_CLEANUP_ONLY')
            if not self.state.linux_closed:fail('BRIDGE_LINUX_CLOSURE_UNPROVEN')
            success=True
        except BaseException as error:
            self.state.note_stop();primary_error=error
            raise
        finally:
            # All cooperative transport, native waits and joins share r27's
            # single clock. A late entry cannot reset the business cutoff.
            try:
                self.state.begin_cleanup(primary_error)
                owner.set_cleanup_deadline(self.state.cleanup_deadline,cooperative_deadline=self.state.cleanup_deadline-FINAL_ENFORCEMENT_SECONDS)
                if not any(t.is_alive() and t.name=='autonomous-owned-frame' for t in channel.threads):process.stdin.close()
                until=self.state.cleanup_deadline-FINAL_ENFORCEMENT_SECONDS
                while True:
                    proof=owner.empty()
                    if process.poll()is not None and not proof['active_processes'] and not proof['pids']:break
                    if time.monotonic()>=until:break
                    time.sleep(min(.01,self.state.remaining()))
                if process.poll()is None or proof['active_processes'] or proof['pids']:
                    forced=True;owner.terminate()
                process.wait(timeout=min(3,self.state.remaining()))
                until=self.state.cleanup_deadline
                while True:
                    proof=owner.empty()
                    if not proof['active_processes'] and not proof['pids']:break
                    if time.monotonic()>=until:fail('BRIDGE_JOB_EMPTY_UNPROVEN')
                    time.sleep(min(.01,self.state.remaining()))
                for thread in channel.threads:thread.join(timeout=min(1,self.state.remaining()))
                if any(t.is_alive() for t in channel.threads):fail('BRIDGE_IO_CLOSE_UNPROVEN')
                for pipe in (process.stdin,process.stdout,process.stderr):pipe.close()
                samples=[]
                for _ in range(2):
                    proof=owner.empty();samples.append(proof)
                    if proof['active_processes'] or proof['pids']:fail('BRIDGE_JOB_EMPTY_UNPROVEN')
                    if self.state.remaining()<.05:fail('BRIDGE_CLEANUP_WALL_EXCEEDED')
                    time.sleep(.05)
                self.state.windows_receipt={'empty_samples':samples,'forced':forced}
                if success and (forced or process.returncode!=0):fail('BRIDGE_HELPER_EXIT_INVALID')
            except BaseException as error:
                cleanup_error=error
            finally:
                # Never let validation/query/close errors skip retained-owner
                # shutdown or the actual close receipt. No bare PID fallback.
                native_close=None
                try:native_close=owner.close()
                except BaseException as error:
                    cleanup_error=cleanup_error or error
                    native_close=getattr(error,'cleanup_receipt',None) or getattr(owner,'close_receipt',None)
                for thread in channel.threads:thread.join(timeout=min(1,self.state.remaining()))
                io_joined=not any(t.is_alive() for t in channel.threads)
                if not io_joined:cleanup_error=cleanup_error or PlatformError('BRIDGE_IO_CLOSE_UNPROVEN',409)
                if time.monotonic()>self.state.cleanup_deadline:
                    cleanup_error=cleanup_error or PlatformError('BRIDGE_CLEANUP_WALL_EXCEEDED',409)
                evidence=dict(self.state.windows_receipt or {})
                evidence.update(helper_pid=process.pid,returncode=getattr(process,'returncode',None),
                    io_joined=io_joined,stdin_bytes=channel.writer.total,stdout_bytes=channel.reader.total,
                    stderr_bytes=channel.stderr_bytes,stderr_sha256=channel.stderr_hash.hexdigest(),
                    stderr_failed=channel.stderr_failed.is_set(),linux_closure_receipt=self.state.linux_closed,
                    binding_sha256=digest(binding),native_close=native_close,
                    bridge_mode=self.state.mode,cleanup_deadline=self.state.cleanup_deadline,
                    cleanup_started=self.state.cleanup_started,first_stop_at=self.state.first_stop_at,
                    cleanup_records=self.state.cleanup_records,cleanup_bytes=self.state.cleanup_bytes,
                    cleanup_protocol_code=self.state.cleanup_protocol_code,
                    cleanup_total_seconds=CLEANUP_GRACE_SECONDS,
                    cleanup_deadline_exceeded=time.monotonic()>self.state.cleanup_deadline,
                    primary_code=None if primary_error is None else getattr(primary_error,'code',type(primary_error).__name__),
                    cleanup_code=None if cleanup_error is None else getattr(cleanup_error,'code',type(cleanup_error).__name__))
                if primary_error is not None and io_joined and not self.state.linux_closed and not self.state.containers:
                    bootstrap_code=channel.bootstrap_diagnostic(binding)
                    if bootstrap_code is not None:evidence['bootstrap_error_code']=bootstrap_code
                if native_close is not None:
                    evidence['forced']=forced or native_close.get('forced',False)
                    if 'empty_samples' not in evidence and 'empty_samples' in native_close:evidence['empty_samples']=native_close['empty_samples']
                if hasattr(owner,'admission'):evidence['held_admission']=owner.admission
                if self.state.cleanup_sqlite_fault is not None:
                    evidence['cleanup_sqlite_fault']=self.state.cleanup_sqlite_fault
                self.state.windows_receipt=evidence
                try:
                    with _cleanup_transaction(self.service,self.state,final=True) as con:
                        self.service._event(con,binding['session_id'],
                            'BRIDGE_CLEANUP_FAILED' if cleanup_error is not None or primary_error is not None else 'BRIDGE_CLOSED',evidence)
                except BaseException as error:
                    cleanup_error=cleanup_error or error
                    evidence['receipt_persist_code']=getattr(error,'code',type(error).__name__)
                self.state.close()
                if primary_error is not None:primary_error.cleanup_receipt=evidence
            if primary_error is None and cleanup_error is not None:raise cleanup_error


class WindowsHostPorts:
    def __init__(self,runtime,service,inputs,broker,state):
        self.service,self.inputs,self.broker,self.state=service,inputs,broker,state
        from fa_research_executor.windows_ports import bind
        self.base=bind(runtime,service,inputs,broker)

    def _guard(self,context,begun):
        self.state.terminal_ack_ready=False
        if self.state.mode!='BUSINESS':fail('BRIDGE_CLEANUP_ONLY')
        context.require_active()
        if self.state.linux_closed:fail('BRIDGE_ALREADY_CLOSED')
        if begun['attempt_id']!=self.state.binding['attempt_id'] or begun['session_id']!=self.state.binding['session_id']:fail('BRIDGE_BINDING_MISMATCH')
        self.state.rpc_calls+=1
        if self.state.rpc_calls>MAX_RPC:fail('BRIDGE_RPC_BUDGET_EXCEEDED')

    def _cleanup_binding(self,c,b):
        self.state.check_cleanup()
        actual={'session_id':b['session_id'],'attempt_id':b['attempt_id'],
                'job_id':c.job_id,'run_id':c.run.run_id,'fencing_token':c.fencing_token}
        if actual!=self.state.binding or self.state.linux_closed:fail('BRIDGE_BINDING_MISMATCH')

    def _cleanup_attempt(self,con,c,b):
        self._cleanup_binding(c,b);c.assert_cleanup_fenced(con,self.state.binding)
        attempt=con.execute('SELECT * FROM autonomous_attempts WHERE attempt_id=? AND job_id=? AND run_id=?',
                            (b['attempt_id'],c.job_id,c.run.run_id)).fetchone()
        session=con.execute('SELECT * FROM autonomous_sessions WHERE session_id=?',(b['session_id'],)).fetchone()
        if (attempt is None or session is None or attempt['session_id']!=b['session_id']
                or attempt['status']!='IN_FLIGHT' or session['active_attempt']!=b['attempt_id']
                or session['owner_id']!=c.run.owner_id):fail('LEASE_LOST')
        return attempt,session

    def _cleanup_budget(self,value):
        size=len(safe_json(value).encode())
        if size>MAX_CLEANUP_FRAME or self.state.cleanup_records>=MAX_CLEANUP_RPC or self.state.cleanup_bytes+size>MAX_CLEANUP_BYTES:
            fail('BRIDGE_CLEANUP_BUDGET_EXCEEDED')
        self.state.cleanup_records+=1;self.state.cleanup_bytes+=size

    def active(self,c,b,v):
        if self.state.mode=='CLEANUP_ONLY':
            self.state.terminal_ack_ready=False
            if v is not None:fail('BRIDGE_INPUT_INVALID')
            with _cleanup_transaction(self.service,self.state) as con:self._cleanup_attempt(con,c,b)
            fail('BRIDGE_CLEANUP_ONLY')
        self._guard(c,b)
        if v is not None:fail('BRIDGE_INPUT_INVALID')
        return self.base['active'](c,b,v)

    def model_call(self,c,b,v):
        self._guard(c,b)
        if b['phase']!='development':fail('FINAL_ADAPTIVE_PORT_FORBIDDEN')
        if b['spec'].get('reuse_policy')=='evaluate_saved':fail('SAVED_BACKTEST_MODEL_FORBIDDEN')
        value=BrokerRequest.model_validate(v)
        if value.attempt_id!=b['attempt_id']:fail('BRIDGE_BINDING_MISMATCH')
        return self.broker.call(c, value.model_dump(), begun=b)

    def export_input(self,c,b,v):
        self._guard(c,b)
        if v is not None:fail('BRIDGE_INPUT_INVALID')
        if b['phase']=='final_test':
            from .autonomous_final import final_scope
            final_scope(self.service,c,b)
            seal=getattr(self,'final_seal',None);packages=getattr(self,'final_packages',None)
            if seal is None or packages is None or b.get('sealed_candidate')!=seal:fail('FINAL_SEAL_BINDING_MISMATCH')
            return packages
        if b['phase']!='development':fail('BRIDGE_INPUT_INVALID')
        return self.base['export_input'](c,b,v)

    def resolve_roles(self,c,b,v):
        self._guard(c,b)
        if b['phase']!='development':fail('FINAL_ADAPTIVE_PORT_FORBIDDEN')
        if b['spec'].get('reuse_policy')=='evaluate_saved':fail('SAVED_BACKTEST_MODEL_FORBIDDEN')
        if v is not None:fail('BRIDGE_INPUT_INVALID')
        profiles=self.base['resolve_roles'](c,b,v)
        return {role:profiles.get(role) for role in ('hypothesis','coder','critic','reporter')}

    def committed_history(self,c,b,v):
        self._guard(c,b)
        if b['phase']!='development':fail('FINAL_ADAPTIVE_PORT_FORBIDDEN')
        if v is not None:fail('BRIDGE_INPUT_INVALID')
        # FA015 reassembles/validates exact latest checkpoint group through
        # owner service reads. Do not build a competing continuation format.
        result=self.base['committed_history'](c,b,v)
        if len(safe_json(result).encode())>1024*1024:fail('BRIDGE_HISTORY_EXCEEDED')
        return result

    def reserve_prefix(self,c,b,v):
        self._guard(c,b);exact(v,{'symbol','index','request_hash'})
        if self.state.inflight_request is not None:fail('BRIDGE_PREFIX_INFLIGHT')
        self.base['reserve_prefix'](c,b,v)
        self.state.inflight_request=v['request_hash']
        return {'reserved':True}

    def commit_stage_artifacts(self,c,b,v):
        self._guard(c,b)
        if type(v)is not list:fail('BRIDGE_INPUT_INVALID')
        return self.base['commit_stage_artifacts'](c,b,v)

    def progress(self,c,b,v):
        self._guard(c,b)
        if type(v) is not dict:fail('BRIDGE_INPUT_INVALID')
        if set(v)=={'stage'}:return self.base['progress'](c,b,v)
        exact(v,{'stage','completed_prefixes','total_prefixes','current_symbol','current_index'})
        return self.service.progress(c,v['stage'],completed_prefixes=v['completed_prefixes'],
            total_prefixes=v['total_prefixes'],current_symbol=v['current_symbol'],current_index=v['current_index'])

    def controller_record(self,c,b,v):
        self.state.terminal_ack_ready=False
        cleanup=self.state.mode=='CLEANUP_ONLY'
        if cleanup:self._cleanup_binding(c,b);self._cleanup_budget(v)
        else:self._guard(c,b)
        if type(v)is not dict:fail('BRIDGE_INPUT_INVALID')
        event=v.get('event');next_container=None;close_helper=False;duplicate=False
        if cleanup and event not in {'HELPER_CLOSED','CONTAINER_STOPPED','FAILED_ATTEMPT_RETAINED_STOPPED','CREATED_INSPECTED_NOT_STARTED'}:
            fail('BRIDGE_CLEANUP_OPERATION_FORBIDDEN')
        if event=='HELPER_CLOSED':
            exact(v,{'event','binding_sha256','runtime_sha256','helpers_closed','containers_closed'})
            if (v['binding_sha256']!=digest(self.state.binding) or v['runtime_sha256']!=self.state.runtime_sha256
                    or v['helpers_closed']is not True or v['containers_closed']is not True
                    or any(item['status']!='STOPPED' for item in self.state.containers.values())):
                fail('BRIDGE_LINUX_CLOSURE_UNPROVEN')
            close_helper=True
        else:
            if event=='CREATED_INSPECTED_NOT_STARTED':keys={'event','request_sha256','image_id','container_id','profile','inspect_sha256'}
            elif event in {'CONTAINER_STOPPED','FAILED_ATTEMPT_RETAINED_STOPPED'}:
                keys={'event','request_sha256','container_id'}|({'stdout_bytes','stderr_bytes'} if event=='CONTAINER_STOPPED' else set())
            else:fail('BRIDGE_CONTROLLER_RECORD_INVALID')
            exact(v,keys)
            if (type(v['container_id'])is not str or not re.fullmatch('[0-9a-f]{64}',v['container_id'])
                    or type(v['request_sha256'])is not str or not re.fullmatch('[0-9a-f]{64}',v['request_sha256'])):
                fail('BRIDGE_CONTROLLER_RECORD_INVALID')
            current=self.state.containers.get(v['container_id'])
            if event=='CREATED_INSPECTED_NOT_STARTED':
                if cleanup and (self.state.cleanup_late_created or not self.state.cleanup_pending_request
                        or v['request_sha256']!=self.state.cleanup_pending_request):fail('BRIDGE_CLEANUP_OPERATION_FORBIDDEN')
                if (current is not None or len(self.state.containers)>=2000 or self.state.image_id is None
                        or v['image_id']!=self.state.image_id or type(v['inspect_sha256'])is not str
                        or not re.fullmatch('[0-9a-f]{64}',v['inspect_sha256']) or v['profile']!='FA012-initial60s-1GiB-rootless'):
                    fail('BRIDGE_CONTROLLER_RECORD_INVALID')
                next_container={'status':'CREATED','request_hash':v['request_sha256']}
            else:
                if (current is None or current['request_hash']!=v['request_sha256']
                        or cleanup and v['container_id'] not in self.state.cleanup_containers):
                    fail('BRIDGE_CONTROLLER_RECORD_INVALID')
                if event=='CONTAINER_STOPPED' and (type(v['stdout_bytes'])is not int or not 0<=v['stdout_bytes']<=MAX_FRAME
                        or type(v['stderr_bytes'])is not int or not 0<=v['stderr_bytes']<=262144):
                    fail('BRIDGE_CONTROLLER_RECORD_INVALID')
                if current['status']=='STOPPED':
                    if current.get('receipt_sha256')!=digest(v):fail('BRIDGE_CONTROLLER_RECORD_INVALID')
                    duplicate=True
                next_container={'status':'STOPPED','request_hash':v['request_sha256'],
                                'terminal_event':event,'receipt_sha256':digest(v)}
        if len(safe_json(v).encode())>16384:fail('BRIDGE_INPUT_INVALID')
        # Persist BEFORE updating in-memory closure. Rejected/fenced/failed DB
        # transactions must not manufacture an acknowledged STOP/HELPER_CLOSED.
        transaction=_cleanup_transaction(self.service,self.state) if cleanup else self.service.database.transaction()
        with transaction as con:
            if cleanup:self._cleanup_attempt(con,c,b)
            else:self.service._attempt(con,c)
            if event!='HELPER_CLOSED':
                rows=con.execute('SELECT symbol,ordinal FROM autonomous_prefixes WHERE attempt_id=? AND request_hash=?',
                                 (b['attempt_id'],v['request_sha256'])).fetchall()
                if len(rows)!=1:fail('BRIDGE_CONTROLLER_RECORD_INVALID')
            if not duplicate:
                self.service._event(con,b['session_id'],'CONTROLLER_RECORD',dict(v,attempt_id=b['attempt_id']))
        if next_container is not None:
            self.state.containers[v['container_id']]=next_container
            if cleanup and event=='CREATED_INSPECTED_NOT_STARTED':
                self.state.cleanup_late_created=True
                self.state.cleanup_containers=self.state.cleanup_containers|{v['container_id']}
            if event in {'CONTAINER_STOPPED','FAILED_ATTEMPT_RETAINED_STOPPED'} and self.state.inflight_request==v['request_sha256']:
                self.state.inflight_request=None
        if close_helper:self.state.linux_closed=True
        self.state.terminal_ack_ready=event in {'HELPER_CLOSED','CONTAINER_STOPPED','FAILED_ATTEMPT_RETAINED_STOPPED'}
        # A late CREATED is evidence of an already-reserved side effect, NEVER
        # an ACK that permits its caller to start the guest after cutoff.
        if cleanup and event=='CREATED_INSPECTED_NOT_STARTED':fail('BRIDGE_CLEANUP_ONLY')
        return {'recorded':True}

    def mapping(self):
        return {name:getattr(self,name) for name in ('active','model_call','export_input','resolve_roles','committed_history','reserve_prefix','commit_stage_artifacts','controller_record','progress')}


def make_bridge_adapter(runtime,service,inputs,broker,deployment,*,image_id):
    from .autonomous_qa_deployment import QABridgeDeployment
    if (type(deployment) not in (BridgeDeployment,QABridgeDeployment)
            or type(image_id) is not str or not re.fullmatch('sha256:[0-9a-f]{64}',image_id)):
        fail('BRIDGE_DEPLOYMENT_INVALID')
    def adapter(context,begun):
        if begun['phase']not in {'development','final_test'}:fail('BRIDGE_INPUT_INVALID')
        if begun['phase']=='final_test' and not inputs.final_supported():fail('AUTONOMOUS_FINAL_UNAVAILABLE')
        from fa_research_executor.bridge import WindowsResearchBridge
        binding={'session_id':begun['session_id'],'attempt_id':begun['attempt_id'],'job_id':context.job_id,
                 'run_id':context.run.run_id,'fencing_token':context.fencing_token}
        state=BridgeState(binding,deployment.runtime_sha256,image_id=image_id)
        with service.database.transaction() as con:
            _,session=service._attempt(con,context)
            remaining=min(begun['reservation']['compute_seconds'],session['deadline']-service.clock())
        if type(deployment) is QABridgeDeployment:
            remaining=min(remaining,deployment.remaining_seconds())
            if remaining<=0:fail('QA_SCOPE_EXPIRED')
        context=DeadlineContext(context,time.monotonic()+remaining)
        if begun['phase']=='development' and begun['spec'].get('saved_candidate_ids'):
            principal=runtime.research_task_service.principal(context.run.owner_id)
            seeds=runtime.research_candidates.context(principal,begun['spec']['saved_candidate_ids'])
            if digest(seeds)!=digest(begun['resolved'].get('candidate_context',[])):fail('RESEARCH_CANDIDATE_HASH_MISMATCH')
        ports=WindowsHostPorts(runtime,service,inputs,broker,state)
        if begun['phase']=='final_test':
            from .autonomous_final import prepare_final
            seal,packages=prepare_final(runtime,service,inputs,context,begun)
            seal_input_reference=begun['spec']['final_test']
            sealed_windows=begun['resolved']['final_windows']
            begun={key:begun[key] for key in ('session_id','attempt_id','phase','candidate_hash','reservation')}
            begun.update(sealed_candidate=seal,
                         spec={'final_test':seal_input_reference},
                         resolved={'final_windows':sealed_windows})
            ports.final_packages=packages;ports.final_seal=seal
        factory=WindowsChannelFactory(runtime,service,deployment,state)
        bridge=WindowsResearchBridge(channel_factory=factory,host_ports=ports.mapping())
        result=(bridge.run_final if begun['phase']=='final_test' else bridge.run_development)(context,begun)
        return Outcome.model_validate(result).model_dump()
    return adapter


def install_windows_bridge(runtime,*,evaluator,deployment,transports=None):
    """Direct sole-integrator entry. All nine host ports/factory are concrete.

    evaluator is FA015's installed descriptor (capabilities/readiness); it is
    not allowed to execute generated source in this Windows process.
    """
    from .autonomous_windows import WindowsInputs,WindowsModelBroker
    from .autonomous_composition import install
    inputs=WindowsInputs(runtime,evaluator);bound={}
    def execute(context,begun):return bound['adapter'](context,begun)
    service=install(runtime,resolve_inputs=inputs,adapter=execute)
    broker=WindowsModelBroker(runtime,service,transports=transports)
    bound['adapter']=make_bridge_adapter(runtime,service,inputs,broker,deployment,image_id=evaluator.image_id)
    runtime.autonomous_bridge=bound['adapter']
    return service
