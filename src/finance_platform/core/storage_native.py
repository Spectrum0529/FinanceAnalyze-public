"""Narrow local registered-payload handles, adapted from Win32/NtCreateFile.

Single validated child names only; no path-based target IO or native lifecycle
operations. Unsupported no-reparse semantics fail closed, without fallback.
"""
from contextlib import contextmanager,ExitStack
from pathlib import Path
import ctypes as C
from ctypes import wintypes as W
import os,re
from .storage_guard import fail,local_path,_opened_info,_final_path,_local_drive

class _Unicode(C.Structure):
    _fields_=[('length',W.USHORT),('maximum',W.USHORT),('buffer',C.c_void_p)]
class _Attributes(C.Structure):
    _fields_=[('length',W.ULONG),('root',W.HANDLE),('name',C.POINTER(_Unicode)),('attributes',W.ULONG),('security',C.c_void_p),('qos',C.c_void_p)]
class _Status(C.Structure):
    _fields_=[('status',C.c_void_p),('information',C.c_size_t)]

def _kernel():
    if os.name!='nt':fail('STORAGE_PLATFORM_UNSUPPORTED')
    k=C.WinDLL('kernel32',use_last_error=True,winmode=0x800)
    signatures={
      'CreateFileW':([W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE],W.HANDLE),
      'CloseHandle':([W.HANDLE],W.BOOL),
      'GetFileInformationByHandleEx':([W.HANDLE,C.c_int,C.c_void_p,W.DWORD],W.BOOL),
      'ReadFile':([W.HANDLE,C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.c_void_p],W.BOOL),
      'WriteFile':([W.HANDLE,C.c_void_p,W.DWORD,C.POINTER(W.DWORD),C.c_void_p],W.BOOL),
      'FlushFileBuffers':([W.HANDLE],W.BOOL),
      'SetFilePointerEx':([W.HANDLE,C.c_longlong,C.c_void_p,W.DWORD],W.BOOL),
      'LocalFree':([C.c_void_p],C.c_void_p),
    }
    for name,(args,result) in signatures.items():
        fn=getattr(k,name);fn.argtypes=args;fn.restype=result
    return k

def _check(k,h,directory,allow_delete_pending=False):
    info=_opened_info(k,h)
    if info['attributes']&0x400 or info['tag'] or info['delete_pending'] and not allow_delete_pending or info['directory']!=directory or bool(info['attributes']&0x10)!=directory:
        fail('STORAGE_PATH_REJECTED')
    if not directory and info['links']!=1:fail('STORAGE_PATH_REJECTED')
    return info

