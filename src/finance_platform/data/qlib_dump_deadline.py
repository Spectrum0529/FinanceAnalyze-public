"""Independent deadline for only the currently held import Job and root."""
import threading
import time
class Deadline:
    def __init__(self,process,job,started,clock=time.monotonic):
        self.process=process;self.job=job;self.remaining=max(0,120-(clock()-started))
        self.cancel=threading.Event();self.thread=threading.Thread(target=self._run,daemon=True)
        self.fired=False;self.root_reaped=False;self.errors=[]
    def start(self):self.thread.start()
    def _run(self):
        if self.cancel.wait(self.remaining):return
        self.fired=True
        try:
            # The same held Job object, never a PID/open/external target.
            self.job.terminate()
            self.process.wait(timeout=10);self.root_reaped=True
        except BaseException:self.errors.append('OWNED_DEADLINE_TERMINATE_OR_REAP_FAILED')
    def stop(self):
        self.cancel.set();self.thread.join(timeout=2)
        if self.thread.is_alive():self.errors.append('OWNED_DEADLINE_JOIN_FAILED')
        return not self.thread.is_alive()
