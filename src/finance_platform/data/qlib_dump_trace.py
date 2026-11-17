"""Bounded labels/time; root watchdog writes only existing private stderr."""
import atexit
import faulthandler
import json
import os
from pathlib import Path
import sys
import time
LABELS=frozenset('ENTRY_VALIDATED TRACE_ARMED INITIAL_PINS_BEGIN INITIAL_PIN_DONE INITIAL_PINS_DONE WAIT_QUERY QUERY_RECEIVED READY_SENT WAIT_EXEC EXEC_RECEIVED POSTEXEC_PINS_BEGIN POSTEXEC_PIN_DONE POSTEXEC_PINS_DONE AUDIT_INIT_BEGIN AUDIT_INSTALLED IDENTITY_BEGIN IDENTITY_DONE DUMP_IMPORT_BEGIN DUMP_IMPORT_DONE CSV_VERIFY_BEGIN CSV_ITEM_DONE CSV_VERIFY_DONE DUMP_INIT_BEGIN DUMP_INIT_DONE DUMP_CALL_BEGIN DUMP_CALL_DONE PROVIDER_IMPORT_BEGIN PROVIDER_IMPORT_DONE PROVIDER_VERIFY_BEGIN PROVIDER_SYMBOL_DONE PROVIDER_VERIFY_DONE COMPLETE'.split())
MAX_RECORDS=64
MAX_PHASE_BYTES=8192
STACK_INTERVAL_SECONDS=45
class PhaseTrace:
    def __init__(self,directory,is_root):
        if type(is_root) is not bool:raise RuntimeError('DIAGNOSTIC_ROLE_INVALID')
        self.root=is_root;self.start=time.monotonic();self.records=0;self.bytes=0;self.armed=False;self.closed=False
        self.stream=(Path(directory)/('phase-'+str(os.getpid())+'.jsonl')).open('xb',buffering=0)
        atexit.register(self.finish)
    def emit(self,label,index=None):
        if type(label) is not str or label not in LABELS or index is not None and (type(index) is not int or not 0<=index<=16):
            raise RuntimeError('DIAGNOSTIC_LABEL_INVALID')
        record={'seq':self.records,'ms':int((time.monotonic()-self.start)*1000),'pid':os.getpid(),'role':'root' if self.root else 'helper','label':label}
        if index is not None:record['index']=index
        raw=(json.dumps(record,separators=(',',':'))+'\n').encode()
        if self.closed or self.records>=MAX_RECORDS or self.bytes+len(raw)>MAX_PHASE_BYTES:raise RuntimeError('DIAGNOSTIC_PHASE_BUDGET')
        if self.stream.write(raw)!=len(raw):raise RuntimeError('DIAGNOSTIC_PHASE_WRITE_FAILED')
        self.records+=1;self.bytes+=len(raw)
    def arm(self):
        if self.root and not self.armed:
            faulthandler.dump_traceback_later(STACK_INTERVAL_SECONDS,repeat=True,file=sys.stderr,exit=False)
            self.armed=True;self.emit('TRACE_ARMED')
    def finish(self):
        if self.closed:return
        if self.armed:faulthandler.cancel_dump_traceback_later();self.armed=False
        self.stream.close();self.closed=True
