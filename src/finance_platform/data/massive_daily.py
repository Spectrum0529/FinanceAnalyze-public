"""Thin official RESTClient.list_aggs adapter. Caller owns credentials/lifecycle.

No client construction, arbitrary URLs, secrets, HTTP reimplementation or retries
here. Official pagination is consumed as an iterator, with bounded output and
current Core context checks. Fixed-client tests do not establish market access.
"""
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from .contracts import DataError
from .simple_inputs import validate_rows

def create_client(credential_ref,resolve_credential):
    """Real official SDK factory; secret remains process-local, never persisted."""
    import re
    from massive import RESTClient
    if type(credential_ref)is not str or re.fullmatch(r'credential:[A-Za-z0-9_.-]{1,100}',credential_ref)is None:raise DataError('CREDENTIAL_REFERENCE_REQUIRED')
    key=resolve_credential(credential_ref)
    if type(key)is not str or not key:raise DataError('CREDENTIAL_NOT_CONFIGURED')
    return RESTClient(api_key=key,base='https://api.massive.com',connect_timeout=10,read_timeout=30,retries=0,pagination=True,verbose=False,trace=False)

def etf_members(client,etf,effective_date,*,context):
    """Official paginated ETF Global add-on; date is membership, not publication."""
    from datetime import date
    from .simple_inputs import tickers
    if tickers(etf)!=[etf]:raise DataError('UNIVERSE_INVALID')
    date.fromisoformat(effective_date);members=[];processed=set()
    for row in client.get_etf_global_constituents(composite_ticker=etf,effective_date=effective_date,limit=1000):
        context.require_active()
        if len(members)>=1000:raise DataError('UNIVERSE_INVALID')
        if row.effective_date!=effective_date or row.composite_ticker!=etf:raise DataError('MEMBERSHIP_DATE_MISMATCH')
        members+=tickers(row.constituent_ticker or '');processed.add(row.processed_date)
    if not members:raise DataError('MEMBERSHIP_NOT_AVAILABLE')
    return {'id':'etf-'+etf+'-'+effective_date,'path':['ETF持仓',etf],'members':sorted(set(members)),
        'as_of':effective_date,'processed_dates':sorted(x for x in processed if x),'available':True,
        'source':'Massive ETF Global /etf-global/v1/constituents','membership_kind':'DATED_SINGLE_SNAPSHOT',
        'historical_constituents':False,'publication_time':None,'requires_plan_entitlement':True}

def classify_candidates(client,symbols,as_of,*,sic_prefixes,context):
    """Bounded official ticker-overview SIC subset; never a full-sector claim."""
    from datetime import date
    from .simple_inputs import tickers
    import re
    date.fromisoformat(as_of)
    if type(sic_prefixes)is not list or not sic_prefixes or any(type(x)is not str or re.fullmatch(r'[0-9]{2,4}',x)is None for x in sic_prefixes):raise DataError('INDUSTRY_FILTER_INVALID')
    members=[]
    if tickers(' '.join(symbols))!=symbols:raise DataError('UNIVERSE_INVALID')
    for symbol in symbols:
        context.require_active();detail=client.get_ticker_details(symbol,date=as_of)
        if str(getattr(detail,'sic_code','')).startswith(tuple(sic_prefixes)):members.append(symbol)
    return {'id':'sic-subset-'+as_of,'path':['行业','SIC候选子集'],'members':members,'as_of':as_of,
        'source':'Massive ticker overview SIC','available':bool(members),'historical_constituents':False,
        'membership_kind':'CLASSIFIED_CANDIDATE_SUBSET','publication_time':None}

def security_metadata(client,symbols,as_of,*,context,expected_types=None):
    """Reference fields only; reporting-period financial values are not availability."""
    rows=[];currencies=set()
    for symbol in symbols:
        context.require_active();detail=client.get_ticker_details(symbol,date=as_of)
        if getattr(detail,'ticker',None)!=symbol:raise DataError('SECURITY_IDENTITY_AMBIGUOUS')
        currency=getattr(detail,'currency_name',None)
        if type(currency)is not str or len(currency)!=3:raise DataError('IMPORT_CURRENCY_REQUIRED')
        if expected_types is not None and (getattr(detail,'type',None) not in expected_types
                or getattr(detail,'market',None)!='stocks' or getattr(detail,'locale',None)!='us'):
            raise DataError('SECURITY_MARKET_OR_IDENTITY_UNSUPPORTED')
        currencies.add(currency.upper())
        rows.append({'ticker':symbol,'provider':'massive','provider_ticker':detail.ticker,'name':getattr(detail,'name',None),'currency':currency.upper(),
            'exchange':getattr(detail,'primary_exchange',None),'company_cik':getattr(detail,'cik',None),
            'composite_figi':getattr(detail,'composite_figi',None),'share_class_figi':getattr(detail,'share_class_figi',None),
            'source_date':as_of,'published_at':None,'available_at':None,
            'mapping_status':'PROVIDER_REFERENCE_SNAPSHOT' if getattr(detail,'composite_figi',None) else 'UNRESOLVED_NO_PROVIDER_SECURITY_ID',
            'historical_symbol_mapping':'UNVERIFIED_NO_TIME_RANGE_JOIN'})
        if expected_types is not None:rows[-1]['type']=detail.type
    if len(currencies)!=1:raise DataError('MULTI_CURRENCY_DATASET_UNSUPPORTED')
    return {'securities':rows,'metadata':{'price_unit':'per_share','volume_unit':'shares','adjustment':'raw',
        'currency':next(iter(currencies)),'timezone':'America/New_York'}}

