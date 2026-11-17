"""Registered personal product payloads; source AccessPolicy is never widened."""
from contextlib import closing,contextmanager,ExitStack
from datetime import datetime,timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import json,os,re
from .contracts import canonical,fingerprint,identifier
from .database import immutable
from .errors import PlatformError
from .security import clean
from .storage_guard import local_path,fail
from .storage_native import directory as native_directory

CATEGORIES={'market_data':('行情与数据集','data/development/platform/storage/market_data'),
            'reports':('结果报告','.local/platform/storage/reports'),
            'artifacts':('模型与研究产物','.local/platform/storage/artifacts')}
MIGRATIONS=('''
CREATE TABLE storage_locations(id TEXT PRIMARY KEY,owner TEXT NOT NULL,category TEXT NOT NULL,directory TEXT,root TEXT NOT NULL,identity TEXT NOT NULL);
CREATE TABLE storage_settings(owner TEXT NOT NULL,category TEXT NOT NULL,revision INTEGER NOT NULL,location TEXT NOT NULL REFERENCES storage_locations(id),PRIMARY KEY(owner,category,revision));
CREATE TABLE storage_bindings(owner TEXT NOT NULL,key TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(owner,key));
CREATE TABLE storage_payloads(owner TEXT NOT NULL,location TEXT NOT NULL,key TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(owner,location,key));
CREATE TABLE storage_archives(owner TEXT NOT NULL,location TEXT NOT NULL,key TEXT NOT NULL,body TEXT NOT NULL,identity TEXT NOT NULL,PRIMARY KEY(owner,location,key));
CREATE TABLE storage_keys(owner TEXT NOT NULL,key TEXT NOT NULL,hash TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(owner,key));
''',)
MESSAGES={'STORAGE_LOCATION_OK':'位置可用。','STORAGE_PATH_REJECTED':'保存位置不符合本地安全限制。',
          'STORAGE_UNAVAILABLE':'保存位置不存在、不可访问或不可写；未回退默认位置。',
          'STORAGE_ROOT_CHANGED':'保存目录已被替换，请保留原目录并检查设置。',
          'STORAGE_REVISION_CONFLICT':'设置已改变，请刷新后重试。',
          'STORAGE_HASH_MISMATCH':'保存内容校验失败。'}

