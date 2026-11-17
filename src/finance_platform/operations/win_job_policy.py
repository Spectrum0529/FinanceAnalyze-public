"""Count every owned Windows Job member, including console infrastructure.

The configured active-process limit is not a total associated-member count.
The fixed venv/helper/PowerShell topology was measured with two conhosts;
unknown identities or extra fan-out fail closed. No global PID discovery.
"""
import ctypes
from ctypes import wintypes
import ntpath

WORKLOAD_LIMIT = 3
CONSOLE_LIMIT = 2
MEMBER_LIMIT = 5


def validate_snapshot(snapshot, identities, workload_images):
    allowed = {ntpath.normcase(p) for p in workload_images}
    console = ntpath.normcase('C:/Windows/System32/conhost.exe')
    pids = snapshot['pids']
    if (snapshot.get('complete_membership') is not True
            or len(set(pids)) != len(pids)
            or snapshot['active_processes'] != len(pids)
            or {i['pid'] for i in identities} != set(pids)
            or len(identities) != len(pids)):
        raise RuntimeError('JOB_MEMBER_ACCOUNTING_UNPROVEN')
    hosts = workloads = 0
    for item in identities:
        if item.get('membership_verified') is not True or item.get('wait') != 258 or not item.get('birth_filetime'):
            raise RuntimeError('JOB_MEMBER_IDENTITY_UNPROVEN')
        image = ntpath.normcase(item.get('image', ''))
        if image == console:
            hosts += 1
        elif image in allowed:
            workloads += 1
        else:
            raise RuntimeError('JOB_UNEXPECTED_MEMBER_IMAGE')
    if hosts > CONSOLE_LIMIT or workloads > WORKLOAD_LIMIT or len(pids) > MEMBER_LIMIT:
        raise RuntimeError('JOB_MEMBER_BUDGET_EXCEEDED')
    return {**snapshot, 'identities': identities, 'workload_members': workloads,
            'console_hosts': hosts, 'configured_workload_limit': WORKLOAD_LIMIT,
            'aggregate_member_budget': MEMBER_LIMIT, 'member_identity_verified': True}


def snapshot(held_job, evidence, workload_images, observations=None):
    """Open only PIDs freshly returned by this held Job; recheck membership.

    Failure to open, observe or classify any member is failure, never exclusion.
    Each temporary process handle is closed in finally, including error paths.
    """
    raw = evidence.snapshot()
    if observations is not None:
        observations.append(raw)
    kernel = held_job.kernel
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    identities = []
    for pid in raw['pids']:
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            raise RuntimeError('JOB_MEMBER_OPEN_FAILED')
        try:
            result = wintypes.BOOL()
            if not kernel.IsProcessInJob(handle, held_job.handle, ctypes.byref(result)) or not result.value:
                raise RuntimeError('JOB_MEMBER_CHANGED')
            size = wintypes.DWORD(32768)
            image = ctypes.create_unicode_buffer(32768)
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)) or not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise RuntimeError('JOB_MEMBER_QUERY_FAILED')
            identities.append({'pid': pid, 'image': image.value,
                'birth_filetime': (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
                'wait': int(kernel.WaitForSingleObject(handle, 0)), 'membership_verified': True})
        finally:
            if not kernel.CloseHandle(handle):
                raise RuntimeError('JOB_MEMBER_HANDLE_CLOSE_FAILED')
    # Caller must preserve this complete raw observation even if validation fails.
    return raw, identities
