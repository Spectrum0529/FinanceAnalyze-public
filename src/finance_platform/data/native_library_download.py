"""Thin library bridge to existing guarded HistoryGateway/SDKRunner only."""
from datetime import datetime,timezone
from .contracts import DataError
from .simple_chunks import DatasetChunks,BATCH_ROWS,NATIVE_FORMAT,MAX_ROWS,MAX_CHUNKS
from .provider_sdk.history_contracts import YAHOO_PRICE_POLICY,NATIVE_PRICE_POLICY,QUANTITY_BASIS
from finance_platform.core.contracts import fingerprint

class _DeadlineContext:
    def __init__(self,original):
        import time
        self.original=original;self.deadline=time.monotonic()+1800
    def __getattr__(self,name):return getattr(self.original,name)
    def require_active(self):
        import time
        self.original.require_active()
        if time.monotonic()>=self.deadline:raise DataError('NATIVE_DOWNLOAD_TIME_BUDGET')
    def assert_fenced(self,connection):
        self.require_active();self.original.assert_fenced(connection)
    def checkpoint(self,*args,**kwargs):
        self.require_active();return self.original.checkpoint(*args,**kwargs)

def provider_capabilities():
    return {'items':[
        {'provider':p,'credentials_required':False,'currency':'USD','assets':['US_EQUITY'] if p=='akshare' else ['US_EQUITY','US_ETF'],
         'price_policy':NATIVE_PRICE_POLICY if p=='akshare' else YAHOO_PRICE_POLICY,'raw_compatible':False,
         'quantity_basis':QUANTITY_BASIS,'volume_unit':'provider_reported_unverified','max_symbols':32,
         'backtest_mode':'ADJUSTED_PRICE_SIMULATION','portfolio_adapter_required':True,
         'qlib_raw_compatible':False,'real_provider_status':'NOT_CERTIFIED_BY_CATALOG',
         'resume':{'unit':'COMPLETE_SYMBOL','attempts_maximum':8,'sdk_units_maximum':288,'seconds_per_attempt':1800,'budget_basis':'SDK_RESERVATIONS_NOT_HTTP_OR_BILLING'},
         'maximum_calendar_days':11346,'earliest_start':'1900-01-02','end_policy':'BEFORE_CURRENT_UTC_DATE',
         'metadata':{'currency':'USD','timezone':'America/New_York','price_unit':'per_adjusted_simulation_unit','volume_unit':'provider_reported_unverified','adjustment':'qfq' if p=='akshare' else 'split_adjusted'},
         'unsupported_symbols':['AI','CIEN'] if p=='akshare' else []} for p in ('yfinance','akshare')]+[
        {'provider':'massive','credentials_required':True,'status':'REGISTERED_USER_CONFIGURATION_REQUIRED_REAL_CALLS_UNVERIFIED',
         'max_symbols':1000,'maximum_symbol_year_units':2000,'assets':['US_EQUITY','US_ETF'],'currency':'USD',
         'adjustments':['raw','split_adjusted'],'price_unit':'per_share','volume_unit':'shares','timezone':'America/New_York',
         'resume':{'unit':'COMPLETE_SYMBOL_CALENDAR_YEAR','requests_maximum':4096,'seconds_per_attempt':1800,'budget_basis':'PERSISTED_PHYSICAL_REQUEST_ATTEMPTS'},
         'rate_policy':{'requests':5,'window_seconds':60,'mode':'GLOBAL_LOCAL_CONSERVATIVE_NOT_PLAN_DETECTION'},
         'membership':['ETF_GLOBAL_SEPARATE_ENTITLEMENT','SIC_CANDIDATE_SUBSET','BOUNDED_EXCHANGE_TYPE_SELECTION'],
         'history_coverage':'ACCOUNT_ENDPOINT_RANGE_DEPENDENT_UNVERIFIED'},
        {'provider':'csv','credentials_required':False,'status':'LOCAL_IMPORT'}]}

def gateway_for_runtime(runtime):
    from .provider_sdk.bootstrap import runtime_components
    from .history_gateway import HistoryGateway
    service,runner=runtime_components(runtime)
    return HistoryGateway(service,runner)

def acquisition_security(view):
    return {
        'ticker':view['ticker'],
        'history_ref':{'history_id':view['history_id'],'version':view['version'],
                       'content_hash':view['history_content_hash']},
        'view_ref':{'content_hash':view['content_hash'],'requested_range':view['requested_range'],
                    'selected_row_indices':view['history_cache']['selected_row_indices'],
                    'source_rows_hash':fingerprint(view['rows'])},
        **{k:view.get(k) for k in ('observation_ref','source_ref','identity_evidence_ref','observed_received_at',
                                  'actual_coverage','corporate_action_basis','price_transform','data_quality')},
    }

