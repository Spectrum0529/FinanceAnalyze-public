"""Selected official Cboe daily capture -> immutable named dataset, no Main HTTP."""
from datetime import datetime,timezone
from hashlib import sha256
from itertools import zip_longest
from finance_platform.core.contracts import fingerprint
from .contracts import DataError
from .simple_chunks import DatasetChunks,BATCH_ROWS,OBSERVED_FORMAT,MAX_ROWS,MAX_CHUNKS
from .native_library_download import _DeadlineContext
from .openbb_source import META,validate_request,gateway_and_runner
from .provider_sdk.contracts import checked_json
from .provider_sdk.openbb_cboe_runtime import project


def archive(store,context,body,call,received,*,kind='raw',collection_unit=None):
    digest=sha256(body).hexdigest();batch='openbb-'+context.run.run_id+'-'+call['ticker']+'-'+kind+('' if collection_unit is None else '-'+collection_unit)
    parsed=checked_json(body,limit=2*1024**2 if kind=='raw' else 8*1024**2,max_nodes=1000000)
    count=len(parsed['data']) if kind=='raw' else len(parsed['official_models'])
    store.begin_batch(batch,{'request':{'source_id':'openbb.cboe.daily.'+kind,'source_url':'https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/'+call['ticker']+'.json',
        'parameters':{'ticker':call['ticker'],'requested_start':call['start'],'requested_end':call['end'],'sha256':digest},
        'schema_version':'openbb-cboe-'+kind+'-1','data_scope':'public_provider_sample'},
        'license':{'id':'CBOE_TERMS_NOT_A_REDISTRIBUTION_GRANT','status':'UNVERIFIED','usage':'LOCAL_PERSONAL'},
        'expected_pages':1,'expected_response_hash':digest,'expected_records':count,'source_version':None})
    context.require_active();store.put_page(batch,0,body,{'received_at':received,'http_status':200,'response_headers':{},'request_headers':None})
    result=store.finish_batch(batch,record_count=count)
    return {'request_key':result['request_key'],'version':result['version'],'sha256':digest,'bytes':len(body)}


def original_rows(reader,evidence):
    ref=evidence['raw_ref'];raw=reader.store.read_version(ref['request_key'],ref['version'])
    if len(raw)>2*1024**2 or len(raw)!=ref['bytes'] or sha256(raw).hexdigest()!=ref['sha256']:raise DataError('DATASET_HASH_MISMATCH')
    rows=project(checked_json(raw,limit=2*1024**2,max_nodes=500000),evidence['call'])
    if fingerprint(rows)!=evidence['rows_hash']:raise DataError('OPENBB_TRANSFORM_MISMATCH')
    return rows,raw


