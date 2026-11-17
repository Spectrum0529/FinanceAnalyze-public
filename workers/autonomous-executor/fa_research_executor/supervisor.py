"""Independent local EOF/deadline supervision for trusted Linux worker.

Only the pump reads stdin; the RPC decoder consumes its bounded queue. This
avoids racing a watchdog against the framed reader for the same pipe bytes.
"""
import queue
import threading
import time
import math
from .protocol import require, Rejected


class CleanupBudget:
    """One local deadline per binding. Enter/update can only shorten it."""
    def __init__(self,*,clock=time.monotonic):
        self.clock=clock;self.deadline=None;self.business_deadline=None
        self.lock=threading.Lock();self.arm_deadline=None;self.close_timer=None;self.wire_updates=0

    def _tighten_locked(self,bound):
        changed=self.deadline is None or bound<self.deadline
        if changed:
            self.deadline=bound
            if self.arm_deadline is not None:self.arm_deadline(bound)

    @property
    def active(self):
        with self.lock:return self.deadline is not None

    def bind_business_deadline(self,deadline):
        with self.lock:
            self.business_deadline=deadline if self.business_deadline is None else min(self.business_deadline,deadline)
            if self.deadline is not None:self._tighten_locked(self.business_deadline+24)

    def begin(self):
        with self.lock:
            start=self.clock()
            if self.business_deadline is not None:start=min(start,self.business_deadline)
            bound=start+24
            self._tighten_locked(bound)

    enter=begin

    def receive_remaining(self,milliseconds,*,sent_at,processing_elapsed_ms):
        require(type(milliseconds)is int and 0<=milliseconds<=24000,'CLEANUP_BUDGET_INVALID')
        now=self.clock()
        require(type(sent_at)in {int,float} and math.isfinite(sent_at)
                and math.isfinite(now) and 0<=sent_at<=now,'CLEANUP_SEND_ANCHOR_INVALID')
        # Original max600s business plus existing24s cooperative cleanup, not
        # an enlarged budget. The carrier also cannot exceed observed RTT.
        require(type(processing_elapsed_ms)is int
                and 0<=processing_elapsed_ms<=624000
                and processing_elapsed_ms<=math.floor((now-sent_at)*1000),
                'CLEANUP_PROCESSING_ELAPSED_INVALID')
        self.enter()
        # Windows stamps both values at one instant W, after fully receiving
        # this exact request at R. E=floor(W-R), M=floor(D-W), hence
        # local_send+E+M <= peer_deadline after clock-offset alignment.
        # Business processing is not cleanup transit. All transit/queue delay
        # remains deducted; existing local/prior bounds can only shorten this.
        with self.lock:
            bound=sent_at+(processing_elapsed_ms+milliseconds)/1000
            if milliseconds==0:bound=min(bound,now)
            self._tighten_locked(bound);self.wire_updates+=1

    tighten=receive_remaining

    def remaining(self):
        with self.lock:
            require(self.deadline is not None,'CLEANUP_NOT_ENTERED')
            return max(0.,self.deadline-self.clock())

    def check(self):
        if self.active:require(self.remaining()>0,'LINUX_CLEANUP_DEADLINE_EXCEEDED')

    def timeout(self,maximum):
        if not self.active:return maximum
        remaining=self.remaining();require(remaining>0,'LINUX_CLEANUP_DEADLINE_EXCEEDED')
        return min(maximum,remaining)

    def join(self,thread,maximum):
        thread.join(timeout=self.timeout(maximum));self.check()

    def install_kernel_enforcement(self):
        """Absolute Linux timer, default terminating signal; no Python handler.

        This also terminates the current helper during a native call holding
        the GIL. It cannot kill or enumerate another process/container.
        """
        import sys,signal,ctypes
        require(sys.platform=='linux' and self.arm_deadline is None,'CLEANUP_KERNEL_TIMER_INVALID')
        require(signal.getsignal(signal.SIGALRM)==signal.SIG_DFL
                and signal.getitimer(signal.ITIMER_REAL)==(0.,0.),'CLEANUP_ALARM_ALREADY_OWNED')
        signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGALRM})
        class Timespec(ctypes.Structure):_fields_=[('seconds',ctypes.c_long),('nanoseconds',ctypes.c_long)]
        class Spec(ctypes.Structure):_fields_=[('interval',Timespec),('value',Timespec)]
        libc=ctypes.CDLL(None,use_errno=True);timer=ctypes.c_void_p()
        libc.timer_create.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
        libc.timer_create.restype=ctypes.c_int
        libc.timer_settime.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.POINTER(Spec),ctypes.c_void_p]
        libc.timer_settime.restype=ctypes.c_int
        libc.timer_delete.argtypes=[ctypes.c_void_p];libc.timer_delete.restype=ctypes.c_int
        require(libc.timer_create(time.CLOCK_MONOTONIC,None,ctypes.byref(timer))==0,'CLEANUP_KERNEL_TIMER_FAILED')
        def arm(deadline):
            seconds=int(deadline);nanos=int((deadline-seconds)*1000000000)
            spec=Spec(Timespec(0,0),Timespec(seconds,nanos))
            require(libc.timer_settime(timer,1,ctypes.byref(spec),None)==0,'CLEANUP_KERNEL_TIMER_FAILED')
        def close():require(libc.timer_delete(timer)==0,'CLEANUP_KERNEL_TIMER_FAILED')
        with self.lock:
            self.arm_deadline=arm;self.close_timer=close
            if self.deadline is not None:arm(self.deadline)

    def business_expired(self):
        with self.lock:return self.business_deadline is not None and self.clock()>=self.business_deadline

    def snapshot(self):
        with self.lock:
            return {'local_limit_ms':24000,'remaining_ms':None if self.deadline is None else max(0,int((self.deadline-self.clock())*1000)),
                    'wire_updates':self.wire_updates,'kernel_enforced':self.close_timer is not None}

    def finish(self):
        self.check()
        if self.close_timer is not None:self.close_timer();self.close_timer=None


