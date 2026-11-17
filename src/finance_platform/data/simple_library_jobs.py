"""Register with existing Core queue; no scheduler or direct provider endpoint."""
from finance_platform.core.pending_writes import require_open_key
from .simple_datasets import DatasetLibrary
from .massive_control import MassiveError as DataError
from .market_lifecycle import read_connection
from predictor.access import AccessPolicy

def install_library(runtime,*,credential_resolver=None,client_factory=None,history_gateway_factory=None,openbb_adapter_enabled=False,openbb_cboe_enabled=False,openbb_runtime_binding=None):
    library=DatasetLibrary(runtime.database,AccessPolicy(runtime.root))
    from .wrds_library import HISTORY_JOB,install_wrds,is_history_request
    install_wrds(runtime,library)
    from .massive_control import MassiveControl,daily_units
    control=MassiveControl(runtime.database)
    runtime.massive_control=control
    from .native_download_journal import NativeDownloadJournal
    native=NativeDownloadJournal(runtime.database,library.market.storage)
    runtime.native_download_journal=native
    if type(openbb_cboe_enabled)is not bool:raise DataError('OPENBB_FEATURE_CONFIGURATION_INVALID')
    runtime.openbb_cboe_enabled=openbb_cboe_enabled
    runtime.openbb_runtime_binding=openbb_runtime_binding
    from .native_library_download import gateway_for_runtime
    from .source_onboarding import install_connection_checks
    install_connection_checks(runtime,library,history_gateway_factory or gateway_for_runtime)
    def connection_admission(owner, ident):
        def guard(con, _job_id):
            if read_connection(con,owner,ident)['archived']:
                raise DataError('MARKET_CONNECTION_ARCHIVED')
        return guard
    def progress(owner,job):
        from contextlib import closing
        import json
        with closing(runtime.database.connect()) as con:
            row=con.execute('SELECT handler,parameters_json,status,progress,error_code,result_json FROM jobs WHERE owner_id=? AND job_id=?',(owner,job)).fetchone()
        if not row:raise DataError('RESOURCE_NOT_FOUND')
        if row[0]==HISTORY_JOB:
            return {'job_id':job,'status':row[2],'progress':row[3],'error_code':row[4],
                    'result':json.loads(row[5]) if row[5] else None,'resume_id':None}
        if row[0]!='data.library-download':raise DataError('RESOURCE_NOT_FOUND')
        connection=library.connection(owner,json.loads(row[1])['connection_id'])
        return native.status(owner,job) if connection['provider'] in {'yfinance','akshare','openbb_cboe'} else control.status(owner,job)
    runtime.dataset_download_progress=progress
    def validate(parameters):
        if type(parameters)is not dict or set(parameters)!={'dataset'} or type(parameters['dataset'])is not dict:raise DataError('IMPORT_SCHEMA')
    def handler(context,parameters):
        validate(parameters);context.require_active()
        dataset=library.market.unpack(context.run.owner_id,parameters['dataset'])
        return library.import_dataset(context.run.owner_id,dataset,'job:'+context.run.run_id,context=context)
    runtime.handlers.register('data.library-import',handler,validate=validate,recoverable=False,auto_recovery_limit=0)
    def submit(principal,body):
        runtime.require_owner(principal,principal.user_id,write=True)
        if type(body)is not dict or set(body)!={'dataset','idempotency_key'}:raise DataError('IMPORT_SCHEMA')
        from contextlib import closing
        import json
        from finance_platform.core.contracts import identifier,fingerprint
        identifier(body['idempotency_key'])
        with closing(runtime.database.connect()) as con:
            require_open_key(con, principal.user_id, 'jobs', body['idempotency_key'])
            old=con.execute('SELECT j.job_id,j.handler,j.parameters_json FROM job_keys k JOIN jobs j ON j.job_id=k.job_id WHERE k.owner_id=? AND k.idempotency_key=?',(principal.user_id,body['idempotency_key'])).fetchone()
        if old:
            parameters=json.loads(old[2]);prior=parameters.get('dataset')
            if type(prior)is dict and prior.get('storage_envelope')=='market-body-1':same=prior['payload']['sha256']==fingerprint(body['dataset'])
            else:same=prior==body['dataset']
            if old[1]!='data.library-import' or not same:raise DataError('IDEMPOTENCY_CONFLICT')
            return runtime.jobs.get(principal,old[0])
        with runtime.database.transaction() as con:
            require_open_key(con, principal.user_id, 'jobs', body['idempotency_key'])
            binding=library.market.storage.bind(principal.user_id,'import-input:'+body['idempotency_key'],connection=con)
            envelope=library.market.pack(principal.user_id,body['dataset'],binding,connection=con)
        return runtime.jobs.submit(principal,'data.library-import',{'dataset':envelope},idempotency_key=body['idempotency_key'],storage_binding=binding)
    runtime.dataset_import_submit=submit
    def make_client(context,connection):
        from .massive_daily import create_client
        if client_factory:return client_factory(connection)
        if not callable(credential_resolver):raise DataError('CREDENTIAL_RESOLVER_UNCONFIGURED')
        return create_client(connection['credential_ref'],lambda ref:credential_resolver(
            context.run.owner_id,ref,provider='massive',endpoint_id=None))
    def close_client(client):
        close=getattr(client,'close',None)
        if callable(close):close()
        else:
            clear=getattr(getattr(client,'client',None),'clear',None)
            if callable(clear):clear()
    runtime.goal_massive_client = (make_client, close_client)
    def download(context,parameters):
        from .massive_daily import etf_members,classify_candidates
        from .massive_workflow import download_daily
        parameters=dict(parameters);resume=parameters.pop('resume_id',None)
        connection=library.connection(context.run.owner_id,parameters['connection_id'])
        if connection['provider']=='openbb_cboe':
            from .openbb_source import require_enabled,validate_request
            from .openbb_library_download import download as download_openbb
            require_enabled(enabled=openbb_cboe_enabled,runtime_binding=openbb_runtime_binding);validate_request(parameters)
            family=native.begin(context.run.owner_id,parameters,context,resume_id=resume)
            context.checkpoint(0,{'resume_id':family})
            return download_openbb(runtime,library,native,family,context,parameters)
        if connection['provider'] in {'yfinance','akshare'}:
            from .native_library_download import download_native
            family=native.begin(context.run.owner_id,parameters,context,resume_id=resume)
            context.checkpoint(0,{'resume_id':family})
            return download_native(runtime,library,context,parameters,connection['provider'],gateway_factory=history_gateway_factory,journal=native,family=family)
        if connection['provider']!='massive':raise DataError('MASSIVE_CONNECTION_REQUIRED')
        if 'membership' not in parameters:
            library.validate_selection(context.run.owner_id,parameters['universe']);daily_units(parameters)
        elif resume is not None:raise DataError('MASSIVE_RESUME_MISMATCH')
        family=control.begin(context.run.owner_id,parameters,context,resume_id=resume)
        context.checkpoint(0,{'resume_id':family})
        client=make_client(context,connection)
        try:
            control.bind(client,family,context)
            if 'membership' in parameters:
                spec=parameters['membership']
                if set(spec)=={'etf','effective_date'}:group=etf_members(client,spec['etf'],spec['effective_date'],context=context)
                elif set(spec)=={'candidates','as_of','sic_prefixes'}:group=classify_candidates(client,spec['candidates'],spec['as_of'],sic_prefixes=spec['sic_prefixes'],context=context)
                elif set(spec)=={'exchange','security_type','as_of','maximum','active'}:
                    from .dated_universes import reference_selection
                    group=reference_selection(client,spec,context=context)
                else:raise DataError('UNIVERSE_INVALID')
                return library.save_universe(context.run.owner_id,group,'job:'+context.run.run_id,context=context)
            return download_daily(runtime,library,control,family,client,context,parameters)
        finally:close_client(client)
    def validate_check(p):
        from datetime import date
        from .simple_inputs import tickers
        if type(p)is not dict or set(p)!={'connection_id','ticker','start','end','adjustment'}:raise DataError('IMPORT_SCHEMA')
        if tickers(p['ticker'])!=[p['ticker']] or p['adjustment'] not in {'raw','split_adjusted'}:raise DataError('MASSIVE_OPTIONS_INVALID')
        try:
            low,high=date.fromisoformat(p['start']),date.fromisoformat(p['end'])
            if low.isoformat()!=p['start'] or high.isoformat()!=p['end'] or not 0<=(high-low).days<=6:raise ValueError
        except (TypeError,ValueError):raise DataError('MASSIVE_PROBE_RANGE') from None
    def check(context,parameters):
        from .massive_workflow import probe
        connection=library.connection(context.run.owner_id,parameters['connection_id'])
        if connection['provider']!='massive':raise DataError('MASSIVE_CONNECTION_REQUIRED')
        family=control.begin(context.run.owner_id,parameters,context,maximum=16)
        client=make_client(context,connection)
        try:
            control.bind(client,family,context,wall_seconds=180)
            return probe(client,context,parameters)
        finally:close_client(client)
    runtime.handlers.register('data.massive-check',check,validate=validate_check,recoverable=False,auto_recovery_limit=0)
    def submit_check(principal,body):
        runtime.require_owner(principal,principal.user_id,write=True)
        if type(body)is not dict or set(body)!={'request','idempotency_key'}:raise DataError('IMPORT_SCHEMA')
        validate_check(body['request'])
        ident=body['request']['connection_id']
        connection=library.connection(principal.user_id,ident)
        if connection['provider']!='massive':raise DataError('MASSIVE_CONNECTION_REQUIRED')
        return runtime.jobs.submit(principal,'data.massive-check',body['request'],idempotency_key=body['idempotency_key'],
                                   goal_link=connection_admission(principal.user_id,ident))
    runtime.massive_check_submit=submit_check
    def validate_download(p):
        p=dict(p) if type(p)is dict else p
        if type(p)is dict and 'resume_id' in p:
            from finance_platform.core.contracts import identifier
            identifier(p.pop('resume_id'))
        base={'connection_id','name','universe','start','end','metadata','kind','benchmark'}
        if type(p)is not dict or set(p) not in (base,{'connection_id','membership'}):raise DataError('IMPORT_SCHEMA')
        if 'metadata' in p:
            from .simple_inputs import metadata,tickers
            from datetime import date
            if p['metadata'] is not None:metadata(p['metadata'])
            if p['metadata'] is not None and p['metadata']['timezone']!='America/New_York':raise DataError('IMPORT_PROVIDER_TIMEZONE_MISMATCH')
            if tickers(' '.join(p['universe']['tickers']))!=p['universe']['tickers'] or not p['universe']['tickers']:raise DataError('UNIVERSE_INVALID')
            try:
                if date.fromisoformat(p['start']).isoformat()!=p['start'] or date.fromisoformat(p['end']).isoformat()!=p['end'] or p['start']>p['end']:raise ValueError
            except (ValueError,TypeError):raise DataError('IMPORT_DATES_INVALID') from None
            if p['kind'] not in {'dataset','benchmark'}:raise DataError('IMPORT_SCOPE')
            if p['kind']=='benchmark':
                b=p['benchmark']
                if type(b)is not dict or set(b)!={'instrument_label','return_basis','dividend_policy'} or b['return_basis']!='price' or len(p['universe']['tickers'])!=1:raise DataError('BENCHMARK_SCHEMA')
            elif p['benchmark'] is not None:raise DataError('BENCHMARK_SCHEMA')
        else:
            from datetime import date
            from .simple_inputs import tickers
            s=p['membership']
            if type(s)is not dict:raise DataError('UNIVERSE_INVALID')
            if set(s)=={'etf','effective_date'}:
                if tickers(s['etf'])!=[s['etf']]:raise DataError('UNIVERSE_INVALID')
                day=s['effective_date']
            elif set(s)=={'candidates','as_of','sic_prefixes'}:
                import re
                if tickers(' '.join(s['candidates']))!=s['candidates'] or not s['candidates'] or type(s['sic_prefixes'])is not list or not s['sic_prefixes'] or any(type(x)is not str or re.fullmatch(r'[0-9]{2,4}',x)is None for x in s['sic_prefixes']):raise DataError('UNIVERSE_INVALID')
                day=s['as_of']
            elif set(s)=={'exchange','security_type','as_of','maximum','active'}:
                from .dated_universes import validate_reference_selection
                validate_reference_selection(s);day=s['as_of']
            else:raise DataError('UNIVERSE_INVALID')
            try:
                if date.fromisoformat(day).isoformat()!=day:raise ValueError
            except (ValueError,TypeError):raise DataError('IMPORT_DATES_INVALID') from None
    runtime.handlers.register('data.library-download',download,validate=validate_download,recoverable=False,auto_recovery_limit=0)
    def submit_download(principal,body):
        runtime.require_owner(principal,principal.user_id,write=True)
        if type(body)is not dict or set(body)!={'request','idempotency_key'}:raise DataError('IMPORT_SCHEMA')
        if is_history_request(body['request']):
            if not callable(getattr(runtime,'wrds_history_submit',None)):raise DataError('WRDS_UNAVAILABLE')
            library.active_connection(principal.user_id,body['request']['connection_id'])
            return runtime.wrds_history_submit(principal,body)
        validate_download(body['request']);ident=body['request']['connection_id']
        connection=library.connection(principal.user_id,ident)
        if connection['provider']=='wrds':raise DataError('WRDS_INPUT_INVALID')
        if 'resume_id' in body['request'] and (connection['provider'] not in {'massive','yfinance','akshare','openbb_cboe'} or 'membership' in body['request']):raise DataError('DOWNLOAD_RESUME_UNSUPPORTED')
        if connection['provider']=='openbb_cboe':
            from .openbb_source import require_enabled,validate_request
            require_enabled(enabled=openbb_cboe_enabled,runtime_binding=openbb_runtime_binding);validate_request(body['request'])
        if connection['provider']=='csv':raise DataError('PROVIDER_DOWNLOAD_UNSUPPORTED')
        if connection['provider'] in {'akshare_cn','tushare','openbb_nasdaq'}:
            library.active_connection(principal.user_id,ident)
            submit_goal_source=getattr(runtime,'goal_provider_download',None)
            if not callable(submit_goal_source):raise DataError('GOAL_DATA_RUNTIME_UNAVAILABLE')
            if 'membership' in body['request'] or 'resume_id' in body['request']:
                raise DataError('GOAL_DATA_REQUIREMENT_UNSUPPORTED')
            return submit_goal_source(principal,body['request'],body['idempotency_key'],connection['provider'])
        if connection['provider'] in {'yfinance','akshare'}:
            from .provider_features import validate_native_request
            validate_native_request(body['request'],connection['provider'])
        if connection['provider']=='massive' and 'membership' not in body['request']:
            daily_units(body['request'])
            m=body['request']['metadata']
            if m is not None and (m['adjustment'] not in {'raw','split_adjusted'} or m['price_unit']!='per_share' or m['volume_unit']!='shares'):raise DataError('IMPORT_UNITS_AMBIGUOUS')
        if 'membership' not in body['request']:library.validate_selection(principal.user_id,body['request']['universe'])
        # The existing queue transaction hook runs for fresh jobs, after any
        # idempotent replay is resolved. An archive concurrent with admission
        # therefore orders before or after the new job as one write.
        return runtime.jobs.submit(principal,'data.library-download',body['request'],idempotency_key=body['idempotency_key'],
                                   goal_link=connection_admission(principal.user_id,ident))
    runtime.dataset_download_submit=submit_download
    def validate_roster(p):
        from .dated_universes import imported_roster
        if type(p)is not dict or set(p)!={'roster'}:raise DataError('UNIVERSE_INVALID')
        imported_roster(p['roster'])
    def save_roster(context,p):
        from .dated_universes import imported_roster
        context.require_active()
        return library.save_universe(context.run.owner_id,imported_roster(p['roster']),'job:'+context.run.run_id,context=context)
    runtime.handlers.register('data.universe-import',save_roster,validate=validate_roster,recoverable=False,auto_recovery_limit=0)
    def submit_roster(principal,body):
        runtime.require_owner(principal,principal.user_id,write=True)
        if type(body)is not dict or set(body)!={'roster','idempotency_key'}:raise DataError('UNIVERSE_INVALID')
        p={'roster':body['roster']};validate_roster(p)
        return runtime.jobs.submit(principal,'data.universe-import',p,idempotency_key=body['idempotency_key'])
    runtime.dataset_universe_submit=submit_roster
    from .research_export_service import ResearchExportService
    exports=ResearchExportService(library,openbb_enabled=openbb_adapter_enabled)
    runtime.dataset_research_exports=exports
    runtime.handlers.register('data.research-export',exports.run,validate=exports.validate,recoverable=False,auto_recovery_limit=0)
    def submit_export(principal,body):
        runtime.require_owner(principal,principal.user_id,write=True)
        if type(body)is not dict or set(body)!={'request','idempotency_key'}:raise DataError('RESEARCH_EXPORT_SCHEMA')
        exports.validate(body['request'])
        record=library.get(principal.user_id,body['request']['reference']['dataset_id'],selectable=True)
        from .openbb_adapter import FORMAT as OPENBB_FORMAT
        if body['request']['format']==OPENBB_FORMAT and record['source']['provider']!='openbb_cboe':raise DataError('OPENBB_EXPORT_SOURCE_UNSUPPORTED')
        return runtime.jobs.submit(principal,'data.research-export',body['request'],idempotency_key=body['idempotency_key'])
    runtime.dataset_research_export_submit=submit_export
    runtime.dataset_research_export_content=exports.content
    runtime.dataset_research_export_page=exports.page
    return library
