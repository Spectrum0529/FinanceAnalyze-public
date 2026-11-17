"""Trusted project deployment for official dump, with no per-export native hash."""
from dataclasses import dataclass
import hashlib,json
from pathlib import Path
from finance_platform.core.contracts import canonical
from finance_platform.core.qlib_native import FilePin,QlibProbeCompletion
from .contracts import DataError
from .qlib_input_bridge import FIXTURE_ID,SOURCE_MANIFEST_SHA,SOURCE_LOGICAL_SHA

PROFILE_ID='FA014-QLIB-US-OFFLINE-V2'
OFFICIAL_SHA='b8f34c57ce1ef4b1772f3909735e66058f21b25bd7ab8a5f16318822401fe53f'
@dataclass(frozen=True)
class QlibDumpDeployment:
    root:Path
    worker_python:FilePin
    worker_manifest:FilePin
    worker_lock:FilePin
    worker_binding:FilePin
    runtime_identity:FilePin
    runtime_files:tuple
    probe_completion:QlibProbeCompletion
    official_dump:FilePin
    bootstrap:FilePin
    profile_source:FilePin
    source_pins:tuple
    execution_review:FilePin
    def __post_init__(self):
        if (type(self.root) is not Path and not isinstance(self.root,Path)) or not self.root.is_absolute():raise DataError('QLIB_DUMP_COMPOSITION_INVALID')
        if (type(self.probe_completion) is not QlibProbeCompletion or type(self.runtime_files) is not tuple
            or type(self.source_pins) is not tuple or len(self.runtime_files)>32 or len(self.source_pins)>32
            or any(type(p) is not FilePin for p in self.files)):raise DataError('QLIB_DUMP_TYPED_BINDING_REQUIRED')
        worker=self.root/'workers/qlib';here=Path(__file__).absolute().parent
        wanted=[(self.worker_python.path,worker/'.venv/Scripts/python.exe'),(self.worker_manifest.path,worker/'pyproject.toml'),
            (self.worker_lock.path,worker/'uv.lock'),(self.probe_completion.worker_root,worker),
            (self.official_dump.path,self.root/'vendor/qlib/scripts/dump_bin.py'),
            (self.bootstrap.path,self.root/'src/finance_platform/core/qlib_bootstrap.py'),
            (self.profile_source.path,self.root/'src/finance_platform/core/qlib_process.py')]
        if any(a!=b for a,b in wanted) or self.official_dump.sha256!=OFFICIAL_SHA:raise DataError('QLIB_DUMP_PROJECT_IDENTITY_MISMATCH')
        names={'qlib_dump.py','qlib_dump_child.py','qlib_dump_domain.py','qlib_dump_trace.py','qlib_dump_deadline.py','qlib_dump_deployment.py','qlib_preparation.py',
            'qlib_export_authorizer.py','qlib_dump_pool_gate.py','qlib_dump_optional_ipv6.py','qlib_input_bridge.py','qlib_export.py','parquet_store.py'}
        if not {here/n for n in names}<={p.path for p in self.source_pins}:raise DataError('QLIB_DUMP_SOURCE_PINS_REQUIRED')
    @property
    def files(self):
        return (self.worker_python,self.worker_manifest,self.worker_lock,self.worker_binding,self.runtime_identity,
            *self.runtime_files,self.official_dump,self.bootstrap,self.profile_source,*self.source_pins,self.execution_review,*self.probe_completion.files)
    def _payload(self):
        return {'schema':'qlib-fixed-synthetic-dump-deployment-1','root':str(self.root),'profile_id':PROFILE_ID,
            'worker_files':[p.metadata() for p in (self.worker_python,self.worker_manifest,self.worker_lock,self.worker_binding,self.runtime_identity,*self.runtime_files)],
            'probe_files':[p.metadata() for p in self.probe_completion.files],
            'code_files':[p.metadata() for p in (self.official_dump,self.bootstrap,self.profile_source,*self.source_pins)],
            'fixture_id':FIXTURE_ID,'source_manifest_sha256':SOURCE_MANIFEST_SHA,'logical_data_hash':SOURCE_LOGICAL_SHA}
    @property
    def digest(self):return hashlib.sha256(canonical(self._payload()).encode()).hexdigest()
    def verify(self):
        for p in self.files:p.read_verified()
        record=json.loads(self.worker_binding.read_verified());identity=json.loads(self.runtime_identity.read_verified())
        # Reuse exact typed FA013 actual measured probe checks, not boolean ready.
        self.probe_completion.verify(self,record,identity)
        if identity.get('version')!=[3,12,14] or identity.get('platform')!='win32' or identity.get('pointer_bits')!=64:raise DataError('QLIB_DUMP_RUNTIME_MISMATCH')
        site=self.root/'workers/qlib/.venv/Lib/site-packages'
        primary=identity.get('primary_distribution_source_files')
        if type(primary) is not list or not 1<=len(primary)<=512:raise DataError('QLIB_DUMP_SOURCE_IDENTITY_REQUIRED')
        for entry in primary:
            if (type(entry) is not dict or set(entry)!={'path','bytes','sha256'} or type(entry['path']) is not str
                or Path(entry['path']).is_absolute() or '..' in Path(entry['path']).parts
                or ':' in entry['path'] or '\\' in entry['path']):raise DataError('QLIB_DUMP_SOURCE_IDENTITY_REQUIRED')
            FilePin(site/entry['path'],entry['bytes'],entry['sha256']).read_verified()
        review=json.loads(self.execution_review.read_verified())
        # This future server-installed review is NOT created by a profile POST.
        # Old one-shot report GOs do not match or become a reusable product gate.
        if (type(review) is not dict or review.get('schema')!='qlib-synthetic-dump-profile-execution-1'
            or review.get('deployment_sha256')!=self.digest or review.get('profile_id')!=PROFILE_ID
            or review.get('scope')!='SYNTHETIC_DEVELOPMENT_ONLY' or review.get('execute') is not True
            or review.get('source_manifest_sha256')!=SOURCE_MANIFEST_SHA
            or review.get('native_seconds')!=120 or review.get('job_memory_bytes')!=2147483648
            or review.get('active_processes')!=2 or review.get('total_created_processes')!=3):
            raise DataError('QLIB_DUMP_PROFILE_EXECUTION_REVIEW_REQUIRED')
        return record,identity
    def document(self):
        _,identity=self.verify();by_path={p.path:p for p in self.runtime_files}
        try:base=by_path[Path(identity['base_python']['path'])];dll=by_path[Path(identity['actual_loaded_python_DLL']['path'])]
        except (KeyError,TypeError):raise DataError('QLIB_DUMP_RUNTIME_MISMATCH') from None
        own={p.path.name:p for p in self.source_pins};prefix=self.root/'workers/qlib/.venv'
        site=prefix/'Lib/site-packages'
        try:optional={'socket':by_path[base.path.parent/'Lib/socket.py'].metadata(),'connection':by_path[site/'urllib3/util/connection.py'].metadata()}
        except KeyError:raise DataError('QLIB_OPTIONAL_PROBE_SOURCE_PINS_REQUIRED')
        return {'schema':'qlib-fixed-synthetic-dump-child-binding-1','deployment_sha256':self.digest,
            'runtime_root':str(self.root),'fixture_id':FIXTURE_ID,'logical_data_hash':SOURCE_LOGICAL_SHA,
            'base_python':base.metadata(),'python_dll':dll.metadata(),'site_root':str(prefix/'Lib/site-packages'),'venv_prefix':str(prefix),
            'official_dump':self.official_dump.metadata(),'identity_helper':self.bootstrap.metadata(),'profile_source':self.profile_source.metadata(),
            'launch_contract':own['qlib_dump_domain.py'].metadata(),'diagnostic_source':own['qlib_dump_trace.py'].metadata(),
            'optional_probe_sources':optional,
            'child_sources':[p.metadata() for p in (own['qlib_dump_child.py'],own['qlib_dump_domain.py'],own['qlib_dump_trace.py'],own['qlib_dump_pool_gate.py'],own['qlib_dump_optional_ipv6.py'],self.bootstrap,self.official_dump)]+list(optional.values()),
            'inputs':[p.metadata() for p in self.files]}
