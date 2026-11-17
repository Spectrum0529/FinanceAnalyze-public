"""Presentation DTO over actual trusted replay, never model-produced numbers.

Uses the existing PortfolioAnalytics equity/calendar/security_pnl wire shape.
Native binary64 engine values remain binary64; no Decimal accuracy claim.
"""
from collections import defaultdict
import math
from .protocol import require, digest
from .autonomous_receipts import CN_SIMULATION


def finite(value):
    require(type(value) in {int,float} and math.isfinite(value), 'REPORT_ENGINE_VALUE_INVALID')
    return value


def curve_points(dates, values, initial):
    require(len(dates)==len(values) and 0<len(dates)<=2000 and initial>0, 'REPORT_CURVE_SHAPE_INVALID')
    previous=initial; points=[]
    for at, value in zip(dates,values):
        value=finite(value)
        points.append({'at':at,'equity':value,'net_profit':value-initial,'return_fraction':value/initial-1,
                       'daily_pnl':value-previous,'daily_return':value/previous-1 if previous>0 else None,
                       'contributed_capital':initial,'valuation_status':'OBSERVED'})
        previous=value
    return points


def calendar(points):
    # Same observed-session aggregation contract as research.portfolio.calendar.
    result={'daily':[{k:p[k] for k in ('at','daily_pnl','daily_return','valuation_status')} for p in points]}
    for name,width in [('monthly',7),('yearly',4)]:
        groups=defaultdict(list)
        for point in points:groups[point['at'][:width]].append(point)
        rows=[]
        for at, values in sorted(groups.items()):
            growth=1.0; complete=all(p['daily_return'] is not None for p in values)
            if complete:
                for p in values:growth*=1+p['daily_return']
            rows.append({'at':at,'net_pnl':math.fsum(p['daily_pnl'] for p in values),
                         'return_fraction':growth-1 if complete else None,'observed_sessions':len(values),
                         'complete':complete,'coverage_basis':'OBSERVED_SESSIONS_ONLY_NOT_FULL_CALENDAR_PERIOD'})
        result[name]=rows
    return result


