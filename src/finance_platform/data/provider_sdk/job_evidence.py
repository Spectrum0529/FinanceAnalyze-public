"""Thin evidence/query adapter for the existing held _WindowsJob, no new Job."""
import ctypes as C
from ctypes import wintypes as W
import time
from finance_platform.data.contracts import DataError


class Accounting(C.Structure):
    _fields_=[(n,C.c_int64) for n in ('UserTime','KernelTime','PeriodUser','PeriodKernel')]+[(n,W.DWORD) for n in ('PageFaults','TotalProcesses','ActiveProcesses','TerminatedProcesses')]


class Membership(C.Structure):
    _fields_=[('assigned',W.DWORD),('returned',W.DWORD),('pids',C.c_size_t*8)]


class Evidence:
    def __init__(self,held_job):
        self.job=held_job;self.k=held_job.kernel
        for name,args in [('QueryInformationJobObject',[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.c_void_p]),('IsProcessInJob',[W.HANDLE,W.HANDLE,C.POINTER(W.BOOL)]),('TerminateJobObject',[W.HANDLE,W.UINT]),('DuplicateHandle',[W.HANDLE,W.HANDLE,W.HANDLE,C.POINTER(W.HANDLE),W.DWORD,W.BOOL,W.DWORD])]:
            getattr(self.k,name).argtypes=args
        self.k.GetCurrentProcess.restype=W.HANDLE

    def snapshot(self):
        accounting=Accounting();members=Membership();length=W.DWORD()
        if not self.k.QueryInformationJobObject(self.job.handle,1,C.byref(accounting),C.sizeof(accounting),None):
            raise DataError('SDK_JOB_QUERY_FAILED')
        if not self.k.QueryInformationJobObject(self.job.handle,3,C.byref(members),C.sizeof(members),C.byref(length)) or members.returned>8 or members.assigned!=members.returned:
            raise DataError('SDK_JOB_QUERY_FAILED')
        return {'active_processes':int(accounting.ActiveProcesses),'total_processes':int(accounting.TotalProcesses),
                'terminated_processes':int(accounting.TerminatedProcesses),'pids':[int(members.pids[i]) for i in range(members.returned)],
                'complete_membership':True,'non_atomic_sequential_queries':True}

    def contains(self,process):
        result=W.BOOL()
        if not self.k.IsProcessInJob(W.HANDLE(int(process._handle)),self.job.handle,C.byref(result)):
            raise DataError('SDK_JOB_MEMBERSHIP_UNPROVEN')
        return bool(result.value)

    def duplicate_into(self,process):
        remote=W.HANDLE()
        if not self.k.DuplicateHandle(self.k.GetCurrentProcess(),self.job.handle,W.HANDLE(int(process._handle)),C.byref(remote),0,False,2):
            raise DataError('SDK_JOB_HANDLE_DUPLICATION_FAILED')
        return int(remote.value)

    def final_empty(self, *, terminate=False):
        if terminate and not self.k.TerminateJobObject(self.job.handle,238):
            raise DataError('SDK_JOB_TERMINATION_FAILED')
        until=time.monotonic()+5
        while True:
            first=self.snapshot()
            if first['active_processes']==0 and first['pids']==[]:
                second=self.snapshot()
                if second['active_processes']==0 and second['pids']==[]:
                    return {'first':first,'second':second,'kernel_final_empty':True}
            if time.monotonic()>=until:raise DataError('SDK_JOB_FINAL_EMPTY_UNPROVEN')
            time.sleep(.02)