class InputPump:
    def __init__(self, stream,*,cleanup_budget=None):
        self.stream=stream; self.queue=queue.Queue(maxsize=16)
        self.eof=threading.Event(); self.stop=threading.Event(); self.buffer=bytearray()
        self.research_stop=threading.Event()
        self.cleanup_budget=cleanup_budget
        self.deadline=None
        self.thread=threading.Thread(target=self._pump,name='linux-owned-input-pump',daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            while not self.stop.is_set():
                block=self.stream.read1(65536)
                if not block:
                    if self.cleanup_budget is not None:self.cleanup_budget.enter()
                    self.eof.set(); return
                while not self.stop.is_set():
                    try:self.queue.put(block,timeout=.1);break
                    except queue.Full:pass
        except BaseException:
            if self.cleanup_budget is not None:self.cleanup_budget.enter()
            self.eof.set()

    def read(self,count):
        if self.cleanup_budget is not None:self.cleanup_budget.check()
        while not self.buffer:
            require(not self.stop.is_set(),'LINUX_SUPERVISOR_CANCELLED')
            require(self.deadline is None or time.monotonic()<self.deadline,'EXECUTOR_DAEMON_READY_TIMEOUT')
            timeout=.1 if self.cleanup_budget is None else self.cleanup_budget.timeout(.1)
            try:self.buffer.extend(self.queue.get(timeout=timeout))
            except queue.Empty:
                if self.eof.is_set():return b''
            if self.cleanup_budget is not None:self.cleanup_budget.check()
        value=bytes(self.buffer[:count]);del self.buffer[:count]
        return value


def supervised(call, *, pump, controller, seconds,cleanup_budget=None):
    done=threading.Event();box={};started=time.monotonic()
    def execute():
        try:box['value']=call()
        except BaseException as error:box['error']=error
        finally:done.set()
    controller.worker_joined=False
    worker=threading.Thread(target=execute,name='linux-owned-research-worker',daemon=True)
    worker.start();reason=None
    while not done.wait(.05):
        if pump.eof.is_set():reason='EOF';break
        if cleanup_budget is not None:
            cleanup_budget.check()
            if cleanup_budget.business_expired():reason='WALL';break
        if time.monotonic()-started>=seconds:reason='WALL';break
    if reason is not None:
        if cleanup_budget is not None:cleanup_budget.enter()
        pump.research_stop.set();controller.cancelled.set()
        if cleanup_budget is None:worker.join(timeout=3)
        else:cleanup_budget.join(worker,3)
        # Exact persisted names/IDs only. Unknown create is never assumed absent.
        controller.abort_owned()
        if cleanup_budget is None:worker.join(timeout=1)
        else:cleanup_budget.join(worker,1)
        require(not worker.is_alive(),'LINUX_WORKER_CLOSE_UNPROVEN')
        controller.worker_joined=True
        raise Rejected('LINUX_SUPERVISOR_'+reason)
    if cleanup_budget is not None:cleanup_budget.enter();cleanup_budget.join(worker,1)
    else:worker.join(timeout=1)
    require(not worker.is_alive(),'LINUX_WORKER_CLOSE_UNPROVEN')
    controller.worker_joined=True
    if 'error' in box:raise box['error']
    return box['value']
