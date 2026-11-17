"""Value-only dated roster import and bounded official reference selection."""
from datetime import date,datetime,timezone
from .simple_inputs import tickers
from .contracts import DataError

def imported_roster(value):
    if type(value)is not dict or set(value)!={'label','members','as_of','source'}:raise DataError('UNIVERSE_INVALID')
    for key,maximum in [('label',120),('source',500)]:
        if type(value[key])is not str or not 1<=len(value[key])<=maximum or any(ord(ch)<32 for ch in value[key]):raise DataError('UNIVERSE_INVALID')
    if type(value['members'])is not list or not value['members'] or tickers(' '.join(value['members']))!=value['members']:raise DataError('UNIVERSE_INVALID')
    try:
        day=date.fromisoformat(value['as_of'])
        if day.isoformat()!=value['as_of'] or day>datetime.now(timezone.utc).date():raise ValueError
    except (ValueError,TypeError):raise DataError('IMPORT_DATES_INVALID') from None
    return {'path':['导入名单',value['label']],'members':value['members'],'as_of':value['as_of'],'source':value['source'],
            'available':True,'historical_constituents':False,'membership_kind':'USER_DATED_STATIC_ROSTER',
            'date_basis':'USER_ASSERTED_MEMBERSHIP_DATE_NOT_PUBLICATION','publication_time':None,
            'observed_received_at':datetime.now(timezone.utc).isoformat(),'source_verified':False}

def validate_reference_selection(spec):
    if type(spec)is not dict or set(spec)!={'exchange','security_type','as_of','maximum','active'}:raise DataError('UNIVERSE_INVALID')
    if spec['exchange'] not in {'XNAS','XNYS','XASE',None} or spec['security_type'] not in {'CS','ETF'} or type(spec['active'])is not bool or type(spec['maximum'])is not int or not 1<=spec['maximum']<=1000:raise DataError('UNIVERSE_INVALID')
    try:
        day=date.fromisoformat(spec['as_of'])
        if day.isoformat()!=spec['as_of'] or day>datetime.now(timezone.utc).date():raise ValueError
    except (TypeError,ValueError):raise DataError('IMPORT_DATES_INVALID') from None

def reference_selection(client,spec,*,context):
    """Alphabetical bounded selection, explicitly not full exchange membership."""
    validate_reference_selection(spec)
    params={'market':'stocks','type':spec['security_type'],'date':spec['as_of'],'active':spec['active'],
            'order':'asc','sort':'ticker','limit':min(1000,spec['maximum']+1)}
    if spec['exchange'] is not None:params['exchange']=spec['exchange']
    members=[];truncated=False;previous=None;seen=0;excluded=0
    for row in client.list_tickers(**params):
        context.require_active();seen+=1
        if seen>2000:raise DataError('UNIVERSE_REFERENCE_ROW_LIMIT')
        def value(key):return row.get(key) if type(row)is dict else getattr(row,key,None)
        symbol=value('ticker')
        if type(symbol)is not str or (previous is not None and symbol<=previous):raise DataError('UNIVERSE_REFERENCE_ORDER')
        previous=symbol
        if value('market')!='stocks' or value('locale')!='us' or value('type')!=spec['security_type'] or value('active')is not spec['active'] or (spec['exchange'] is not None and value('primary_exchange')!=spec['exchange']):raise DataError('UNIVERSE_REFERENCE_BINDING')
        try:valid=tickers(symbol)==[symbol]
        except DataError:valid=False
        if not valid:excluded+=1;continue
        if len(members)==spec['maximum']:truncated=True;break
        members.append(symbol)
    if not members:raise DataError('MEMBERSHIP_NOT_AVAILABLE')
    return {'path':['交易所筛选',spec['exchange'] or '美国股票'],'members':members,'as_of':spec['as_of'],
            'source':'Massive RESTClient.list_tickers /v3/reference/tickers','available':True,'historical_constituents':False,
            'membership_kind':'BOUNDED_REFERENCE_SELECTION','selection_parameters':dict(spec),'publication_time':None,
            'observed_received_at':datetime.now(timezone.utc).isoformat(),'excluded_unsupported_symbols':excluded,
            'selection_completeness':'TRUNCATED_ALPHABETICAL_SUBSET' if truncated else 'REQUESTED_FILTER_ITERATOR_EXHAUSTED',
            'historical_identity_join':'UNVERIFIED_NO_TIME_RANGE_JOIN'}