class StorageService:
    def __init__(self,database,policy):
        self.db,self.policy,self.root=database,policy,policy.root
        database.apply_module_migrations('product_storage_locations',MIGRATIONS)
        with database.transaction() as con:
            for table,keys in [('storage_locations',('id',)),('storage_settings',('owner','category','revision')),
                               ('storage_bindings',('owner','key')),('storage_payloads',('owner','location','key')),
                               ('storage_archives',('owner','location','key')),('storage_keys',('owner','key'))]:immutable(con,table,keys)
    def _principal(self,p):
        identifier(p.user_id)
        if p.user_id!='local-personal' and p.role!='admin':fail('FORBIDDEN')
    def _category(self,c):
        if c not in CATEGORIES:fail('STORAGE_CATEGORY_INVALID')
    def _custom(self,directory):
        path=local_path(directory)
        self._custom_scope(path)
        with native_directory(path) as handle:
            actual=handle.canonical()
            self._custom_scope(actual)
            return actual
    def _custom_scope(self,path):
        # No custom destinations within/containing this workspace. Exact
        # trusted default allocations below are the only exception.
        if path==Path(path.anchor) or path.is_relative_to(self.root) or self.root.is_relative_to(path):fail()
        # Sibling explicit fixture/data destinations are allowed; deny common
        # system/runtime, personal credential and private-backup namespaces.
        forbidden={'windows','program files','program files (x86)','programdata','appdata','.local','.git','.agents','.ssh','.aws','.azure','.codex','.venv','venv','python','devtools','node_modules','secrets','credentials','authorized_raw','candidate_holdout','prospective','backups','backup'}
        if any(p.casefold() in forbidden for p in path.parts):fail()
        personal=Path.home()
        if personal.is_relative_to(path):fail()
        return path
    def _base(self,owner,category,directory):
        self._category(category);identifier(owner)
        base=self.root/CATEGORIES[category][1] if directory is None else self._custom(directory)
        if directory is None:
            with native_directory(base,create=True):pass
        return base
    def _new_location(self,con,owner,category,directory):
        base=self._base(owner,category,directory)
        with native_directory(base) as parent:
            loc='location-'+uuid4().hex
            with ExitStack() as stack:
                for part in ('FinanceAnalyze',sha256(owner.encode()).hexdigest(),category):
                    parent=stack.enter_context(parent.child_directory(part,create=True))
                location=stack.enter_context(parent.child_directory(loc,create=True,exclusive=True))
                self._probe(location)
                con.execute('INSERT INTO storage_locations VALUES(?,?,?,?,?,?)',(loc,owner,category,str(base) if directory is not None else None,str(location.canonical()),canonical(location.info['identity'])))
        return loc
    @staticmethod
    def _probe(handle):
        raw=b'FinanceAnalyze storage probe'
        with handle.create_file('probe-'+uuid4().hex,delete_on_close=True) as probe:
            probe.write_flush(raw)
            if probe.read(len(raw),expected_size=len(raw))!=raw:fail('STORAGE_HASH_MISMATCH')
    def _current(self,con,owner,c):
        row=con.execute('SELECT revision,location FROM storage_settings WHERE owner=? AND category=? ORDER BY revision DESC LIMIT 1',(owner,c)).fetchone()
        if row:return row[0],row[1]
        loc=self._new_location(con,owner,c,None)
        con.execute('INSERT INTO storage_settings VALUES(?,?,0,?)',(owner,c,loc));return 0,loc
    def _view(self,con,owner,c):
        current=con.execute('SELECT revision,location FROM storage_settings WHERE owner=? AND category=? ORDER BY revision DESC LIMIT 1',(owner,c)).fetchone()
        if not current:
            default=str(self.root/CATEGORIES[c][1])
            return {'category':c,'label':CATEGORIES[c][0],'revision':0,'location_id':None,'directory':None,
                    'effective_directory':default,'default_directory':default,'uses_default':True}
        revision,loc=current
        row=con.execute('SELECT directory,root FROM storage_locations WHERE id=? AND owner=? AND category=?',(loc,owner,c)).fetchone()
        return {'category':c,'label':CATEGORIES[c][0],'revision':revision,'location_id':loc,'directory':row[0],
                'effective_directory':row[1],'default_directory':str(self.root/CATEGORIES[c][1]),'uses_default':row[0] is None}
    def get_settings(self,principal):
        self._principal(principal)
        with closing(self.db.connect()) as con:items=[self._view(con,principal.user_id,c) for c in CATEGORIES]
        return {'schema':'storage-settings-1','categories':items,'effective_for':'NEW_JOBS_AND_NEW_SYNCHRONOUS_ALLOCATIONS','moves_existing':False,'runtime_temporary_files':'MANAGED_SEPARATELY'}
    def check_location(self,principal,category,directory):
        self._principal(principal);self._category(category)
        normalized=None;effective=None
        try:
            base=self._base(principal.user_id,category,directory);normalized=str(base) if directory is not None else None
            with native_directory(base) as parent:
                candidate=parent
                stack=ExitStack()
                with stack:
                    for part in ('FinanceAnalyze',sha256(principal.user_id.encode()).hexdigest(),category):
                        try:candidate=stack.enter_context(candidate.child_directory(part))
                        except PlatformError as error:
                            if error.code=='STORAGE_OBJECT_MISSING':break
                            raise
                self._probe(parent)
            effective=str(base/'FinanceAnalyze'/sha256(principal.user_id.encode()).hexdigest()/category)
            code='STORAGE_LOCATION_OK'
        except (OSError,PlatformError) as error:code=getattr(error,'code','STORAGE_UNAVAILABLE')
        return {'schema':'storage-location-check-1','category':category,'directory':normalized,'effective_directory':effective,
                'valid':code=='STORAGE_LOCATION_OK','code':code,'message':MESSAGES.get(code,'保存位置检查失败。'),'creates_only_owned_probe':True}
    def save_location(self,principal,category,directory,*,expected_revision,idempotency_key):
        self._principal(principal);self._category(category);identifier(idempotency_key)
        if type(expected_revision)is not int or expected_revision<0:fail('INVALID_INPUT')
        request=fingerprint({'category':category,'directory':directory,'expected_revision':expected_revision})
        with self.db.transaction() as con:
            old=con.execute('SELECT hash,response FROM storage_keys WHERE owner=? AND key=?',(principal.user_id,idempotency_key)).fetchone()
            if old:
                if old[0]!=request:fail('IDEMPOTENCY_CONFLICT')
                return json.loads(old[1])
            current=con.execute('SELECT revision FROM storage_settings WHERE owner=? AND category=? ORDER BY revision DESC LIMIT 1',(principal.user_id,category)).fetchone()
            revision=current[0] if current else 0
            if revision!=expected_revision:fail('STORAGE_REVISION_CONFLICT')
            loc=self._new_location(con,principal.user_id,category,directory)
            con.execute('INSERT INTO storage_settings VALUES(?,?,?,?)',(principal.user_id,category,revision+1,loc))
            value=self._view(con,principal.user_id,category)
            con.execute('INSERT INTO storage_keys VALUES(?,?,?,?)',(principal.user_id,idempotency_key,request,canonical(value)))
        return value
    def bind(self,owner_id,binding_key,*,categories=tuple(CATEGORIES),connection=None):
        identifier(owner_id);identifier(binding_key)
        if not categories or len(set(categories))!=len(categories):fail('INVALID_INPUT')
        for c in categories:self._category(c)
        def bind(con):
            row=con.execute('SELECT body FROM storage_bindings WHERE owner=? AND key=?',(owner_id,binding_key)).fetchone()
            if row:
                result=json.loads(row[0])
                if any(c not in result['locations'] for c in categories):fail('STORAGE_BINDING_INVALID')
                return result
            locations={}
            for c in categories:
                revision,loc=self._current(con,owner_id,c);locations[c]={'location_id':loc,'revision':revision}
            value={'schema':'storage-binding-1','owner_id':owner_id,'binding_key':binding_key,'locations':locations}
            con.execute('INSERT INTO storage_bindings VALUES(?,?,?)',(owner_id,binding_key,canonical(value)));return value
        if connection is not None:return bind(connection)
        with self.db.transaction() as con:return bind(con)
    def lookup_binding(self,owner_id,binding_key,connection=None):
        identifier(owner_id);identifier(binding_key)
        def lookup(con):return con.execute('SELECT body FROM storage_bindings WHERE owner=? AND key=?',(owner_id,binding_key)).fetchone()
        if connection is not None:row=lookup(connection)
        else:
            with closing(self.db.connect()) as con:row=lookup(con)
        if not row:fail('STORAGE_TASK_BINDING_REQUIRED')
        return json.loads(row[0])
    def get_binding(self,owner_id,binding_key):
        return self.lookup_binding(owner_id,binding_key)
    def bind_snapshot(self,owner_id,binding_key,snapshot,*,connection):
        """Trusted prepared-input submit hook. No client path/reference authority."""
        identifier(owner_id);identifier(binding_key)
        if snapshot.get('owner_id')!=owner_id:fail('STORAGE_BINDING_INVALID')
        for category in snapshot.get('locations',{}):self._binding(snapshot,category,connection)
        if not snapshot.get('locations'):fail('STORAGE_BINDING_INVALID')
        value={**snapshot,'binding_key':binding_key}
        old=connection.execute('SELECT body FROM storage_bindings WHERE owner=? AND key=?',(owner_id,binding_key)).fetchone()
        if old:
            if json.loads(old[0])!=value:fail('STORAGE_BINDING_INVALID')
        else:connection.execute('INSERT INTO storage_bindings VALUES(?,?,?)',(owner_id,binding_key,canonical(value)))
        return value
    def _binding(self,binding,c,connection=None):
        if type(binding)is not dict or set(binding)!={'schema','owner_id','binding_key','locations'} or binding['schema']!='storage-binding-1':fail('STORAGE_BINDING_INVALID')
        def lookup(con):return con.execute('SELECT body FROM storage_bindings WHERE owner=? AND key=?',(binding['owner_id'],binding['binding_key'])).fetchone()
        if connection is not None:row=lookup(connection)
        else:
            with closing(self.db.connect()) as con:row=lookup(con)
        if not row or json.loads(row[0])!=binding or c not in binding['locations']:fail('STORAGE_BINDING_INVALID')
        return binding['locations'][c]['location_id']
    @contextmanager
    def _gate(self,owner,loc,c,operation,connection=None):
        self._category(c);identifier(owner);identifier(loc)
        def lookup(con):return con.execute('SELECT root,identity,directory FROM storage_locations WHERE id=? AND owner=? AND category=?',(loc,owner,c)).fetchone()
        if connection is not None:row=lookup(connection)
        else:
            with closing(self.db.connect()) as con:row=lookup(con)
        if not row:fail('STORAGE_REFERENCE_INVALID')
        digest=sha256(row[0].encode()).hexdigest()
        try:
            with native_directory(Path(row[0]),expected=json.loads(row[1])) as handle:
                actual=handle.canonical()
                if actual!=Path(row[0]):fail('STORAGE_ROOT_CHANGED')
                if row[2] is not None:self._custom_scope(actual.parents[3])
                self.policy.log(request_hash=digest,partition='registered_product_payload',operation=operation,outcome='allowed',reason='registered_owner_category')
                yield handle
        except (OSError,PlatformError) as error:
            self.policy.log(request_hash=digest,partition='registered_product_payload',operation=operation,outcome='denied',reason='registered_payload_unavailable')
            if isinstance(error,OSError):fail('STORAGE_UNAVAILABLE')
            raise
    def put(self,binding,category,raw,*,media_type,connection=None):
        if type(raw)is not bytes or len(raw)>64*1024**2 or type(media_type)is not str or not re.fullmatch(r'[a-zA-Z0-9.+/-]{1,100}',media_type):fail('STORAGE_PAYLOAD_INVALID')
        loc=self._binding(binding,category,connection);owner=binding['owner_id'];digest=sha256(raw).hexdigest()
        def previous(con):return con.execute("SELECT body FROM storage_payloads WHERE owner=? AND location=? AND json_extract(body,'$.sha256')=? AND json_extract(body,'$.media_type')=? ORDER BY key LIMIT 1",(owner,loc,digest,media_type)).fetchone()
        if connection is not None:old=previous(connection)
        else:
            with closing(self.db.connect()) as con:old=previous(con)
        if old:
            ref=json.loads(old[0])
            if self._read(owner,ref,category=category,maximum_bytes=len(raw),connection=connection)!=raw:fail('STORAGE_HASH_MISMATCH')
            return ref
        # Exclusive final creation needs no path-based link/rename/unlink. Any
        # interrupted file remains unreferenced; DB publication is the boundary.
        key='objects/'+digest+'-'+uuid4().hex+'.bin'
        ref={'schema':'storage-payload-1','category':category,'location_id':loc,'owner_id':owner,'key':key,'sha256':digest,'bytes':len(raw),'media_type':media_type}
        with self._gate(owner,loc,category,'write',connection) as root:
            with root.child_directory('objects',create=True) as objects:
                with objects.create_file(key.split('/')[1]) as target:
                    target.write_flush(raw)
                    if target.read(len(raw),expected_size=len(raw))!=raw:fail('STORAGE_HASH_MISMATCH')
        def register(con):
            old=con.execute('SELECT body FROM storage_payloads WHERE owner=? AND location=? AND key=?',(owner,loc,key)).fetchone()
            if old:
                if json.loads(old[0])!=ref:fail('STORAGE_REFERENCE_INVALID')
            else:con.execute('INSERT INTO storage_payloads VALUES(?,?,?,?)',(owner,loc,key,canonical(ref)))
        if connection is not None:register(connection)
        else:
            with self.db.transaction() as con:register(con)
        return ref
    def read(self,owner_id,reference,*,category,maximum_bytes):
        return self._read(owner_id,reference,category=category,maximum_bytes=maximum_bytes)
    def _read(self,owner_id,reference,*,category,maximum_bytes,connection=None):
        fields={'schema','category','location_id','owner_id','key','sha256','bytes','media_type'}
        ref=reference
        if type(ref)is not dict or set(ref)!=fields or ref['schema']!='storage-payload-1' or ref['owner_id']!=owner_id or ref['category']!=category or type(maximum_bytes)is not int or type(ref['bytes'])is not int or not 0<=ref['bytes']<=maximum_bytes or not re.fullmatch(r'[0-9a-f]{64}',ref['sha256']) or type(ref['key'])is not str or not re.fullmatch('objects/'+ref['sha256']+r'(?:-[0-9a-f]{32})?\.bin',ref['key']):fail('STORAGE_REFERENCE_INVALID')
        def lookup(con):return con.execute('SELECT body FROM storage_payloads WHERE owner=? AND location=? AND key=?',(owner_id,ref['location_id'],ref['key'])).fetchone()
        if connection is not None:row=lookup(connection)
        else:
            with closing(self.db.connect()) as con:row=lookup(con)
        if not row or json.loads(row[0])!=ref:fail('STORAGE_REFERENCE_INVALID')
        with self._gate(owner_id,ref['location_id'],category,'read',connection) as root:
            with root.child_directory('objects') as objects:
                with objects.open_file(ref['key'].split('/')[1]) as target:
                    raw=target.read(maximum_bytes,expected_size=ref['bytes'])
            if sha256(raw).hexdigest()!=ref['sha256']:fail('STORAGE_HASH_MISMATCH')
        return raw
    def read_digest(self,owner_id,location_id,digest,*,category,maximum_bytes):
        """Trusted RawStore byte resolver; outer saved resource supplies scope."""
        identifier(owner_id);identifier(location_id)
        if type(digest)is not str or not re.fullmatch(r'[0-9a-f]{64}',digest):fail('STORAGE_REFERENCE_INVALID')
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT body FROM storage_payloads WHERE owner=? AND location=? AND json_extract(body,'$.sha256')=? AND json_extract(body,'$.media_type')='application/octet-stream' ORDER BY key LIMIT 1",(owner_id,location_id,digest)).fetchone()
        if not row:fail('STORAGE_UNAVAILABLE')
        return self.read(owner_id,json.loads(row[0]),category=category,maximum_bytes=maximum_bytes)
    def encode_payload(self,binding,category,value,*,media_type='application/json',connection=None):
        return self.put(binding,category,canonical(clean(value)).encode(),media_type=media_type,connection=connection)
    def decode_payload(self,owner_id,ref,*,category,maximum_bytes):
        try:return json.loads(self.read(owner_id,ref,category=category,maximum_bytes=maximum_bytes))
        except (ValueError,UnicodeError) as e:
            if isinstance(e,PlatformError):raise
            fail('STORAGE_PAYLOAD_INVALID')
    def archive_directory(self,binding,category,*,namespace,object_id):
        # No current product producer consumes DirectoryRef. Durable archives
        # use put/read; do not expose a path-shaped IO escape as future authority.
        fail('STORAGE_DIRECTORY_PORT_UNAVAILABLE')
    def resolve_archive(self,owner_id,reference,*,category):
        fail('STORAGE_DIRECTORY_PORT_UNAVAILABLE')

def get_storage(runtime):
    if getattr(runtime,'storage',None)is None:
        from predictor.access import AccessPolicy
        runtime.storage=StorageService(runtime.database,AccessPolicy(runtime.root))
    return runtime.storage
