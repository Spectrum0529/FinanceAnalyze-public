"""Durable public download attempts; conservative SDK reservations never refund.

Existing HistoryGateway/SDKRunner still own actual network/time/request budgets.
This small journal adds per-symbol reuse and whole-family bounds, not a second
transport or a claim that SDK reservations are physical/billed request counts.
"""
from contextlib import closing
from datetime import datetime,timezone
from uuid import uuid4
import json
from finance_platform.core.contracts import canonical,fingerprint,identifier
from finance_platform.core.database import immutable
from .contracts import DataError

MIGRATIONS=('''
CREATE TABLE native_download_families(id TEXT PRIMARY KEY,owner TEXT NOT NULL,request TEXT NOT NULL,hash TEXT NOT NULL);
CREATE TABLE native_download_attempts(seq INTEGER PRIMARY KEY,family TEXT NOT NULL REFERENCES native_download_families(id),job TEXT UNIQUE NOT NULL,run TEXT NOT NULL);
CREATE TABLE native_download_reservations(seq INTEGER PRIMARY KEY,family TEXT NOT NULL REFERENCES native_download_families(id),job TEXT NOT NULL,ticker TEXT NOT NULL,sdk_units INTEGER NOT NULL);
CREATE TABLE native_download_units(family TEXT NOT NULL REFERENCES native_download_families(id),ticker TEXT NOT NULL,body TEXT NOT NULL,hash TEXT NOT NULL,location TEXT NOT NULL,PRIMARY KEY(family,ticker));
''',)
MAX_ATTEMPTS=8
MAX_SDK_UNITS=288 #32 symbols *3 conservative calls *3 passes; no refund on crash.

