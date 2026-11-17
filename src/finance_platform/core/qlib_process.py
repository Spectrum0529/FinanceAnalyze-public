"""Two fixed Qlib Job profiles; reuse the existing strategy Job primitive.

This changes only the newly held export Job instance before admission. Existing
strategy defaults and all other Job instances retain their cap1 policy. A Job
Object limits trusted code; it is not a Python/native-code sandbox.
"""
from dataclasses import dataclass
from enum import Enum
import ctypes
from ctypes import wintypes
import time

from .errors import PlatformError


class QlibProcessProfile(Enum):
    TRAIN = "qlib-train-1"
    OFFICIAL_DUMP = "qlib-official-dump-1"


@dataclass(frozen=True)
class _Limits:
    wall_seconds: int
    memory_bytes: int
    active_processes: int
    stdout_bytes: int = 1048576
    stderr_bytes: int = 1048576
    request_bytes: int = 524288
    result_bytes: int = 4194304
    artifact_bytes: int = 67108864


def profile_limits(profile):
    if type(profile) is not QlibProcessProfile:
        raise PlatformError("QLIB_PROFILE_INVALID")
    return _Limits(120, 2147483648,
                   1 if profile is QlibProcessProfile.TRAIN else 2)


class _Basic(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _Extended(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _Basic), ("IoInfo", _IO),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _strategy_job(process, memory):
    from finance_platform.strategies.worker import _WindowsJob
    return _WindowsJob(process, memory)


def _configure(kernel):
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.IsProcessInJob.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL


def _query(job, info_class, target):
    size = wintypes.DWORD()
    if not job.kernel.QueryInformationJobObject(
            job.handle, info_class, ctypes.byref(target), ctypes.sizeof(target),
            ctypes.byref(size)):
        raise PlatformError("QLIB_JOB_QUERY_FAILED")
    return target


def _get_limits(job):
    return _query(job, 9, _Extended())


def _set_limits(job, limits):
    if not job.kernel.SetInformationJobObject(
            job.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise PlatformError("QLIB_JOB_LIMIT_FAILED")


def _native_contains(job, process):
    contained = wintypes.BOOL()
    if not job.kernel.IsProcessInJob(
            wintypes.HANDLE(int(process._handle)), job.handle, ctypes.byref(contained)):
        raise PlatformError("QLIB_JOB_MEMBERSHIP_UNPROVEN")
    return bool(contained.value)


def _native_snapshot(job):
    accounting = _query(job, 1, _Accounting())
    class Pids(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 3),
        ]
    pids = _query(job, 3, Pids())
    count = int(pids.NumberOfProcessIdsInList)
    if count > 2:
        raise PlatformError("QLIB_JOB_PROCESS_BUDGET")
    return {
        "active_processes": int(accounting.ActiveProcesses),
        "total_processes_started": int(accounting.TotalProcesses),
        "pids": sorted(int(pids.ProcessIdList[i]) for i in range(count)),
    }


def _native_terminate(job):
    if not job.kernel.TerminateJobObject(job.handle, 1):
        raise PlatformError("QLIB_JOB_TERMINATE_FAILED")


def _native_duplicate_query(job, process):
    target = wintypes.HANDLE()
    if not job.kernel.DuplicateHandle(job.kernel.GetCurrentProcess(), job.handle,
            wintypes.HANDLE(int(process._handle)), ctypes.byref(target), 0x0004, False, 0):
        raise PlatformError("QLIB_JOB_HANDLE_DUPLICATION_FAILED")
    return int(target.value)


class QlibOwnedJob:
    """Only an exact newly created process; caller must reap/join own I/O."""
    def __init__(self, process, profile):
        self.profile = profile
        self.limits = profile_limits(profile)
        self.process = process
        self._job = _strategy_job(process, self.limits.memory_bytes)
        self._closed = False
        try:
            _configure(self._job.kernel)
            limits = _get_limits(self._job)
            self._check_limits(limits, 1)
            if profile is QlibProcessProfile.OFFICIAL_DUMP:
                # No generic/user-controlled cap. Only this fresh held Job.
                limits.BasicLimitInformation.ActiveProcessLimit = 2
                _set_limits(self._job, limits)
            self._check_limits(_get_limits(self._job), self.limits.active_processes)
        except BaseException:
            self.close()
            raise

    def _check_limits(self, limits, cap):
        basic = limits.BasicLimitInformation
        # KILL_ON_JOB_CLOSE | JOB_MEMORY | ACTIVE_PROCESS. No breakaway flags.
        if (int(basic.LimitFlags) != 0x2208
                or int(basic.ActiveProcessLimit) != cap
                or int(limits.JobMemoryLimit) != self.limits.memory_bytes):
            raise PlatformError("QLIB_JOB_LIMIT_UNPROVEN")

    def snapshot(self):
        if self._closed:
            raise PlatformError("QLIB_JOB_HANDLE_CLOSED")
        self._check_limits(_get_limits(self._job), self.limits.active_processes)
        result = _native_snapshot(self._job)
        if (type(result["active_processes"]) is not int
                or not 0 <= result["active_processes"] <= self.limits.active_processes
                or len(result["pids"]) != result["active_processes"]
                or len(set(result["pids"])) != len(result["pids"])):
            raise PlatformError("QLIB_JOB_PROCESS_BUDGET")
        return {**result, "profile": self.profile.value,
                "active_process_limit": self.limits.active_processes,
                "job_memory_bytes": self.limits.memory_bytes}

    def verify_before_admission(self):
        proof = self.snapshot()
        if (self.process.poll() is not None or proof["pids"] != [self.process.pid]
                or proof["active_processes"] != 1
                or not _native_contains(self._job, self.process)):
            raise PlatformError("QLIB_JOB_MEMBERSHIP_UNPROVEN")
        return proof

    def terminate(self):
        if not self._closed:
            _native_terminate(self._job)

    def duplicate_into(self, process):
        if self._closed or process is not self.process:
            raise PlatformError("QLIB_JOB_MEMBERSHIP_UNPROVEN")
        self.verify_before_admission()
        return _native_duplicate_query(self._job, process)

    def require_empty(self, *, deadline_seconds=2):
        if (type(deadline_seconds) not in (int, float)
                or not 0 <= deadline_seconds <= 10):
            raise PlatformError("QLIB_CLEANUP_BUDGET_INVALID")
        deadline = time.monotonic() + deadline_seconds
        while True:
            proof = self.snapshot()
            if proof["active_processes"] == 0 and not proof["pids"]:
                return {**proof, "kernel_final_empty": True}
            if time.monotonic() >= deadline:
                raise PlatformError("QLIB_JOB_FINAL_EMPTY_UNPROVEN")
            time.sleep(.01)

    def close(self):
        if not self._closed:
            self._job.close()
            self._closed = True