@contextmanager
def _private_descriptor(k):
    """Protected current-user/SYSTEM/Administrators DACL at creation.

    Same SID policy as existing operations/windows_acl.py, applied atomically
    to new objects. Existing parents and operations helper are never changed.
    """
    adv=C.WinDLL('advapi32',use_last_error=True,winmode=0x800)
    k.GetCurrentProcess.restype=W.HANDLE
    adv.OpenProcessToken.argtypes=[W.HANDLE,W.DWORD,C.POINTER(W.HANDLE)];adv.OpenProcessToken.restype=W.BOOL
    adv.GetTokenInformation.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD,C.POINTER(W.DWORD)];adv.GetTokenInformation.restype=W.BOOL
    adv.ConvertSidToStringSidW.argtypes=[C.c_void_p,C.POINTER(C.c_void_p)];adv.ConvertSidToStringSidW.restype=W.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[W.LPCWSTR,W.DWORD,C.POINTER(C.c_void_p),C.POINTER(W.DWORD)];adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=W.BOOL
    token=W.HANDLE();sid_string=C.c_void_p();descriptor=C.c_void_p();length=W.DWORD()
    try:
        if not adv.OpenProcessToken(k.GetCurrentProcess(),8,C.byref(token)):fail('STORAGE_UNAVAILABLE')
        adv.GetTokenInformation(token,1,None,0,C.byref(length))
        if not 0<length.value<=65536:fail('STORAGE_UNAVAILABLE')
        user=C.create_string_buffer(length.value)
        if not adv.GetTokenInformation(token,1,user,length.value,C.byref(length)):fail('STORAGE_UNAVAILABLE')
        sid=C.cast(user,C.POINTER(C.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid,C.byref(sid_string)):fail('STORAGE_UNAVAILABLE')
        value=C.wstring_at(sid_string)
        if not re.fullmatch(r'S-1-[0-9-]+',value):fail('STORAGE_UNAVAILABLE')
        sddl='D:P(A;OICI;FA;;;'+value+')(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,C.byref(descriptor),None):fail('STORAGE_UNAVAILABLE')
        yield descriptor,{value,'S-1-5-18','S-1-5-32-544'}
    finally:
        if descriptor.value:k.LocalFree(descriptor)
        if sid_string.value:k.LocalFree(sid_string)
        if token.value:k.CloseHandle(token)

def _verify_private(k,h,allowed):
    adv=C.WinDLL('advapi32',use_last_error=True,winmode=0x800)
    adv.GetSecurityInfo.argtypes=[W.HANDLE,W.DWORD,W.DWORD,C.POINTER(C.c_void_p),C.c_void_p,C.POINTER(C.c_void_p),C.c_void_p,C.POINTER(C.c_void_p)];adv.GetSecurityInfo.restype=W.DWORD
    adv.GetSecurityDescriptorControl.argtypes=[C.c_void_p,C.POINTER(W.WORD),C.POINTER(W.DWORD)];adv.GetSecurityDescriptorControl.restype=W.BOOL
    adv.GetAce.argtypes=[C.c_void_p,W.DWORD,C.POINTER(C.c_void_p)];adv.GetAce.restype=W.BOOL
    adv.ConvertSidToStringSidW.argtypes=[C.c_void_p,C.POINTER(C.c_void_p)];adv.ConvertSidToStringSidW.restype=W.BOOL
    owner=C.c_void_p();dacl=C.c_void_p();security=C.c_void_p()
    def sid_text(pointer):
        text=C.c_void_p()
        if not adv.ConvertSidToStringSidW(pointer,C.byref(text)):fail('STORAGE_UNAVAILABLE')
        try:return C.wstring_at(text)
        finally:k.LocalFree(text)
    class ACL(C.Structure):
        _fields_=[('revision',C.c_ubyte),('reserved',C.c_ubyte),('size',W.WORD),('count',W.WORD),('reserved2',W.WORD)]
    try:
        if adv.GetSecurityInfo(h,1,5,C.byref(owner),None,C.byref(dacl),None,C.byref(security)) or not dacl.value:fail('STORAGE_UNAVAILABLE')
        control=W.WORD();revision=W.DWORD()
        if not adv.GetSecurityDescriptorControl(security,C.byref(control),C.byref(revision)) or not control.value&0x1000 or sid_text(owner) not in allowed:fail('STORAGE_PATH_REJECTED')
        acl=C.cast(dacl,C.POINTER(ACL)).contents
        if not 1<=acl.count<=16:fail('STORAGE_PATH_REJECTED')
        seen=set()
        for i in range(acl.count):
            ace=C.c_void_p()
            if not adv.GetAce(dacl,i,C.byref(ace)) or C.c_ubyte.from_address(ace.value).value!=0:fail('STORAGE_PATH_REJECTED')
            sid=sid_text(ace.value+8)
            if sid not in allowed:fail('STORAGE_PATH_REJECTED')
            seen.add(sid)
        if not allowed<=seen:fail('STORAGE_PATH_REJECTED')
    finally:
        if security.value:k.LocalFree(security)

def _name(name):
    if type(name)is not str or not name or len(name)>240 or any(x in name for x in ('\\','/',':','\x00')) or name in ('.','..') or name.endswith(('.',' ')):
        fail('STORAGE_REFERENCE_INVALID')

def _open_child(parent,name,*,directory,create=False,exclusive=False,delete_on_close=False,share=1):
    _name(name);_check(parent.k,parent.h,True,parent.delete_on_close)
    buffer=C.create_unicode_buffer(name);text=_Unicode(len(name.encode('utf-16le')),C.sizeof(buffer),C.cast(buffer,C.c_void_p))
    attributes=_Attributes(C.sizeof(_Attributes),parent.h,C.pointer(text),0x1040,None,None)
    native=C.WinDLL('ntdll',use_last_error=True,winmode=0x800)
    native.NtCreateFile.argtypes=[C.POINTER(W.HANDLE),W.ULONG,C.POINTER(_Attributes),C.POINTER(_Status),C.c_void_p,W.ULONG,W.ULONG,W.ULONG,W.ULONG,C.c_void_p,W.ULONG]
    native.NtCreateFile.restype=C.c_long
    status_block=_Status();handle=W.HANDLE()
    access=0x120081 if directory else (0x120083 if create else 0x120081)
    if delete_on_close:access|=0x10000
    options=0x200020|(1 if directory else 0x40)|(0x1000 if delete_on_close else 0)
    disposition=2 if exclusive else 3 if create else 1
    def call():return native.NtCreateFile(C.byref(handle),access,C.byref(attributes),C.byref(status_block),None,0x80,share,disposition,options,None,0)
    if create:
        with _private_descriptor(parent.k) as (descriptor,allowed):
            attributes.security=descriptor;status=call()
    else:status=call()
    if status<0:
        if status&0xffffffff==0xc0000035:fail('STORAGE_OBJECT_EXISTS')
        if status&0xffffffff in (0xc0000034,0xc000003a):fail('STORAGE_OBJECT_MISSING')
        if status&0xffffffff in (0xc000050b,0xc0000280,0xc0000279):fail('STORAGE_PATH_REJECTED')
        fail('STORAGE_UNAVAILABLE')
    try:
        info=_check(parent.k,handle, directory,delete_on_close)
        if create and status_block.information==2:_verify_private(parent.k,handle,allowed)
        return Handle(parent.k,handle.value,directory,info,delete_on_close)
    except BaseException:
        parent.k.CloseHandle(handle);raise

class Handle:
    def __init__(self,k,h,directory,info,delete_on_close=False):self.k,self.h,self.directory,self.info,self.delete_on_close=k,h,directory,info,delete_on_close
    def close(self):
        if self.h is not None:self.k.CloseHandle(self.h);self.h=None
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def canonical(self):
        _check(self.k,self.h,self.directory,self.delete_on_close)
        path=_final_path(self.k,self.h);_local_drive(self.k,path);return path
    def child_directory(self,name,*,create=False,exclusive=False,delete_on_close=False):
        return _open_child(self,name,directory=True,create=create,exclusive=exclusive,delete_on_close=delete_on_close)
    def create_file(self,name,*,delete_on_close=False):
        return _open_child(self,name,directory=False,create=True,exclusive=True,delete_on_close=delete_on_close)
    def open_file(self,name):return _open_child(self,name,directory=False)
    def write_flush(self,raw):
        if self.directory or type(raw)is not bytes or len(raw)>64*1024**2:fail('STORAGE_PAYLOAD_INVALID')
        _check(self.k,self.h,False,self.delete_on_close)
        if not self.k.SetFilePointerEx(self.h,0,None,0):fail('STORAGE_UNAVAILABLE')
        for offset in range(0,len(raw),1024*1024):
            block=raw[offset:offset+1024*1024];buffer=C.create_string_buffer(block);count=W.DWORD()
            if not self.k.WriteFile(self.h,buffer,len(block),C.byref(count),None) or count.value!=len(block):fail('STORAGE_UNAVAILABLE')
        if not self.k.FlushFileBuffers(self.h):fail('STORAGE_UNAVAILABLE')
    def read(self,maximum,*,expected_size=None):
        if self.directory or type(maximum)is not int or not 0<=maximum<=64*1024**2:fail('STORAGE_PAYLOAD_INVALID')
        info=_check(self.k,self.h,False,self.delete_on_close)
        # FileStandardInfo carries a signed 64-bit size; query via handle.
        from .storage_guard import _StandardInfo
        standard=_StandardInfo()
        if not self.k.GetFileInformationByHandleEx(self.h,1,C.byref(standard),C.sizeof(standard)):fail('STORAGE_UNAVAILABLE')
        size=standard.size
        if not 0<=size<=maximum or expected_size is not None and size!=expected_size:fail('STORAGE_HASH_MISMATCH')
        if not self.k.SetFilePointerEx(self.h,0,None,0):fail('STORAGE_UNAVAILABLE')
        blocks=[];remaining=size
        while remaining:
            buffer=C.create_string_buffer(min(remaining,1024*1024));count=W.DWORD()
            if not self.k.ReadFile(self.h,buffer,len(buffer),C.byref(count),None) or not 0<count.value<=remaining:fail('STORAGE_UNAVAILABLE')
            blocks.append(buffer.raw[:count.value]);remaining-=count.value
        if _check(self.k,self.h,False,self.delete_on_close)['identity']!=info['identity']:fail('STORAGE_ROOT_CHANGED')
        return b''.join(blocks)

@contextmanager
def directory(path,*,create=False,expected=None,update_children=False):
    """Drive-root bootstrap then one child at a time, no later absolute IO."""
    path=local_path(str(path),_internal=True);k=_kernel();_local_drive(k,path)
    root=k.CreateFileW(path.anchor,0x120081,1,None,3,0x02200000,None)
    if root in (None,C.c_void_p(-1).value):fail('STORAGE_UNAVAILABLE')
    try:info=_check(k,root,True)
    except BaseException:k.CloseHandle(root);raise
    with ExitStack() as stack:
        current=stack.enter_context(Handle(k,root,True,info))
        chain=[current]
        for index, part in enumerate(path.parts[1:], start=1):
            share = 3 if update_children and index == len(path.parts) - 1 else 1
            # Allow entry updates while still holding this directory against moves.
            current=stack.enter_context(_open_child(current,part,directory=True,create=create,share=share));chain.append(current)
        if expected is not None and current.info['identity']!=expected:fail('STORAGE_ROOT_CHANGED')
        yield current
        for item in chain:_check(k,item.h,True)