def download_native(runtime,library,context,parameters,provider,*,gateway_factory=None,journal=None,family=None,
                    before_publish=None,publication_guard=None):
    context=_DeadlineContext(context)
    from .provider_features import validate_native_request
    validate_native_request(parameters,provider)
    if provider not in {'yfinance','akshare'}:raise DataError('PROVIDER_UNSUPPORTED')
    if 'membership' in parameters:raise DataError('NATIVE_MEMBERSHIP_UNSUPPORTED')
    library.validate_selection(context.run.owner_id,parameters['universe'])
    symbols=parameters['universe']['tickers']
    if not 1<=len(symbols)<=32:raise DataError('NATIVE_SYMBOL_LIMIT')
    expected=NATIVE_PRICE_POLICY if provider=='akshare' else YAHOO_PRICE_POLICY
    meta={'currency':'USD','timezone':'America/New_York','price_unit':'per_adjusted_simulation_unit',
          'volume_unit':'provider_reported_unverified','adjustment':'qfq' if provider=='akshare' else 'split_adjusted'}
    if parameters['metadata'] is not None and parameters['metadata']!=meta:raise DataError('NATIVE_PRICE_BASIS_MISMATCH')
    # Reject known unsupported symbols before constructing/starting the runner.
    from .provider_sdk.history_contracts import sina_symbol
    if provider=='akshare':
        if any(t in {'AI','CIEN'} for t in symbols):raise DataError('SINA_KNOWN_FACTOR_QUALITY_UNSUPPORTED')
        if any(not sina_symbol(t) for t in symbols):raise DataError('SINA_SYMBOL_MAPPING_UNVERIFIED')
    context.require_active();gateway=None
    chunks=DatasetChunks(library.policy,context.run.owner_id);descriptors=[];coverage=[];evidence=[];identities=[];total=0
    for index,ticker in enumerate(symbols):
        context.require_active()
        previous=journal.unit(context.run.owner_id,family,ticker) if journal else None
        if previous is not None:
            from .resume_chunks import promote_unit
            unit,scope=previous
            if unit['evidence']['ticker']!=ticker or unit['identity']['ticker']!=ticker or unit['manifest']['securities'][0]['ticker']!=ticker:
                raise DataError('NATIVE_HISTORY_BINDING_MISMATCH')
            promoted=promote_unit(library,context,unit['manifest'],scope,start=parameters['start'],end=parameters['end'],provider=provider)
            from itertools import zip_longest
            source_record={'start':parameters['start'],'end':parameters['end'],'universe':{'tickers':[ticker]},
                'row_storage':{'acquisition':{'provider':provider,'securities':[unit['evidence']],'price_policy':expected,'quantity_basis':QUANTITY_BASIS}}}
            binding=library.market.storage.lookup_binding(context.run.owner_id,'job:'+context.job_id)
            actual_rows=DatasetChunks(library.policy,context.run.owner_id,binding=binding).stream(promoted,start=parameters['start'],end=parameters['end'])
            for actual,original in zip_longest(actual_rows,native_expected_rows(library,context.run.owner_id,source_record,context=context)):
                if actual!=original:raise DataError('NATIVE_DATASET_ROW_BINDING_MISMATCH')
            total+=promoted['row_count'];descriptors.extend(promoted['chunks']);coverage.extend(promoted['securities'])
            evidence.append(unit['evidence']);identities.append(unit['identity'])
            if total>MAX_ROWS or len(descriptors)>MAX_CHUNKS:raise DataError('IMPORT_SERVER_ROWS_LIMIT')
            context.checkpoint(min(90,int(90*(index+1)/len(symbols))),{'resume_id':family,'completed_symbols':index+1,'requested_symbols':len(symbols),'rows':total})
            continue
        if journal:journal.reserve(family,ticker,context)
        if gateway is None:gateway=(gateway_factory or gateway_for_runtime)(runtime)
        first_descriptor=len(descriptors)
        # Exact verified chart identity is resolved by the existing service;
        # restrict currency/asset before any provider price acquisition.
        import time
        security=gateway.resolve_security(context,ticker,time.monotonic()+240)
        instrument=security['instrument']
        if instrument.get('currency')!='USD' or instrument.get('asset_class') not in {'US_EQUITY','US_ETF'}:raise DataError('SECURITY_MARKET_OR_IDENTITY_UNSUPPORTED')
        if provider=='akshare' and instrument['asset_class']!='US_EQUITY':raise DataError('AKSHARE_ETF_UNSUPPORTED')
        view=gateway.obtain(context,{'ticker':ticker,'provider':provider,'start_date':parameters['start'],'end_date':parameters['end'],'history_policy':'refresh'})
        if view['provider']!=provider or view['ticker']!=ticker or view['price_policy']!=expected or view['quantity_basis']!=QUANTITY_BASIS or view['raw_compatible'] is not False:raise DataError('NATIVE_HISTORY_BINDING_MISMATCH')
        if view['identity_evidence_ref']!={k:security[k] for k in ('security_id','content_hash')}:raise DataError('NATIVE_IDENTITY_BINDING_MISMATCH')
        rows=view['rows']
        if not rows:raise DataError('IMPORT_PARTIAL_SYMBOL_FAILURE')
        if len(rows)>9000:raise DataError('IMPORT_ROWS_LIMIT')
        before=None;buffer=[]
        for row in rows:
            if total%128==0:context.require_active()
            if (row.get('currency')!='USD' or row.get('raw_compatible') is not False or row.get('provider_symbol')!=ticker or
                row.get('existing_instrument_id')!=instrument['instrument_id']):raise DataError('NATIVE_ROW_BINDING_MISMATCH')
            day=row['session_date']
            if before is not None and day<=before:raise DataError('IMPORT_DUPLICATE_OR_UNSORTED_BAR')
            before=day;total+=1
            if total>MAX_ROWS:raise DataError('IMPORT_SERVER_ROWS_LIMIT')
            buffer.append({'ticker':ticker,'session_date':day,**{k:row[k] for k in ('open','high','low','close','volume')}})
            if len(buffer)==BATCH_ROWS:
                descriptors.append(chunks.put(buffer,start=parameters['start'],end=parameters['end'],scope='public',context=context,provider=provider));buffer=[]
        if buffer:descriptors.append(chunks.put(buffer,start=parameters['start'],end=parameters['end'],scope='public',context=context,provider=provider))
        if len(descriptors)>MAX_CHUNKS:raise DataError('IMPORT_SERVER_CHUNKS_LIMIT')
        coverage.append({'ticker':ticker,'rows':len(rows),'first':rows[0]['session_date'],'last':rows[-1]['session_date']})
        evidence.append({**acquisition_security(view),'pin_run_id':context.run.run_id})
        identities.append({'ticker':ticker,'instrument':instrument,'identity_reference':view['identity_evidence_ref'],
            'historical_symbol_mapping':'UNVERIFIED','source':'guarded HistoryGateway chart identity'})
        if journal:
            journal.save(family,ticker,{'manifest':{'format':NATIVE_FORMAT,'chunks':descriptors[first_descriptor:],
                'row_count':len(rows),'securities':[coverage[-1]]},'evidence':evidence[-1],'identity':identities[-1]},context)
        context.checkpoint(min(90,int(90*(index+1)/len(symbols))),{'resume_id':family,'completed_symbols':index+1,'requested_symbols':len(symbols),'rows':total})
    manifest={'format':NATIVE_FORMAT,'chunks':descriptors,'row_count':total,'securities':coverage,'status':'COMPLETE_GUARDED_SYMBOL_REQUESTS',
        'completeness':'UNVERIFIED_PROVIDER_RESPONSE','missing_sessions':'NOT_INFERRED',
        'acquisition':{'provider':provider,'price_policy':expected,'quantity_basis':QUANTITY_BASIS,'raw_compatible':False,'securities':evidence}}
    body={**parameters,'metadata':meta,'rows':None,'source':{'provider':provider,'reference':'guarded HistoryGateway.obtain/refresh','universe_as_of':None},
        'received_at':datetime.now(timezone.utc).isoformat(),'scope':'public'}
    if before_publish is not None:
        before_publish(body, manifest, identities)
    return library.import_dataset(context.run.owner_id,body,'job:'+context.run.run_id,context=context,
        security_metadata=identities,manifest=manifest,publication_guard=publication_guard)

