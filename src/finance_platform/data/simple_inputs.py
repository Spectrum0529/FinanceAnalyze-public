"""Bounded daily inputs and explicitly dated security selections; no downloads."""
import csv,io,re
from datetime import date
from decimal import Decimal,InvalidOperation
from .contracts import DataError

FIELDS=('open','high','low','close','volume')
META={'price_unit','volume_unit','adjustment','currency','timezone'}
def tickers(value):
    if type(value)is not str or len(value)>20000:raise DataError('UNIVERSE_INVALID')
    items=re.split(r'[\s,;]+',value.strip().upper()) if value.strip() else []
    if any(re.fullmatch(r'(?:[A-Z][A-Z0-9.\-]{0,15}|6[0-9]{5}\.SH|[03][0-9]{5}\.SZ)',x)is None for x in items) or len(items)>1000:raise DataError('UNIVERSE_INVALID')
    return sorted(set(items))

def universe_catalog():
    return {'items':[
      {'id':'mag7-definition-1','path':['常用','Mag 7'],'members':['AAPL','AMZN','GOOG','GOOGL','META','MSFT','NVDA','TSLA'],
       'company_count':7,'source':'https://www.nasdaq.com/articles/what-to-expect-from-the-magnificent-seven-stocks-in-2024',
       'as_of':'2026-10-30','date_basis':'CURATED_SELECTION_CAPTURE_DATE','observed_on':'2026-10-30','membership_kind':'NAMED_GROUP_DEFINITION','historical_constituents':False,'available':True},
      {'id':'storage-subset-1','path':['主题','存储（示例子集）'],'members':['MU','STX'],
       'as_of':'2026-10-30','source':'https://www.micron.com/products ; https://www.seagate.com/gb/en/stories/corporate-overview/',
       'date_basis':'CURATED_SELECTION_CAPTURE_DATE','membership_kind':'CURATED_BUSINESS_SUBSET','historical_constituents':False,'available':True},
      {'id':'technology-subset-1','path':['行业','科技（Mag7定义子集）'],'members':['AAPL','GOOG','GOOGL','MSFT','NVDA'],
       'as_of':'2026-10-30','source':'https://www.nasdaq.com/articles/what-to-expect-from-the-magnificent-seven-stocks-in-2024',
       'date_basis':'CURATED_SELECTION_CAPTURE_DATE','membership_kind':'CURATED_SUBSET_NOT_FULL_SECTOR','historical_constituents':False,'available':True},
      {'id':'us-index-etf-watchlist-1','path':['常用','美国宽基ETF（非成分股）'],'members':['DIA','IWM','QQQ','SPY'],
       'as_of':'2026-11-07','source':'https://www.ssga.com/us/en/individual/etfs ; https://www.invesco.com/qqq-etf/ ; https://www.ishares.com/us/products/239710/',
       'date_basis':'CURATED_SELECTION_CAPTURE_DATE','membership_kind':'ETF_INSTRUMENT_WATCHLIST_NOT_HOLDINGS','historical_constituents':False,'available':True},
      *[{'id':key,'path':path,'members':[],'as_of':None,'source':None,'available':False,'reason_code':'DATED_MEMBERSHIP_REQUIRED'} for key,path in [
       ('semiconductors',['主题','半导体（需来源名单）']),('healthcare',['行业','医药与生物科技（需来源名单）']),('qqq',['ETF持仓','QQQ（按日期获取）'])]]],
       'notice':'Mag7是具名公司定义，8代码不等于8公司；不代表任何历史动态成分。其他分类需接入带来源日期的名单。'}

def resolve_universe(value,*,catalog=None):
    if type(value)is not dict or set(value)-{'groups','manual','exclude'}:raise DataError('UNIVERSE_INVALID')
    groups=value.get('groups',[])
    if type(groups)is not list or len(groups)>32 or any(type(x)is not str for x in groups):raise DataError('UNIVERSE_INVALID')
    known={e['id']:e for e in (catalog or universe_catalog())['items']};selected=[];members=set(tickers(value.get('manual','')))
    for key in sorted(set(groups)):
        entry=known.get(key)
        if not entry or not entry.get('available') or not entry.get('source') or not entry.get('as_of'):raise DataError('DATED_MEMBERSHIP_REQUIRED')
        members.update(tickers(' '.join(entry['members'])));selected.append(entry)
    members.difference_update(tickers(value.get('exclude','')))
    if not members or len(members)>1000:raise DataError('UNIVERSE_EMPTY_OR_OVERSIZED')
    return {'tickers':sorted(members),'groups':selected,'manual':tickers(value.get('manual','')),'excluded':tickers(value.get('exclude','')),
      'selection_mode':'STATIC_SELECTED_MEMBERS','historical_constituents':False}