def download(runtime,library,journal,family,context,p):
    validate_request(p);context=_DeadlineContext(context);owner=context.run.owner_id
    library.validate_selection(owner,p['universe']);gateway=None;runner=None
    chunks=DatasetChunks(library.policy,owner);descriptors=[];coverage=[];identities=[];receipts=[];total=0
    for index,ticker in enumerate(p['universe']['tickers']):
        context.require_active();saved=journal.unit(owner,family,ticker)
        if saved:
            from .resume_chunks import promote_unit
            unit,scope=saved;reader=DatasetChunks(library.policy,owner,read_scope=scope)
            rows,raw=original_rows(reader,unit['evidence'])
            models,frame=official_rows(reader,unit['evidence'],rows)
            if unit['identity']['ticker']!=ticker or unit['evidence']['call']['ticker']!=ticker or unit['evidence']['call']['start']!=p['start'] or unit['evidence']['call']['end']!=p['end']:raise DataError('OPENBB_RESPONSE_BINDING')
            manifest=promote_unit(library,context,unit['manifest'],scope,start=p['start'],end=p['end'],provider='openbb_cboe')
            binding=library.market.storage.lookup_binding(owner,'job:'+context.job_id)
            current=DatasetChunks(library.policy,owner,binding=binding)
            for actual,expected in zip_longest(current.stream(manifest,start=p['start'],end=p['end']),rows):
                if actual!=expected:raise DataError('OPENBB_TRANSFORM_MISMATCH')
            evidence=dict(unit['evidence']);identity=unit['identity']
            if scope['location_id']!=binding['locations']['market_data']['location_id']:
                evidence['raw_ref']=archive(current.store,context,raw,evidence['call'],evidence['observed_received_at'])
                evidence['official_frame_ref']=archive(current.store,context,frame,evidence['call'],evidence['observed_received_at'],kind='official')
        else:
            journal.reserve(family,ticker,context)
            if gateway is None:gateway,runner=gateway_and_runner(runtime)
            import time
            rows,manifest,evidence,identity=capture_unit(context,owner,p,ticker,gateway,runner,chunks,
                                                         deadline=time.monotonic()+240)
            journal.save(family,ticker,{'manifest':manifest,'evidence':evidence,'identity':identity},context)
        total+=len(rows);descriptors.extend(manifest['chunks']);coverage.extend(manifest['securities']);receipts.append(evidence);identities.append(identity)
        if total>MAX_ROWS or len(descriptors)>MAX_CHUNKS:raise DataError('IMPORT_SERVER_ROWS_LIMIT')
        context.checkpoint(min(95,int(95*(index+1)/len(p['universe']['tickers']))),{'resume_id':family,'completed_symbols':index+1,'requested_symbols':len(p['universe']['tickers']),'rows':total})
    manifest={'format':OBSERVED_FORMAT,'chunks':descriptors,'row_count':total,'securities':coverage,'status':'COMPLETE_REQUESTED_SYMBOL_RESPONSES',
              'completeness':'UNVERIFIED_PROVIDER_RESPONSE','missing_sessions':'NOT_INFERRED',
              'provider_evidence':{'provider':'openbb_cboe','price_policy':'CBOE_PUBLISHED_OBSERVED_ADJUSTMENT_UNVERIFIED','raw_compatible':False,'securities':receipts}}
    body={**p,'metadata':dict(META),'rows':None,'source':{'provider':'openbb_cboe','reference':'official CboeEquityHistoricalFetcher/day + bounded public HTTPS','universe_as_of':None},
          'received_at':datetime.now(timezone.utc).isoformat(),'scope':'public'}
    return library.import_dataset(owner,body,'job:'+context.run.run_id,context=context,security_metadata=identities,manifest=manifest)


def capture_unit(context,owner,p,ticker,gateway,runner,chunks,*,deadline,collection_unit=None):
    security=gateway.resolve_security(context,ticker,deadline);instrument=security['instrument']
    if instrument.get('currency')!='USD' or instrument.get('asset_class')not in {'US_EQUITY','US_ETF'}:raise DataError('SECURITY_MARKET_OR_IDENTITY_UNSUPPORTED')
    call={'provider':'openbb_cboe','ticker':ticker,'provider_symbol':ticker,'start':p['start'],'end':p['end'],
          'profile':'history-daily-2','operation':'collect','asset_class':instrument['asset_class'],'max_rows':9000}
    result,artifacts,execution=runner.run(context,owner,call,whole_deadline=deadline)
    context.require_active();entities=[e for e in result['entities'] if e['role']=='openbb_cboe_daily']
    expected_path='/api/global/delayed_quotes/charts/historical/'+ticker+'.json'
    if len(entities)!=1 or entities[0]['origin']!='cdn.cboe.com' or entities[0]['path']!=expected_path:raise DataError('OPENBB_RESPONSE_BINDING')
    entity=entities[0];raw=artifacts[entity['artifact']];rows=project(checked_json(raw,limit=2*1024**2,max_nodes=500000),call)
    if checked_json(artifacts['observations.json'],limit=32*1024**2,max_nodes=500000)!=rows:raise DataError('OPENBB_TRANSFORM_MISMATCH')
    unit_chunks=[]
    for offset in range(0,len(rows),BATCH_ROWS):unit_chunks.append(chunks.put(rows[offset:offset+BATCH_ROWS],start=p['start'],end=p['end'],scope='public',context=context,provider='openbb_cboe'))
    manifest={'format':OBSERVED_FORMAT,'chunks':unit_chunks,'row_count':len(rows),'securities':[{'ticker':ticker,'rows':len(rows),'first':rows[0]['session_date'],'last':rows[-1]['session_date']}]}
    evidence={'ticker':ticker,'call':call,'sdk_ref':result['sdk_ref'],'raw_ref':archive(chunks.store,context,raw,call,entity['observed_received_at'],collection_unit=collection_unit),
              'official_frame_ref':archive(chunks.store,context,artifacts['sdk-frame.json'],call,entity['observed_received_at'],kind='official',collection_unit=collection_unit),
              'rows_hash':fingerprint(rows),'observed_received_at':entity['observed_received_at'],'published_at':None,'available_at':None,
              'price_policy':'CBOE_PUBLISHED_OBSERVED_ADJUSTMENT_UNVERIFIED','raw_compatible':False,
              'execution':{key:execution[key] for key in ('binding','child_reaped','kernel_membership_proven','kernel_final_empty','network_class')}}
    identity={'ticker':ticker,'instrument':instrument,'identity_reference':{k:security[k] for k in ('security_id','content_hash')},
              'historical_symbol_mapping':'UNVERIFIED','source':'guarded HistoryGateway chart identity; Cboe response exact ticker'}
    return rows,manifest,evidence,identity


