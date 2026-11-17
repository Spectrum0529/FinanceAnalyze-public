"""Bind an installed reviewed fixture to a real claimed job, without regeneration.

The HTTP/job parameter surface is a fixed fixture ID only. Catalogs are trusted
server composition, not caller supplied paths or synthetic declarations.
"""
from contextlib import closing
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

from finance_platform.core.contracts import canonical, fingerprint, identifier
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.jobs import WorkerJobContext
from finance_platform.data.contracts import DataError
from predictor.access import AccessPolicy, AccessError
from .provider_sdk.artifacts import regular_owned
from .parquet_store import ParquetRows
from .qlib_export import QlibInputExporter, FIXTURE_ID, COLUMNS, validate_fixture

SOURCE_MANIFEST_SHA='ced13b79dfb580796baab0aa4885cbe8355fb696157b52e6e6a48e94996b4311'
SOURCE_LOGICAL_SHA='c484782648708a4b654f73569d44f00b4f08d1db1154e0f79f2200811d81b68e'
BRIDGE_MIGRATIONS=("""CREATE TABLE data_qlib_native_attempts(
 attempt_id TEXT PRIMARY KEY,export_id TEXT NOT NULL REFERENCES data_qlib_input_exports(export_id),
 owner_id TEXT NOT NULL,run_id TEXT NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL);
 CREATE TABLE data_qlib_native_receipts(
 native_id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL UNIQUE REFERENCES data_qlib_native_attempts(attempt_id),
 owner_id TEXT NOT NULL,run_id TEXT NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL);""",)

def validate_parameters(parameters):
    if type(parameters) is not dict or parameters!={'fixture_id':FIXTURE_ID}:
        raise PlatformError('INVALID_INPUT',422)

class _RoutedSourcePolicy(AccessPolicy):
    """Same partition gate, but this new consumer owns its audit destination."""
    def __init__(self,root,destination,allowed):
        self._destination=destination;self._files=frozenset(Path(p).resolve() for p in allowed)
        super().__init__(root)
    def log(self,**event):self._destination.log(**event)
    def authorize(self,requested,*,operation='read'):
        path=(self.root/requested).resolve()
        if path not in self._files:
            self.log(request_hash=hashlib.sha256(str(path).encode()).hexdigest(),partition='unclassified',
                operation=operation,outcome='denied',reason='not_in_reviewed_fixture_catalog')
            raise AccessError('Source is not in the installed fixture catalog')
        return super().authorize(requested,operation=operation)

class ReviewedFixtureCatalog:
    def __init__(self,server_catalog):
        self.document=copy.deepcopy(server_catalog)
        if (self.document.get('schema')!='qlib-fixed-reviewed-fixture-1'
            or self.document.get('fixture_id')!=FIXTURE_ID
            or self.document.get('source_manifest',{}).get('sha256')!=SOURCE_MANIFEST_SHA
            or self.document.get('logical_data_hash')!=SOURCE_LOGICAL_SHA):
            raise DataError('QLIB_FIXTURE_NOT_ALLOWED')
        self.root=Path(self.document['source_policy_root']).resolve(strict=True)
        self.files={entry['relative']:entry for entry in self.document['source_files']}
        if len(self.files)!=19 or set(self.files)!={entry['relative'] for entry in self.document['source_files']}:
            raise DataError('QLIB_FIXTURE_CATALOG_INVALID')
        for relative in self.files:
            if Path(relative).is_absolute() or '..' in Path(relative).parts or ':' in relative or '\\' in relative:
                raise DataError('QLIB_FIXTURE_CATALOG_INVALID')
    def reader(self,destination):
        policy=_RoutedSourcePolicy(self.root,destination,[self.root/relative for relative in self.files])
        def read(relative):
            if relative not in self.files:raise DataError('QLIB_FIXTURE_NOT_ALLOWED')
            entry=self.files[relative];path=regular_owned(self.root,relative)
            raw=policy.read_bytes(path)
            if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
                raise DataError('QLIB_FIXTURE_SOURCE_CHANGED')
            return raw
        return read