class NativeDownloadJournal:
    def __init__(self,database,storage):
        self.db,self.storage=database,storage
        database.apply_module_migrations('native_library_download_journal',MIGRATIONS)
        with database.transaction() as con:
            for table,keys in [('native_download_families',('id',)),('native_download_attempts',('seq',)),('native_download_reservations',('seq',)),('native_download_units',('family','ticker'))]:immutable(con,table,keys)
    def begin(self,owner,request,context,resume_id=None):
        identifier(owner);encoded=canonical(request)
        if len(encoded.encode())>1024*1024:raise DataError('IMPORT_BYTES_LIMIT')
        context.require_active()
        with self.db.transaction() as con:
            context.assert_fenced(con)
            parent=getattr(context.run,'parent_run_id',None)
            if parent:
                inherited=con.execute('SELECT family FROM native_download_attempts WHERE run=?',(parent,)).fetchone()
                if inherited:
                    if resume_id is not None and resume_id!=inherited[0]:raise DataError('NATIVE_RESUME_MISMATCH')
                    resume_id=inherited[0]
            if resume_id is None:
                family='native-download-'+uuid4().hex
                con.execute('INSERT INTO native_download_families VALUES(?,?,?,?)',(family,owner,encoded,fingerprint(request)))
            else:
                identifier(resume_id);family=resume_id
                row=con.execute('SELECT request,hash FROM native_download_families WHERE owner=? AND id=?',(owner,family)).fetchone()
                if not row:raise DataError('RESOURCE_NOT_FOUND')
                if fingerprint(json.loads(row[0]))!=row[1] or row[1]!=fingerprint(request):raise DataError('NATIVE_RESUME_MISMATCH')
                last=con.execute('SELECT j.status FROM native_download_attempts a JOIN jobs j ON j.job_id=a.job WHERE a.family=? ORDER BY a.seq DESC LIMIT 1',(family,)).fetchone()
                if not last or last[0] not in {'FAILED','CANCELLED'}:raise DataError('NATIVE_RESUME_BUSY')
            if con.execute('SELECT count(*) FROM native_download_attempts WHERE family=?',(family,)).fetchone()[0]>=MAX_ATTEMPTS:raise DataError('NATIVE_DOWNLOAD_ATTEMPT_BUDGET')
            con.execute('INSERT INTO native_download_attempts(family,job,run) VALUES(?,?,?)',(family,context.job_id,context.run.run_id))
        return family
    def reserve(self,family,ticker,context):
        with self.db.transaction() as con:
            context.assert_fenced(con)
            bound=con.execute('SELECT f.owner FROM native_download_families f JOIN native_download_attempts a ON a.family=f.id WHERE f.id=? AND a.job=?',(family,context.job_id)).fetchone()
            if not bound or bound[0]!=context.run.owner_id:raise DataError('RESOURCE_NOT_FOUND')
            used=con.execute('SELECT coalesce(sum(sdk_units),0) FROM native_download_reservations WHERE family=?',(family,)).fetchone()[0]
            if used+3>MAX_SDK_UNITS:raise DataError('NATIVE_DOWNLOAD_SDK_BUDGET')
            con.execute('INSERT INTO native_download_reservations(family,job,ticker,sdk_units) VALUES(?,?,?,3)',(family,context.job_id,ticker))
    def unit(self,owner,family,ticker):
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT u.body,u.hash,u.location FROM native_download_units u JOIN native_download_families f ON f.id=u.family WHERE f.owner=? AND f.id=? AND u.ticker=?',(owner,family,ticker)).fetchone()
        if not row:return None
        body=json.loads(row[0])
        if fingerprint(body)!=row[1]:raise DataError('DATASET_HASH_MISMATCH')
        return body,{'owner_id':owner,'location_id':row[2]}
    def save(self,family,ticker,body,context):
        if len(canonical(body).encode())>2*1024**2:raise DataError('IMPORT_BYTES_LIMIT')
        binding=self.storage.lookup_binding(context.run.owner_id,'job:'+context.job_id)
        location=binding['locations']['market_data']['location_id']
        with self.db.transaction() as con:
            context.assert_fenced(con)
            bound=con.execute('SELECT 1 FROM native_download_attempts WHERE family=? AND job=?',(family,context.job_id)).fetchone()
            if not bound:raise DataError('RESOURCE_NOT_FOUND')
            old=con.execute('SELECT hash FROM native_download_units WHERE family=? AND ticker=?',(family,ticker)).fetchone()
            if old:
                if old[0]!=fingerprint(body):raise DataError('DATASET_HASH_MISMATCH')
                return
            con.execute('INSERT INTO native_download_units VALUES(?,?,?,?,?)',(family,ticker,canonical(body),fingerprint(body),location))
    def status(self,owner,job):
        identifier(owner);identifier(job)
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT f.id,f.request,f.hash FROM native_download_families f JOIN native_download_attempts a ON a.family=f.id WHERE f.owner=? AND a.job=?',(owner,job)).fetchone()
            if not row:raise DataError('RESOURCE_NOT_FOUND')
            latest=con.execute('SELECT a.job,j.status FROM native_download_attempts a JOIN jobs j ON j.job_id=a.job WHERE a.family=? ORDER BY a.seq DESC LIMIT 1',(row[0],)).fetchone()
            attempts=con.execute('SELECT count(*) FROM native_download_attempts WHERE family=?',(row[0],)).fetchone()[0]
            spent=con.execute('SELECT coalesce(sum(sdk_units),0) FROM native_download_reservations WHERE family=?',(row[0],)).fetchone()[0]
            completed=con.execute('SELECT count(*) FROM native_download_units WHERE family=?',(row[0],)).fetchone()[0]
        request=json.loads(row[1])
        if fingerprint(request)!=row[2]:raise DataError('DATASET_HASH_MISMATCH')
        reason=('STALE_ATTEMPT' if job!=latest[0] else 'COMPLETED' if latest[1]=='SUCCEEDED' else 'NOT_TERMINAL' if latest[1] not in {'FAILED','CANCELLED'} else 'ATTEMPT_BUDGET_EXHAUSTED' if attempts>=MAX_ATTEMPTS else 'SDK_BUDGET_EXHAUSTED' if spent>=MAX_SDK_UNITS and completed<len(request['universe']['tickers']) else 'READY')
        return {'schema':'native-download-progress-1','job_id':job,'latest_job_id':latest[0],'latest_status':latest[1],
                'resume_id':row[0],'eligible_resume':reason=='READY','resume_reason':reason,'original_request':request,
                'completed_units':completed,'total_units':len(request['universe']['tickers']),
                'attempts_used':attempts,'attempts_maximum':MAX_ATTEMPTS,'sdk_units_reserved':spent,'sdk_units_maximum':MAX_SDK_UNITS,
                'budget_basis':'CONSERVATIVE_SDK_CALL_RESERVATIONS_NOT_HTTP_OR_BILLING','resume_unit':'COMPLETE_SYMBOL',
                'seconds_per_attempt':1800,'family_wall_envelope_seconds':MAX_ATTEMPTS*1800,'network_budget':'EXISTING_SDKRUNNER_PER_CALL_LIMITS','validation':'NOT_RUN_IN_DEVELOPMENT_BATCH'}
