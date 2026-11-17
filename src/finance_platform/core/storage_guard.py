"""Windows path metadata compatibility; payload byte IO uses storage_native."""
from contextlib import contextmanager,ExitStack
from pathlib import Path,PureWindowsPath
import ctypes as C
from ctypes import wintypes as W
import os,re,stat
from .errors import PlatformError

def fail(code='STORAGE_PATH_REJECTED'):
    error=PlatformError(code,409)
    messages={'STORAGE_PATH_REJECTED':'保存位置不符合本地安全限制。',
              'STORAGE_UNAVAILABLE':'保存位置不存在、不可访问或不可写；未回退默认位置。',
              'STORAGE_ROOT_CHANGED':'保存目录已被替换，请检查原目录；未回退默认位置。',
              'STORAGE_HASH_MISMATCH':'保存内容校验失败。',
              'STORAGE_REVISION_CONFLICT':'设置已改变，请刷新后重试。',
              'STORAGE_TASK_BINDING_REQUIRED':'任务缺少提交时的保存位置绑定，已停止写入。'}
    error.message=messages.get(code,error.message)
    raise error

def local_path(value,*,_internal=False):
    if type(value)is not str or len(value)>(32760 if _internal else 220) or not re.match(r'^[A-Za-z]:[\\/]',value) or any(x in value for x in ('%','$','\x00')):fail()
    p=PureWindowsPath(value)
    if not p.is_absolute() or p.drive.startswith('\\') or any(x in ('.','..') for x in re.split(r'[\\/]',value)[1:]):fail()
    for part in p.parts[1:]:
        if part.endswith((' ','.')) or any(c in part for c in ':*?"<>|') or PureWindowsPath(part).is_reserved():fail()
    return Path(str(p))

def identity(path):
    info=Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:fail()
    return [info.st_dev,info.st_ino]

def _local_drive(kernel,path):
    """Check only the caller's selected drive; never enumerate/query others."""
    kernel.GetDriveTypeW.argtypes=[W.LPCWSTR];kernel.GetDriveTypeW.restype=W.UINT
    if kernel.GetDriveTypeW(str(Path(path).anchor))!=3:fail('STORAGE_PATH_REJECTED')

@contextmanager
def creating_directory(path):
    """Create one checked layer at a time while retaining every parent handle."""
    path=Path(path).absolute()
    try:
        with ExitStack() as stack:
            chain=list(reversed(path.parents))+[path]
            for node in chain:
                # Parent is already held. No recursive mkdir can traverse an
                # unchecked intermediate junction before rejection.
                if not node.exists() and not node.is_symlink():node.mkdir()
                stack.enter_context(held(node))
            yield path
    except OSError:fail('STORAGE_UNAVAILABLE')

class _AttributeTagInfo(C.Structure):
    _fields_=[('attributes',W.DWORD),('tag',W.DWORD)]

class _FileIdInfo(C.Structure):
    _fields_=[('volume',C.c_ulonglong),('identifier',C.c_ubyte*16)]

class _StandardInfo(C.Structure):
    _fields_=[('allocation',C.c_longlong),('size',C.c_longlong),('links',W.DWORD),
             ('delete_pending',C.c_ubyte),('directory',C.c_ubyte)]

def _opened_info(kernel,handle):
    """Query the held object itself; pathname lstat alone is not authority."""
    tag=_AttributeTagInfo();file_id=_FileIdInfo();standard=_StandardInfo()
    for kind,record in ((9,tag),(18,file_id),(1,standard)):
        if not kernel.GetFileInformationByHandleEx(handle,kind,C.byref(record),C.sizeof(record)):
            fail('STORAGE_UNAVAILABLE')
    return {'identity':[file_id.volume,int.from_bytes(bytes(file_id.identifier),'little')],
            'attributes':tag.attributes,'tag':tag.tag,'links':standard.links,
            'directory':bool(standard.directory),'delete_pending':bool(standard.delete_pending)}

def _final_path(kernel,handle):
    """Normalized DOS spelling from a verified handle, never user device input."""
    kernel.GetFinalPathNameByHandleW.argtypes=[W.HANDLE,W.LPWSTR,W.DWORD,W.DWORD]
    kernel.GetFinalPathNameByHandleW.restype=W.DWORD
    count=kernel.GetFinalPathNameByHandleW(handle,None,0,0)
    if not 0<count<32768:fail('STORAGE_PATH_REJECTED')
    buffer=C.create_unicode_buffer(count+1)
    actual=kernel.GetFinalPathNameByHandleW(handle,buffer,count+1,0)
    if not 0<actual<=count:fail('STORAGE_PATH_REJECTED')
    value=buffer.value
    if not re.match(r'^\\\\\?\\[A-Za-z]:\\',value):fail('STORAGE_PATH_REJECTED')
    return local_path(value[4:],_internal=True)

@contextmanager
def held(path,expected=None,*,file=False,canonical=False):
    """Hold every existing ancestor, rejecting reparse before consuming bytes.

    Every object denies FILE_SHARE_WRITE/DELETE for the entire IO interval.
    OPEN_REPARSE_POINT prevents following the final opened component; each
    ancestor is opened and checked before the next component is consumed.
    Handle-derived tag/type/identity checks precede yielding any path.
    This Path-returning compatibility helper is NOT an IO capability: attribute
    writes can still mutate a directory. Registered byte IO uses storage_native.
    """
    if os.name!='nt':fail('STORAGE_PLATFORM_UNSUPPORTED')
    path=Path(path).absolute();handles=[]
    k=C.WinDLL('kernel32',use_last_error=True,winmode=0x800)
    _local_drive(k,path)
    k.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE];k.CreateFileW.restype=W.HANDLE
    k.CloseHandle.argtypes=[W.HANDLE];k.CloseHandle.restype=W.BOOL
    k.GetFileInformationByHandleEx.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD]
    k.GetFileInformationByHandleEx.restype=W.BOOL
    try:
        chain=list(reversed(path.parents))+[path]
        for node in chain:
            info=node.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:fail()
            is_file=node==path and file
            if is_file:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:fail()
            elif not stat.S_ISDIR(info.st_mode):fail()
            # FILE_LIST_DIRECTORY is a data-read access on directory objects;
            # attributes-only opens do not engage Windows share-access checks.
            h=k.CreateFileW(str(node),0x80000000 if is_file else 0x81,1,None,3,0x00200000|(0 if is_file else 0x02000000),None)
            if h in (None,C.c_void_p(-1).value):fail('STORAGE_UNAVAILABLE')
            handles.append(h)
            opened=_opened_info(k,h)
            if opened['attributes']&0x400 or opened['tag'] or opened['delete_pending']:
                fail('STORAGE_PATH_REJECTED')
            if opened['directory']==is_file or bool(opened['attributes']&0x10)==is_file:
                fail('STORAGE_PATH_REJECTED')
            if is_file and opened['links']!=1:fail('STORAGE_PATH_REJECTED')
            if opened['identity']!=[info.st_dev,info.st_ino]:fail('STORAGE_ROOT_CHANGED')
            after=node.lstat()
            if stat.S_ISLNK(after.st_mode) or getattr(after,'st_file_attributes',0)&0x400:fail('STORAGE_PATH_REJECTED')
            if opened['identity']!=[after.st_dev,after.st_ino]:fail('STORAGE_ROOT_CHANGED')
        if expected is not None and opened['identity']!=expected:fail('STORAGE_ROOT_CHANGED')
        yield _final_path(k,h) if canonical else path
    except OSError:
        fail('STORAGE_UNAVAILABLE')
    finally:
        for h in reversed(handles):k.CloseHandle(h)
