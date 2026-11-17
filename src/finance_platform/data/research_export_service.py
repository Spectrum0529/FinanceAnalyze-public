"""Fenced research export jobs and owner-only download of committed artifacts."""
from contextlib import closing
from datetime import date
from hashlib import sha256
import json
from finance_platform.core.contracts import canonical,fingerprint,identifier,digest
from finance_platform.core.database import immutable
from .contracts import DataError
from .research_inputs import FORMAT,MAX_EXPORT_BYTES,export_research_input
from . import openbb_adapter

class ResearchExportService:
    def __init__(self,library,*,openbb_enabled=False):
        if type(openbb_enabled)is not bool:raise DataError('OPENBB_FEATURE_CONFIGURATION_INVALID')
        self.library,self.db,self.openbb_enabled=library,library.db,openbb_enabled
        self.db.apply_module_migrations('data_library_research_exports',('CREATE TABLE data_library_research_exports(owner TEXT NOT NULL,job TEXT PRIMARY KEY,body TEXT NOT NULL,hash TEXT NOT NULL);',))
        with self.db.transaction() as con:immutable(con,'data_library_research_exports',('job',))
    def capabilities(self):
        return {'formats':[{'format':FORMAT,'available':True,'status':'REGISTERED_NOT_RUNTIME_VALIDATED'},openbb_adapter.capability(enabled=self.openbb_enabled)],
                'maximum_rows':200000,'maximum_bytes':MAX_EXPORT_BYTES,'window':'INCLUSIVE_DATES','validation':'NOT_RUN_IN_DEVELOPMENT_BATCH'}
    def validate(self,p):
        if type(p)is not dict or set(p)!={'reference','start','end','format'}:raise DataError('RESEARCH_EXPORT_SCHEMA')
        ref=p['reference']
        if type(ref)is not dict or set(ref)!={'dataset_id','version','content_hash'} or type(ref['version'])is not int or ref['version']<1:raise DataError('RESEARCH_EXPORT_SCHEMA')
        identifier(ref['dataset_id']);digest(ref['content_hash'])
        for key in ('start','end'):
            if p[key] is not None:
                try:
                    if date.fromisoformat(p[key]).isoformat()!=p[key]:raise ValueError
                except (TypeError,ValueError):raise DataError('IMPORT_DATES_INVALID') from None
        if p['start'] is not None and p['end'] is not None and p['start']>p['end']:raise DataError('IMPORT_DATES_INVALID')
        if p['format'] not in {FORMAT,openbb_adapter.FORMAT}:raise DataError('RESEARCH_EXPORT_FORMAT')
        if p['format']==openbb_adapter.FORMAT:openbb_adapter.require_enabled(enabled=self.openbb_enabled)
    def run(self,context,p):
        self.validate(p);context.require_active();owner=context.run.owner_id
        self.library.accept_for_run(owner,p['reference'],context.run.run_id,context=context)
        package=export_research_input(self.library,owner,p['reference'],context.run.run_id,start=p['start'],end=p['end'],context=context)
        if p['format']==FORMAT:
            raw=canonical(package).encode('utf-8');manifest={key:package['manifest'][key] for key in ('format','rows')}
            manifest.update(sha256=sha256(raw).hexdigest(),bytes=len(raw))
        else:raw,manifest=openbb_adapter.adapt(package,library=self.library,record=self.library.get(owner,p['reference']['dataset_id']),owner=owner,enabled=self.openbb_enabled,context=context)
        if len(raw)>MAX_EXPORT_BYTES:raise DataError('RESEARCH_INPUT_LIMIT')
        binding=self.library.market.storage.lookup_binding(owner,'job:'+context.job_id)
        context.require_active()
        with self.db.transaction() as con:
            context.assert_fenced(con)
            ref=self.library.market.storage.put(binding,'market_data',raw,media_type='application/json',connection=con)
            body={'payload':ref,'manifest':manifest,'dataset_reference':p['reference']}
            con.execute('INSERT INTO data_library_research_exports VALUES(?,?,?,?)',(owner,context.job_id,canonical(body),fingerprint(body)))
        return {'job_id':context.job_id,**manifest,'dataset_reference':p['reference'],'content_endpoint':'data/library/exports/'+context.job_id+'/content',
                'pit_status':'UNVERIFIED','permitted_use':'LOCAL_PERSONAL_RESEARCH_ONLY'}
    def content(self,owner,job):
        identifier(owner);identifier(job)
        with closing(self.db.connect()) as con:
            row=con.execute("SELECT e.body,e.hash FROM data_library_research_exports e JOIN jobs j ON j.job_id=e.job AND j.owner_id=e.owner WHERE e.owner=? AND e.job=? AND j.handler='data.research-export' AND j.status='SUCCEEDED'",(owner,job)).fetchone()
        if not row:raise DataError('RESOURCE_NOT_FOUND')
        body=json.loads(row[0])
        if fingerprint(body)!=row[1]:raise DataError('DATASET_HASH_MISMATCH')
        raw=self.library.market.storage.read(owner,body['payload'],category='market_data',maximum_bytes=MAX_EXPORT_BYTES)
        if len(raw)!=body['manifest']['bytes'] or sha256(raw).hexdigest()!=body['manifest']['sha256']:raise DataError('DATASET_HASH_MISMATCH')
        return raw

    def page(self,owner,job,*,offset=0,limit=65536):
        import base64
        if type(offset)is not int or not 0<=offset<=MAX_EXPORT_BYTES or type(limit)is not int or not 1<=limit<=65536:raise DataError('RESEARCH_EXPORT_PAGE_INVALID')
        raw=self.content(owner,job)
        if offset>len(raw):raise DataError('RESEARCH_EXPORT_PAGE_INVALID')
        end=min(len(raw),offset+limit)
        return {'job_id':job,'sha256':sha256(raw).hexdigest(),'bytes':len(raw),'offset':offset,'limit':limit,
                'content_base64':base64.b64encode(raw[offset:end]).decode('ascii'),'next_offset':end if end<len(raw) else None,
                'media_type':'application/json','immutable':True}
