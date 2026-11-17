"""Current-owner synthetic preparation and durable post-core registration chain."""
from contextlib import closing
import hashlib,json
from finance_platform.core.auth import Principal,require_writer
from finance_platform.core.contracts import canonical,identifier
from finance_platform.core.database import immutable
from finance_platform.core.jobs import WorkerJobContext
from finance_platform.core.pagination import validate_pagination
from finance_platform.core.security import safe_json,now_text
from .contracts import DataError
from .qlib_input_bridge import OwnedQlibInputBridge,FIXTURE_ID,SOURCE_LOGICAL_SHA
from .qlib_export_authorizer import QlibExportAuthorizer,validate_register_parameters
from .qlib_dump import QlibOfficialDumpRunner

PROFILE_ID='FA014-QLIB-US-OFFLINE-V2'
PREPARE='data.qlib-prepare'
REGISTER='data.qlib-register'
MIGRATIONS=("""CREATE TABLE data_qlib_prepare_dispatches(
 prepare_job_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,prepare_run_id TEXT NOT NULL,
 native_id TEXT NOT NULL UNIQUE,registration_job_id TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL);
 CREATE TABLE data_qlib_register_request_keys(
 owner_id TEXT NOT NULL,request_key TEXT NOT NULL,source_job_id TEXT NOT NULL,
 created_at TEXT NOT NULL,PRIMARY KEY(owner_id,request_key));""",)
def validate_prepare_parameters(value):
    if type(value) is not dict or set(value)!={'profile_id'} or value['profile_id']!=PROFILE_ID:
        raise DataError('QLIB_PREPARATION_PROFILE_UNSUPPORTED')

