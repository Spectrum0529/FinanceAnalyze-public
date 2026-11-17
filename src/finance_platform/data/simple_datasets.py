"""Immutable bounded dataset library on the existing Database migration API."""
import json,re
from contextlib import closing
from datetime import datetime,timezone
from uuid import uuid4
from finance_platform.core.pending_writes import require_open_key
from finance_platform.core.contracts import canonical,fingerprint,identifier,parse_instant
from finance_platform.core.database import immutable
from finance_platform.core.security import clean
from .contracts import DataError,validate_paging
from .simple_inputs import metadata,validate_rows,tickers

MIGRATIONS=('''
CREATE TABLE data_library_connections(id TEXT PRIMARY KEY,owner TEXT NOT NULL,body TEXT NOT NULL);
CREATE TABLE data_library_versions(id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,rows_hash TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE data_library_lifecycle(id TEXT NOT NULL REFERENCES data_library_versions(id),revision INTEGER NOT NULL,trashed INTEGER NOT NULL,PRIMARY KEY(id,revision));
CREATE TABLE data_library_refs(id TEXT NOT NULL REFERENCES data_library_versions(id),result_id TEXT NOT NULL,PRIMARY KEY(id,result_id));
CREATE TABLE data_library_keys(owner TEXT NOT NULL,key TEXT NOT NULL,request_hash TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(owner,key));
CREATE TABLE data_library_uploads(id TEXT PRIMARY KEY,owner TEXT NOT NULL,raw BLOB NOT NULL,hash TEXT NOT NULL);
''','''CREATE TABLE data_library_universes(id TEXT PRIMARY KEY,owner TEXT NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,origin_job TEXT NOT NULL);''')

CATALOG_MIGRATIONS = ('''
CREATE TABLE data_library_publications(
    dataset_id TEXT PRIMARY KEY REFERENCES data_library_versions(id),
    origin_job_id TEXT REFERENCES jobs(job_id)
);
CREATE INDEX data_library_catalog_order ON data_library_versions(owner,kind,created DESC,id);
CREATE INDEX data_library_catalog_key ON data_library_keys(owner,json_extract(response,'$.dataset_id'));
''',)

# Legacy external bodies expose no origin in SQLite. The immutable publication
# receipt binds their dataset id and hash to either the original job's run key
# or the synchronous import allocation. Legacy inline bodies retain the origin.
_CATALOG_SCOPE = '''
FROM data_library_versions v
JOIN data_library_lifecycle l ON l.id=v.id
    AND l.revision=(SELECT max(state.revision) FROM data_library_lifecycle state WHERE state.id=v.id)
LEFT JOIN data_library_publications p ON p.dataset_id=v.id
WHERE v.owner=? AND v.kind=? AND l.trashed=?
AND CASE
    WHEN p.dataset_id IS NOT NULL THEN p.origin_job_id IS NULL OR EXISTS (
        SELECT 1 FROM jobs j
        WHERE j.job_id=p.origin_job_id AND j.owner_id=v.owner AND j.status='SUCCEEDED'
    )
    WHEN json_extract(v.body,'$.storage_envelope')='market-body-1' THEN EXISTS (
        SELECT 1 FROM data_library_keys k
        WHERE k.owner=v.owner AND json_extract(k.response,'$.dataset_id')=v.id
            AND json_extract(k.response,'$.content_hash')=v.hash
            AND (
                k.key NOT GLOB 'job:*'
                OR EXISTS (
                    SELECT 1 FROM storage_bindings b
                    WHERE b.owner=v.owner AND b.key='dataset:'||k.key
                    AND json_extract(b.body,'$.locations.market_data.location_id')=json_extract(v.body,'$.payload.location_id')
                )
                OR EXISTS (
                    SELECT 1 FROM jobs j
                    WHERE j.run_id=substr(k.key,5) AND j.owner_id=v.owner AND j.status='SUCCEEDED'
                )
            )
    )
    ELSE coalesce(json_extract(v.body,'$.origin_job_id'),'')='' OR EXISTS (
        SELECT 1 FROM jobs j
        WHERE j.job_id=json_extract(v.body,'$.origin_job_id') AND j.owner_id=v.owner AND j.status='SUCCEEDED'
    )
END
'''

