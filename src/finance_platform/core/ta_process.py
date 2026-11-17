"""TA-only immutable initial profile; existing Windows Job primitive reused."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import time

from .errors import PlatformError
from .qlib_process import (_strategy_job, _configure, _get_limits, _native_contains,
                           _native_snapshot, _native_terminate, _native_duplicate_query)


@dataclass(frozen=True)
class TALimits:
    profile: str = "ta-native-offline-one-two-r1"
    wall_seconds: int = 120
    memory_bytes: int = 1610612736
    active_processes: int = 1
    native_live_threads: int = 16
    python_live_workers: int = 8
    python_thread_starts: int = 128
    executor_workers: int = 1
    stderr_bytes: int = 1048576
    protocol_bytes: int = 8388608
    result_bytes: int = 2097152
    artifact_bytes: int = 67108864
    artifact_files: int = 4096
    trace_rows: int = 4096
    model_calls: int = 32
    tool_calls: int = 16
    output_chars: int = 160000


LIMITS = TALimits()


def native_thread_count(pid):
    """Query only this new child's current kernel threads, never historical PIDs."""
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("thread_id", wintypes.DWORD), ("owner_pid", wintypes.DWORD),
                    ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                    ("flags", wintypes.DWORD)]
    kernel = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x800)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Thread32First", "Thread32Next"):
        fn = getattr(kernel, name)
        fn.argtypes, fn.restype = [wintypes.HANDLE, ctypes.POINTER(Entry)], wintypes.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise PlatformError("TA_THREAD_QUERY_FAILED")
    count = 0
    try:
        item = Entry()
        item.size = ctypes.sizeof(item)
        more = kernel.Thread32First(snapshot, ctypes.byref(item))
        if not more:
            raise PlatformError("TA_THREAD_QUERY_FAILED")
        while more:
            count += int(item.owner_pid == pid)
            more = kernel.Thread32Next(snapshot, ctypes.byref(item))
        if ctypes.get_last_error() != 18:
            raise PlatformError("TA_THREAD_QUERY_FAILED")
        return count
    finally:
        if not kernel.CloseHandle(snapshot):
            raise PlatformError("TA_THREAD_QUERY_FAILED")


def _observe_rejected_members(held_job, raw):
    """Bounded attribution, never an allowance or a historical-PID lookup.

    Reuse the operations reader's OpenProcess -> same-Job membership recheck ->
    image/birth/wait query. Do not call its topology/classification policy.
    Only the PIDs just returned by this held Job may be inspected.
    """
    if (type(raw) is not dict or type(raw.get("pids")) is not list
            or len(raw["pids"]) > 2 or len(set(raw["pids"])) != len(raw["pids"])
            or any(type(pid) is not int or not 0 < pid < 2**32 for pid in raw["pids"])):
        return {"status": "INVALID_MEMBER_SNAPSHOT", "identities": [], "admission_permitted": False}
    if not raw["pids"]:
        return {"status": "NO_CURRENT_MEMBERS_TO_ATTRIBUTE", "identities": [], "admission_permitted": False}
    try:
        from types import SimpleNamespace
        from finance_platform.operations.win_job_policy import snapshot as observe
        evidence = SimpleNamespace(snapshot=lambda: {**raw, "pids": list(raw["pids"])})
        observed, identities = observe(held_job, evidence, ())
        rows = []
        for identity in identities:
            row = {key: identity[key] for key in ("pid", "birth_filetime", "wait", "membership_verified")}
            image = identity["image"]
            if type(image) is str and 0 < len(image) <= 4096:
                row.update(image=image, image_status="OBSERVED")
            else:
                row["image_status"] = "UNAVAILABLE_OR_PATH_BOUND_EXCEEDED"
            rows.append(row)
        return {"status": "CURRENT_HELD_JOB_MEMBERS_OBSERVED", "snapshot": observed,
                "identities": rows, "admission_permitted": False,
                "classification_policy_applied": False}
    except BaseException as error:
        # Diagnostics must never replace the original budget failure. Only
        # fixed helper codes are retained; no exception text or command line.
        known = {"JOB_MEMBER_OPEN_FAILED", "JOB_MEMBER_CHANGED", "JOB_MEMBER_QUERY_FAILED", "JOB_MEMBER_HANDLE_CLOSE_FAILED"}
        code = error.args[0] if error.args and error.args[0] in known else "TA_MEMBER_ATTRIBUTION_UNAVAILABLE"
        return {"status": "MEMBER_ATTRIBUTION_UNAVAILABLE", "code": code,
                "identities": [], "admission_permitted": False}


