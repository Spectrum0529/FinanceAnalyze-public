"""Concrete product native supervisor; fixed product bootstrap, not case dispatch.
Numeric containment, private channel and teardown are copied from frozen r04.
ProductSession supplies compact/native/public hash semantics.
"""
from dataclasses import asdict
import hashlib
import queue
import secrets
import subprocess
import threading
import time
from pathlib import Path
from .errors import PlatformError
from .ta_protocol import Channel,encoded,digest,checked
from .ta_process import native_thread_count
from .ta_native import _regular,_disk_budget,_owned_bytecode_prefix
from .ta_product import ProductSession,LIMITS,ProductOwnedJob as TAOwnedJob,fail

# This is a closing opportunity inside the original native wall deadline,
# never permission to continue model/tool work or extend the execution budget.
CANCEL_CLOSE_SECONDS = 5.0

class ProductSessionDriver(ProductSession):
    def _cancel_active(self, started, cancellation):
        try:
            self._active(started)
        except PlatformError as error:
            # require_active verifies the current owner/lease/fence before
            # reporting this exact, persisted user-cancellation condition.
            if error.code != "JOB_CANCELLED" or self.committed:
                raise
            now = time.monotonic()
            if cancellation["deadline"] is None:
                cancellation["deadline"] = min(started + LIMITS.wall_seconds,
                                                now + CANCEL_CLOSE_SECONDS)
            if now >= cancellation["deadline"]:
                cancellation["deadline_exceeded"] = True
                raise
            return False
        if cancellation["deadline"] is not None:
            fail("TA_CANCEL_STATE_CHANGED")
        return True

    def _drive(self):
            process = job = None
            readers = []
            inbox = queue.Queue(maxsize=4)
            io_failed = threading.Event()
            started = time.monotonic()
            final = None
            stderr_count = 0
            admitted = False
            primary = None
            channel = None
            cleaned = False
            cancellation = {"deadline": None, "deadline_exceeded": False}
            observation_ids = set()
            provider_failure = None
            ready_identity = None
            launch_stage = "SOURCE_REVIEW"
            requested_creationflags = subprocess.DETACHED_PROCESS
            early_stderr_started = False
            def stderr():
                nonlocal stderr_count
                try:
                    with (self.attempt/"stderr.bin").open("xb") as stream:
                        while chunk := process.stderr.read(4096):
                            stderr_count += len(chunk)
                            stream.write(chunk[:max(0,LIMITS.stderr_bytes-stream.tell())])
                            if stderr_count > LIMITS.stderr_bytes:
                                io_failed.set()
                                break
                except BaseException:
                    io_failed.set()
            try:
                self._active(started)
                runtime, worker, _ = self.binding.verify()
                files = self.binding.files
                cache=_owned_bytecode_prefix(self.attempt,self.binding.attempt_base,self.context.run.owner_id,self.context.run.run_id,create=True)
                launch_stage = "CREATE_PROCESS"
                process = subprocess.Popen([str(files["base_python"].path), "-I", "-S", "-B", "-X", "utf8", "-X", "pycache_prefix="+str(cache), str(files["bootstrap"].path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=self.attempt, env=self._environment(), close_fds=True, creationflags=requested_creationflags)
                launch_stage = "CHILD_CREATED"
                stderr_thread = threading.Thread(target=stderr, daemon=True)
                readers.append(stderr_thread)
                stderr_thread.start()
                early_stderr_started = True
                job = TAOwnedJob(process)
                launch_stage = "INNER_JOB_ADMISSION"
                job.admit()
                if io_failed.is_set():
                    fail("TA_PIPE_FAILED_OR_LIMIT")
                launch_stage = "INNER_JOB_ADMITTED"
                key = secrets.token_bytes(32)
                packet = {"schema":"ta-product-admission-1", "admission":self.admission, "attempt_root":str(self.attempt),
                    "expected_pid":process.pid, "job_handle":job.duplicate_into(),
                    "worker_project":str(Path(worker["site_root"]).parents[2]), "site_root":worker["site_root"],
                    "venv_prefix":runtime["venv_prefix"], "pins":{name:pin.metadata() for name,pin in self.binding.pins},
                    "source_identity":self.binding.source_identity, "fixtures":self.fixtures, "marker":self.marker,
                    "limits":asdict(LIMITS), "session_key":key.hex()}
                raw = encoded(packet)
                if len(raw) > 524288:
                    fail("TA_BOOTSTRAP_PACKET_LIMIT")
                # Private pipe only. This packet/key is never written to a report.
                channel = Channel(process.stdout, process.stdin, key, side="parent")
                def initial_packet():
                    try:
                        process.stdin.write(b"GO\n" + raw + b"\n")
                        process.stdin.flush()
                    except BaseException:
                        io_failed.set()
                def receive():
                    try:
                        while True:
                            item = channel.receive()
                            inbox.put(item, timeout=1)
                    except EOFError:
                        pass
                    except BaseException:
                        io_failed.set()
                for function in (receive, initial_packet):
                    thread = threading.Thread(target=function, daemon=True)
                    readers.append(thread)
                    thread.start()
                ready = False
                staged_wait = False
                while True:
                    if admitted and self.result is None:
                        self._cancel_active(started, cancellation)
                    else:
                        self._active(started)
                    if self.provider_session is not None and self.result is None and cancellation["deadline"] is None:
                        self.provider_session.meter.remaining()
                    if io_failed.is_set():
                        fail("TA_PIPE_FAILED_OR_LIMIT")
                    if process.poll() is None:
                        running = job.snapshot()
                        if running["pids"] != [process.pid] and process.poll() is None:
                            fail("TA_JOB_MEMBERSHIP_UNPROVEN")
                        if native_thread_count(process.pid) > LIMITS.native_live_threads:
                            fail("TA_NATIVE_THREAD_BUDGET")
                    _disk_budget(self.attempt)
                    if staged_wait:
                        with self.lock:
                            if self.receipt is not None:
                                channel.send("STAGED_OK", self.receipt)
                                staged_wait = False
                    try:
                        kind, value = inbox.get(timeout=.02)
                    except queue.Empty:
                        if process.poll() is not None:
                            break
                        continue
                    if kind == "READY" and not ready:
                        identity = value
                        if (identity.get("actual_pid") != process.pid or identity.get("actual_self_in_held_job") is not True
                                or identity.get("duplicated_job_handle_closed") is not True
                                or Path(identity.get("actual_image", "")) != files["base_python"].path
                                or Path(identity.get("actual_python_dll", "")) != files["base_dll"].path
                                or identity.get("actual_python_dll_sha256") != files["base_dll"].sha256
                                or identity.get("source_identity") != self.binding.source_identity
                                or identity.get("sys_executable") != str(files["base_python"].path)
                                or identity.get("site_root") != worker["site_root"]
                                or identity.get("prefix") != runtime["venv_prefix"]
                                or identity.get("base_prefix") != str(files["base_python"].path.parent)
                                or identity.get("version") != [3,12,14]
                                or identity.get("automatic_site_startup") is not False or identity.get("native_imported") is not False):
                            fail("TA_READY_IDENTITY_MISMATCH")
                        self.binding.review(self.context, self.admission["case"], self.attempt)
                        if any(identity.get(key) is not True for key in ("owned_bytecode_prefix_verified","owned_bytecode_prefix_initially_empty","dont_write_bytecode")):fail("TA_BYTECODE_PREFIX_UNPROVEN")
                        ready_identity = {**identity,"held_job_before_native":job.admit()}
                        with (self.attempt/"ready-identity.json").open("xb") as stream:
                            stream.write(encoded(ready_identity))
                        channel.send("READY_OK", {"admission_hash":digest(self.admission)})
                        ready = admitted = True
                    elif not ready:
                        fail("TA_ADMISSION_ORDER_INVALID")
                    elif kind in {"ACTIVE", "VERIFY"} and self.result is None:
                        if value != {"admission_hash":digest(self.admission)}:
                            fail("TA_CURRENT_BINDING_MISMATCH")
                        active = self._cancel_active(started, cancellation)
                        if kind == "VERIFY" and active:
                            self.binding.review(self.context, self.admission["case"], self.attempt)
                            active = self._cancel_active(started, cancellation)
                        channel.send(kind+"_OK", {"active":True} if active else
                                     {"active":False, "reason_code":"JOB_CANCELLED"})
                    elif kind == "PROVIDER_MODEL" and self.result is None and self.failure_record is None:
                        if self.provider_session is None:
                            fail("TA_EXTERNAL_EXECUTION_APPROVAL_REQUIRED")
                        self._active(started)
                        self.provider_session.verify_execution(value,self.admission["binding"])
                        reply=self.provider_session.invoke(value["request"])
                        provider_failure=reply["error_code"] or provider_failure
                        channel.send("PROVIDER_MODEL_OK",reply)
                    elif kind == 'MODEL_OBSERVATION' and self.result is None and self.failure_record is None:
                        from .model_usage import retain_completed_ta_observation
                        snapshot=self.admission['request']['input_bundle'].get('team_snapshot')
                        recorded=retain_completed_ta_observation(self.context,value,snapshot=snapshot,seen=observation_ids)
                        channel.send('MODEL_OBSERVATION_OK',{'recorded':recorded})
                    elif kind == "FAILURE" and self.result is None:
                        from .ta_protocol import optional_trace_payload
                        value=optional_trace_payload(value,524288)
                        if type(value) is not dict or len(encoded(value)) > 524288 or len(value.get("trace", [])) > LIMITS.trace_rows:
                            fail("TA_FAILURE_RECORD_INVALID")
                        if self.provider_session is not None:
                            self._provider_counts(value.get("consumed"))
                            spent=value["consumed"]
                            value["consumed"]={**spent,"wall_millis":max(spent["wall_millis"],self.provider_session.meter.wall_millis())}
                        from .model_usage import retain_trace
                        retain_trace(self.context,value.get('trace',[]),source='FAILURE')
                        with (self.attempt/"native-failure.json").open("xb") as stream:
                            stream.write(encoded(value))
                        self.failure_record=checked(encoded(value))
                        channel.send("FAILURE_OK", {"preserved":True})
                    elif kind == "STAGED" and self.result is None:
                        self._active(started)
                        self.result = self._validate_staged(value)
                        with (self.attempt/"staged-evidence.json").open("xb") as stream:
                            stream.write(encoded(value))
                        staged_wait = True
                        self.staged.set()
                    elif kind == "CLEANED" and self.committed and not staged_wait:
                        if value != {"result_hash":self.receipt["result_hash"], "checkpoint_confirmed":True}:
                            fail("TA_CHECKPOINT_ACK_INVALID")
                        cleaned = True
                        channel.send("CLEANED_OK", {"accepted":True})
                    else:
                        fail("TA_CONTROL_PROTOCOL_ORDER_INVALID")
                process.wait(timeout=2)
                for thread in readers:
                    thread.join(timeout=2)
                if cancellation["deadline"] is not None:
                    fail("JOB_CANCELLED")
                if process.returncode != 0 or not cleaned or not self.committed or any(t.is_alive() for t in readers) or io_failed.is_set():
                    fail(provider_failure or "TA_CHILD_COMPLETION_UNPROVEN")
                final = job.empty()
                _disk_budget(self.attempt)
                self.acknowledged = True
            except BaseException as error:
                primary = getattr(error,"code",None) or "TA_HOST_EXECUTION_FAILED"
                self.failure = primary
            finally:
                cleanup_failed = False
                cleanup_errors = []
                def cleanup_error(phase, error=None):
                    nonlocal cleanup_failed
                    cleanup_failed = True
                    if len(cleanup_errors) < 16:
                        cleanup_errors.append({"phase": phase,
                            "code": error.code if isinstance(error, PlatformError) else "TA_CLEANUP_OPERATION_FAILED",
                            "error_type": None if error is None else type(error).__name__})
                if job is not None:
                    # Terminate this held Job on every failed attempt, even if
                    # the root exited; no PID/outer-Job inference is used.
                    try:
                        if not self.acknowledged:
                            job.terminate()
                    except BaseException as error:
                        cleanup_error("TERMINATE_HELD_JOB", error)
                    try:
                        process.wait(timeout=5)
                    except BaseException as error:
                        cleanup_error("REAP_CHILD", error)
                    try:
                        final = job.empty() if self.acknowledged else {**job.cleanup_empty(), "profile": LIMITS.profile}
                    except BaseException as error:
                        cleanup_error("PROVE_HELD_JOB_EMPTY", error)
                    try:
                        job.close()
                    except BaseException as error:
                        cleanup_error("CLOSE_HELD_JOB", error)
                elif process is not None:
                    try:
                        if process.poll() is None:
                            process.kill()
                    except BaseException as error:
                        cleanup_error("KILL_UNADMITTED_CHILD", error)
                    try:
                        process.wait(timeout=5)
                    except BaseException as error:
                        cleanup_error("REAP_UNADMITTED_CHILD", error)
                if process is not None:
                    for name, pipe in (("STDIN", process.stdin), ("STDOUT", process.stdout), ("STDERR", process.stderr)):
                        try:
                            pipe.close()
                        except BaseException as error:
                            cleanup_error("CLOSE_" + name, error)
                for thread in readers:
                    try:
                        if thread.ident is not None:
                            thread.join(timeout=2)
                        if thread.is_alive():
                            cleanup_error("JOIN_IO_THREAD")
                    except BaseException as error:
                        cleanup_error("JOIN_IO_THREAD", error)
                if cleanup_failed:
                    self.failure = "TA_OWNED_CLEANUP_UNPROVEN"
                    self.acknowledged = False
                try:
                    # Newly owned checkpoint files remain local. Hash only after
                    # owned child has been reaped and final Job emptiness checked.
                    checkpoint = []
                    directory = self.attempt/"cache/checkpoints"
                    if final and not cleanup_failed and directory.is_dir():
                        for path in directory.iterdir():
                            if path.name not in {self.admission["binding"]["ticker"]+suffix for suffix in (".db",".db-wal",".db-shm",".db-journal")}:
                                fail("TA_CHECKPOINT_FILE_UNEXPECTED")
                            checked_path = _regular(self.attempt, path.relative_to(self.attempt).as_posix())
                            raw = checked_path.read_bytes()
                            checkpoint.append({"path":str(path.relative_to(self.attempt)),"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()})
                    evidence = {"primary_code":primary,"final_code":self.failure,"admitted":admitted,
                        "core_committed":self.committed,"checkpoint_ack_confirmed":self.acknowledged,
                        "child_reaped":process is not None and process.poll() is not None,
                        "exit_code":None if process is None else process.returncode,"held_job_final":final,
                        "stderr_bytes":stderr_count,"protocol_tx":0 if channel is None else channel.tx,
                        "protocol_rx":0 if channel is None else channel.rx,"cleanup_failed":bool(cleanup_failed),
                        "checkpoint_files":checkpoint,"source_identity":self.binding.source_identity,
                        "profile":asdict(LIMITS),"candidate_is_main":False}
                    evidence["ready_identity"] = ready_identity
                    evidence["launch_control"] = {
                        "stage": launch_stage, "requested_creationflags": requested_creationflags,
                        "create_no_window_requested": bool(requested_creationflags & subprocess.CREATE_NO_WINDOW),
                        "detached_process_requested": bool(requested_creationflags & subprocess.DETACHED_PROCESS),
                        "close_fds_requested": True,
                        "direct_child_pid": None if process is None else process.pid,
                        "inner_job_acquired": job is not None,
                        "early_stderr_started": early_stderr_started,
                        "last_inner_limits": None if job is None else job.last_limits,
                        "last_inner_snapshot": None if job is None else job.last_observation,
                        "inner_budget_rejection": None if job is None else job.last_budget_rejection,
                        "member_source_attribution": "DIRECT_CHILD_PID_ONLY_OTHERS_UNVERIFIED",
                    }
                    evidence["cleanup_errors"] = cleanup_errors
                    evidence["cancel_close"] = {
                        "requested": cancellation["deadline"] is not None,
                        "maximum_seconds": CANCEL_CLOSE_SECONDS,
                        "inside_original_wall_deadline": True,
                        "deadline_exceeded": cancellation["deadline_exceeded"],
                        "failure_record_received": self.failure_record is not None,
                    }
                    with (self.attempt/"parent-outcome.json").open("xb") as stream:
                        stream.write(encoded(evidence))
                except BaseException:
                    self.failure = "TA_EVIDENCE_SEAL_FAILED"
                    self.acknowledged = False
                if self.resume_store is not None and self.failure is not None and not self.committed:
                    try:
                        self.resume_store.capture_closed(self)
                    except BaseException:
                        # Resume evidence failure never overwrites original
                        # failure/cleanup facts or grants checkpoint availability.
                        try:
                            with (self.attempt/"evidence/resume-freeze-unavailable.json").open("xb") as stream:
                                stream.write(encoded({"available":False,"reason_code":"NO_VERIFIED_FAILURE_CHECKPOINT"}))
                        except BaseException:
                            pass
                self.staged.set()
                self.done.set()
