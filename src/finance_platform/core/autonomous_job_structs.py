"""Pure ctypes layouts copied unchanged from FA010 frozen r04 (SHA77327278...). No runner/import side effects."""
import ctypes as C
from ctypes import wintypes as W

class Basic(C.Structure):
    _fields_ = [('user', C.c_int64), ('job_user', C.c_int64), ('flags', W.DWORD),
                ('min_ws', C.c_size_t), ('max_ws', C.c_size_t), ('active', W.DWORD),
                ('affinity', C.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]

class IO(C.Structure):
    _fields_ = [(n, C.c_uint64) for n in ('read', 'write', 'other', 'read_bytes', 'write_bytes', 'other_bytes')]

class Limits(C.Structure):
    _fields_ = [('basic', Basic), ('io', IO), ('process_memory', C.c_size_t),
                ('job_memory', C.c_size_t), ('peak_process', C.c_size_t), ('peak_job', C.c_size_t)]

class Members(C.Structure):
    _fields_ = [('assigned', W.DWORD), ('returned', W.DWORD), ('ids', C.c_size_t * 64)]

class Accounting(C.Structure):
    _fields_ = [('user', C.c_int64), ('kernel', C.c_int64), ('period_user', C.c_int64),
                ('period_kernel', C.c_int64), ('faults', W.DWORD), ('total', W.DWORD),
                ('active', W.DWORD), ('terminated', W.DWORD)]

class SI(C.Structure):
    _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR), ('title', W.LPWSTR),
                ('x', W.DWORD), ('y', W.DWORD), ('xs', W.DWORD), ('ys', W.DWORD), ('xc', W.DWORD),
                ('yc', W.DWORD), ('fill', W.DWORD), ('flags', W.DWORD), ('show', W.WORD),
                ('reserved2', W.WORD), ('reserved_bytes', C.c_void_p),
                ('stdin', W.HANDLE), ('stdout', W.HANDLE), ('stderr', W.HANDLE)]

class PI(C.Structure):
    _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE), ('pid', W.DWORD), ('tid', W.DWORD)]