def portfolio_view(evaluation, rows_by_symbol, train_end, options, *, source_sha256, input_receipt_sha256,
                   research_market=None):
    require(1<=len(rows_by_symbol)<=8 and set(rows_by_symbol)==set(evaluation['sleeves']), 'REPORT_SYMBOL_MISMATCH')
    engines={sleeve.get('engine') for sleeve in evaluation['sleeves'].values()}
    require(len(engines)==1 and all(type(engine) is str and engine for engine in engines), 'REPORT_ENGINE_MISMATCH')
    engine=next(iter(engines))
    # A projection of saved results must retain their recorded engine identity.
    trade_source='BACKTESTING_NATIVE' if engine=='backtesting.py-0.6.6' else 'TRUSTED_REPLAY'
    dates=None; security=[]; curves={}; trades=[]; positions=[]
    for symbol, rows in sorted(rows_by_symbol.items()):
        current=[r['at'] for r in rows[train_end:]]
        require(dates is None or dates==current,'REPORT_CALENDAR_MISMATCH');dates=current
        sleeve=evaluation['sleeves'][symbol];initial=finite(sleeve['initial_cash'])
        points=curve_points(dates,sleeve['equity'],initial);curves[symbol]=points
        native_dates=[rows[train_end-1]['at']]+dates
        def date_at(index):
            require(type(index) is int and 0<=index<len(native_dates),'REPORT_TRADE_BAR_INVALID')
            return native_dates[index]
        for t in sleeve['closed_trades']:
            trades.append({'ticker':symbol,**t,'entry_at':date_at(t['entry_bar']),
                           'exit_at':date_at(t['exit_bar']),'source':trade_source+'_CLOSED_TRADE'})
        for t in sleeve['open_positions']:
            positions.append({'ticker':symbol,**t,'entry_at':date_at(t['entry_bar']),
                              'valuation_at':dates[-1],'source':trade_source+'_OPEN_TRADE'})
        require(len(trades)+len(positions)<=20000,'REPORT_TRADE_CAP_EXCEEDED')
        net=finite(sleeve['equity'][-1])-initial
        realized=math.fsum(finite(t['pnl']) for t in sleeve['closed_trades'])
        count=len(sleeve['closed_trades'])+len(sleeve['open_positions'])
        security.append({'ticker':symbol,'initial_capital':initial,'final_equity':sleeve['equity'][-1],
                         'net_pnl':net,'realized_pnl':realized,'unrealized_pnl':net-realized,
                         'unrealized_basis':'ACCOUNT_NET_PNL_MINUS_NATIVE_CLOSED_TRADE_PNL_INCLUDES_OPEN_ENTRY_COSTS',
                         'fees':finite(sleeve['fees']),'trade_count':count,
                         'trade_count_basis':'NATIVE_CLOSED_AND_OPEN_TRADE_LEGS_NOT_ORDER_COUNT',
                         'return_fraction':net/initial,'quantity':sleeve['state']['quantity']})
    tolerance=max(1e-7,abs(options['cash'])*1e-12)
    account=curve_points(dates,evaluation['equity'],finite(options['cash']))
    for i,p in enumerate(account):
        require(math.isclose(math.fsum(curves[s][i]['equity'] for s in curves),p['equity'],rel_tol=1e-10,abs_tol=tolerance), 'REPORT_EQUITY_RECONCILIATION_FAILED')
    active=[v for v in security if v['trade_count']>0]
    ranked=sorted(active,key=lambda r:(-r['net_pnl'],r['ticker']))
    worst=sorted(active,key=lambda r:(r['net_pnl'],r['ticker']))
    net=account[-1]['net_profit']; security_net=math.fsum(s['net_pnl'] for s in security)
    residual=net-security_net
    require(math.isclose(net,security_net,rel_tol=1e-10,abs_tol=tolerance),'REPORT_PNL_RECONCILIATION_FAILED')
    result={'schema':'autonomous-backtest-visuals-1','family':'autonomous-backtest-result-1',
            'equity_curve':account,'calendar':calendar(account),
            'security_pnl':{'all':ranked,'top5':ranked[:5],'worst':worst,'all_sleeves':security,
                            'rank_basis':'NET_MARK_TO_MARKET_PNL_INCLUDING_CLOSED_AND_OPEN_POSITIONS',
                            'no_trades_reason':'ALL_CASH_NO_EXECUTED_TRADES' if not active else None},
            'per_symbol_equity':curves,'trades':trades,'open_positions':positions,
            'benchmark':{'comparable':True,'coverage_complete':True,
                         'curve':curve_points(dates,evaluation['benchmark_equity'],options['cash']),
                         'metadata':{'instrument_label':'相同本金买入并持有','engine':engine}},
            'request':{'benchmark_mode':'buy_hold'},'engine':engine,
            'return_basis':'SOURCE_PRICE_UNITS_AS_RECORDED_NO_ADDED_CORPORATE_ACTIONS',
            'return_definition':'NET_MARK_TO_MARKET_PROFIT_OVER_INITIAL_CAPITAL_NO_EXTERNAL_FLOWS',
            'reconciliation':{'initial_capital':options['cash'],'net_profit':net,'security_net_pnl':security_net,
                              'residual':residual,'tolerance':{'relative':1e-10,'absolute':tolerance}},
            'coverage':{'first':dates[0],'last':dates[-1],'symbols':len(curves),
                        'calendar':'OBSERVED_SESSIONS_ONLY_NO_GAP_FILL','PIT':'NOT_CERTIFIED'},
            'source_sha256':source_sha256,'input_receipt_sha256':input_receipt_sha256,
            'costs':{'commission_both_sides':options['fee_rate'],'spread_rate':options['spread_rate'],
                     'fees_already_in_equity':True,'fill':'NEXT_OPEN','forced_final_close':False},
            'numeric_basis':'ACTUAL_TRUSTED_ENGINE_BINARY64_NOT_AI_ESTIMATES'}
    if research_market is not None:
        result.update(CN_SIMULATION, corporate_actions=research_market['corporate_actions'])
    result['engine_projection_sha256']=digest(result)
    return result
