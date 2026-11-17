"""Finite TA parent host. No native run is authorized by importing this module.

Only server-created TABinding/real WorkerJobContext are accepted. A staged
Mapping owns its live child until real JobService.finish commits and is read
back. Other research cases, HTTP and checkpoint retry remain fail closed.
"""
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import time
import uuid

from .errors import PlatformError
from .jobs import JobService, WorkerJobContext
from .qlib_native import FilePin, _regular, _protect
from .ta_process import LIMITS, TAOwnedJob, native_thread_count
from .ta_protocol import Channel, checked, digest, encoded
from .ta_identity import verify_source

CASES = frozenset({"one_round", "two_round"})
PIN_NAMES = frozenset({"worker_binding", "manifest", "lock", "runtime", "worker_python",
    "venv_config", "base_python", "base_dll", "wheel", "import_receipt", "source_receipt",
    "bootstrap", "identity", "protocol", "guard", "child", "payload", "workflow", "sources",
    "security_receipt", "coordinator_scope"})


def fail(code):
    raise PlatformError(code)


def _disk_budget(root):
    """TA has no Qlib provider exemption; count every new own file."""
    pending, nodes, files, total = [root], 0, 0, 0
    while pending:
        for path in pending.pop().iterdir():
            nodes += 1
            if nodes > 8192:
                fail("TA_ARTIFACT_COUNT_BUDGET")
            try:
                relative = path.relative_to(root).as_posix()
                if path.is_dir():
                    pending.append(_regular(root,relative,directory=True))
                else:
                    target = _regular(root,relative)
                    total += target.stat().st_size
                    files += 1
                    if files > LIMITS.artifact_files or total > LIMITS.artifact_bytes:
                        fail("TA_ARTIFACT_BUDGET")
            except FileNotFoundError:
                continue
    return {"files":files,"bytes":total}


def load_pure(pin, name):
    pin.read_verified()
    spec = importlib.util.spec_from_file_location(name, pin.path)
    module = importlib.util.module_from_spec(spec)
    # Dataclasses and pydantic resolve the exact fixed module name.
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class TABinding:
    pins: tuple
    source_identity: str
    attempt_base: Path

    def __post_init__(self):
        if (type(self.pins) is not tuple or any(type(pin) is not FilePin for _, pin in self.pins)
                or len(self.pins) != len(PIN_NAMES) or {name for name, _ in self.pins} != PIN_NAMES
                or len(self.source_identity) != 64 or any(c not in "0123456789abcdef" for c in self.source_identity)
                or not Path(self.attempt_base).is_absolute()):
            fail("TA_BINDING_INVALID")

    @property
    def files(self):
        return dict(self.pins)

    def verify(self):
        files = self.files
        for pin in files.values():
            pin.read_verified()
        runtime = checked(files["runtime"].read_verified())
        worker = checked(files["worker_binding"].read_verified())
        for key, runtime_key in (("worker_python", "python"), ("base_python", "base_python"),
                                 ("base_dll", "actual_loaded_python_DLL"), ("venv_config", "venv_config")):
            if runtime[runtime_key] != files[key].metadata():
                fail("TA_RUNTIME_PIN_GRAPH_MISMATCH")
        for key, worker_key in (("worker_python", "python"), ("lock", "lock"), ("manifest", "manifest"),
                                ("wheel", "wheel"), ("runtime", "runtime_identity")):
            if worker[worker_key] != files[key].metadata():
                fail("TA_WORKER_PIN_GRAPH_MISMATCH")
        if (runtime["version"] != [3, 12, 14] or runtime["platform"] != "win32"
                or Path(worker["site_root"]) != Path(runtime["venv_prefix"])/"Lib/site-packages"
                or files["lock"].sha256 != "35cfc278c965e120749a60e8f7197c7a00c2e7105392130078e93326484e4950"
                or files["wheel"].sha256 != "44ff72729b717a2f4098f75e5b96d6339b017f073e0f77e0b397bc51d5762dec"
                or files["import_receipt"].sha256 != "859743803a83d090be05e58641cf14f0698307c9b6a79d0419167b71204c75f6"):
            fail("TA_EXACT_DEDICATED_RUNTIME_REQUIRED")
        sources = checked(files["sources"].read_verified())
        if digest({"rows":sources["rows"], "guard_sources":sources["guard_sources"]}) != self.source_identity:
            fail("TA_CURRENT_SOURCE_IDENTITY_MISMATCH")
        for row in sources["rows"]:
            verify_source(row,worker["site_root"])
        return runtime, worker, sources

    def review(self, context, case, attempt):
        self.verify()
        security = checked(self.files["security_receipt"].read_verified())
        scope = checked(self.files["coordinator_scope"].read_verified())
        expected = {"source_identity": self.source_identity, "profile": LIMITS.profile,
                    "case": case, "owner_id": context.run.owner_id, "run_id": context.run.run_id,
                    "job_id": context.job_id, "fencing_token": context.fencing_token,
                    "attempt_root": str(attempt)}
        # These documents are actual immutable reviewer/coordinator outputs,
        # supplied by trusted composition. There is no API GO flag or auto-GO.
        if (security.get("decision") != "GO" or security.get("reviewer") != "FA012-R03"
                or security.get("binding") != expected or scope.get("decision") != "RELEASE"
                or scope.get("coordinator") != "FA001-R03" or scope.get("binding") != expected
                or scope.get("security_receipt_sha256") != self.files["security_receipt"].sha256):
            fail("TA_CURRENT_EXACT_CASE_RELEASE_REQUIRED")