def read_native_accepted(library,owner,reference,run_id,*,start=None,end=None):
    from datetime import date,timedelta
    from .provider_sdk.service import ProviderDataService
    from .history_gateway import HistoryGateway
    from .simple_inputs import validate_rows
    record=library.read_accepted(owner,reference,run_id)
    if record.get('market_provenance',{}).get('native_references'):
        from .market_lineage import read_derived
        return read_derived(library,owner,reference,run_id,record,start=start,end=end)
    evidence=record.get('row_storage',{}).get('acquisition')
    if type(evidence)is not dict or evidence.get('provider')not in {'akshare','yfinance'}:raise DataError('NATIVE_DATASET_REQUIRED')
    data=library.read_for_backtest(owner,reference,run_id,start=start,end=end)
    gateway=HistoryGateway(ProviderDataService(library.db,library.policy),None)
    views={}
    for item in evidence['securities']:
        ref=item['history_ref'];ticker=item['ticker'];frozen=item['view_ref'];window=frozen['requested_range']
        original=gateway.read_pinned(owner,ref['history_id'],ref['version'],item['pin_run_id'],kind='run',start=window['start'],end=window['end'])
        if (original['history_content_hash']!=ref['content_hash'] or original['content_hash']!=frozen['content_hash'] or
            original['history_cache']['selected_row_indices']!=frozen['selected_row_indices'] or fingerprint(original['rows'])!=frozen['source_rows_hash'] or
            original['observation_ref']!=item['observation_ref'] or original['source_ref']!=item['source_ref'] or original['identity_evidence_ref']!=item['identity_evidence_ref'] or
            original['provider']!=evidence['provider'] or original['ticker']!=ticker or original['price_policy']!=evidence['price_policy'] or
            original['quantity_basis']!=evidence['quantity_basis'] or original['raw_compatible']is not False):raise DataError('NATIVE_HISTORY_BINDING_MISMATCH')
        projected=[{'ticker':ticker,'session_date':r['session_date'],**{k:r[k] for k in ('open','high','low','close','volume')}} for r in original['rows']]
        fixed=validate_rows(projected,record['start'],record['end'],[ticker],allow_missing_volume=True)
        if fixed!=[r for r in record['rows'] if r['ticker']==ticker]:raise DataError('NATIVE_DATASET_ROW_BINDING_MISMATCH')
        stop=(date.fromisoformat(data['window']['end'])+timedelta(days=1)).isoformat()
        views[ticker]=gateway.read_pinned(owner,ref['history_id'],ref['version'],item['pin_run_id'],kind='run',start=data['window']['start'],end=stop)
    if sorted(views)!=record['universe']['tickers']:raise DataError('NATIVE_HISTORY_BINDING_MISMATCH')
    return {**data,'history_views':views,'quantity_basis':evidence['quantity_basis'],'raw_compatible':False,
        'price_policy':evidence['price_policy'],'backtest_mode':'ADJUSTED_PRICE_SIMULATION'}


