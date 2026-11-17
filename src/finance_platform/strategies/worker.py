"""Bounded trusted-only worker; Windows Job Object is a resource limit, not a sandbox."""

from dataclasses import dataclass
from datetime import timedelta
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from finance_platform.core.contracts import StrategyContext, StrategyOutput, OutputKind, payload
from .contracts import StrategyError, canonical, digest, number, utc, validate_output


@dataclass(frozen=True)
class WorkerLimits:
    seconds: float = 5
    memory_bytes: int = 128 * 1024 * 1024
    output_bytes: int = 256_000

    def __post_init__(self):
        if not 0.1 <= self.seconds <= 60 or not 32*1024*1024 <= self.memory_bytes <= 512*1024*1024 or not 1024 <= self.output_bytes <= 256_000:
            raise StrategyError("WORKER_LIMITS_INVALID")


class _WindowsJob:
    def __init__(self, process, memory):
        # KILL_ON_JOB_CLOSE, JOB_MEMORY, ACTIVE_PROCESS=1. No child process fan-out.
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(x, ctypes.c_uint64) for x in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x200 | 0x8
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        limits.JobMemoryLimit = memory
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            if self.handle:
                self.kernel.CloseHandle(self.handle)
            raise StrategyError("WORKER_RESOURCE_LIMIT_UNAVAILABLE")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def bounded_process(argv, request, limits, work_root, *, active_guard=None):
    if os.name != "nt":
        raise StrategyError("TRUSTED_WORKER_TARGET_ENV_UNVERIFIED")
    encoded = canonical(request)
    if len(encoded) > 256_000:
        raise StrategyError("WORKER_INPUT_TOO_LARGE")
    # Keep only the runtime's Windows requirement; do not inherit PATH, user home or secrets.
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"), "PYTHONUTF8": "1"}
    Path(work_root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="finance-strategy-", dir=work_root) as directory:
        env.update({"TEMP": directory, "TMP": directory})
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=directory, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        job = None
        exceeded = threading.Event()
        buffers = [bytearray(), bytearray()]

        def drain(pipe, target):
            while chunk := pipe.read(4096):
                if len(target) + len(chunk) > limits.output_bytes:
                    exceeded.set()
                    process.kill()
                    break
                target.extend(chunk)
            pipe.close()

        try:
            job = _WindowsJob(process, limits.memory_bytes)
            threads = [threading.Thread(target=drain, args=(pipe, buf), daemon=True)
                       for pipe, buf in zip((process.stdout, process.stderr), buffers)]
            for thread in threads:
                thread.start()
            # Input is small, but a plugin may never consume it; writer is also bounded by wall time.
            def write():
                try:
                    process.stdin.write(encoded)
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            writer = threading.Thread(target=write, daemon=True)
            writer.start()
            deadline = time.monotonic() + limits.seconds
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StrategyError("PLUGIN_TIMEOUT")
                try:
                    process.wait(timeout=min(remaining, .2) if active_guard else remaining)
                except subprocess.TimeoutExpired:
                    if active_guard:
                        active_guard()
            for thread in threads:
                thread.join(timeout=1)
            writer.join(timeout=1)
            if exceeded.is_set():
                raise StrategyError("PLUGIN_OUTPUT_TOO_LARGE")
            if process.returncode != 0 or buffers[1]:
                raise StrategyError("PLUGIN_EXECUTION_FAILED")
            try:
                return json.loads(buffers[0])
            except (UnicodeError, json.JSONDecodeError):
                raise StrategyError("PLUGIN_RESPONSE_INVALID") from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if job:
                job.close()