def require_real_context(context):
    if type(context) is not WorkerJobContext or type(context.service) is not JobService:
        fail("TA_REAL_CORE_CONTEXT_REQUIRED")


def committed_receipt(context, result):
    """Read the actual committed core row AND success event; never invent success."""
    require_real_context(context)
    with closing(context.service.database.connect()) as con:
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (context.job_id,)).fetchone()
        run = con.execute("SELECT owner_id,run_id FROM runs WHERE run_id=?", (context.run.run_id,)).fetchone()
        event = con.execute("SELECT metadata_json FROM job_events WHERE job_id=? AND run_id=? AND kind='SUCCEEDED' ORDER BY event_id DESC LIMIT 1",
                            (context.job_id, context.run.run_id)).fetchone()
    if (row is None or run is None or event is None or row["status"] != "SUCCEEDED"
            or row["cancel_requested"] != 0 or row["owner_id"] != context.run.owner_id
            or row["run_id"] != context.run.run_id or row["fencing_token"] != context.fencing_token
            or row["lease_owner"] is not None or row["lease_until"] is not None
            or run["owner_id"] != context.run.owner_id or run["run_id"] != context.run.run_id
            or checked(row["result_json"]) != result
            or checked(event["metadata_json"]).get("result_hash") != hashlib.sha256(row["result_json"].encode()).hexdigest()):
        fail("TA_COMMITTED_CORE_SUCCESS_UNPROVEN")
    return {"status": "SUCCEEDED", "owner_id": context.run.owner_id, "run_id": context.run.run_id,
            "job_id": context.job_id, "fencing_token": context.fencing_token, "result_hash": digest(result)}


class StagedTAResult(Mapping):
    """Worker-only transport. Mutation cannot change what the child staged."""
    def __init__(self, session, result):
        self._session = session
        self._raw = encoded(result)

    def __iter__(self):
        return iter(checked(self._raw))

    def __len__(self):
        return len(checked(self._raw))

    def __getitem__(self, key):
        return checked(self._raw)[key]

    def finish_core(self, context):
        return self._session.finish_core(context, checked(self._raw))

    def abort(self):
        self._session.abort()


