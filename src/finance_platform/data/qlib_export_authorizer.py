"""Thin completed-export VersionRef/rights/stage adapter for FA015 r02.

Construction is trusted server composition, outside resolver transactions.
No client path, owner declaration, synthetic flag or prepared-only export can
create a completed grant. Positive native registration awaits a real r04 dump.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from finance_platform.core.auth import Principal, require_writer
from finance_platform.core.contracts import VersionRef, payload, identifier, fingerprint, canonical
from finance_platform.core.database import immutable
from finance_platform.core.jobs import WorkerJobContext
from finance_platform.core.pagination import validate_pagination
from finance_platform.core.security import safe_json, now_text
from .contracts import DataError
from .service import DataService
from .provider_sdk.artifacts import regular_owned
from .qlib_input_bridge import OwnedQlibInputBridge, SOURCE_MANIFEST_SHA, SOURCE_LOGICAL_SHA, FIXTURE_ID

MIGRATIONS=("""CREATE TABLE data_qlib_ready_exports(
 resource_id TEXT NOT NULL,version TEXT NOT NULL,owner_id TEXT NOT NULL,
 native_id TEXT NOT NULL UNIQUE,body TEXT NOT NULL,content_hash TEXT NOT NULL,
 created_at TEXT NOT NULL,PRIMARY KEY(resource_id,version));
 CREATE TABLE data_qlib_training_stages(
 stage_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,run_id TEXT NOT NULL UNIQUE,
 job_id TEXT NOT NULL,fencing_token INTEGER NOT NULL,body TEXT NOT NULL,
 content_hash TEXT NOT NULL,created_at TEXT NOT NULL);""",)
FIELDS=('open','high','low','close','vwap','volume','factor')

def _hash(value):return hashlib.sha256(safe_json(value).encode()).hexdigest()
def _unique_pairs(items):
    value={}
    for key,item in items:
        if key in value:raise DataError('QLIB_PARENT_MARKER_INVALID')
        value[key]=item
    return value
def _reference(value):
    if type(value) is not dict or set(value)!={'resource_id','version','content_hash'}:raise DataError('QLIB_REFERENCE_INVALID')
    try:result=VersionRef(**value)
    except (ValueError,TypeError):raise DataError('QLIB_REFERENCE_INVALID') from None
    return result
def validate_register_parameters(value):
    if type(value) is not dict or set(value)!={'native_id'}:raise DataError('INVALID_INPUT')
    identifier(value['native_id'])

class QlibExportAuthorizer:
    def __init__(self,runtime,bridge,workflow_contract):
        if type(bridge) is not OwnedQlibInputBridge or bridge.db is not runtime.database or bridge.policy.root!=runtime.root.resolve():
            raise DataError('QLIB_ADAPTER_COMPOSITION_INVALID')
        if not callable(getattr(workflow_contract,'validate_input',None)):raise DataError('QLIB_ADAPTER_COMPOSITION_INVALID')
        self.runtime,self.bridge,self.db,self.contract=runtime,bridge,runtime.database,workflow_contract
        self.policy=bridge.policy;self.versions=runtime.versions
        self.data=DataService(self.db,self.policy)
        self.db.apply_module_migrations('data_qlib_ready_exports_1',MIGRATIONS)
        with self.db.transaction() as con:
            immutable(con,'data_qlib_ready_exports',('resource_id','version'))
            immutable(con,'data_qlib_training_stages',('stage_id',))
    def _principal(self,principal,*,write=False):
        if type(principal) is not Principal:raise DataError('QLIB_PRINCIPAL_REQUIRED')
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT username,role,enabled FROM users WHERE user_id=?',(principal.user_id,)).fetchone()
        if row is None or not row['enabled'] or (row['username'],row['role'])!=(principal.username,principal.role):
            raise DataError('QLIB_PRINCIPAL_CHANGED')
        if write:require_writer(principal)
    def _context(self,principal,context,handler):
        self._principal(principal,write=True)
        if type(context) is not WorkerJobContext or context.service.database is not self.db or context.run.owner_id!=principal.user_id or context.run.handler!=handler:
            raise DataError('QLIB_CONTEXT_BINDING_INVALID')
        context.require_active()
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT j.owner_id,j.handler,j.run_id,r.owner_id AS run_owner,r.spec_json,r.spec_hash FROM jobs j JOIN runs r USING(run_id) WHERE j.job_id=?',
                (context.job_id,)).fetchone()
        if (row is None or row['owner_id']!=principal.user_id or row['run_owner']!=principal.user_id
            or row['handler']!=handler or row['run_id']!=context.run.run_id
            or hashlib.sha256(row['spec_json'].encode()).hexdigest()!=row['spec_hash']
            or canonical(context.run)!=row['spec_json']):
            raise DataError('QLIB_CONTEXT_BINDING_INVALID')
    def _version(self,principal,reference):
        ref=_reference(reference)
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT owner_id,kind FROM resource_versions WHERE resource_id=? AND version=?',(ref.resource_id,ref.version)).fetchone()
        # Strict owner equality includes admins; core VersionService's broader
        # admin read scope never expands a Qlib data grant.
        if row is None or row['owner_id']!=principal.user_id or row['kind']!='data':raise DataError('QLIB_EXPORT_OWNER_OR_KIND_INVALID')
        value=self.versions.get(principal,ref)
        if _hash(value)!=ref.content_hash:raise DataError('QLIB_VERSION_CONTENT_CHANGED')
        return value
    def fixed_source_definition(self,owner):
        """Server configuration for the already reviewed V2 synthetic fixture."""
        identifier(owner)
        return {'source_id':'qlib-v2-source-'+hashlib.sha256(owner.encode()).hexdigest()[:32],
            'name':'Reviewed fixed Qlib V2 synthetic fixture','provider':'synthetic',
            'capabilities':['historical'],'assets':['US_EQUITY'],'frequencies':['1Day'],'fields':list(FIELDS),
            'declared_history':[],'verified_coverage':[],
            'license':{'license_id':'qlib-v2-private-development','evidence':SOURCE_MANIFEST_SHA,
                'use_allowed':['development'],'rights_verified':False},
            'connection':dict.fromkeys(['endpoint','key_env','secret_env','user_agent_env','feed'])}
    def _source(self,owner):
        expected=self.fixed_source_definition(owner)
        source=self.data.source(owner,expected['source_id'])
        self.data._rights(source,'SYNTHETIC','development')
        base={k:v for k,v in source.items() if k not in {'version','content_hash'}}
        if base!=expected:raise DataError('QLIB_SOURCE_BINDING_CHANGED')
        return source
    def _install_fixed_source_if_absent(self,principal,context):
        """One fixed source row, using DataService's existing immutable schema.

        Inserting only revision1 under the same writer lock prevents a race
        with register_source from restoring permissions of a newer revision.
        No generic source/update API or client declaration is implemented here.
        """
        definition=self.fixed_source_definition(principal.user_id)
        with self.db.transaction() as con:
            context.assert_fenced(con);self._principal(principal,write=True)
            row=con.execute('SELECT owner_id FROM data_sources WHERE source_id=? LIMIT 1',(definition['source_id'],)).fetchone()
            if row is not None:
                if row['owner_id']!=principal.user_id:raise DataError('RESOURCE_NOT_FOUND')
                return
            con.execute('INSERT INTO data_sources VALUES(?,?,?,?,?,?)',
                (definition['source_id'],1,principal.user_id,canonical(definition),fingerprint(definition),now_text()))
    def _native(self,owner,native_id):
        native=self.bridge.get_native(owner,native_id)
        with closing(self.db.connect()) as con:
            job=con.execute('SELECT status FROM jobs WHERE owner_id=? AND run_id=?',(owner,native['run_id'])).fetchone()
        if job is None or job['status']!='SUCCEEDED':raise DataError('QLIB_NATIVE_CORE_SUCCESS_REQUIRED')
        if native.get('state')!='VERIFIED_NATIVE_PROVIDER_PUBLISHED' or native.get('logical_data_hash')!=SOURCE_LOGICAL_SHA:
            raise DataError('QLIB_NATIVE_COMPLETION_REQUIRED')
        input_body=self.bridge.get(owner,native['export_id'])
        consumption=input_body.get('source_consumption',{})
        if (input_body.get('fixture_id')!=FIXTURE_ID or input_body.get('owner_id')!=owner
            or input_body['run_id']!=native['run_id'] or input_body['logical_data_hash']!=SOURCE_LOGICAL_SHA
            or consumption.get('source_manifest_ref',{}).get('sha256')!=SOURCE_MANIFEST_SHA
            or consumption.get('catalog_sha256')!=fingerprint(self.bridge.catalog.document)):
            raise DataError('QLIB_SOURCE_BINDING_CHANGED')
        directory=self.bridge._directory(owner,input_body['run_id'],input_body['export_id'])
        raw=self.policy.read_bytes(regular_owned(self.policy.root,(directory/'input_manifest.json').relative_to(self.policy.root).as_posix()))
        if hashlib.sha256(raw).hexdigest()!=native['owner_manifest_sha256']:raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        for item in input_body['instruments']:self.bridge.parquet._final_bytes(owner,item['parquet'])
        expected=directory/'native-attempts'/native['attempt_id']/'native'
        if Path(native['provider_uri'])!=expected:raise DataError('QLIB_EXPORT_PATH_INVALID')
        files=sorted([{'path':item['relative'],'bytes':item['bytes'],'sha256':item['sha256']} for item in native['outputs']],key=lambda i:i['path'])
        data={'fixture_id':FIXTURE_ID,'scope':'SYNTHETIC','calendar':input_body['calendar'],
            'instruments':input_body['universe'],'benchmark':input_body['benchmark'],'fields':list(FIELDS),
            'logical_data_hash':SOURCE_LOGICAL_SHA,'snapshot_content_hash':None,'selected_view_hash':None,
            'native_manifest_hash':_hash({'schema':'qlib-native-provider-files-1','files':files}),
            'native_dump_succeeded':True,'export_storage_dtype':'float32',
            'factor_unit_contract':'FA014_SYNTHETIC_SPLIT_NORMALIZED_FACTOR_VOLUME_V1',
            'feature_max_lead_sessions':0,'provider_files':files}
        self.contract.validate_input(data)
        csv=[{'symbol':i['symbol'],**i['CSV']} for i in input_body['instruments']]
        parquet=[{'symbol':i['symbol'],**i['parquet']} for i in input_body['instruments']]
        provenance={'native_id':native_id,'native_receipt_hash':native['content_hash'],
            'owner_manifest_sha256':native['owner_manifest_sha256'],'source_manifest_sha256':SOURCE_MANIFEST_SHA,
            'logical_data_hash':SOURCE_LOGICAL_SHA,'CSV_manifest_sha256':_hash(csv),'Parquet_manifest_sha256':_hash(parquet),
            'native_manifest_hash':data['native_manifest_hash'],'export_id':native['export_id']}
        return native,data,provenance
    def register_completed(self,principal,native_id,*,context):
        self._context(principal,context,'data.qlib-register');identifier(native_id)
        native,data,provenance=self._native(principal.user_id,native_id)
        # Called only after a real native receipt and its core SUCCEEDED job.
        # Source metadata alone is never a completed-export VersionRef/grant.
        self._install_fixed_source_if_absent(principal,context)
        source=self._source(principal.user_id)
        source_value={'schema':'qlib-reviewed-fixture-source-1','fixture_id':FIXTURE_ID,
            'source_id':source['source_id'],'source_version':source['version'],'source_hash':source['content_hash'],
            'license_hash':fingerprint(source['license']),'source_manifest_sha256':SOURCE_MANIFEST_SHA,
            'logical_data_hash':SOURCE_LOGICAL_SHA,'scope':'SYNTHETIC','purpose':'development_readonly'}
        source_ref=payload(VersionRef(source['source_id'],str(source['version']),_hash(source_value)))
        value={'schema':'qlib-completed-export-1','owner_id':principal.user_id,'source_reference':source_ref,
            'source_value':source_value,'data':data,'provenance':provenance}
        reference=payload(VersionRef('qlib-export-'+native_id,'1',_hash(value)))
        record={'export_reference':reference,'source_reference':source_ref,'source_value':source_value,
            'native_id':native_id,'provenance':provenance,'data':data}
        context.require_active()
        with self.db.transaction() as con:
            context.assert_fenced(con);self._principal(principal,write=True)
            if self._source(principal.user_id)!=source or self._native(principal.user_id,native_id)[1:]!=(data,provenance):
                raise DataError('QLIB_AUTHORIZATION_CHANGED')
            old=con.execute('SELECT body,content_hash FROM data_qlib_ready_exports WHERE native_id=? AND owner_id=?',(native_id,principal.user_id)).fetchone()
            if old:
                if json.loads(old['body'])!=record or old['content_hash']!=_hash(record):raise DataError('QLIB_EXPORT_REGISTRATION_CHANGED')
                return reference
            context.assert_fenced(con)  # Revalidation may outlive the lease.
            for ref,value_to_store in [(source_ref,source_value),(reference,value)]:
                self.versions._store(con,principal.user_id,ref['resource_id'],ref['version'],'data',safe_json(value_to_store),ref['content_hash'])
            con.execute('INSERT INTO data_qlib_ready_exports VALUES(?,?,?,?,?,?,?)',
                (reference['resource_id'],reference['version'],principal.user_id,native_id,safe_json(record),_hash(record),now_text()))
        return reference
    def authorize(self,principal,export_reference,*,purpose):
        if purpose!='development_readonly':raise DataError('QLIB_PURPOSE_FORBIDDEN')
        self._principal(principal);ref=_reference(export_reference)
        value=self._version(principal,export_reference)
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT body,content_hash FROM data_qlib_ready_exports WHERE resource_id=? AND version=? AND owner_id=?',
                (ref.resource_id,ref.version,principal.user_id)).fetchone()
        if row is None:raise DataError('QLIB_COMPLETED_EXPORT_NOT_REGISTERED')
        record=json.loads(row['body'])
        if (_hash(record)!=row['content_hash'] or record['export_reference']!=export_reference
            or value!={'schema':'qlib-completed-export-1','owner_id':principal.user_id,'source_reference':record['source_reference'],
                'source_value':record['source_value'],'data':record['data'],'provenance':record['provenance']}):
            raise DataError('QLIB_EXPORT_REGISTRATION_CHANGED')
        if self._version(principal,record['source_reference'])!=record['source_value']:raise DataError('QLIB_SOURCE_BINDING_CHANGED')
        source=self._source(principal.user_id);sv=record['source_value']
        if (source['source_id'],source['version'],source['content_hash'],fingerprint(source['license']))!=(sv['source_id'],sv['source_version'],sv['source_hash'],sv['license_hash']):
            raise DataError('QLIB_SOURCE_BINDING_CHANGED')
        _,data,provenance=self._native(principal.user_id,record['native_id'])
        if data!=record['data'] or provenance!=record['provenance']:raise DataError('QLIB_AUTHORIZATION_CHANGED')
        return json.loads(safe_json({'owner_id':principal.user_id,'export_reference':export_reference,
            'source_reference':record['source_reference'],'data':data}))
    def list_ready(self,principal,*,limit=50,offset=0):
        self._principal(principal);validate_pagination(limit,offset)
        with closing(self.db.connect()) as con:
            rows=con.execute('SELECT body FROM data_qlib_ready_exports WHERE owner_id=? ORDER BY created_at,resource_id LIMIT ? OFFSET ?',
                (principal.user_id,limit,offset)).fetchall()
            total=con.execute('SELECT count(*) FROM data_qlib_ready_exports WHERE owner_id=?',(principal.user_id,)).fetchone()[0]
        items=[];unavailable=0
        for row in rows:
            reference=json.loads(row['body'])['export_reference']
            try:grant=self.authorize(principal,reference,purpose='development_readonly')
            except (DataError,ValueError):unavailable+=1;continue
            items.append({'export_reference':reference,'source_reference':grant['source_reference'],
                'fixture_id':grant['data']['fixture_id'],'logical_data_hash':grant['data']['logical_data_hash'],
                'native_manifest_hash':grant['data']['native_manifest_hash'],'state':'DATA_READY',
                'native_dump_succeeded':True,'scope':'SYNTHETIC',
                'readiness_scope':'DATA_OWNER_ONLY_WORKER_RELEASE_CHECK_REQUIRED'})
        return {'items':items,'total_registered':total,'unavailable_on_page':unavailable,'limit':limit,'offset':offset,
            'next_offset':offset+len(rows) if len(rows)==limit else None}
    def _target(self,principal,destination,context):
        self._context(principal,context,'research.qlib')
        root=self.runtime.root;attempt=root/'.local/platform/native_research'/hashlib.sha256(principal.user_id.encode()).hexdigest()/context.run.run_id
        expected=attempt/'provider'
        if not Path(destination).is_absolute() or Path(destination).absolute()!=expected:raise DataError('QLIB_STAGE_TARGET_INVALID')
        for path in (attempt,*attempt.parents):
            try:info=path.lstat()
            except OSError:raise DataError('QLIB_PARENT_MARKER_INVALID') from None
            if path.is_symlink() or getattr(info,'st_file_attributes',0)&0x400 or not stat.S_ISDIR(info.st_mode):raise DataError('QLIB_STAGE_TARGET_INVALID')
            if path==root:break
        marker=regular_owned(root,(attempt/'parent-prepared.json').relative_to(root).as_posix())
        if marker.stat().st_size>4096:raise DataError('QLIB_PARENT_MARKER_INVALID')
        raw=marker.read_bytes();record=json.loads(raw,object_pairs_hook=_unique_pairs)
        wanted={'owner_hash':hashlib.sha256(principal.user_id.encode()).hexdigest(),'run_id':context.run.run_id,
            'job_id':context.job_id,'fencing_token':context.fencing_token}
        if record!=wanted or type(record.get('fencing_token')) is not int or {p.name for p in attempt.iterdir()}!={'parent-prepared.json'}:
            raise DataError('QLIB_PARENT_MARKER_INVALID')
        return attempt,expected,hashlib.sha256(raw).hexdigest()
    def stage(self,principal,export_reference,destination,*,context):
        self._context(principal,context,'research.qlib');_reference(export_reference)
        if payload(context.run.data)!=export_reference:raise DataError('QLIB_STAGE_RUN_REFERENCE_MISMATCH')
        attempt,target,marker_hash=self._target(principal,destination,context)
        grant=self.authorize(principal,export_reference,purpose='development_readonly')
        value=self._version(principal,export_reference);native=self.bridge.get_native(principal.user_id,value['provenance']['native_id'])
        source=Path(native['provider_uri']);target.mkdir(exist_ok=False)
        for entry in grant['data']['provider_files']:
            context.require_active()
            original=regular_owned(self.policy.root,(source/entry['path']).relative_to(self.policy.root).as_posix())
            raw=self.policy.read_bytes(original)
            if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
            output=self.contract.owned_path(target,entry['path']);output.parent.mkdir(parents=True,exist_ok=True)
            output=self.contract.owned_path(target,entry['path'])
            with output.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            checked=regular_owned(self.runtime.root,output.relative_to(self.runtime.root).as_posix())
            if checked.stat().st_size!=entry['bytes'] or hashlib.sha256(checked.read_bytes()).hexdigest()!=entry['sha256']:raise DataError('QLIB_STAGE_BYTE_MISMATCH')
        self.contract.verify_provider(attempt,grant['data']);context.require_active()
        marker=regular_owned(self.runtime.root,(attempt/'parent-prepared.json').relative_to(self.runtime.root).as_posix())
        if hashlib.sha256(marker.read_bytes()).hexdigest()!=marker_hash or {p.name for p in attempt.iterdir()}!={'parent-prepared.json','provider'}:
            raise DataError('QLIB_PARENT_MARKER_INVALID')
        stage={'owner_id':principal.user_id,'run_id':context.run.run_id,'job_id':context.job_id,'fencing_token':context.fencing_token,
            'export_reference':export_reference,'source_reference':grant['source_reference'],
            'native_manifest_hash':grant['data']['native_manifest_hash'],'parent_marker_sha256':marker_hash,
            'file_count':len(grant['data']['provider_files'])}
        with self.db.transaction() as con:
            context.assert_fenced(con)
            if self.authorize(principal,export_reference,purpose='development_readonly')!=grant:raise DataError('QLIB_AUTHORIZATION_CHANGED')
            context.assert_fenced(con)  # Recheck after the audited reauthorization.
            con.execute('INSERT INTO data_qlib_training_stages VALUES(?,?,?,?,?,?,?,?)',
                ('qlib-stage-'+uuid4().hex,principal.user_id,context.run.run_id,context.job_id,context.fencing_token,safe_json(stage),_hash(stage),now_text()))
        return {'state':'STAGED','export_reference':export_reference,'native_manifest_hash':stage['native_manifest_hash'],'file_count':stage['file_count']}
    def register_handler(self,context,parameters):
        validate_register_parameters(parameters)
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT username,role FROM users WHERE user_id=? AND enabled=1',(context.run.owner_id,)).fetchone()
        if row is None:raise DataError('QLIB_PRINCIPAL_CHANGED')
        principal=Principal(context.run.owner_id,row['username'],row['role'])
        return {'export_reference':self.register_completed(principal,parameters['native_id'],context=context)}