def expected_rows(library,owner,record,*,context=None):
    from contextlib import closing
    import json
    evidence=record.get('row_storage',{}).get('provider_evidence')
    if not evidence or evidence.get('provider')!='openbb_cboe' or [e['ticker'] for e in evidence['securities']]!=record['universe']['tickers']:raise DataError('OPENBB_RESPONSE_BINDING')
    with closing(library.db.connect()) as con:stored=con.execute('SELECT body FROM data_library_versions WHERE owner=? AND id=?',(owner,record['dataset_id'])).fetchone()
    reader=DatasetChunks(library.policy,owner,read_scope=library.market.scope(owner,stored[0]))
    for item in evidence['securities']:
        if context:context.require_active()
        if item['call']['provider']!='openbb_cboe' or item['call']['ticker']!=item['ticker'] or item['call']['start']!=record['start'] or item['call']['end']!=record['end']:raise DataError('OPENBB_RESPONSE_BINDING')
        rows,_=original_rows(reader,item);yield from rows


def official_rows(reader,evidence,rows):
    from decimal import Decimal
    import math,json
    ref=evidence['official_frame_ref'];raw=reader.store.read_version(ref['request_key'],ref['version'])
    if len(raw)>8*1024**2 or len(raw)!=ref['bytes'] or sha256(raw).hexdigest()!=ref['sha256']:raise DataError('DATASET_HASH_MISMATCH')
    # Explicit interoperability float view only; authoritative rows remain decimal strings.
    frame=json.loads(raw)
    if frame.get('format')!='openbb-cboe-source-projection-1' or frame.get('rows')!=rows or evidence['sdk_ref']['name']!='openbb-cboe' or evidence['sdk_ref']['version']!='2.0.0':raise DataError('OPENBB_TRANSFORM_MISMATCH')
    models=frame['official_models']
    if len(models)!=len(rows):raise DataError('OPENBB_TRANSFORM_MISMATCH')
    for model,row in zip(models,rows):
        if model.get('date')!=row['session_date']:raise DataError('OPENBB_TRANSFORM_MISMATCH')
        for key in ('open','high','low','close'):
            if type(model.get(key))not in (int,float) or not math.isfinite(model[key]) or not math.isclose(model[key],float(row[key]),rel_tol=1e-12,abs_tol=0):raise DataError('OPENBB_TRANSFORM_MISMATCH')
        volume=model.get('volume')
        if (volume is None)!=(row['volume']is None) or (volume is not None and (type(volume)not in (int,float) or not math.isfinite(volume) or Decimal(str(volume))!=Decimal(row['volume']))):raise DataError('OPENBB_TRANSFORM_MISMATCH')
    return models,raw


def export_official_rows(library,owner,record,*,start,end,context):
    from contextlib import closing
    import json
    if record['source']['provider']!='openbb_cboe':raise DataError('OPENBB_EXPORT_SOURCE_UNSUPPORTED')
    with closing(library.db.connect()) as con:stored=con.execute('SELECT body FROM data_library_versions WHERE owner=? AND id=?',(owner,record['dataset_id'])).fetchone()
    reader=DatasetChunks(library.policy,owner,read_scope=library.market.scope(owner,stored[0]));result=[]
    for item in record['row_storage']['provider_evidence']['securities']:
        context.require_active();rows,_=original_rows(reader,item);models,_=official_rows(reader,item,rows)
        result.extend({'symbol':item['ticker'],**model} for model,row in zip(models,rows) if start<=row['session_date']<=end)
        if len(result)>200000:raise DataError('RESEARCH_INPUT_LIMIT')
    return result
