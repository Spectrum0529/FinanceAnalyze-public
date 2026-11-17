"""Promote only this new task's verified final recorder manifest as opaque bytes.

No directory scan, model loading, checkpoint copying or native execution.
Source recorder/control roots stay managed; result logical hash is unchanged.
"""
from contextlib import closing, ExitStack
from hashlib import sha256
import json
from .database import immutable
from .errors import PlatformError
from .qlib_process import QlibProcessProfile, profile_limits
from .security import safe_json

MIGRATIONS=('''
CREATE TABLE research_native_artifact_payloads(
 run_id TEXT NOT NULL REFERENCES runs(run_id),job_id TEXT NOT NULL REFERENCES jobs(job_id),
 owner_id TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,
 reference_json TEXT NOT NULL,PRIMARY KEY(run_id,path));
''',)

class QlibArtifactArchive:
    def __init__(self,runtime):
        self.runtime=runtime
        runtime.database.apply_module_migrations('research_native_artifact_storage',MIGRATIONS)
        with runtime.database.transaction()as con:
            immutable(con,'research_native_artifact_payloads',('run_id','path'))

    def prepare(self,context,attempt,result):
        # Caller has already run original workflow.validate_result and launcher
        # final exact manifest validation. Recheck the closed finite fields here.
        from finance_platform.research.qlib_workflow import owned_path, safe_relative
        from .storage_locations import get_storage
        from .storage_native import directory
        binding=context.storage_binding
        if (binding.get('owner_id')!=context.run.owner_id or binding.get('binding_key')!='job:'+context.job_id
                or result.get('owner_id')!=context.run.owner_id or result.get('run_id')!=context.run.run_id
                or result.get('job_id')!=context.job_id or result.get('fencing_token')!=context.fencing_token):
            raise PlatformError('QLIB_ARTIFACT_BINDING_INVALID',409)
        artifacts=result.get('native_artifacts')
        if type(artifacts)is not list or not 0<len(artifacts)<=4096:
            raise PlatformError('QLIB_ARTIFACT_MANIFEST_INVALID',409)
        total=0;seen=set()
        for row in artifacts:
            if type(row)is not dict or set(row)!={'path','sha256','bytes'}:
                raise PlatformError('QLIB_ARTIFACT_MANIFEST_INVALID',409)
            relative=safe_relative(row['path'])
            if relative in seen or type(row['bytes'])is not int or not 0<=row['bytes']<=64*1024**2:
                raise PlatformError('QLIB_ARTIFACT_MANIFEST_INVALID',409)
            seen.add(relative);total+=row['bytes']
        if total>profile_limits(QlibProcessProfile.TRAIN).artifact_bytes:
            raise PlatformError('QLIB_ARTIFACT_MANIFEST_INVALID',409)
        artifact_root=owned_path(attempt,result['native_artifact_root'])
        if not artifact_root.is_relative_to(attempt/'recorders'):
            raise PlatformError('QLIB_ARTIFACT_SCOPE_INVALID',409)
        storage=get_storage(self.runtime);prepared=[]
        with directory(artifact_root) as root_handle:
            for row in artifacts:
                context.require_active()
                path=owned_path(artifact_root,row['path'])
                with ExitStack() as stack:
                    parent=root_handle
                    parts=path.relative_to(artifact_root).parts
                    for part in parts[:-1]:
                        parent=stack.enter_context(parent.child_directory(part))
                    with parent.open_file(parts[-1]) as source:
                        raw=source.read(64*1024**2,expected_size=row['bytes'])
                    if len(raw)!=row['bytes'] or sha256(raw).hexdigest()!=row['sha256']:
                        raise PlatformError('QLIB_NATIVE_ARTIFACT_CHANGED',409)
                    # Explicitly exclude SQLite content, independent of filename.
                    # Live TA checkpoints and recorder DBs are not promotion ports.
                    if raw.startswith(b'SQLite format 3\x00'):
                        raise PlatformError('QLIB_CHECKPOINT_PROMOTION_FORBIDDEN',409)
                reference=storage.put(binding,'artifacts',raw,media_type='application/octet-stream')
                prepared.append({**row,'reference':reference})
        context.require_active()
        return prepared

    def record(self,context,prepared,*,connection):
        context.assert_fenced(connection)
        for row in prepared:
            expected=(context.run.run_id,context.job_id,context.run.owner_id,row['path'],row['sha256'],row['bytes'],safe_json(row['reference']))
            old=connection.execute('SELECT run_id,job_id,owner_id,path,sha256,bytes,reference_json FROM research_native_artifact_payloads WHERE run_id=? AND path=?',(context.run.run_id,row['path'])).fetchone()
            if old is not None:
                if tuple(old)!=expected:raise PlatformError('QLIB_ARTIFACT_BINDING_INVALID',409)
            else:
                connection.execute('INSERT INTO research_native_artifact_payloads VALUES(?,?,?,?,?,?,?)',expected)

    def read(self,principal,run_id,path):
        # Supported server-owned reader, not exposed as generic HTTP/MCP IO.
        with closing(self.runtime.database.connect())as con:
            row=con.execute("SELECT a.* FROM research_native_artifact_payloads a JOIN jobs j ON j.job_id=a.job_id AND j.owner_id=a.owner_id AND j.run_id=a.run_id WHERE a.run_id=? AND a.path=? AND a.owner_id=? AND j.status='SUCCEEDED'",(run_id,path,principal.user_id)).fetchone()
        if row is None:raise PlatformError('NOT_FOUND',404)
        raw=self.runtime.storage.read(principal.user_id,json.loads(row['reference_json']),category='artifacts',maximum_bytes=64*1024**2)
        if len(raw)!=row['bytes']or sha256(raw).hexdigest()!=row['sha256']:
            raise PlatformError('STORAGE_HASH_MISMATCH',409)
        return raw