@dataclass(frozen=True)
class TANativeHost:
    binding: TABinding

    def handle(self, context, parameters):
        """Explicit trusted harness registration only; no default/API enablement."""
        if type(parameters) is not dict or set(parameters) != {"case"}:
            fail("TA_FINITE_CASE_PARAMETERS_REQUIRED")
        return self.execute_case(context, parameters["case"])

    def attempt_path(self, context):
        owner = hashlib.sha256(context.run.owner_id.encode()).hexdigest()
        # Run IDs are strict core identifiers; reject separators before joining.
        run = context.run.run_id
        if (type(run) is not str or not run or len(run)>128 or not run[0].isalnum()
                or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for c in run)):
            fail("TA_RUN_NAMESPACE_INVALID")
        return Path(self.binding.attempt_base).absolute()/owner/run

    def execute_case(self, context, case):
        require_real_context(context)
        if type(case) is not str or case not in CASES:
            fail("TA_CASE_NOT_RELEASED")
        context.require_active()
        attempt = self.attempt_path(context)
        self.binding.review(context, case, attempt)
        if os.name != "nt":
            fail("TA_WINDOWS_TARGET_REQUIRED")
        parent = attempt.parent
        parent.mkdir(parents=True, exist_ok=True)
        _regular(Path(self.binding.attempt_base), parent.name, directory=True)
        _protect(parent)
        attempt.mkdir(exist_ok=False)
        _protect(attempt)
        for relative in ("evidence", "home/AppData/Roaming", "home/AppData/Local", "tmp"):
            (attempt/relative).mkdir(parents=True, exist_ok=False)
        fixtures = []
        for name in (".env", ".env.enterprise"):
            path = attempt/name
            with path.open("xb"):
                pass
            info = path.lstat()
            fixtures.append({"filename": name, "path": str(path), "bytes": 0,
                             "sha256": hashlib.sha256(b"").hexdigest(), "st_dev": info.st_dev,
                             "st_ino": info.st_ino, "st_nlink": info.st_nlink})
        payload = load_pure(self.binding.files["payload"], "fa013_ta_binding_payload")
        workflow = payload.load_workflow()
        value = payload.case_binding(case, workflow, owner_id=context.run.owner_id,
            logical_task_id=context.run.run_id, run_id=context.run.run_id, job_id=context.job_id,
            fencing_token=context.fencing_token)
        proof = {"complete_imports": list(workflow.IMPORTS), "commit":workflow.ENGINE["commit"],
            "wheel_sha256":self.binding.files["wheel"].sha256,
            "worker_lock_sha256":self.binding.files["lock"].sha256,
            "fixture_sha256":value["model_profile"]["fixture_sha256"],
            "import_receipt_sha256": self.binding.files["import_receipt"].sha256,
            "security_receipt_sha256": self.binding.files["security_receipt"].sha256,
            "source_manifest_sha256": self.binding.files["sources"].sha256}
        admission = {"case": case, "binding": value, "attempt_root": str(attempt),
                     "proof": proof, "resume": None, "expected_state_hash": None}
        marker = {"owner_id": value["owner_id"], "run_id": value["run_id"], "job_id": value["job_id"],
                  "fencing_token": value["fencing_token"], "case": case,
                  "admission_hash": digest(admission), "source_identity": self.binding.source_identity,
                  "session_id": uuid.uuid4().hex}
        with (attempt/"owner-marker.json").open("xb") as stream:
            stream.write(encoded(marker))
        session = _TASession(self.binding, context, admission, fixtures, marker)
        session.start()
        return session.wait_staged()


