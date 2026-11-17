"""Child-local exact CPython pool call validation for CPython issue159237.

Policy OFFICIAL_POOL_FLAGS_0_TO_DETACHED_8_V1 supersedes the historical
no-argument-modification contract for this exact official pool call only.
The C audit command_line field is broken in 3.12.14. Validate all nine incoming
arguments (including flags exactly0), then translate only creationflags to8
(Windows DETACHED_PROCESS). Keep the other eight arguments byte/value exact,
call the original WinAPI, and bind its one audit event to the same thread.
Original root/thread/command/parent/pipe/two-call/Job restrictions remain.
This is an application guard, not isolation against hostile Python.
"""
import _thread
import re
from pathlib import Path

class CanonicalPoolGate:
    def __init__(self,base,pid,is_root,*,thread_id=_thread.get_ident):
        self.base=str(Path(base));self.pid=pid;self.is_root=is_root
        self.thread_id=thread_id;self.root_thread=thread_id();self.count=0;self.pending=None
        prefix=' '.join('"%s"'%x for x in [self.base,'-B','-S','-I','-X','utf8','-c'])
        prefix+=' "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid='+str(pid)+', pipe_handle='
        self.pattern=re.compile(re.escape(prefix)+r'([1-9][0-9]{0,19})'+re.escape(')" "--multiprocessing-fork"'))
    def validate(self,args,kwargs):
        if (not self.is_root or self.count>=2 or self.pending is not None or self.thread_id()!=self.root_thread
            or kwargs or len(args)!=9 or args[0]!=self.base or type(args[1]) is not str
            or args[2] is not None or args[3] is not None or args[4] is not False
            or type(args[5]) is not int or args[5]!=0 or any(v is not None for v in args[6:])):
            raise RuntimeError('QLIB_EXACT_POOL_CALL_REQUIRED')
        match=self.pattern.fullmatch(args[1])
        if match is None or int(match.group(1))>=2**64:raise RuntimeError('QLIB_EXACT_POOL_COMMAND_REQUIRED')
    def wrap(self,original):
        def create(*args,**kwargs):
            self.validate(args,kwargs)
            proof={'thread':self.thread_id(),'application':args[0],'cwd':args[7],'used':False}
            self.pending=proof
            try:
                # Only after exact incoming validation: flags0 -> DETACHED8.
                delegated_args=(*args[:5],0x00000008,*args[6:])
                result=original(*delegated_args,**kwargs)
                if not proof['used']:raise RuntimeError('QLIB_ORIGINAL_AUDIT_EVENT_REQUIRED')
                return result
            finally:self.pending=None
        return create
    def audit(self,values):
        proof=self.pending
        if (proof is None or proof['used'] or proof['thread']!=self.thread_id() or len(values)!=3
            or values[0]!=proof['application'] or values[2]!=proof['cwd']):return False
        # Do not inspect the corrupted command_line object. The exact original
        # input was checked before entering C; each proof can be consumed once.
        proof['used']=True;self.count+=1
        return True
    def install(self,module):
        original=module.CreateProcess
        if (type(original).__name__!='builtin_function_or_method' or original.__module__!='_winapi'
            or original.__name__!='CreateProcess'):raise RuntimeError('QLIB_ORIGINAL_WINAPI_REQUIRED')
        module.CreateProcess=self.wrap(original)
