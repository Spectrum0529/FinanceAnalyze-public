"""Only trusted accepted HistoryGateway views enter adjusted simulations."""
from .portfolio import fail
from .daily import DailyRequest,DailySeries
from .daily_price_policy import validate_price_policy


def acquisition(dataset):
    value=dataset.get('acquisition')or dataset.get('row_storage',{}).get('acquisition')
    if value is None:return None
    if type(value)is not dict or value.get('provider')not in{'akshare','yfinance'}or value.get('raw_compatible')is not False or value.get('quantity_basis')!='ADJUSTED_SIMULATION_UNITS':fail('PORTFOLIO_NATIVE_ACQUISITION_INVALID')
    meta=dataset.get('metadata',{})
    if meta.get('adjustment')!=('qfq'if value['provider']=='akshare'else'split_adjusted')or meta.get('price_unit')!='per_adjusted_simulation_unit' or meta.get('currency')!='USD':fail('PORTFOLIO_NATIVE_PRICE_BASIS_MISMATCH')
    return value


def series(dataset,ticker,request):
    source=acquisition(dataset)
    if source is None:fail('PORTFOLIO_NATIVE_ACQUISITION_REQUIRED')
    views=dataset.get('history_views')
    if type(views)is not dict or ticker not in views:fail('PORTFOLIO_ACCEPTED_NATIVE_VIEW_REQUIRED')
    if request.provider!=source['provider']:fail('PORTFOLIO_NATIVE_PROVIDER_MISMATCH')
    view=views[ticker]
    if view.get('price_policy')!=source['price_policy']:fail('PORTFOLIO_NATIVE_POLICY_MISMATCH')
    result=DailySeries.from_history(view,request);policy=validate_price_policy(view,request)
    return result,policy


def read_accepted(library,owner,reference,acceptance_id,record,*,start=None,end=None):
    if acquisition(record)is None:return record
    value=library.read_native_for_backtest(owner,reference,acceptance_id,start=start,end=end)
    if value.get('dataset_reference')!=reference:fail('PORTFOLIO_NATIVE_DATASET_BINDING_MISMATCH')
    return {**record,**value}


def verify_views(dataset):
    if acquisition(dataset)is None:return
    views=dataset.get('history_views')
    if type(views)is not dict or set(views)!={r['ticker']for r in dataset['rows']}:fail('PORTFOLIO_ACCEPTED_NATIVE_VIEW_REQUIRED')
    from .portfolio import number
    for ticker,view in views.items():
        rows=[r for r in dataset['rows']if r['ticker']==ticker]
        req=DailyRequest.parse({'ticker':ticker,'provider':view['provider'],'strategy':'buy_hold','start_date':dataset['window']['start'],'end_date':dataset['window']['end']})
        actual,policy=series(dataset,ticker,req)
        projection=[(r['session_date'],*[number(r[k])for k in('open','high','low','close')],None if r['volume']is None else number(r['volume']))for r in rows]
        if projection!=[(b.session_date,b.open,b.high,b.low,b.close,b.volume)for b in actual.bars]:fail('PORTFOLIO_NATIVE_ROW_PROJECTION_MISMATCH')


def basis_key(dataset):
    value=acquisition(dataset)
    if value is None:return None
    return (value['provider'],value['price_policy'],tuple(sorted(v['history_content_hash']for v in dataset.get('history_views',{}).values())))
