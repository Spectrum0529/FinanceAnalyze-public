"""Official Cboe Fetcher inside the admitted SDK child, fixed gated transport.

No openbb/obb app import, account/profile discovery, default HTTP, disk cache,
key, arbitrary URL or generated code. Upstream transformation is checked against
original selected source rows; its float conversion never replaces decimals.
"""
import asyncio,hashlib,json,math
from importlib import metadata
from pathlib import Path
from decimal import Decimal
from datetime import date,datetime,timezone
from .network_gate import BoundedSafeHTTP
from .contracts import checked_json,number
from finance_platform.data.contracts import DataError

REFERENCES={
 'https://cdn.cboe.com/api/global/us_indices/definitions/all_indices.json',
 'https://cdn.cboe.com/api/global/au_indices/definitions/all-definitions.json',
 'https://cdn.cboe.com/api/global/us_indices/definitions/GlobalIndices.csv'}

def verify(pins):
    refs=[]
    for name,expected in [('openbb-core','2.0.1'),('openbb-cboe','2.0.0')]:
        try:dist=metadata.distribution(name)
        except metadata.PackageNotFoundError:raise DataError('OPENBB_DEPENDENCY_MISSING') from None
        pin=pins.get(name)
        if dist.version!=expected or type(pin)is not dict or pin.get('version')!=expected or not pin.get('modules'):raise DataError('OPENBB_VERSION_UNSUPPORTED')
        for relative,digest in pin['modules'].items():
            raw=Path(dist.locate_file(relative)).read_bytes()
            if hashlib.sha256(raw.replace(b'\r\n',b'\n')).hexdigest()!=digest:raise DataError('SDK_INTERFACE_CHANGED')
        refs.append({'name':name,'version':expected,'artifact_sha256':pin['artifact_sha256']})
    return refs

def project(body,call):
    if type(body)is not dict or body.get('symbol')!=call['ticker'] or type(body.get('data'))is not list or len(body['data'])>40000:raise DataError('OPENBB_RESPONSE_BINDING')
    rows=[];previous=None
    for item in body['data']:
        if type(item)is not dict:raise DataError('OPENBB_RESPONSE_BINDING')
        day=item.get('date')
        try:
            if date.fromisoformat(day).isoformat()!=day:raise ValueError
        except (TypeError,ValueError):raise DataError('OPENBB_RESPONSE_BINDING') from None
        if previous is not None and day<=previous:raise DataError('IMPORT_DUPLICATE_OR_UNSORTED_BAR')
        previous=day
        if not call['start']<=day<=call['end']:continue
        if len(rows)>=9000:raise DataError('IMPORT_ROWS_LIMIT')
        values={key:number(item.get(key),minimum=Decimal('0')) for key in ('open','high','low','close')}
        if Decimal(values['open'])<=0:raise DataError('IMPORT_ROW_INVALID')
        volume=item.get('volume',item.get('stock_volume'))
        rows.append({'ticker':call['ticker'],'session_date':day,**values,'volume':None if volume is None else number(volume,minimum=Decimal('0'))})
    if not rows:raise DataError('IMPORT_PARTIAL_SYMBOL_FAILURE')
    from finance_platform.data.simple_inputs import validate_rows
    return validate_rows(rows,call['start'],call['end'],[call['ticker']],allow_missing_volume=True)

def collect(call,budget,pins):
    required={'provider','ticker','provider_symbol','start','end','profile','operation','asset_class','max_rows'}
    if set(call)!=required or call['provider']!='openbb_cboe' or call['provider_symbol']!=call['ticker'] or call['asset_class']not in {'US_EQUITY','US_ETF'} or call['operation']!='collect' or call['profile']!='history-daily-2' or call['max_rows']!=9000:raise DataError('SDK_INPUT_INVALID')
    from finance_platform.data.openbb_source import validate_window_symbol
    validate_window_symbol(call['ticker'],call['start'],call['end'])
    refs=verify(pins);budget.check()
    import openbb_core.provider.utils.helpers as core_helpers
    import openbb_cboe.utils.helpers as helpers
    from openbb_cboe.models.equity_historical import CboeEquityHistoricalFetcher as Fetcher
    chart='https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/'+call['ticker']+'.json'
    allowed=REFERENCES|{chart};port=BoundedSafeHTTP(budget)
    port.allowed_hosts=('cdn.cboe.com',);port.path_prefixes=tuple('/'+url.split('/',3)[3] for url in allowed)
    originals=(core_helpers.amake_requests,helpers.amake_request)
    async def one(url,response_callback=None,**kwargs):
        if url not in allowed or kwargs:budget.abort('OPENBB_ENDPOINT_REJECTED')
        response=port.fetch(url,{})
        if response.status in {401,402,403}:budget.abort('OPENBB_SOURCE_ACCESS_REJECTED')
        if response.status==429:budget.abort('OPENBB_SOURCE_RATE_LIMITED')
        if response.status!=200:budget.abort('OPENBB_SOURCE_HTTP_FAILED')
        role='openbb_cboe_daily' if url==chart else 'openbb_cboe_reference'
        budget.attempts[-1]['role']=role
        budget.entities.append({'role':role,'origin':'cdn.cboe.com','path':'/'+url.split('/',3)[3],
            'parameters':{},'observed_received_at':datetime.now(timezone.utc).isoformat(),
            'content_decoding':'IDENTITY_ENTITY_NOT_TLS_WIRE','body':response.body})
        budget.check()
        if url.endswith('.csv'):return response.body.decode('utf-8-sig')
        return checked_json(response.body,limit=2*1024**2,max_nodes=500000)
    async def many(urls,**kwargs):
        if type(urls)is not list or urls!=[chart] or kwargs:budget.abort('OPENBB_ENDPOINT_REJECTED')
        return [await one(url) for url in urls]
    core_helpers.amake_requests=many;helpers.amake_request=one
    try:
        query=Fetcher.transform_query({'symbol':call['ticker'],'start_date':call['start'],'end_date':call['end'],'interval':'1d','use_cache':False})
        raw=asyncio.run(Fetcher.aextract_data(query,credentials=None))
        budget.check()
        if type(raw)is not list or len(raw)!=1:raise DataError('OPENBB_RESPONSE_BINDING')
        rows=project(raw[0],call)
        # Upstream end filter includes end+1: pass an already exact selected crop.
        crop=[dict(item) for item in raw[0]['data'] if call['start']<=item['date']<=call['end']]
        standard=Fetcher.transform_data(query,[{**raw[0],'data':crop}])
        if len(standard)!=len(rows):raise DataError('OPENBB_TRANSFORM_MISMATCH')
        for model,row in zip(standard,rows):
            if model.date.isoformat()!=row['session_date']:raise DataError('OPENBB_TRANSFORM_MISMATCH')
            for key in ('open','high','low','close'):
                value=getattr(model,key)
                if not math.isfinite(value) or not math.isclose(value,float(row[key]),rel_tol=1e-12,abs_tol=0):raise DataError('OPENBB_TRANSFORM_MISMATCH')
            actual=getattr(model,'volume',None)
            if (actual is None)!=(row['volume']is None) or (actual is not None and Decimal(str(actual))!=Decimal(row['volume'])):raise DataError('OPENBB_TRANSFORM_MISMATCH')
        return {'name':'openbb-cboe','version':'2.0.0','dependencies':refs,'operation':'CboeEquityHistoricalFetcher.aextract_data/transform_data'}, {'format':'openbb-cboe-source-projection-1','rows':rows,'official_models':[model.model_dump(mode='json',exclude_none=True) for model in standard]},rows
    finally:core_helpers.amake_requests,helpers.amake_request=originals
