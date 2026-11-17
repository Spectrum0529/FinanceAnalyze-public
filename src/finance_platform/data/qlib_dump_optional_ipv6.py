"""One exact optional probe refusal; never a network/socket permission."""
import errno,hashlib,json,os,sys
from pathlib import Path
class OptionalIPv6Refusal:
    def __init__(self,root,sources,*,is_root):
        if type(is_root) is not bool:raise RuntimeError('OPTIONAL_PROBE_ROLE_INVALID')
        if type(sources) is not dict or set(sources)!={'socket','connection'}:raise RuntimeError('OPTIONAL_PROBE_SOURCE_REQUIRED')
        self.sources=sources;self.keys={k:os.path.normcase(os.path.abspath(v['path'])) for k,v in sources.items()}
        self.refusals=0;self.returns=0;self.records=0;self.bytes=0;self.installed=False;self.invalid=False
        self.pid=os.getpid();self.role='root' if is_root else 'official_pool_helper'
        self.stream=(root/('probe-refusal-'+str(self.pid)+'.jsonl')).open('xb',buffering=0)
        if not self._hashes_match():raise RuntimeError('OPTIONAL_PROBE_SOURCE_CHANGED')
    def _hashes_match(self):
        for entry in self.sources.values():
            raw=Path(entry['path']).read_bytes()  # Pinned runtime code, never data.
            if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:return False
        return True
    def _frame(self,frame,source,name,first,line=None):
        return (frame is not None and frame.f_code.co_name==name and frame.f_code.co_firstlineno==first
            and (line is None or frame.f_lineno==line)
            and os.path.normcase(os.path.abspath(frame.f_code.co_filename))==self.keys[source])
    def matches(self,values,caller):
        return (len(values)==4 and all(type(v) is int for v in values[1:]) and tuple(values[1:])==(23,1,0)
            and self._frame(caller,'socket','__init__',221,233)
            and self._frame(caller.f_back,'connection','_has_ipv6',114,126)
            and self._frame(caller.f_back.f_back,'connection','<module>',1,137))
    def _write(self,value):
        value={'pid':self.pid,'role':self.role,**value}
        raw=(json.dumps(value,separators=(',',':'))+'\n').encode()
        if self.records>=3 or self.bytes+len(raw)>8192:raise RuntimeError('OPTIONAL_PROBE_PROOF_BOUND')
        if self.stream.write(raw)!=len(raw):raise RuntimeError('OPTIONAL_PROBE_PROOF_WRITE_FAILED')
        self.records+=1;self.bytes+=len(raw)
    def refuse(self,values,caller):
        # Returning delegates every mismatch/repeat to the original audit
        # refusal + DENIED monitor, including unexpected socket constructors.
        if self.refusals or not self.matches(values,caller) or not self._hashes_match():return
        self._write({'event':'OPTIONAL_IPV6_CONSTRUCTOR_REFUSED','attempt':1,'family':23,'type':1,'proto':0,
            'source_sha256':self.sources['connection']['sha256'],'constructor_line':126,'module_call_line':137,
            'OS_constructor_permission':False,'refusal':'OSError(EACCES)_BEFORE_ORIGINAL_CONSTRUCTOR'})
        self.refusals=1
        raise OSError(errno.EACCES,'QLIB_OPTIONAL_IPV6_CAPABILITY_REFUSED_BEFORE_CONSTRUCTOR')
    def hook(self,event,values):
        if event=='socket.__new__':self.refuse(values,sys._getframe(1))
    def profile(self,frame,event,result):
        if event!='return' or frame.f_code.co_name!='_has_ipv6' or not self._frame(frame,'connection','_has_ipv6',114):return
        if self.refusals!=1 or self.returns or result is not False:
            self.invalid=True;self._write({'event':'UNEXPECTED_OPTIONAL_PROBE_RETURN','proof':False})
            raise RuntimeError('OPTIONAL_PROBE_NATIVE_FALLBACK_UNPROVEN')
        self._write({'event':'ORIGINAL_OPTIONAL_PROBE_RETURN','returned_false':True,'return_count':1})
        self.returns=1
    def install_profile(self):
        if sys.getprofile() is not None:raise RuntimeError('OPTIONAL_PROBE_PROFILE_ALREADY_SET')
        sys.setprofile(self.profile);self.installed=True
    def verify_after_import(self):
        if not self.installed or sys.getprofile()!=self.profile or self.invalid or (self.refusals,self.returns)!=(1,1):
            raise RuntimeError('OPTIONAL_PROBE_NATIVE_FALLBACK_UNPROVEN')
        module=sys.modules.get('urllib3.util.connection')
        if module is None or getattr(module,'HAS_IPV6',None) is not False:raise RuntimeError('OPTIONAL_PROBE_MODULE_RESULT_UNPROVEN')
        return {'expected_optional_ipv6_constructor_refusals':1,'original_HAS_IPV6':False}
    def close(self):
        try:
            if self.installed:
                if sys.getprofile()!=self.profile:raise RuntimeError('OPTIONAL_PROBE_PROFILE_CHANGED')
                sys.setprofile(None);self.installed=False
        finally:self.stream.close()