class TrustedWorker:
    def __init__(self, packages, limits=WorkerLimits(), *, active_guard=None):
        self.packages, self.limits = packages, limits
        self.active_guard = active_guard

    def execute(self, package_hash, context: StrategyContext, *, event_type="bar", trusted=False):
        if not trusted:
            raise StrategyError("UNTRUSTED_PLUGIN_EXECUTION_DISABLED")
        manifest = self.packages.verify(package_hash)
        if context.mode.value not in manifest.raw["modes"] or event_type not in {"bar", "timer", "batch", "external"}:
            raise StrategyError("STRATEGY_MODE_OR_EVENT_UNSUPPORTED")
        config = payload(context.config)
        manifest.parameters(config)
        view = payload(context.data_view)
        rows = view.get("rows", [])
        history = view.get("history", [])
        for row in [*rows, *history]:
            if not isinstance(row, dict) or row.get("instrument_id") not in context.allowed_instruments or utc(row.get("available_at")) > context.as_of:
                raise StrategyError("STRATEGY_DATA_VIEW_NOT_ASOF")
        model = view.get("model")
        if manifest.raw["model"] is not None:
            reference = manifest.raw["model"]
            if utc(reference["train_cutoff"]) >= context.as_of or model is None or digest(model) != reference["sha256"]:
                raise StrategyError("STRATEGY_MODEL_CUTOFF_OR_HASH_INVALID")
            if model.get("version") != reference["version"] or model.get("train_cutoff", reference["train_cutoff"]) != reference["train_cutoff"]:
                raise StrategyError("STRATEGY_MODEL_VERSION_INVALID")
            if "window" in model and not utc(model["window"]["prediction_start"]) <= context.as_of <= utc(model["window"]["prediction_end"]):
                raise StrategyError("STRATEGY_MODEL_OUTSIDE_INFERENCE_WINDOW")
        request = {"clock": payload(context.as_of), "mode": context.mode.value,
                   "source_id": manifest.raw["id"],
                   "parameters": config, "rows": rows, "account": payload(context.account_snapshot),
                   "state": payload(context.state), "model": model, "event_type": event_type}
        if "history" in view:
            request["history"] = history
        # Windows venv redirector starts another process. Use this venv's pinned
        # base interpreter directly so ACTIVE_PROCESS_LIMIT=1 also covers Python.
        if self.active_guard:
            self.active_guard()
        response = bounded_process([getattr(sys, "_base_executable", sys.executable), "-I", "-S", "-B", str(Path(__file__).with_name("runner.py")),
                                    str(self.packages.path(package_hash)), manifest.raw["entrypoint"]], request, self.limits,
                                   self.packages.root.parent / "worker-temp", active_guard=self.active_guard)
        if self.active_guard:
            self.active_guard()
        validate_output(response, manifest, request["clock"])
        external_expiry = None
        if manifest.raw["style"] == "external":
            message = response["message"]
            fields = {"source_id", "sequence", "message_id", "produced_at", "effective_at", "expires_at", "mode", "outputs"}
            previous = request["state"].get("sequence", 0)
            if not isinstance(message, dict) or set(message) != fields or type(previous) is not int or type(message["sequence"]) is not int or message["sequence"] != previous+1 or response["state"].get("sequence") != message["sequence"]:
                raise StrategyError("EXTERNAL_PROCESS_SEQUENCE_INVALID")
            if message["source_id"] != manifest.raw["id"] or message["message_id"] != manifest.raw["id"]+"-"+str(message["sequence"]) or message["mode"] != context.mode.value or message["outputs"] != response["outputs"]:
                raise StrategyError("EXTERNAL_PROCESS_SOURCE_OR_CONTENT_INVALID")
            external_expiry = utc(message["expires_at"])
            if utc(message["produced_at"]) != context.as_of or utc(message["effective_at"]) != context.as_of or not context.as_of < external_expiry <= context.as_of+timedelta(minutes=5):
                raise StrategyError("EXTERNAL_PROCESS_MESSAGE_EXPIRED_OR_FUTURE")
            response["state"]["last_message_hash"] = digest(message)
        outputs = []
        units = {"score": "dimensionless", "signal": "direction", "target_weight": "decimal_weight",
                 "target_quantity": "shares", "order_intent": "shares"}
        for i, raw in enumerate(response["outputs"]):
            output = StrategyOutput(output_id="out-" + digest({"package": package_hash, "clock": request["clock"], "index": i, "output": raw})[:32],
                                    instrument_id=raw["instrument_id"], kind=OutputKind(raw["kind"]), value=number(raw["value"]),
                                    unit=units[raw["kind"]], produced_at=context.as_of, effective_at=context.as_of,
                                    expires_at=external_expiry or context.as_of + timedelta(days=7), mode=context.mode,
                                    side=raw.get("side"), order_type=raw.get("order_type"),
                                    limit_price=None if raw.get("limit_price") is None else number(raw["limit_price"]))
            context.validate_output(output)
            outputs.append(output)
        return tuple(outputs), response["state"]