class OwnedQlibInputBridge(QlibInputExporter):
    def __init__(self,database,policy,catalog):
        if not isinstance(catalog,ReviewedFixtureCatalog):raise DataError('QLIB_FIXTURE_NOT_ALLOWED')
        super().__init__(database,policy)
        self.catalog=catalog
        self.db.apply_module_migrations('data_qlib_owned_bridge_1',BRIDGE_MIGRATIONS)
        with self.db.transaction() as con:
            immutable(con,'data_qlib_native_attempts',('attempt_id',))
            immutable(con,'data_qlib_native_receipts',('native_id',))
    @staticmethod
    def _active(context):
        if type(context) is not WorkerJobContext:raise DataError('QLIB_REAL_JOB_CONTEXT_REQUIRED')
        context.require_active()
    def _regular(self,path):
        # Check every component below the trusted AccessPolicy root, including
        # the attempt/native directory itself, not just its relative files.
        try:relative=Path(path).relative_to(self.policy.root).as_posix()
        except ValueError:raise DataError('QLIB_EXPORT_PATH_INVALID') from None
        return regular_owned(self.policy.root,relative)
    def consume_fixture(self,context,*,fixture_id):
        self._active(context)
        if fixture_id!=FIXTURE_ID:raise DataError('QLIB_FIXTURE_NOT_ALLOWED')
        read=self.catalog.reader(self.policy)
        source_raw=read(self.catalog.document['source_manifest']['relative'])
        source=json.loads(source_raw)
        if hashlib.sha256(source_raw).hexdigest()!=SOURCE_MANIFEST_SHA or source['logical_data_hash']!=SOURCE_LOGICAL_SHA:
            raise DataError('QLIB_FIXTURE_SOURCE_CHANGED')
        owner,run_id=context.run.owner_id,context.run.run_id
        export_id='qlib-input-'+uuid4().hex
        directory=self._directory(owner,run_id,export_id);directory.mkdir(parents=True,exist_ok=False)
        csv_dir=self.policy.authorize(directory/'csv');csv_dir.mkdir()
        items=[];decoded={}
        for old,entry in zip(source['instruments'],self.catalog.document['instruments'],strict=True):
            self._active(context)
            if old['symbol']!=entry['symbol']:raise DataError('QLIB_FIXTURE_CATALOG_INVALID')
            descriptor=old['parquet'];parquet=read(entry['parquet_relative'])
            rows=ParquetRows._decode(parquet,descriptor)
            if fingerprint(rows)!=old['rows_logical_hash']:raise DataError('QLIB_EXPORT_LOGICAL_MISMATCH')
            raw=read(entry['CSV_relative'])
            parsed=list(csv.DictReader(io.StringIO(raw.decode('utf-8'),newline='')))
            expected=[{**{key:row[key] for key in COLUMNS if key!='date'},'date':row['session_date']} for row in rows]
            if parsed!=expected:raise DataError('QLIB_INPUT_CSV_PARQUET_MISMATCH')
            store=self.parquet._store(owner)
            if store._publish(parquet)!=descriptor['blob_sha256']:raise DataError('PARQUET_BYTE_HASH_MISMATCH')
            self.parquet._final_bytes(owner,descriptor)
            item=copy.deepcopy(old);item['CSV']=self._write(csv_dir,old['CSV']['name'],raw)
            item['source']='STAGED_FROM_FIXED_REVIEWED_V2_FIXTURE'
            items.append(item);decoded[item['symbol']]=rows
        fixture={key:source[key] for key in ['fixture_id','origin','seed','calendar','universe','benchmark','semantics','split_indices']}
        fixture['instruments']=decoded;validate_fixture(fixture)
        if fingerprint(fixture)!=SOURCE_LOGICAL_SHA:raise DataError('QLIB_EXPORT_LOGICAL_MISMATCH')
        body=copy.deepcopy(source)
        body.update(owner_id=owner,run_id=run_id,export_id=export_id,instruments=items,
            state='OWNED_INPUT_STAGED_NATIVE_UNEXECUTED',provider_uri=None,native_output=None,
            source_consumption={'contract':'FIXED_REVIEWED_V2_BYTE_PRESERVING_STAGE_1',
                'source_manifest_ref':self.catalog.document['source_manifest'],
                'source_owner_id':source['owner_id'],'source_run_id':source['run_id'],'source_export_id':source['export_id'],
                'source_logical_data_hash':SOURCE_LOGICAL_SHA,'catalog_sha256':fingerprint(self.catalog.document)})
        self._write(directory,'input_manifest.json',canonical(body).encode())
        self._active(context)
        with self.db.transaction() as con:
            context.assert_fenced(con)
            con.execute('INSERT INTO data_qlib_input_exports VALUES(?,?,?,?,?)',
                (export_id,owner,run_id,canonical(body),fingerprint(body)))
        return self.owned_input(context,export_id)
    def owned_input(self,context,export_id):
        self._active(context)
        body=self.get(context.run.owner_id,export_id)
        if body['run_id']!=context.run.run_id or body['owner_id']!=context.run.owner_id:
            raise DataError('RESOURCE_NOT_FOUND')
        directory=self._directory(body['owner_id'],body['run_id'],export_id)
        files=[]
        for entry in body['instruments']:
            self.parquet._final_bytes(body['owner_id'],entry['parquet'])
            files.append({'symbol':entry['symbol'],'path':str(directory/'csv'/entry['CSV']['name']),
                'rows':entry['row_count'],'bytes':entry['CSV']['bytes'],'sha256':entry['CSV']['sha256']})
        manifest=regular_owned(directory,'input_manifest.json');raw=self.policy.read_bytes(manifest)
        return {'owner_id':body['owner_id'],'run_id':body['run_id'],'export_id':export_id,
            'owner_manifest_sha256':hashlib.sha256(raw).hexdigest(),'logical_data_hash':body['logical_data_hash'],
            'source_manifest_sha256':SOURCE_MANIFEST_SHA,'source_manifest_ref':body['source_consumption']['source_manifest_ref'],
            'input_manifest':str(manifest),'directory':str(directory),'CSV_directory':str(directory/'csv'),
            'CSV_files':files,'calendar':body['calendar'],'universe':body['universe'],'benchmark':body['benchmark'],
            'provider_uri':None}
    def begin_attempt(self,context,export_id,attempt_id,binding_sha256):
        owned=self.owned_input(context,export_id);identifier(attempt_id)
        if not attempt_id.startswith('qlib-native-') or len(binding_sha256)!=64:raise DataError('INVALID_INPUT')
        directory=Path(owned['directory'])/'native-attempts'/attempt_id
        for item in (directory,*directory.parents):
            if item.exists() or item.is_symlink():
                if item.is_symlink() or getattr(item.lstat(),'st_file_attributes',0)&0x400:raise DataError('QLIB_EXPORT_PATH_INVALID')
            if item==self.policy.root:break
        if self.policy.authorize(directory)!=directory:raise DataError('QLIB_EXPORT_PATH_INVALID')
        directory.mkdir(parents=True,exist_ok=False)
        body={'attempt_id':attempt_id,'export_id':export_id,'owner_id':owned['owner_id'],'run_id':owned['run_id'],
            'binding_sha256':binding_sha256,'owned_input':owned,'directory':str(directory)}
        self._write(directory,'attempt_manifest.json',canonical(body).encode())
        with self.db.transaction() as con:
            context.assert_fenced(con)
            con.execute('INSERT INTO data_qlib_native_attempts VALUES(?,?,?,?,?,?)',
                (attempt_id,export_id,owned['owner_id'],owned['run_id'],canonical(body),fingerprint(body)))
        return body
    def attempt(self,context,attempt_id):
        self._active(context);identifier(attempt_id)
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT body,content_hash FROM data_qlib_native_attempts WHERE attempt_id=? AND owner_id=? AND run_id=?',
                (attempt_id,context.run.owner_id,context.run.run_id)).fetchone()
        if row is None:raise DataError('RESOURCE_NOT_FOUND')
        body=json.loads(row['body'])
        if fingerprint(body)!=row['content_hash']:raise DataError('QLIB_EXPORT_LOGICAL_MISMATCH')
        owned=self.owned_input(context,body['export_id'])
        if owned!=body['owned_input']:raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        path=self._regular(Path(body['directory'])/'attempt_manifest.json')
        if self.policy.read_bytes(path)!=canonical(body).encode():raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        return body
    def publish_native(self,context,attempt_id):
        """Server completion only: derive receipt from verified owned files."""
        attempt=self.attempt(context,attempt_id);directory=Path(attempt['directory'])
        result=json.loads(self.policy.read_bytes(self._regular(directory/'controller_result.json')))
        verification_raw=self.policy.read_bytes(self._regular(directory/'native_verification.json'))
        verification=json.loads(verification_raw)
        if (result.get('status')!='VERIFIED_OFFICIAL_DUMP_UNPUBLISHED'
            or result.get('binding_sha256')!=attempt['binding_sha256']
            or result.get('owner_manifest_sha256')!=attempt['owned_input']['owner_manifest_sha256']
            or result.get('provider_verification_sha256')!=hashlib.sha256(verification_raw).hexdigest()
            or not result.get('final_empty',{}).get('kernel_final_empty')
            or result.get('root_reaped') is not True or result.get('drains_joined') is not True
            or result.get('pipes_closed') is not True or result.get('control_errors')!=[]
            or verification.get('status')!='PASS_OFFICIAL_LOCAL_PROVIDER_READ'
            or verification.get('feature_reads')!=63 or verification.get('calendar_dates')!=420
            or verification.get('input_owner_manifest_sha256')!=attempt['owned_input']['owner_manifest_sha256']):
            raise DataError('QLIB_NATIVE_VERIFICATION_REQUIRED')
        files=result.get('outputs',[])
        expected={'calendars/day.txt','instruments/all.txt'}|{f'features/{item["symbol"].lower()}/{field}.day.bin'
            for item in attempt['owned_input']['CSV_files'] for field in ['open','close','high','low','volume','vwap','factor']}
        if len(files)!=65 or {f['relative'] for f in files}!=expected:raise DataError('QLIB_NATIVE_VERIFICATION_REQUIRED')
        for entry in files:
            path=self._regular(directory/'native'/entry['relative']);raw=self.policy.read_bytes(path)
            if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
                raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        if {p.relative_to(directory/'native').as_posix() for p in (directory/'native').rglob('*') if p.is_file()}!={f['relative'] for f in files}:
            raise DataError('QLIB_NATIVE_VERIFICATION_REQUIRED')
        self._active(context)
        body={'native_id':'qlib-native-receipt-'+uuid4().hex,'owner_id':context.run.owner_id,'run_id':context.run.run_id,
            'export_id':attempt['export_id'],'attempt_id':attempt_id,'logical_data_hash':SOURCE_LOGICAL_SHA,
            'owner_manifest_sha256':attempt['owned_input']['owner_manifest_sha256'],'source_manifest_sha256':SOURCE_MANIFEST_SHA,
            'binding_sha256':attempt['binding_sha256'],'state':'VERIFIED_NATIVE_PROVIDER_PUBLISHED',
            'provider_uri':str(directory/'native'),'verification_sha256':hashlib.sha256(verification_raw).hexdigest(),
            'outputs':files,'financial_scope':'Synthetic V2 feature/index/NaN/native-float32 verification only; no training or market approval.'}
        self._write(directory,'provider_receipt.json',canonical(body).encode())
        with self.db.transaction() as con:
            context.assert_fenced(con)
            con.execute('INSERT INTO data_qlib_native_receipts VALUES(?,?,?,?,?,?)',
                (body['native_id'],attempt_id,context.run.owner_id,context.run.run_id,canonical(body),fingerprint(body)))
        return {**body,'content_hash':fingerprint(body)}
    def get_native(self,owner,native_id):
        """Caller supplies its authenticated owner ID; no URI/path argument."""
        identifier(owner);identifier(native_id)
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT body,content_hash FROM data_qlib_native_receipts WHERE native_id=? AND owner_id=?',(native_id,owner)).fetchone()
        if row is None:raise DataError('RESOURCE_NOT_FOUND')
        body=json.loads(row['body'])
        if fingerprint(body)!=row['content_hash']:raise DataError('QLIB_EXPORT_LOGICAL_MISMATCH')
        directory=self._directory(owner,body['run_id'],body['export_id'])/'native-attempts'/body['attempt_id']
        if self.policy.read_bytes(self._regular(directory/'provider_receipt.json'))!=canonical(body).encode():raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        for entry in body['outputs']:
            raw=self.policy.read_bytes(self._regular(directory/'native'/entry['relative']))
            if len(raw)!=entry['bytes'] or hashlib.sha256(raw).hexdigest()!=entry['sha256']:raise DataError('QLIB_EXPORT_BYTE_MISMATCH')
        return {**body,'content_hash':row['content_hash']}
