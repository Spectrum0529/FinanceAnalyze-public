"""r35 exact one-plan recovery validation; no process, SQL or Docker effects."""
import hashlib
import math
import re
import time
from pathlib import PurePosixPath
from .protocol import require, exact, sha, digest, strict_json, validate_request
from .supervisor import CleanupBudget

PLAN=None # optional locally reviewed historical binding
BINDING=None # optional locally reviewed historical binding
BINDING_SHA=None # optional locally reviewed historical binding
OLD_RUNTIME=None # optional locally reviewed historical binding
CID=None # optional locally reviewed historical binding
REQUEST_SHA=None # optional locally reviewed historical binding
PRESTART_SHA=None # optional locally reviewed historical binding
CONTRACT_SHA=None # optional locally reviewed historical binding
WRITER_PROOF_SHA=None # optional locally reviewed historical binding
IMAGE=None # optional locally reviewed historical binding
PROFILE=None # optional locally reviewed historical binding
from .paths import executor_root
STAGING=str(executor_root()/'runtime/staging')


class RecoveryBudget(CleanupBudget):
    """Fresh fixed15s clock; reuse unchanged absolute POSIX self-timer code."""
    def __init__(self,*,clock=time.monotonic):
        super().__init__(clock=clock)
        self.started=clock();self.deadline=self.started+15
    def check(self):
        now=self.clock()
        require(math.isfinite(now) and self.started<=now,'RECOVERY_CLOCK_INVALID')
        require(now<self.deadline,'RECOVERY_DEADLINE_EXCEEDED')
    def begin(self):self.check()
    enter=begin
    def timeout(self,maximum):
        self.check();return min(maximum,self.remaining())
    def snapshot(self):
        return {'limit_ms':15000,'remaining_ms':max(0,int(self.remaining()*1000)),
                'kernel_enforced':self.close_timer is not None}


def validate_header(header):
    from .legacy_bindings import require_configured
    require_configured()
    exact(header,'schema operation_id contract_sha256 writer_proof_sha256 verifier_runtime_sha256 verifier_manifest_sha256 original_prestart_receipt')
    require(header['schema']=='fa-retained-plan-recovery-request-1'
            and type(header['operation_id'])is str and re.fullmatch('[a-f0-9]{32}',header['operation_id'])
            and header['contract_sha256']==CONTRACT_SHA and header['writer_proof_sha256']==WRITER_PROOF_SHA,
            'RECOVERY_AUTHORITY_MISMATCH')
    sha(header['verifier_runtime_sha256']);sha(header['verifier_manifest_sha256'])
    require(header['original_prestart_receipt']=={
        'request_sha256':REQUEST_SHA,'container_id':CID,'image_id':IMAGE,
        'profile':PROFILE,'inspect_sha256':PRESTART_SHA},'RECOVERY_PRESTART_BINDING_MISMATCH')
    return digest(header)


def request_path(name):
    from .legacy_bindings import require_configured
    require_configured()
    require(type(name)is str and re.fullmatch('fa-attempt-[a-f0-9]{32}',name),'RECOVERY_NAME_INVALID')
    return str(PurePosixPath(STAGING)/('input-'+name[11:])/'request.json')