class QlibPreparationService:
    def __init__(self,runtime,bridge,authorizer,runner=None):
        if (type(bridge) is not OwnedQlibInputBridge or type(authorizer) is not QlibExportAuthorizer
            or authorizer.bridge is not bridge or bridge.db is not runtime.database
            or runner is not None and type(runner) is not QlibOfficialDumpRunner):
            raise DataError('QLIB_PREPARATION_COMPOSITION_INVALID')
        self.runtime,self.db,self.bridge,self.authorizer,self.runner=runtime,runtime.database,bridge,authorizer,runner
        self._dispatch_cursor=None
        self.db.apply_module_migrations('data_qlib_prepare_dispatches_1',MIGRATIONS)
        with self.db.transaction() as con:immutable(con,'data_qlib_prepare_dispatches',('prepare_job_id',))
    def _principal(self,p,write=False):
        self.authorizer._principal(p,write=write)
    def _profile_rights(self,p):
        # No source/catalog is installed from a read/submit path. An absent
        # fixed source can be registered later only under the existing r06 job;
        # an existing withdrawal/drift is already a preparation denial.
        definition=self.authorizer.fixed_source_definition(p.user_id)
        try:self.authorizer.data.source(p.user_id,definition['source_id'])
        except DataError as e:
            if e.code=='RESOURCE_NOT_FOUND':return
            raise
        self.authorizer._source(p.user_id)
    def list_capabilities(self,principal):
        self._principal(principal)
        reason='QLIB_DUMP_EXECUTION_UNCONFIGURED'
        if self.runner is not None:
            try:self.runner.require_configured();reason=None
            except Exception as e:
                # Fixed public reason only, never raw review/path/config errors.
                reason='QLIB_DUMP_EXECUTION_BINDING_UNAVAILABLE'
        if reason is None:
            try:self._profile_rights(principal)
            except DataError:reason='QLIB_SYNTHETIC_SOURCE_RIGHTS_UNAVAILABLE'
        return {'profiles':[{'profile_id':PROFILE_ID,'scope':'SYNTHETIC','title':'固定合成数据（离线开发）',
            'purpose':'development_readonly','supported':True,'can_prepare':reason is None and principal.role in {'admin','researcher'},
            'blocked_reason':reason,'market_data_claim':False,'formal_research_approved':False}],
            'unsupported':['yfinance_history','akshare_adjusted_history','unreviewed_price_basis_vwap_factor'],
            'production_job_handler':PREPARE,'registration_is_separate_core_job':True}
    def submit_prepare(self,principal,*,profile_id,idempotency_key=None):
        parameters={'profile_id':profile_id}
        self._principal(principal,True);validate_prepare_parameters(parameters)
        if self.runner is None:raise DataError('QLIB_DUMP_EXECUTION_UNCONFIGURED')
        self.runner.require_configured()
        self._profile_rights(principal)
        return self.runtime.jobs.submit(principal,PREPARE,parameters,idempotency_key=idempotency_key)
    def submit_register(self,principal,*,source_job_id,idempotency_key=None):
        self._completed(principal,source_job_id)
        if idempotency_key is not None:
            identifier(idempotency_key)
            with self.db.transaction() as con:
                self._principal(principal,True)
                old=con.execute('SELECT source_job_id FROM data_qlib_register_request_keys WHERE owner_id=? AND request_key=?',
                    (principal.user_id,idempotency_key)).fetchone()
                if old is not None and old['source_job_id']!=source_job_id:raise DataError('QLIB_REGISTRATION_IDEMPOTENCY_CONFLICT')
                if old is None:con.execute('INSERT INTO data_qlib_register_request_keys VALUES(?,?,?,?)',
                    (principal.user_id,idempotency_key,source_job_id,now_text()))
        # Per-source stable queue key prevents duplicate jobs even when a
        # browser retries with a new caller key or post-finish recovery races.
        return self.after_core_finish(source_job_id)
    def prepare_handler(self,context,parameters):
        validate_prepare_parameters(parameters)
        if type(context) is not WorkerJobContext or context.service.database is not self.db:raise DataError('QLIB_REAL_JOB_CONTEXT_REQUIRED')
        p=self._owner_principal(context.run.owner_id)
        self.authorizer._context(p,context,PREPARE)
        if self.runner is None:raise DataError('QLIB_DUMP_EXECUTION_UNCONFIGURED')
        self.runner.require_configured()
        self._profile_rights(p)
        # Fresh current owner/run export every new non-idempotent job. Never use
        # a prior native_id, a global DATA_READY flag or a client source path.
        owned=self.bridge.consume_fixture(context,fixture_id=FIXTURE_ID)
        result=self.runner.run(context,self.bridge,owned['export_id'])
        if result.get('status')!='VERIFIED_OFFICIAL_DUMP_UNPUBLISHED' or not result.get('attempt_id'):
            raise DataError('QLIB_NATIVE_VERIFICATION_REQUIRED')
        context.require_active()
        self._profile_rights(p)
        native=self.bridge.publish_native(context,result['attempt_id'])
        if native['owner_id']!=p.user_id or native['run_id']!=context.run.run_id or native['export_id']!=owned['export_id']:
            raise DataError('QLIB_PREPARATION_RECEIPT_MISMATCH')
        # Worker must first call ordinary Jobs.finish on THIS returned result.
        # Registration is dispatched only after that committed SUCCEEDED row.
        return {'schema':'qlib-synthetic-preparation-completed-1','profile_id':PROFILE_ID,'scope':'SYNTHETIC',
            'owner_id':p.user_id,'prepare_job_id':context.job_id,'prepare_run_id':context.run.run_id,
            'export_id':owned['export_id'],'native_id':native['native_id'],'native_receipt_hash':native['content_hash'],
            'logical_data_hash':SOURCE_LOGICAL_SHA,'state':'NATIVE_PUBLISHED_AWAITING_CORE_FINISH'}
    def _owner_principal(self,owner):
        with closing(self.db.connect()) as con:row=con.execute('SELECT user_id,username,role,enabled FROM users WHERE user_id=?',(owner,)).fetchone()
        if row is None or not row['enabled']:raise DataError('QLIB_PRINCIPAL_CHANGED')
        p=Principal(row['user_id'],row['username'],row['role']);self._principal(p,True);return p
    def _completed(self,principal,job_id):
        identifier(job_id);self._principal(principal,True)
        job=self.runtime.jobs.get(principal,job_id)
        # Core admin visibility never grants another owner's registration.
        if job['owner_id']!=principal.user_id or job['handler']!=PREPARE or job['status']!='SUCCEEDED':
            raise DataError('QLIB_PREPARATION_CORE_SUCCESS_REQUIRED')
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT parameters_json,parameters_hash FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        if hashlib.sha256(row['parameters_json'].encode()).hexdigest()!=row['parameters_hash']:raise DataError('QLIB_PREPARATION_JOB_CHANGED')
        validate_prepare_parameters(json.loads(row['parameters_json']))
        result=job['result']
        keys={'schema','profile_id','scope','owner_id','prepare_job_id','prepare_run_id','export_id','native_id','native_receipt_hash','logical_data_hash','state'}
        if (type(result) is not dict or set(result)!=keys or result['schema']!='qlib-synthetic-preparation-completed-1'
            or result['profile_id']!=PROFILE_ID or result['scope']!='SYNTHETIC' or result['owner_id']!=principal.user_id
            or result['prepare_job_id']!=job_id or result['prepare_run_id']!=job['run_id']
            or result['logical_data_hash']!=SOURCE_LOGICAL_SHA or result['state']!='NATIVE_PUBLISHED_AWAITING_CORE_FINISH'):
            raise DataError('QLIB_PREPARATION_RESULT_INVALID')
        native,_,_=self.authorizer._native(principal.user_id,result['native_id'])
        if (native['run_id']!=job['run_id'] or native['export_id']!=result['export_id']
            or native['content_hash']!=result['native_receipt_hash']):
            raise DataError('QLIB_PREPARATION_RECEIPT_MISMATCH')
        return job,result
    def after_core_finish(self,job_id):
        """Trusted Worker post-finish hook, never API GET/resolve/capabilities."""
        identifier(job_id)
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT owner_id,handler,status FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        if row is None or row['handler']!=PREPARE:return None
        p=self._owner_principal(row['owner_id'])
        job,result=self._completed(p,job_id)
        key='qlib-register-'+hashlib.sha256(job_id.encode()).hexdigest()
        # Idempotent queue submit is outside any outer writer transaction.
        registration=self.runtime.jobs.submit(p,REGISTER,{'native_id':result['native_id']},idempotency_key=key)
        with self.db.transaction() as con:
            # Source job is terminal; current-principal and exact immutable
            # success/result must still hold at the chain-record commit.
            current=con.execute('SELECT owner_id,status,result_json FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            self._principal(p,True)
            if current['owner_id']!=p.user_id or current['status']!='SUCCEEDED' or json.loads(current['result_json'])!=result:
                raise DataError('QLIB_PREPARATION_JOB_CHANGED')
            old=con.execute('SELECT registration_job_id FROM data_qlib_prepare_dispatches WHERE prepare_job_id=?',(job_id,)).fetchone()
            if old is None:con.execute('INSERT INTO data_qlib_prepare_dispatches VALUES(?,?,?,?,?,?)',
                (job_id,p.user_id,job['run_id'],result['native_id'],registration['job_id'],now_text()))
            elif old['registration_job_id']!=registration['job_id']:raise DataError('QLIB_PREPARATION_DISPATCH_CHANGED')
        return registration
    def dispatch_pending(self,*,limit=10):
        """Trusted worker tick recovery; bounded, never run from a GET."""
        if type(limit) is not int or not 1<=limit<=50:raise DataError('INVALID_INPUT')
        with closing(self.db.connect()) as con:
            cursor=self._dispatch_cursor
            condition=' AND (j.created_at>? OR (j.created_at=? AND j.job_id>?))' if cursor else ''
            args=[PREPARE,*( [cursor[0],cursor[0],cursor[1]] if cursor else []),limit]
            rows=con.execute("""SELECT j.job_id,j.created_at FROM jobs j
                LEFT JOIN data_qlib_prepare_dispatches d ON d.prepare_job_id=j.job_id
                WHERE j.handler=? AND j.status='SUCCEEDED' AND d.prepare_job_id IS NULL
                """+condition+' ORDER BY j.created_at,j.job_id LIMIT ?',args).fetchall()
        # A blocked old row cannot permanently starve later successes. Cursor
        # is scan progress only; restart/replay uses the stable core job key.
        self._dispatch_cursor=(rows[-1]['created_at'],rows[-1]['job_id']) if rows else None
        results=[]
        for row in rows:
            try:results.append(self.after_core_finish(row['job_id']))
            except Exception:results.append({'prepare_job_id':row['job_id'],'state':'REGISTRATION_DISPATCH_BLOCKED'})
        return results
    def status(self,principal,job_id):
        self._principal(principal);job=self.runtime.jobs.get(principal,job_id)
        if job['owner_id']!=principal.user_id or job['handler']!=PREPARE:raise DataError('RESOURCE_NOT_FOUND')
        with closing(self.db.connect()) as con:
            row=con.execute('SELECT registration_job_id FROM data_qlib_prepare_dispatches WHERE prepare_job_id=? AND owner_id=?',(job_id,principal.user_id)).fetchone()
        registration=self.runtime.jobs.get(principal,row['registration_job_id']) if row else None
        reference=None
        if registration and registration['status']=='SUCCEEDED':
            value=registration.get('result')
            if type(value) is dict and set(value)=={'export_reference'}:
                self.authorizer.authorize(principal,value['export_reference'],purpose='development_readonly')
                reference=value['export_reference']
        return {'profile_id':PROFILE_ID,'scope':'SYNTHETIC','preparation_job':job,
            'registration_job':registration,'export_reference':reference,'state':'DATA_READY' if reference else
            ('PREPARATION_FAILED' if job['status'] in {'FAILED','CANCELLED'} else
             ('REGISTRATION_FAILED' if registration and registration['status'] in {'FAILED','CANCELLED'} else 'PENDING'))}

def register_preparation_handlers(registry,service):
    registry.register(PREPARE,service.prepare_handler,validate=validate_prepare_parameters,recoverable=False,auto_recovery_limit=0)
    # Existing r06 register handler is authoritative; compose once, not twice.
    if REGISTER not in registry._handlers:
        registry.register(REGISTER,service.authorizer.register_handler,validate=validate_register_parameters,recoverable=False,auto_recovery_limit=0)
    elif registry.get(REGISTER).handler!=service.authorizer.register_handler:
        raise DataError('QLIB_REGISTER_HANDLER_COMPOSITION_CHANGED')

def install_qlib_input_preparation(runtime,*,deployment=None):
    """Trusted data composition only, before queue/resolver transactions.

    Reuse the exact already-constructed r06 authorizer/bridge from data.jobs.
    FA010 supplies reviewed project deployment only when actually released.
    No default report fixture runner, process, data preparation or new grants.
    """
    adapter=getattr(runtime,'qlib_export_authorizer',None)
    if type(adapter) is not QlibExportAuthorizer:raise DataError('QLIB_REVIEWED_CATALOG_COMPOSITION_REQUIRED')
    runner=QlibOfficialDumpRunner(runtime,deployment)
    service=QlibPreparationService(runtime,adapter.bridge,adapter,runner)
    register_preparation_handlers(runtime.handlers,service)
    runtime.qlib_input_preparation=service
    return service