class DatasetLibrary:
    def __init__(self,database,policy):
        self.db,self.policy=database,policy
        from .market_storage import MarketStorage
        self.market=MarketStorage(database,policy)
        database.apply_module_migrations('data_library',MIGRATIONS)
        database.apply_module_migrations('data_library_storage',('CREATE TABLE data_library_upload_payloads(id TEXT PRIMARY KEY,owner TEXT NOT NULL,ref TEXT NOT NULL);',))
        database.apply_module_migrations('data_library_catalog', CATALOG_MIGRATIONS)
        from .market_lifecycle import initialize
        initialize(database)
        with database.transaction() as con:
            for table,keys in [('data_library_connections',('id',)),('data_library_versions',('id',)),('data_library_lifecycle',('id','revision')),('data_library_refs',('id','result_id')),('data_library_keys',('owner','key'))]:immutable(con,table,keys)
            immutable(con,'data_library_uploads',('id',))
            immutable(con,'data_library_universes',('id',))
            immutable(con,'data_library_upload_payloads',('id',))
            immutable(con,'data_library_publications',('dataset_id',))

    def save_universe(self,owner,group,key,*,context):
        if not group.get('source') or not group.get('as_of') or not group.get('available') or group.get('historical_constituents') is not False:raise DataError('DATED_MEMBERSHIP_REQUIRED')
        if tickers(' '.join(group['members']))!=group['members']:raise DataError('UNIVERSE_INVALID')
        def insert(con):
            ident='universe-'+uuid4().hex;body={**group,'id':ident,'origin_job_id':context.job_id}
            con.execute('INSERT INTO data_library_universes VALUES(?,?,?,?,?)',(ident,owner,canonical(clean(body)),fingerprint(body),context.job_id))
            return body
        return self._write(owner,key,group,insert,context)

    def universe_catalog(self,owner):
        from .simple_inputs import universe_catalog
        identifier(owner)
        with closing(self.db.connect()) as con:
            rows=con.execute("SELECT u.body,u.hash FROM data_library_universes u JOIN jobs j ON j.job_id=u.origin_job AND j.owner_id=u.owner WHERE u.owner=? AND j.status='SUCCEEDED' ORDER BY u.id LIMIT 200",(owner,)).fetchall()
        result=universe_catalog()
        for row in rows:
            body=json.loads(row[0])
            if fingerprint(body)!=row[1]:raise DataError('DATASET_HASH_MISMATCH')
            result['items'].append(body)
        return result

    def resolve_universe(self,owner,value):
        from .simple_inputs import resolve_universe
        return resolve_universe(value,catalog=self.universe_catalog(owner))

    def validate_selection(self,owner,selection):
        try:
            resolved=self.resolve_universe(owner,{'groups':[g['id'] for g in selection['groups']],
                'manual':' '.join(selection['manual']),'exclude':' '.join(selection['excluded'])})
        except (KeyError,TypeError):raise DataError('IMPORT_UNIVERSE_REQUIRED') from None
        if resolved!=selection:raise DataError('IMPORT_UNIVERSE_SOURCE_MISMATCH')
        return resolved

    def upload_csv(self,owner,raw,key):
        from .simple_inputs import preview_csv
        from .contracts import safe_bytes
        import hashlib
        safe_bytes(raw,1024*1024);preview_csv(raw)
        digest=hashlib.sha256(raw).hexdigest()
        def insert(con):
            binding=self.market.storage.bind(owner,'upload:'+key,categories=('market_data',),connection=con)
            ref=self.market.storage.put(binding,'market_data',raw,media_type='text/csv',connection=con)
            ident='upload-'+uuid4().hex;con.execute('INSERT INTO data_library_uploads VALUES(?,?,?,?)',(ident,owner,b'',digest))
            con.execute('INSERT INTO data_library_upload_payloads VALUES(?,?,?)',(ident,owner,canonical(ref)))
            return {'upload_id':ident,'sha256':digest,'bytes':len(raw)}
        return self._write(owner,key,{'action':'upload_csv','sha256':digest},insert)

    def _write(self,owner,key,request,operation,context=None,*,publication_guard=None):
        identifier(owner);identifier(key);encoded=canonical(clean(request))
        if len(encoded.encode())>2*1024**2:raise DataError('IMPORT_BYTES_LIMIT')
        digest=fingerprint(request)
        if context:context.require_active()
        with self.db.transaction() as con:
            if context:context.assert_fenced(con)
            if publication_guard is not None:publication_guard(con)
            previous=con.execute('SELECT request_hash,response FROM data_library_keys WHERE owner=? AND key=?',(owner,key)).fetchone()
            if previous:
                if previous[0]!=digest:raise DataError('IDEMPOTENCY_CONFLICT')
                return json.loads(previous[1])
            require_open_key(con, owner, 'library', key)
            response=operation(con)
            con.execute('INSERT INTO data_library_keys VALUES(?,?,?,?)',(owner,key,digest,canonical(response)))
            return response

    def save_connection(self,owner,body,key,*,publication_guard=None):
        if type(body)is not dict or set(body)!={'name','provider','credential_ref'}:raise DataError('CONNECTION_SCHEMA')
        if body['provider'] not in {'csv','massive','yfinance','akshare','openbb_cboe','akshare_cn','tushare','openbb_nasdaq','wrds'} or type(body['name'])is not str or not 1<=len(body['name'])<=100:raise DataError('CONNECTION_SCHEMA')
        reference=body['credential_ref']
        if body['provider']=='wrds' and (type(reference)is not str or not re.fullmatch(r'credential:FA_[0-9a-f]{64}',reference)):
            raise DataError('WRDS_CREDENTIAL_REQUIRED')
        if body['provider'] in {'yfinance','akshare','openbb_cboe','akshare_cn','openbb_nasdaq'} and reference is not None:raise DataError('PUBLIC_PROVIDER_CREDENTIAL_FORBIDDEN')
        if reference is not None and (type(reference)is not str or re.fullmatch(r'credential:[A-Za-z0-9_.-]{1,100}',reference)is None):raise DataError('CREDENTIAL_REFERENCE_REQUIRED')
        return self._write(owner,key,body,lambda con:self._insert_connection(con,owner,body),
                           publication_guard=publication_guard)

    @staticmethod
    def _insert_connection(con,owner,body):
        ident='connection-'+uuid4().hex
        con.execute('INSERT INTO data_library_connections VALUES(?,?,?)',(ident,owner,canonical(clean(body))))
        return {'connection_id':ident,**body,'status':'LOCAL_IMPORT' if body['provider']=='csv' else 'REAL_CALLS_NOT_VERIFIED'}

    def list_connections(self,owner):
        from .market_lifecycle import connections
        page=connections(self,owner,limit=100,with_references=False)
        items=page['items']
        if page['total']>100:
            items.extend(connections(self,owner,limit=100,offset=100,with_references=False)['items'])
        return {'items':items}

    def connection(self,owner,ident):
        from .market_lifecycle import read_connection
        with closing(self.db.connect()) as con:
            state=read_connection(con,owner,ident)
        return {key:state[key] for key in ('name','provider','credential_ref')}

    def active_connection(self,owner,ident):
        from .market_lifecycle import read_connection
        with closing(self.db.connect()) as con:
            state=read_connection(con,owner,ident)
        if state['archived']:raise DataError('MARKET_CONNECTION_ARCHIVED')
        return {key:state[key] for key in ('name','provider','credential_ref')}

    def import_dataset(self,owner,body,key,*,context=None,security_metadata=None,manifest=None,goal_data_provenance=None,publication_guard=None,managed_origin=None):
        identifier(owner);identifier(key)
        if context:context.require_active()
        request=body if manifest is None else {'body':body,'manifest':manifest,'security_metadata':security_metadata}
        if goal_data_provenance is not None:
            if context is None or publication_guard is None or manifest is not None:
                raise DataError('GOAL_DATA_CAPTURE_INVALID')
            request={'body':body,'goal_data_provenance':goal_data_provenance}
        if managed_origin is not None:
            if publication_guard is None or manifest is not None or goal_data_provenance is not None:
                raise DataError('MARKET_PROVENANCE_INVALID')
            request=managed_origin['request']
        with closing(self.db.connect()) as con:
            previous=con.execute('SELECT request_hash,response FROM data_library_keys WHERE owner=? AND key=?',(owner,key)).fetchone()
            if previous and managed_origin is not None:publication_guard(con)
        if previous:
            if previous[0]!=fingerprint(request):raise DataError('IDEMPOTENCY_CONFLICT')
            return json.loads(previous[1])
        required={'connection_id','name','universe','start','end','metadata','rows','source','received_at','scope','kind','benchmark'}
        if type(body)is not dict or not required<=set(body) or set(body)-required-{'original_upload_id','csv_mapping'}:raise DataError('IMPORT_SCHEMA')
        if body['scope'] not in {'synthetic','public','licensed_local'} or body['kind'] not in {'dataset','benchmark'}:raise DataError('IMPORT_SCOPE')
        metadata(body['metadata']);received=parse_instant(body['received_at'])
        if type(body['source'])is not dict or set(body['source'])!={'provider','reference','universe_as_of'}:raise DataError('IMPORT_SOURCE_REQUIRED')
        if body['source']['provider']=='wrds' or body['scope']=='licensed_local':
            from .wrds_library import require_capture
            require_capture(body,manifest,security_metadata,context)
        if type(body['source'])is dict and body['source'].get('provider')=='openbb_cboe' and goal_data_provenance is None:
            from .openbb_source import META
            if body['metadata']!=META:raise DataError('OPENBB_PRICE_BASIS_MISMATCH')
            if context is None or type(manifest)is not dict or manifest.get('provider_evidence',{}).get('provider')!='openbb_cboe' or not security_metadata:raise DataError('OPENBB_CAPTURE_REQUIRED')
        elif type(body['source'])is dict and body['source'].get('provider') in {'akshare_cn','tushare','openbb_nasdaq','openbb_cboe'}:
            from .goal_data.goal_data import CAPABILITIES
            from .goal_data.goal_data_service import metadata_for
            if goal_data_provenance is None or goal_data_provenance.get('provider')!=body['source']['provider']:
                raise DataError('GOAL_DATA_CAPTURE_INVALID')
            cap=CAPABILITIES[body['source']['provider']]
            adjustment=body['metadata']['adjustment']
            if (adjustment not in cap.adjustments or body['metadata']!=metadata_for(adjustment,cap)
                    or goal_data_provenance.get('market')!=cap.market
                    or goal_data_provenance.get('asset') not in cap.assets):
                raise DataError('GOAL_DATA_REQUIREMENT_UNSUPPORTED')
        elif body['metadata']['adjustment']=='provider_observed_unverified':raise DataError('OPENBB_PRICE_BASIS_MISMATCH')
        if received>datetime.now(timezone.utc):raise DataError('IMPORT_FUTURE_RECEIPT')
        selection=body['universe']
        if type(selection)is not dict or not isinstance(selection.get('tickers'),list) or selection.get('historical_constituents') is not False:raise DataError('IMPORT_UNIVERSE_REQUIRED')
        symbols=tickers(' '.join(selection['tickers']))
        if symbols!=selection['tickers']:raise DataError('IMPORT_UNIVERSE_REQUIRED')
        self.validate_selection(owner,selection)
        if manifest is None:
            rows=validate_rows(body['rows'],body['start'],body['end'],symbols,
                allow_missing_volume=(goal_data_provenance is not None or managed_origin is not None)
                                     and body['metadata']['volume_unit']=='provider_reported_unverified')
            quality={'rows':len(rows),'securities':len(symbols),'first':min(r['session_date'] for r in rows),'last':max(r['session_date'] for r in rows),'missing_sessions':'NOT_INFERRED','complete_symbols':True}
        else:
            # Private server port only: browser/API imports cannot supply a manifest.
            if context is None or body['rows'] is not None or body.get('original_upload_id'):raise DataError('IMPORT_MANIFEST_INTERNAL_ONLY')
            from .simple_chunks import DatasetChunks
            rows=None;counts={};first=None;last=None;total=0
            for r in DatasetChunks(self.policy,owner,binding=self.market.bind(owner,context=context)).stream(manifest,start=body['start'],end=body['end']):
                if total%2000==0:context.require_active()
                total+=1;counts[r['ticker']]=counts.get(r['ticker'],0)+1
                first=min(first or r['session_date'],r['session_date']);last=max(last or r['session_date'],r['session_date'])
            if sorted(counts)!=symbols:raise DataError('IMPORT_PARTIAL_SYMBOL_FAILURE')
            if {r['ticker']:r['rows'] for r in manifest['securities']}!=counts:raise DataError('DATASET_HASH_MISMATCH')
            quality={'rows':total,'securities':len(symbols),'first':first,'last':last,'missing_sessions':'NOT_INFERRED','complete_symbols':True,
                'completeness':'UNVERIFIED_PROVIDER_RESPONSE','completed_provider_iterators':True}
        original_upload=None
        if body.get('original_upload_id'):
            from .simple_inputs import preview_csv
            import hashlib
            with closing(self.db.connect()) as con:upload=self._upload(owner,body['original_upload_id'])
            if not upload:raise DataError('RESOURCE_NOT_FOUND')
            if hashlib.sha256(upload[0]).hexdigest()!=upload[1]:raise DataError('DATASET_HASH_MISMATCH')
            preview=preview_csv(upload[0],{'mapping':body.get('csv_mapping',{}),'metadata':body['metadata'],'ticker':symbols[0] if len(symbols)==1 else ''})
            if preview['status']!='READY' or validate_rows(preview['rows'],body['start'],body['end'],symbols)!=rows:raise DataError('CSV_SOURCE_ROWS_MISMATCH')
            original_upload={'upload_id':body['original_upload_id'],'sha256':upload[1],'bytes':len(upload[0])}
        if quality['last']>received.date().isoformat():raise DataError('IMPORT_FUTURE_OBSERVATION')
        benchmark_identity=None
        if body['kind']=='benchmark':
            b=body['benchmark']
            if type(b)is not dict or set(b)!={'instrument_label','return_basis','dividend_policy'} or b['return_basis'] not in {'price','total_return'} or len(symbols)!=1:raise DataError('BENCHMARK_SCHEMA')
            if b['return_basis']=='total_return':raise DataError('TOTAL_RETURN_EVIDENCE_NOT_SUPPORTED')
            if (body['source']['provider']=='yfinance' and manifest is not None
                    and manifest.get('acquisition',{}).get('provider')=='yfinance'):
                from .benchmark_sources import capture_identity
                benchmark_identity=capture_identity('yfinance',symbols,body['metadata'],security_metadata)
        elif body['benchmark'] is not None:raise DataError('BENCHMARK_SCHEMA')
        name=body['name']
        if name is not None and (type(name)is not str or not 1<=len(name)<=120 or any(ord(c)<32 for c in name)):raise DataError('DATASET_NAME_INVALID')
        # Display name is generated once, includes both coverage and acquisition date.
        title=f"{name or '日线数据'}_{body['start']}至{body['end']}_导入{received.date()}"
        content={'rows':rows} if manifest is None else {'chunk_rows_hashes':[r['rows_hash'] for r in manifest['chunks']]}
        rows_hash=fingerprint({**content,'metadata':body['metadata'],'kind':body['kind'],'benchmark':body['benchmark']})
        normalized={**body,'submitted_rows':body['rows'],'rows':rows,'name':title,'origin_job_id':context.job_id if context else None,'pit_status':'UNVERIFIED','availability_basis':'RECEIPT_ONLY','identity_status':'PROVIDER_SNAPSHOT_WINDOW_UNVERIFIED' if security_metadata else 'TICKER_ONLY',
          'security_metadata':security_metadata,'original_upload':original_upload,
          'input_content_hash':rows_hash,
          'published_at':None,'available_at':None,'return_basis':'price','corporate_actions_status':'NOT_PROVIDED','external_cash_flows_status':'NOT_PROVIDED',
          'quality':quality,'version':1}
        if manifest is not None:normalized['row_storage']=manifest
        if goal_data_provenance is not None:normalized['goal_data_provenance']=goal_data_provenance
        if benchmark_identity is not None:normalized['benchmark_identity']=benchmark_identity
        if managed_origin is not None:
            normalized['market_provenance']=managed_origin['provenance']
            if managed_origin['acquisition'] is not None:normalized['acquisition']=managed_origin['acquisition']
        digest=fingerprint(normalized)
        write_binding=self.market.bind(owner,'dataset:'+key,context=context)
        stored_normalized=self.market.pack(owner,normalized,write_binding)
        def insert(con):
            connection=con.execute('SELECT body FROM data_library_connections WHERE owner=? AND id=?',(owner,body['connection_id'])).fetchone()
            if not connection:raise DataError('RESOURCE_NOT_FOUND')
            if json.loads(connection[0])['provider']!=body['source']['provider']:raise DataError('IMPORT_SOURCE_MISMATCH')
            ident='dataset-'+uuid4().hex
            duplicate=con.execute('SELECT id FROM data_library_versions WHERE owner=? AND rows_hash=? ORDER BY created LIMIT 1',(owner,rows_hash)).fetchone()
            con.execute('INSERT INTO data_library_versions VALUES(?,?,?,?,?,?,?)',(ident,owner,body['kind'],canonical(stored_normalized),digest,rows_hash,datetime.now(timezone.utc).isoformat()))
            con.execute('INSERT INTO data_library_lifecycle VALUES(?,1,0)',(ident,))
            con.execute('INSERT INTO data_library_publications VALUES(?,?)',(ident,normalized['origin_job_id']))
            return {'dataset_id':ident,'version':1,'content_hash':digest,'name':title,'duplicate_of':duplicate[0] if duplicate else None,'pit_status':'UNVERIFIED'}
        return self._write(owner,key,request,insert,context,publication_guard=publication_guard)

    def get(self,owner,ident,*,include_rows=False,selectable=False, _include_acceptances=False):
        identifier(owner);identifier(ident)
        with closing(self.db.connect()) as con:
            con.execute('BEGIN')
            return self._get(con,owner,ident,include_rows=include_rows,selectable=selectable,
                             _include_acceptances=_include_acceptances)

    def _get(self,con,owner,ident,*,include_rows=False,selectable=False, _include_acceptances=False):
        row=con.execute('''SELECT v.body,v.hash,p.dataset_id,p.origin_job_id
            FROM data_library_versions v LEFT JOIN data_library_publications p ON p.dataset_id=v.id
            WHERE v.owner=? AND v.id=?''',(owner,ident)).fetchone()
        if not row:raise DataError('RESOURCE_NOT_FOUND')
        state=con.execute('SELECT trashed,revision FROM data_library_lifecycle WHERE id=? ORDER BY revision DESC LIMIT 1',(ident,)).fetchone()
        display=con.execute('SELECT name FROM data_library_display_names WHERE id=? ORDER BY revision DESC LIMIT 1',(ident,)).fetchone()
        refs=con.execute("SELECT result_id FROM data_library_refs WHERE id=? AND (? OR result_id NOT LIKE 'accepted-plan:%') ORDER BY result_id", (ident, _include_acceptances)).fetchall()
        scope=self.market.scope(owner,row[0]);body=self.market.unpack(owner,row[0])
        if fingerprint(body)!=row[1]:raise DataError('DATASET_HASH_MISMATCH')
        if row[2] is not None and body.get('origin_job_id')!=row[3]:raise DataError('DATASET_HASH_MISMATCH')
        if body.get('origin_job_id'):
            job=con.execute('SELECT status FROM jobs WHERE job_id=? AND owner_id=?',(body['origin_job_id'],owner)).fetchone()
            if not job or job[0]!='SUCCEEDED':raise DataError('DATASET_NOT_COMMITTED')
        if selectable and state[0]:raise DataError('DATASET_IN_TRASH')
        if include_rows and body.get('row_storage'):
            from .simple_chunks import DatasetChunks
            body['rows']=list(DatasetChunks(self.policy,owner,read_scope=scope).stream(body['row_storage'],start=body['start'],end=body['end']))
        if not include_rows:body.pop('rows');body.pop('submitted_rows')
        if display is not None:body['name']=display[0]
        return {**body,'dataset_id':ident,'content_hash':row[1],'trashed':bool(state[0]),'revision':state[1],
                'referenced_by':[r[0] for r in refs]}

    def list_datasets(self,owner,*,trash=False,kind='dataset',limit=50,offset=0):
        identifier(owner);validate_paging(limit,offset)
        if type(trash)is not bool or kind not in {'dataset','benchmark'}:raise DataError('INVALID_INPUT')
        filters=(owner,kind,int(trash))
        with closing(self.db.connect()) as con:
            con.execute('BEGIN')
            total=con.execute('SELECT count(*) '+_CATALOG_SCOPE,filters).fetchone()[0]
            rows=con.execute('SELECT v.id '+_CATALOG_SCOPE+' ORDER BY v.created DESC,v.id LIMIT ? OFFSET ?',(*filters,limit,offset)).fetchall()
            items=[self._get(con,owner,row[0]) for row in rows]
        return {'items':items,'total':total,'limit':limit,'offset':offset}

    def set_trash(self,owner,ident,trashed,key,*,publication_guard=None):
        self.get(owner,ident)
        if type(trashed)is not bool:raise DataError('INVALID_INPUT')
        def change(con):
            revision=con.execute('SELECT max(revision)+1 FROM data_library_lifecycle WHERE id=?',(ident,)).fetchone()[0]
            con.execute('INSERT INTO data_library_lifecycle VALUES(?,?,?)',(ident,revision,int(trashed)))
            return {'dataset_id':ident,'trashed':trashed,'physical_delete':False}
        return self._write(owner,key,{'id':ident,'trashed':trashed},change,publication_guard=publication_guard)

    def pin(self,owner,reference,result_id):
        record=self.get(owner,reference['dataset_id']);identifier(result_id)
        if reference!={k:record[k] for k in ('dataset_id','version','content_hash')}:raise DataError('DATASET_REFERENCE_MISMATCH')
        with self.db.transaction() as con:
            if not con.execute('SELECT 1 FROM data_library_refs WHERE id=? AND result_id=?',(reference['dataset_id'],result_id)).fetchone():
                con.execute('INSERT INTO data_library_refs VALUES(?,?)',(reference['dataset_id'],result_id))
        return reference

    def accept_for_run(self,owner,reference,run_id,*,context=None,check_approval=None):
        """Atomic selection/pin. Once accepted, trash cannot invalidate this run."""
        identifier(owner);identifier(run_id);self.get(owner,reference['dataset_id'])
        if context:context.require_active()
        with self.db.transaction() as con:
            if check_approval is not None:
                check_approval(con)
            if context:context.assert_fenced(con)
            row=con.execute('SELECT hash FROM data_library_versions WHERE owner=? AND id=?',(owner,reference['dataset_id'])).fetchone()
            if not row:raise DataError('RESOURCE_NOT_FOUND')
            if reference!={'dataset_id':reference['dataset_id'],'version':1,'content_hash':row[0]}:raise DataError('DATASET_REFERENCE_MISMATCH')
            accepted=con.execute('SELECT 1 FROM data_library_refs WHERE id=? AND result_id=?',(reference['dataset_id'],run_id)).fetchone()
            state=con.execute('SELECT trashed FROM data_library_lifecycle WHERE id=? ORDER BY revision DESC LIMIT 1',(reference['dataset_id'],)).fetchone()
            if state[0] and not accepted:raise DataError('DATASET_IN_TRASH')
            if not accepted:con.execute('INSERT INTO data_library_refs VALUES(?,?)',(reference['dataset_id'],run_id))
        return reference

    def read_accepted(self,owner,reference,run_id):
        record=self.get(owner,reference['dataset_id'],include_rows=True,_include_acceptances=True)
        if reference!={k:record[k] for k in ('dataset_id','version','content_hash')} or run_id not in record['referenced_by']:raise DataError('DATASET_NOT_ACCEPTED_FOR_RUN')
        return record

    def read_for_backtest(self,owner,reference,run_id,*,start=None,end=None):
        data=self.read_accepted(owner,reference,run_id)
        start=start or data['quality']['first'];end=end or data['quality']['last']
        if not data['start']<=start<=end<=data['end']:raise DataError('BACKTEST_WINDOW_OUTSIDE_DATASET')
        if data['source']['provider']=='openbb_cboe' and data.get('goal_data_provenance'):
            raise DataError('OPENBB_PRICE_BASIS_MISMATCH')
        if data['source']['provider']=='openbb_cboe':
            from itertools import zip_longest
            from .openbb_library_download import expected_rows
            for actual,expected in zip_longest(data['rows'],expected_rows(self,owner,data)):
                if actual!=expected:raise DataError('OPENBB_TRANSFORM_MISMATCH')
        rows=[r for r in data['rows'] if start<=r['session_date']<=end]
        if not rows:raise DataError('BACKTEST_WINDOW_EMPTY')
        return {'dataset_reference':reference,'rows':rows,'metadata':data['metadata'],'universe':data['universe'],
            'window':{'start':start,'end':end},'return_basis':'price','pit_status':'UNVERIFIED',
            'published_at':None,'available_at':None,'corporate_actions':None,'external_cash_flows':None,
            'acquisition':data.get('acquisition') or data.get('row_storage',{}).get('acquisition')}

    def _upload(self,owner,ident):
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT raw,hash FROM data_library_uploads WHERE owner=? AND id=?',(owner,ident)).fetchone()
            payload=con.execute('SELECT ref FROM data_library_upload_payloads WHERE owner=? AND id=?',(owner,ident)).fetchone()
        if not row:return None
        if payload:
            raw=self.market.storage.read(owner,json.loads(payload[0]),category='market_data',maximum_bytes=1024*1024)
            return (raw,row[1])
        return row

    def original_csv(self,owner,ident):
        import hashlib
        record=self.get(owner,ident);upload=record['original_upload']
        if not upload:raise DataError('ORIGINAL_CSV_NOT_AVAILABLE')
        row=self._upload(owner,upload['upload_id'])
        if not row or hashlib.sha256(row[0]).hexdigest()!=upload['sha256']:raise DataError('DATASET_HASH_MISMATCH')
        return row[0]

    def read_native_for_backtest(self,owner,reference,run_id,*,start=None,end=None):
        """Exact accepted dataset -> original pinned history -> legal subset.

        No runner exists on this read path. Explicit IDs/hashes only, never
        latest ticker lookup or acquisition during backtest/report reopening.
        """
        from .native_library_download import read_native_accepted
        return read_native_accepted(self,owner,reference,run_id,start=start,end=end)


    def read_for_research(self,owner,reference,run_id,*,start=None,end=None,context,maximum_rows=200000,parse_cache=None):
        """Accepted immutable source -> verified stream -> bounded selected window.

        Source hashes/order/count remain checked even outside the selected crop.
        Physical scope comes only from the stored source envelope, never latest
        settings or a caller path. Native data also checks original history pins.
        """
        from datetime import date
        identifier(owner);identifier(run_id);context.require_active()
        if context.run.owner_id!=owner or context.run.run_id!=run_id:raise DataError('DATASET_NOT_ACCEPTED_FOR_RUN')
        if type(maximum_rows)is not int or not 1<=maximum_rows<=200000:raise DataError('RESEARCH_INPUT_LIMIT')
        record=self.get(owner,reference['dataset_id'])
        if reference!={k:record[k] for k in ('dataset_id','version','content_hash')} or run_id not in record['referenced_by']:raise DataError('DATASET_NOT_ACCEPTED_FOR_RUN')
        start=start or record['quality']['first'];end=end or record['quality']['last']
        try:
            if date.fromisoformat(start).isoformat()!=start or date.fromisoformat(end).isoformat()!=end or not record['start']<=start<=end<=record['end']:raise ValueError
        except (ValueError,TypeError):raise DataError('BACKTEST_WINDOW_OUTSIDE_DATASET') from None
        if record.get('row_storage'):
            from .simple_chunks import DatasetChunks
            with closing(self.db.connect()) as con:
                stored=con.execute('SELECT body FROM data_library_versions WHERE owner=? AND id=?',(owner,reference['dataset_id'])).fetchone()
            scope = self.market.scope(owner, stored[0])
            parse_options = {}
            if parse_cache is not None and scope is not None and record['scope']!='licensed_local':
                parse_options = {'parse_cache': parse_cache, 'parse_identity': {
                    'dataset_reference': reference, 'dataset_scope': record['scope'],
                    'purpose': 'autonomous_research_input', 'phase': 'development',
                    'window': {'start': start, 'end': end}, 'visible_through': end,
                    'engine_sha256': fingerprint({'code': context.run.code_hash,
                                                  'dependencies': context.run.dependency_hash,
                                                  'parser': 'dataset-parquet-chunk-to-canonical-rows-v1'}),
                }}
            source = DatasetChunks(self.policy, owner, read_scope=scope).stream(
                record['row_storage'], start=record['start'], end=record['end'], **parse_options)
        else:source=iter(self.get(owner,reference['dataset_id'],include_rows=True)['rows'])
        native=record.get('row_storage',{}).get('acquisition')
        if record.get('acquisition') is not None or record.get('market_provenance',{}).get('native_references'):
            from .market_lineage import derived_research_acquisition
            rows=list(source)
            native=derived_research_acquisition(self,owner,record,rows,context=context)
            source=iter(rows)
        elif native:
            from itertools import zip_longest
            from .native_library_download import native_expected_rows
            def checked(rows):
                for actual,expected in zip_longest(rows,native_expected_rows(self,owner,record,context=context)):
                    if actual!=expected:raise DataError('NATIVE_DATASET_ROW_BINDING_MISMATCH')
                    yield actual
            source=checked(source)
        if record.get('row_storage',{}).get('provider_evidence'):
            from itertools import zip_longest
            from .openbb_library_download import expected_rows
            def checked_openbb(rows):
                for actual,expected in zip_longest(rows,expected_rows(self,owner,record,context=context)):
                    if actual!=expected:raise DataError('OPENBB_TRANSFORM_MISMATCH')
                    yield actual
            source=checked_openbb(source)
        selected=[];total=0
        for row in source:
            if total%128==0:context.require_active()
            total+=1
            if start<=row['session_date']<=end:
                if len(selected)>=maximum_rows:raise DataError('RESEARCH_INPUT_LIMIT')
                selected.append(row)
        if total!=record['quality']['rows']:raise DataError('DATASET_HASH_MISMATCH')
        if not selected:raise DataError('BACKTEST_WINDOW_EMPTY')
        return {'dataset_reference':reference,'rows':selected,'metadata':record['metadata'],'universe':record['universe'],
                'window':{'start':start,'end':end},'return_basis':'price','pit_status':'UNVERIFIED',
                'published_at':None,'available_at':None,'corporate_actions':None,'external_cash_flows':None,
                'acquisition':native or record.get('row_storage',{}).get('provider_evidence') or record.get('row_storage',{}).get('wrds_capture')}