class _TASession:
    def __init__(self, binding, context, admission, fixtures, marker):
        self.binding, self.context, self.admission = binding, context, admission
        self.attempt, self.fixtures, self.marker = Path(admission["attempt_root"]), fixtures, marker
        self.lock = threading.RLock()
        self.staged, self.done, self.cancelled = threading.Event(), threading.Event(), threading.Event()
        self.result = self.receipt = self.failure = None
        self.committed = self.acknowledged = False
        self.thread = threading.Thread(target=self._drive, name="ta-owned-host", daemon=True)

    def start(self):
        self.thread.start()

    def wait_staged(self):
        self.staged.wait(LIMITS.wall_seconds + 15)
        if self.result is None or self.failure is not None:
            self.abort()
            fail(self.failure or "TA_HOST_NO_STAGED_RESULT")
        return StagedTAResult(self, self.result)

    def finish_core(self, context, result):
        # This lock spans finish+readback, so the polling thread cannot mistake
        # the successful lease removal for a lost RUNNING execution.
        require_real_context(context)
        with self.lock:
            if (context is not self.context or self.result is None or self.receipt is not None
                    or self.failure is not None or self.cancelled.is_set() or result != self.result):
                fail("TA_STAGED_CORE_BINDING_MISMATCH")
            context.require_active()
            context.service.finish(context.job_id, context.worker_id, context.fencing_token, result)
            # Set even if the verification fails: never try to fail a job whose
            # transaction has already committed; retain checkpoint in that case.
            self.committed = True
            self.receipt = committed_receipt(context, result)
        if not self.done.wait(LIMITS.wall_seconds + 15) or self.failure is not None or not self.acknowledged:
            self.abort()
            fail("TA_CORE_SUCCEEDED_CHECKPOINT_ACK_UNPROVEN")
        return self.receipt

    def abort(self):
        self.cancelled.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=10)

    def _active(self, started):
        with self.lock:
            if self.cancelled.is_set():
                fail("TA_SESSION_ABORTED")
            if time.monotonic()-started >= LIMITS.wall_seconds:
                fail("TA_NATIVE_TIMEOUT")
            if not self.committed:
                self.context.require_active()

    def _validate_staged(self, value):
        if type(value) is not dict or set(value) != {"result", "trace", "state_hash", "effects"}:
            fail("TA_STAGED_SCHEMA_INVALID")
        result, binding = value["result"], self.admission["binding"]
        if (type(result) is not dict or len(encoded(result)) > LIMITS.result_bytes
                or result.get("family") != "tradingagents-native-report-1" or result.get("scope") != "OFFLINE_FIXTURE"
                or result.get("publication_gate") != "CORE_JOB_SUCCEEDED_REQUIRED"
                or result.get("checkpoint_state") != "RETAINED_UNTIL_CORE_JOB_SUCCEEDED_ACK"
                or any(result.get(key) != binding[key] for key in ("owner_id", "run_id", "job_id", "fencing_token",
                        "logical_task_id", "data_reference", "snapshot_content_hash", "selected_view_hash", "model_profile", "tools_manifest_hash"))
                or type(value["trace"]) is not list or len(value["trace"]) > LIMITS.trace_rows
                or digest(result["native_state"]) != value["state_hash"]):
            fail("TA_STAGED_BINDING_INVALID")
        effects = value["effects"]
        if (effects.get("unexpected") != 0 or effects.get("expected_ipv6") != 1
                or effects.get("expected_windows_refusals") not in (0,3)
                or not 0 <= effects.get("threads_started", -1) <= LIMITS.python_thread_starts
                or not 1 <= effects.get("sqlite_connects", -1) <= 8):
            fail("TA_EFFECTS_UNPROVEN")
        from .ta_team_admission import fallback_accounting
        if not fallback_accounting([None]*effects['expected_windows_refusals'],effects.get('windows_fallback')):
            fail("TA_EFFECTS_UNPROVEN")
        budget = result.get("budget", {})
        for key, cap in (("model",32),("tool",16),("chars",160000),("wall_millis",120000)):
            if type(budget.get(key)) is not int or not 0 <= budget[key] <= cap:
                fail("TA_BUDGET_RESULT_INVALID")
        from .model_usage import retain_trace
        retain_trace(self.context,value['trace'],source='STAGED')
        return result

    def _environment(self):
        root = self.attempt
        # New explicit environment; no inherited environment iteration/lookup.
        return {"SystemRoot":"C:\\Windows", "WINDIR":"C:\\Windows", "OS":"Windows_NT",
                "COMSPEC":"C:\\Windows\\System32\\cmd.exe", "PATH":"C:\\Windows\\System32",
                "NUMBER_OF_PROCESSORS":"1", "HOME":str(root/"home"), "USERPROFILE":str(root/"home"),
                "APPDATA":str(root/"home/AppData/Roaming"), "LOCALAPPDATA":str(root/"home/AppData/Local"),
                "TEMP":str(root/"tmp"), "TMP":str(root/"tmp"), "PYTHONNOUSERSITE":"1",
                "PYTHONDONTWRITEBYTECODE":"1", "PYTHON_DOTENV_DISABLED":"1", "LANGSMITH_TRACING":"false",
                "LANGCHAIN_TRACING_V2":"false", "OTEL_SDK_DISABLED":"true", "OMP_NUM_THREADS":"1",
                "OPENBLAS_NUM_THREADS":"1", "MKL_NUM_THREADS":"1", "NUMEXPR_NUM_THREADS":"1"}

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
        ready_identity = None
        try:
            self._active(started)
            runtime, worker, _ = self.binding.verify()
            files = self.binding.files
            process = subprocess.Popen([str(files["base_python"].path), "-I", "-S", "-B", "-X", "utf8", str(files["bootstrap"].path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.attempt, env=self._environment(), close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
            job = TAOwnedJob(process)
            job.admit()
            key = secrets.token_bytes(32)
            packet = {"schema":"ta-admission-1", "admission":self.admission, "attempt_root":str(self.attempt),
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
            for function in (receive, stderr, initial_packet):
                thread = threading.Thread(target=function, daemon=True)
                readers.append(thread)
                thread.start()
            ready = False
            staged_wait = False
            while True:
                self._active(started)
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
                    if kind == "VERIFY":
                        self.binding.review(self.context, self.admission["case"], self.attempt)
                    self._active(started)
                    channel.send(kind+"_OK", {"active":True})
                elif kind == "FAILURE" and self.result is None:
                    from .ta_protocol import optional_trace_payload
                    value=optional_trace_payload(value,524288)
                    if type(value) is not dict or len(encoded(value)) > 524288 or len(value.get("trace", [])) > LIMITS.trace_rows:
                        fail("TA_FAILURE_RECORD_INVALID")
                    from .model_usage import retain_trace
                    retain_trace(self.context,value.get('trace',[]),source='FAILURE')
                    with (self.attempt/"native-failure.json").open("xb") as stream:
                        stream.write(encoded(value))
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
            if process.returncode != 0 or not cleaned or not self.committed or any(t.is_alive() for t in readers) or io_failed.is_set():
                fail("TA_CHILD_COMPLETION_UNPROVEN")
            final = job.empty()
            _disk_budget(self.attempt)
            self.acknowledged = True
        except BaseException as error:
            primary = error.code if isinstance(error, PlatformError) else "TA_HOST_EXECUTION_FAILED"
            self.failure = primary
        finally:
            cleanup_failed = False
            if job is not None:
                try:
                    if process.poll() is None:
                        job.terminate()
                    process.wait(timeout=5)
                    final = job.empty()
                except BaseException:
                    cleanup_failed = True
                finally:
                    job.close()
            elif process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                except BaseException:
                    cleanup_failed = True
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    try:
                        pipe.close()
                    except BaseException:
                        cleanup_failed = True
            for thread in readers:
                if thread.ident is not None:
                    thread.join(timeout=2)
                cleanup_failed |= thread.is_alive()
            if cleanup_failed:
                self.failure = "TA_OWNED_CLEANUP_UNPROVEN"
                self.acknowledged = False
            try:
                # Newly owned checkpoint files remain local. Hash only after
                # owned child has been reaped and final Job emptiness checked.
                checkpoint = []
                directory = self.attempt/"cache/checkpoints"
                if final and directory.is_dir():
                    for path in directory.iterdir():
                        if path.name not in {"SYNTHA.db", "SYNTHA.db-wal", "SYNTHA.db-shm", "SYNTHA.db-journal"}:
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
                with (self.attempt/"parent-outcome.json").open("xb") as stream:
                    stream.write(encoded(evidence))
            except BaseException:
                self.failure = "TA_EVIDENCE_SEAL_FAILED"
                self.acknowledged = False
            self.staged.set()
            self.done.set()


def _owned_bytecode_prefix(attempt,attempt_base,owner_id,run_id,*,create):
    import stat
    root=Path(attempt);base=Path(attempt_base)
    if (type(create)is not bool or not root.is_absolute() or not base.is_absolute() or ".."in root.parts or ".."in base.parts
            or type(owner_id)is not str or not owner_id or type(run_id)is not str or not run_id or len(run_id)>128 or not run_id[0].isalnum()
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in run_id)
            or root!=base/hashlib.sha256(owner_id.encode()).hexdigest()/run_id):
        raise RuntimeError("TA_BYTECODE_PREFIX_OWNER_RUN_INVALID")
    parent=root/"tmp"
    for item in reversed((parent,*parent.parents)):
        info=item.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
            raise RuntimeError("TA_BYTECODE_PREFIX_ALIAS_OR_NON_DIRECTORY")
    prefix=parent/"python-bytecode"
    if create:prefix.mkdir(exist_ok=False)
    info=prefix.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400 or any(prefix.iterdir()):
        raise RuntimeError("TA_BYTECODE_PREFIX_NOT_FRESH_EMPTY_DIRECTORY")
    return prefix