def stream_daily(client,symbols,start,end,*,adjusted,context,chunks,scope='public',allow_empty=False):
    """SDK pagination -> bounded validated Parquet batches -> small manifest.

    Never materialize the complete provider response or send it through a
    browser request. A missing/failed symbol aborts publication; persisted
    chunks remain unreferenced. Completeness means every requested iterator
    exhausted, never proven exchange-calendar or historical identity coverage.
    """
    from .simple_chunks import BATCH_ROWS,MAX_ROWS,MAX_CHUNKS,FORMAT
    from .simple_inputs import tickers
    from datetime import date
    if type(adjusted)is not bool or tickers(' '.join(symbols))!=symbols or not symbols:raise DataError('MASSIVE_OPTIONS_INVALID')
    try:
        if date.fromisoformat(start).isoformat()!=start or date.fromisoformat(end).isoformat()!=end or start>end:raise ValueError
    except (ValueError,TypeError):raise DataError('IMPORT_DATES_INVALID') from None
    descriptors=[];count=0;coverage=[]
    for ticker in symbols:
        context.require_active();buffer=[];previous=None;seen=0;first=None
        try:
            for item in client.list_aggs(ticker=ticker,multiplier=1,timespan='day',from_=start,to=end,adjusted=adjusted,sort='asc',limit=50000):
                if seen%128==0:context.require_active()
                if count>=MAX_ROWS:raise DataError('IMPORT_SERVER_ROWS_LIMIT')
                def field(key):return item[key] if isinstance(item,dict) else getattr(item,key)
                day=datetime.fromtimestamp(field('timestamp')/1000,timezone.utc).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                if previous is not None and day<=previous:raise DataError('IMPORT_DUPLICATE_OR_UNSORTED_BAR')
                previous=day;first=first or day;seen+=1;count+=1
                buffer.append({'ticker':ticker,'session_date':day,**{k:str(field(k)) for k in ('open','high','low','close','volume')}})
                if len(buffer)==BATCH_ROWS:
                    descriptors.append(chunks.put(buffer,start=start,end=end,scope=scope,context=context));buffer=[]
                    if len(descriptors)>MAX_CHUNKS:raise DataError('IMPORT_SERVER_CHUNKS_LIMIT')
            if not seen and not allow_empty:raise DataError('IMPORT_PARTIAL_SYMBOL_FAILURE')
            if buffer:descriptors.append(chunks.put(buffer,start=start,end=end,scope=scope,context=context))
            if len(descriptors)>MAX_CHUNKS:raise DataError('IMPORT_SERVER_CHUNKS_LIMIT')
            coverage.append({'ticker':ticker,'rows':seen,'first':first,'last':previous})
        except DataError:raise
        except Exception:
            context.require_active()
            raise DataError('MASSIVE_SYMBOL_FAILED') from None
    context.require_active()
    return {'format':FORMAT,'chunks':descriptors,'row_count':count,'securities':coverage,'status':'COMPLETE_ITERATORS_EXHAUSTED',
            'completeness':'UNVERIFIED_PROVIDER_RESPONSE','missing_sessions':'NOT_INFERRED'}

def collect_daily(client,symbols,start,end,*,adjusted,context,max_rows=10000):
    if type(adjusted)is not bool or type(max_rows)is not int or not 1<=max_rows<=10000:raise DataError('MASSIVE_OPTIONS_INVALID')
    rows=[];failures=[]
    for ticker in symbols:
        context.require_active();before=len(rows)
        try:
            for item in client.list_aggs(ticker=ticker,multiplier=1,timespan='day',from_=start,to=end,adjusted=adjusted,sort='asc',limit=50000):
                context.require_active()
                if len(rows)>=max_rows:raise DataError('IMPORT_ROWS_LIMIT')
                def field(key):return item[key] if isinstance(item,dict) else getattr(item,key)
                day=datetime.fromtimestamp(field('timestamp')/1000,timezone.utc).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                rows.append({'ticker':ticker,'session_date':day,**{k:str(field(k)) for k in ('open','high','low','close','volume')}})
            if len(rows)==before:failures.append({'ticker':ticker,'code':'NO_BARS_RETURNED'})
        except DataError:raise
        except Exception:
            context.require_active();del rows[before:];failures.append({'ticker':ticker,'code':'MASSIVE_SYMBOL_FAILED'})
    if failures:return {'status':'INCOMPLETE','failures':failures,'rows':rows,'publishable':False}
    return {'status':'COMPLETE','rows':validate_rows(rows,start,end,symbols),'publishable':True,
        'adjustment':'split_adjusted' if adjusted else 'raw','return_basis':'price',
        'available_at':None,'publication_time':None,'pit_status':'UNVERIFIED','sdk':'massive.RESTClient.list_aggs'}