def native_expected_rows(library,owner,record,*,context):
    """Verify exact original accepted history, yielding one bounded symbol at a time.

    This does not instantiate a network runner. Receipt/source/identity pins are
    compared before the original projection is compared with immutable chunks.
    """
    from .simple_inputs import validate_rows
    for ticker,view in native_source_views(library,owner,record,active=context.require_active):
        projected=[{'ticker':ticker,'session_date':r['session_date'],**{k:r[k] for k in ('open','high','low','close','volume')}} for r in view['rows']]
        yield from validate_rows(projected,record['start'],record['end'],[ticker],allow_missing_volume=True)


def native_source_views(library,owner,record,*,active):
    from .provider_sdk.service import ProviderDataService
    from .history_gateway import HistoryGateway
    evidence=record.get('row_storage',{}).get('acquisition')
    if type(evidence)is not dict or evidence.get('provider')not in {'akshare','yfinance'}:raise DataError('NATIVE_DATASET_REQUIRED')
    items=evidence['securities']
    if [item['ticker'] for item in items]!=record['universe']['tickers']:raise DataError('NATIVE_HISTORY_BINDING_MISMATCH')
    gateway=HistoryGateway(ProviderDataService(library.db,library.policy),None)
    for item in items:
        active();ref=item['history_ref'];frozen=item['view_ref'];window=frozen['requested_range']
        view=gateway.read_pinned(owner,ref['history_id'],ref['version'],item['pin_run_id'],kind='run',start=window['start'],end=window['end'])
        if (view['history_content_hash']!=ref['content_hash'] or view['content_hash']!=frozen['content_hash'] or
            view['history_cache']['selected_row_indices']!=frozen['selected_row_indices'] or fingerprint(view['rows'])!=frozen['source_rows_hash'] or
            any(view[k]!=item[k] for k in ('observation_ref','source_ref','identity_evidence_ref')) or
            view['provider']!=evidence['provider'] or view['ticker']!=item['ticker'] or view['price_policy']!=evidence['price_policy'] or
            view['quantity_basis']!=evidence['quantity_basis'] or view['raw_compatible']is not False):raise DataError('NATIVE_HISTORY_BINDING_MISMATCH')
        if not 0<len(view['rows'])<=9000:raise DataError('IMPORT_ROWS_LIMIT')
        yield item['ticker'],view