class TAOwnedJob:
    def __init__(self, process):
        self.process, self.closed = process, False
        self.last_observation = self.last_budget_rejection = None
        self.last_limits = None
        self.job = _strategy_job(process, LIMITS.memory_bytes)
        try:
            _configure(self.job.kernel)
            # The caller must retain this held Job before admit() can reject.
            # No GO/packet is sent before the unchanged strict admit check.
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        if self.closed:
            raise PlatformError("TA_JOB_LIMIT_UNPROVEN")
        limits = _get_limits(self.job)
        self.last_limits = {
            "limit_flags": int(limits.BasicLimitInformation.LimitFlags),
            "active_process_limit": int(limits.BasicLimitInformation.ActiveProcessLimit),
            "job_memory_bytes": int(limits.JobMemoryLimit),
        }
        if (int(limits.BasicLimitInformation.LimitFlags) != 0x2208
                or int(limits.BasicLimitInformation.ActiveProcessLimit) != 1
                or int(limits.JobMemoryLimit) != LIMITS.memory_bytes):
            raise PlatformError("TA_JOB_LIMIT_UNPROVEN")
        value = _native_snapshot(self.job)
        self.last_observation = {**value, "direct_child_pid": self.process.pid,
            "direct_child_exit_code": self.process.poll(),
            "query_model": "SEQUENTIAL_ACCOUNTING_THEN_PID_LIST"}
        reasons = []
        if value["active_processes"] not in (0, 1):
            reasons.append("ACTIVE_PROCESS_BUDGET")
        if len(value["pids"]) != value["active_processes"]:
            reasons.append("ACCOUNTING_PID_LIST_DISAGREEMENT")
        if value["total_processes_started"] != 1:
            reasons.append("LIFETIME_TOTAL_NOT_ONE")
        if reasons:
            self.last_budget_rejection = {**self.last_observation, "reasons": reasons}
            # Bounded observation only. A second snapshot never retries
            # or rescinds this admission rejection.
            if "ACCOUNTING_PID_LIST_DISAGREEMENT" in reasons:
                try:
                    follow_up = _native_snapshot(self.job)
                    self.last_budget_rejection["follow_up_snapshot"] = {**follow_up,
                        "direct_child_exit_code": self.process.poll()}
                except PlatformError as error:
                    self.last_budget_rejection["follow_up_error_code"] = error.code
            # Reuse the freshest snapshot from this same rejection. Identity
            # handles are rechecked against this held Job by the existing reader.
            member_snapshot = self.last_budget_rejection.get("follow_up_snapshot", value)
            self.last_budget_rejection["member_observation"] = _observe_rejected_members(self.job, member_snapshot)
            raise PlatformError("TA_JOB_PROCESS_BUDGET")
        return {**value, "profile": LIMITS.profile, "memory_bytes": LIMITS.memory_bytes}

    def admit(self):
        value = self.snapshot()
        if self.process.poll() is not None or value["pids"] != [self.process.pid] or not _native_contains(self.job, self.process):
            raise PlatformError("TA_JOB_MEMBERSHIP_UNPROVEN")
        return value

    def duplicate_into(self):
        self.admit()
        return _native_duplicate_query(self.job, self.process)

    def terminate(self):
        _native_terminate(self.job)

    def empty(self):
        deadline = time.monotonic() + 2
        while True:
            proof = self.snapshot()
            if not proof["pids"] and proof["active_processes"] == 0:
                return {**proof, "kernel_final_empty": True}
            if time.monotonic() >= deadline:
                raise PlatformError("TA_JOB_FINAL_EMPTY_UNPROVEN")
            time.sleep(.01)

    def cleanup_empty(self):
        """Failure teardown proof only; never authorizes native execution.

        Lifetime TotalProcesses cannot decrease after a rejected creation.
        Query this same held Job for actual zero membership after termination,
        retaining the rejected budget observation separately. empty()/admit()
        continue to enforce the original total=1 execution policy.
        """
        if self.closed:
            raise PlatformError("TA_JOB_FINAL_EMPTY_UNPROVEN")
        deadline = time.monotonic() + 2
        while True:
            proof = _native_snapshot(self.job)
            if (type(proof.get("active_processes")) is int
                    and proof["active_processes"] == 0
                    and type(proof.get("pids")) is list and not proof["pids"]
                    and type(proof.get("total_processes_started")) is int
                    and proof["total_processes_started"] >= 0):
                return {**proof, "profile": LIMITS.profile,
                        "kernel_final_empty": True,
                        "purpose": "FAILURE_CLEANUP_ONLY",
                        "execution_admission_permitted": False,
                        "prior_budget_rejected": self.last_budget_rejection is not None}
            if time.monotonic() >= deadline:
                raise PlatformError("TA_JOB_FINAL_EMPTY_UNPROVEN")
            time.sleep(.01)

    def close(self):
        if not self.closed:
            self.job.close()
            self.closed = True