def metadata(value):
    if type(value)is not dict or set(value)!=META:raise DataError('IMPORT_METADATA_REQUIRED')
    native=value['price_unit']=='per_adjusted_simulation_unit' and value['volume_unit']=='provider_reported_unverified' and value['adjustment'] in {'split_adjusted','qfq'}
    conventional=value['price_unit']=='per_share' and value['volume_unit']=='shares' and value['adjustment'] in {'raw','split_adjusted'}
    observed=value['price_unit']=='per_provider_observed_unit' and value['volume_unit']=='provider_reported_unverified' and value['adjustment']=='provider_observed_unverified'
    if not (native or conventional or observed):raise DataError('IMPORT_UNITS_AMBIGUOUS')
    if type(value['currency'])is not str or re.fullmatch('[A-Z]{3}',value['currency'])is None:raise DataError('IMPORT_CURRENCY_REQUIRED')
    from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
    try:ZoneInfo(value['timezone'])
    except (TypeError,ValueError,ZoneInfoNotFoundError):raise DataError('IMPORT_TIMEZONE_REQUIRED') from None
    return dict(value)

def validate_rows(rows,start,end,universe,*,allow_missing_volume=False):
    try:
        if date.fromisoformat(start).isoformat()!=start or date.fromisoformat(end).isoformat()!=end or start>end:raise ValueError
    except (TypeError,ValueError):raise DataError('IMPORT_DATES_INVALID') from None
    if type(rows)is not list or not 0<len(rows)<=10000:raise DataError('IMPORT_ROWS_LIMIT')
    output=[];seen=set()
    for row in rows:
        if type(row)is not dict or set(row)!={'ticker','session_date',*FIELDS}:raise DataError('IMPORT_ROW_SCHEMA')
        if row['ticker'] not in universe or type(row['session_date'])is not str:raise DataError('IMPORT_UNIVERSE_MISMATCH')
        try:
            day=date.fromisoformat(row['session_date']).isoformat()
            if not start<=day<=end:raise ValueError
            fields=FIELDS[:-1] if allow_missing_volume and row['volume'] is None else FIELDS
            if any(type(row[k])not in (str,int) or len(str(row[k]))>64 for k in fields):raise ValueError
            values={k:Decimal(str(row[k])) for k in fields}
            if any(not x.is_finite() or x<0 for x in values.values()):raise ValueError
            if any(len(x.as_tuple().digits)>36 or not -24<=x.as_tuple().exponent<=24 for x in values.values()):raise ValueError
            if min(values[k] for k in FIELDS[:4])<=0 or values['low']>min(values['open'],values['close']) or values['high']<max(values['open'],values['close']) or values['low']>values['high']:raise ValueError
        except (ValueError,TypeError,InvalidOperation):raise DataError('IMPORT_ROW_INVALID') from None
        key=(row['ticker'],day)
        if key in seen:raise DataError('IMPORT_DUPLICATE_BAR')
        seen.add(key);output.append({'ticker':key[0],'session_date':day,**{k:format(v,'f') for k,v in values.items()},**({'volume':None} if 'volume'not in values else {})})
    missing=sorted(set(universe)-{r['ticker'] for r in output})
    if missing:raise DataError('IMPORT_PARTIAL_SYMBOL_FAILURE')
    return sorted(output,key=lambda r:(r['ticker'],r['session_date']))

def preview_csv(raw,options=None):
    options=options or {}
    if type(raw)is not bytes or not 0<len(raw)<=1024*1024:raise DataError('IMPORT_CSV_LIMIT')
    try:
        text=raw.decode('utf-8-sig');reader=csv.DictReader(io.StringIO(text),dialect=csv.Sniffer().sniff(text[:8192],delimiters=',;\t'))
        headers=reader.fieldnames or []
        if len(headers)!=len(set(headers)):raise ValueError
        rows=list(reader)
        if len(rows)>10000:raise ValueError
    except (ValueError,UnicodeError,csv.Error):raise DataError('IMPORT_CSV_INVALID') from None
    aliases={'ticker':['ticker','symbol','代码'],'session_date':['date','session_date','日期'],**{k:[k] for k in FIELDS}}
    explicit=options.get('mapping',{});mapping={};missing=[]
    for field,names in aliases.items():
        choices=[h for h in headers if h.strip().lower() in names]
        if field in explicit:
            if explicit[field] not in headers:raise DataError('IMPORT_MAPPING_INVALID')
            mapping[field]=explicit[field]
        elif len(choices)==1:mapping[field]=choices[0]
        elif field=='ticker' and len(tickers(options.get('ticker','')))==1:pass
        else:missing.append(field)
    missing+=sorted(META-set(options.get('metadata',{})))
    if missing:return {'status':'NEEDS_INPUT','missing':missing,'mapping':mapping,'rows':len(rows)}
    meta=metadata(options['metadata']);ticker=tickers(options.get('ticker',''))
    parsed=[{field:(row[mapping[field]] if field in mapping else ticker[0]) for field in aliases} for row in rows]
    for row in parsed:row['ticker']=row['ticker'].strip().upper();row['session_date']=row['session_date'].strip()
    return {'status':'READY','mapping':mapping,'metadata':meta,'rows':parsed}