def _profile(attrs,name,stage):
    from .legacy_bindings import require_configured
    require_configured()
    """Same original static controller profile, with truthful stopped state.

    Image immutability is bound by exact r35 image ID, without another Docker
    image/info API. No created-state substitution or new prestart receipt.
    """
    from .controller import ENTRY,ENV,TMPFS
    a=attrs;h=a['HostConfig'];c=a['Config'];state=a['State']
    require(a['Id']==CID and a['Name']=='/'+name and a['Image']==IMAGE,'RECOVERY_OBJECT_IDENTITY_MISMATCH')
    require(c['Labels']=={'finance.executor_owner':'fa015-autonomous-r01','finance.attempt':name[11:]},'RECOVERY_OBJECT_IDENTITY_MISMATCH')
    require(c['User']=='10001:10001' and c['Entrypoint']==ENTRY and not c.get('Cmd') and not c.get('Tty')
        and c['WorkingDir']=='/tmp' and not c.get('Healthcheck'),'RECOVERY_PROFILE_MISMATCH')
    expected={'PATH':'/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
        'LANG':'C.UTF-8','PYTHON_VERSION':'3.12.14',
        'PYTHON_SHA256':'5c8462af5790baf43a321a1559dbe0db06d1be4300fb85fb53c40060668e548a',**ENV}
    require(sorted(c['Env'])==sorted(k+'='+v for k,v in expected.items()),'RECOVERY_PROFILE_MISMATCH')
    require(h['NetworkMode']=='none' and h['ReadonlyRootfs']is True and h['Privileged']is False
        and h['CapDrop']==['ALL'] and not h.get('CapAdd') and h['SecurityOpt']==['no-new-privileges=true'],'RECOVERY_PROFILE_MISMATCH')
    require(h.get('PidMode','')=='' and h['IpcMode']=='private' and h['CgroupnsMode']=='private'
        and h.get('UsernsMode','')!='host','RECOVERY_PROFILE_MISMATCH')
    require(not any(h.get(k)for k in('Devices','DeviceRequests','PortBindings','ExtraHosts','Binds','Links','VolumesFrom')),'RECOVERY_PROFILE_MISMATCH')
    require(h['Memory']==1073741824 and h['MemorySwap']==1073741824 and h['PidsLimit']==64
        and h['NanoCpus']==1000000000 and h['Tmpfs']==TMPFS and h['Init']is True,'RECOVERY_PROFILE_MISMATCH')
    require(h['RestartPolicy']['Name']=='no' and h['LogConfig']['Type']=='none'
        and {v['Name']:(v['Soft'],v['Hard'])for v in h['Ulimits']}=={'nofile':(128,128),'core':(0,0)},'RECOVERY_PROFILE_MISMATCH')
    mounts=a['Mounts'];bind=[m for m in mounts if m['Type']=='bind']
    require(len(bind)==1 and bind[0]['Source']==stage and bind[0]['Destination']=='/input'
        and bind[0]['RW']is False and bind[0]['Propagation']=='rprivate'
        and all(m['Type']=='bind' or m['Type']=='tmpfs' and m['Destination']in TMPFS for m in mounts),'RECOVERY_PROFILE_MISMATCH')
    require(state['Status']in {'created','exited'} and state['Running']is False and state['Restarting']is False
        and type(state['Pid'])is int and state['Pid']==0 and 'ExecIDs'in a and a['ExecIDs']in(None,[])
        and type(state['ExitCode'])is int,'RECOVERY_OBJECT_NOT_STOPPED')


def validate_observation(snapshot,value,budget):
    from .legacy_bindings import require_configured
    require_configured()
    exact(value,'request_bytes path_evidence samples')
    raw=value['request_bytes'];require(type(raw)is bytes and len(raw)<=4194304,'RECOVERY_REQUEST_SIZE_INVALID')
    require(hashlib.sha256(raw).hexdigest()==REQUEST_SHA,'RECOVERY_REQUEST_HASH_MISMATCH')
    claim=snapshot['claim'];path=request_path(claim['name'])
    require(len(raw)<=claim['request_limit'],'EXECUTOR_REQUEST_EXCEEDS_RESERVATION')
    facts=value['path_evidence'];exact(facts,'path resolved_path is_regular nlink no_symlink_components size')
    require(facts['path']==path and facts['resolved_path']==path and facts['is_regular']is True
        and facts['no_symlink_components']is True and type(facts['nlink'])is int and facts['nlink']==1
        and type(facts['size'])is int and facts['size']==len(raw),'RECOVERY_REQUEST_PATH_UNPROVEN')
    request=validate_request(strict_json(raw,4194304))
    require(all(request[k]==v for k,v in BINDING.items()) and request['runtime_sha256']==OLD_RUNTIME
        and request['reservation_id']==PLAN and request['experiment_id']==PLAN+':'+claim['symbol']
        and len(request['input']['rows'])-1==claim['ordinal'],'RECOVERY_REQUEST_BINDING_MISMATCH')
    samples=value['samples'];require(type(samples)is list and len(samples)==2,'RECOVERY_TWO_SAMPLES_REQUIRED')
    safe=[];previous=None;profile_sha=None;state_preimage=None
    for sample in samples:
        budget.check();exact(sample,'at_monotonic attrs');at=sample['at_monotonic']
        require(type(at)in(int,float) and math.isfinite(at) and budget.started<=at<=budget.clock()
            and (previous is None or at>=previous+.05),'RECOVERY_SAMPLE_TIME_INVALID')
        _profile(sample['attrs'],claim['name'],str(PurePosixPath(path).parent));previous=at
        attrs=sample['attrs'];state=attrs['State']
        current_profile=digest({k:attrs[k]for k in('Image','Config','HostConfig','Mounts')})
        current_state={k:state[k]for k in('Status','Running','Restarting','Pid','ExitCode')}
        require(profile_sha is None or(profile_sha==current_profile and state_preimage==current_state),'RECOVERY_CURRENT_SAMPLES_CHANGED')
        profile_sha=current_profile;state_preimage=current_state
        safe.append({'at_monotonic':at,'inspect_sha256':digest(sample['attrs']),'container_id':CID,
                     'status':state['Status'],'running':False,'restarting':False,'pid':0,
                     'exit_code':state.get('ExitCode')})
    budget.check()
    return {'request_sha256':REQUEST_SHA,'path_evidence':facts,'samples':safe,
            'request_binding_sha256':digest(BINDING),'source_sha256':request['source_sha256'],
            'input_sha256':request['input_sha256']}
