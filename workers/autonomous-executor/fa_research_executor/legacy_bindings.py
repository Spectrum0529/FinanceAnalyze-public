"""Optional reviewed historical records; never accepted from a research request."""
from copy import deepcopy
import hashlib
import os
import re
import stat
from .protocol import require, strict_json

_identity = None
_configured = False

def configured():
    return _configured

def require_configured():
    require(_configured, 'HISTORICAL_BINDING_UNAVAILABLE')

def validate(raw):
    value = strict_json(raw,65536)
    require(type(value) is dict and set(value)=={'schema','recovery','retained'}
            and value['schema']=='financeanalyze-legacy-binding-1','HISTORICAL_BINDING_INVALID')
    recovery,retained=value['recovery'],value['retained']
    names={'PLAN','BINDING','BINDING_SHA','OLD_RUNTIME','CID','REQUEST_SHA','PRESTART_SHA','CONTRACT_SHA','WRITER_PROOF_SHA','IMAGE','PROFILE'}
    require(type(recovery)is dict and set(recovery)==names,'HISTORICAL_BINDING_INVALID')
    def matched(pattern,item): return type(item)is str and re.fullmatch(pattern,item) is not None
    uuid=r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
    require(matched(uuid,recovery['PLAN']),'HISTORICAL_BINDING_INVALID')
    binding=recovery['BINDING']
    require(type(binding)is dict and set(binding)=={'attempt_id','job_id','run_id','session_id','fencing_token'}
            and all(matched(uuid,binding[k])for k in ('attempt_id','job_id','run_id','session_id'))
            and binding['attempt_id']==recovery['PLAN'] and type(binding['fencing_token'])is int
            and binding['fencing_token']>0,'HISTORICAL_BINDING_INVALID')
    for key in names-{'PLAN','BINDING','IMAGE','PROFILE'}:
        require(matched('[a-f0-9]{64}',recovery[key]),'HISTORICAL_BINDING_INVALID')
    require(matched('sha256:[a-f0-9]{64}',recovery['IMAGE']) and matched('[A-Za-z0-9_.-]{1,128}',recovery['PROFILE']),
            'HISTORICAL_BINDING_INVALID')
    from .protocol import digest
    require(digest(binding)==recovery['BINDING_SHA'],'HISTORICAL_BINDING_INVALID')
    require(type(retained)is dict and set(retained)=={'OWNER_UID','READONLY_SHA','PROVENANCE_SHA','FILES','BLOCKED_OPERATION'}
            and retained['OWNER_UID']==1000 and type(retained['OWNER_UID'])is int,'HISTORICAL_BINDING_INVALID')
    require(matched('[a-f0-9]{64}',retained['READONLY_SHA']) and matched('[a-f0-9]{64}',retained['PROVENANCE_SHA'])
            and matched('[a-f0-9]{32}',retained['BLOCKED_OPERATION']),'HISTORICAL_BINDING_INVALID')
    files=retained['FILES']
    require(type(files)is list and len(files)==3,'HISTORICAL_BINDING_INVALID')
    for row in files:
        require(type(row)is list and len(row)==5 and matched(uuid,row[0])
                and all(matched('[a-f0-9]{64}',item)for item in row[1:4])
                and type(row[4])is list and len(row[4])==5
                and all(type(n)is int and n>=0 for n in row[4]) and 0<row[4][2]<=16384,
                'HISTORICAL_BINDING_INVALID')
    require(len({row[0]for row in files})==3,'HISTORICAL_BINDING_INVALID')
    return value

def configure(manifest):
    """Called after entrypoint validates the pinned runtime manifest and paths."""
    global _identity,_configured
    expected=manifest.get('dependencies',{}).get('legacy_recovery_sha256')
    identity=expected or 'NO_HISTORICAL_BINDING'
    require(_identity is None or _identity==identity,'HISTORICAL_BINDING_CHANGED')
    if _identity==identity:return
    if expected is None:
        _identity=identity
        return
    require(type(expected)is str and re.fullmatch('[a-f0-9]{64}',expected),'HISTORICAL_BINDING_INVALID')
    from .paths import executor_root
    path=executor_root()/'deployment/legacy-recovery.json'
    for parent in (path.parent,*path.parent.parents):
        require(not parent.is_symlink() and parent.resolve()==parent,'HISTORICAL_BINDING_PATH_INVALID')
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
    with os.fdopen(os.open(path,flags),'rb')as stream:
        info=os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=65536
                and info.st_uid==os.geteuid() and not info.st_mode&0o077,'HISTORICAL_BINDING_PATH_INVALID')
        raw=stream.read(65537)
    require(hashlib.sha256(raw).hexdigest()==expected,'HISTORICAL_BINDING_CHANGED')
    value=validate(raw)
    from . import recovery,retained_closure
    for key,item in value['recovery'].items():setattr(recovery,key,deepcopy(item))
    for key,item in value['retained'].items():
        if key=='FILES':item=tuple((r[0],r[1],r[2],r[3],tuple(r[4]))for r in item)
        setattr(retained_closure,key,deepcopy(item))
    _configured=True
    _identity=identity
