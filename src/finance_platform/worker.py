"""Independent trusted-handler worker. Upload strategy execution belongs to SDK."""
import logging
import threading
import uuid

from .core.errors import PlatformError
from .core.jobs import WorkerJobContext
from .core.ta_native import StagedTAResult

log = logging.getLogger("finance_platform.worker")


class Worker:
    def __init__(self, runtime, *, worker_id=None, lease_seconds=30):
        self.runtime = runtime
        self.worker_id = worker_id or "worker:" + str(uuid.uuid4())
        self.lease_seconds = lease_seconds

    def run_once(self):
        self.runtime.scheduler.tick()
        claim = self.runtime.jobs.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if claim is None:
            return None
        context = WorkerJobContext(self.runtime.jobs, claim["job_id"], self.worker_id, claim["fencing_token"], claim["run"])
        stopped, lost = threading.Event(), threading.Event()
        def renew():
            while not stopped.wait(max(0.1, self.lease_seconds / 3)):
                try:
                    self.runtime.jobs.heartbeat(context.job_id, self.worker_id, context.fencing_token, lease_seconds=self.lease_seconds)
                except PlatformError:
                    lost.set()
                    break
                except Exception:
                    lost.set()
                    break
        thread = threading.Thread(target=renew, name="platform-lease", daemon=True)
        thread.start()
        result = None
        core_succeeded = False
        try:
            definition = self.runtime.handlers.get(claim["handler"])
            from pathlib import Path
            import hashlib
            actual_dependency_hash = hashlib.sha256((self.runtime.root / "uv.lock").read_bytes()).hexdigest()
            if self.runtime.handlers.current_code_hash(definition.handler) != context.run.code_hash or actual_dependency_hash != context.run.dependency_hash:
                raise PlatformError("CONFLICT", 409)
            result = definition.handler(context, claim["parameters"])
            if lost.is_set():
                raise PlatformError("LEASE_LOST", 409)
            context.require_active()
            if type(result) is StagedTAResult:
                try:
                    result.finish_core(context)
                finally:
                    core_succeeded = result._session.committed
            else:
                self.runtime.jobs.finish(context.job_id, self.worker_id, context.fencing_token, result)
                core_succeeded = True
        except Exception as error:
            code = error.code if isinstance(error, PlatformError) else "HANDLER_FAILED"
            try:
                if core_succeeded:
                    raise PlatformError("TA_CORE_SUCCEEDED_CHECKPOINT_ACK_UNPROVEN")
                self.runtime.jobs.fail(context.job_id, self.worker_id, context.fencing_token, code)
            except PlatformError:
                # Lost owner can never finalize the replacement execution.
                code = "TA_CORE_SUCCEEDED_CHECKPOINT_ACK_UNPROVEN" if core_succeeded else "LEASE_LOST"
            log.error("platform_job_failed", extra={"job_id": context.job_id, "run_id": context.run.run_id, "code": code})
        finally:
            if type(result) is StagedTAResult:
                result.abort()
            stopped.set()
            thread.join(timeout=2)
        return claim["job_id"]

    def run(self, stop: threading.Event, *, poll_seconds=0.5):
        while not stop.is_set():
            if self.run_once() is None:
                stop.wait(poll_seconds)
