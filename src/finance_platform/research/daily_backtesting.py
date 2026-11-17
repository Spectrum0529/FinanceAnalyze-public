"""FinanceAnalyze daily long/cash execution and compatible saved-report output.

An intent is formed after a completed bar and can execute only at the next
observed open. This engine supports one long position, with no leverage,
shorting, intrabar orders or forced end-of-data liquidation.
"""
from datetime import date, datetime, timezone
from importlib.metadata import version
import math

from finance_platform.core.contracts import fingerprint
from .daily import (DailyError, DailyRequest, RESULT_FAMILY, ENGINE_VERSION, ENGINE,
                    STRATEGY_VERSION, CALCULATION_VERSION, ASSUMPTIONS)


def _text(value):
    if value is None:
        return None
    value = float(value)
    return str(value) if math.isfinite(value) else None


class _ExecutionVersions:
    """Resolve report dependency metadata once per portfolio execution."""
    def __init__(self):
        self._report_versions = None

    def report(self):
        if self._report_versions is None:
            self._report_versions = {**ENGINE, "pandas_version": version("pandas"),
                                     "numpy_version": version("numpy")}
        return self._report_versions.copy()


def simulate(series, *, active=lambda: None, progress=lambda value: None,
             include_fills=False, strategy_parameters=None):
    return _simulate(series, active=active, progress=progress, include_fills=include_fills,
                     strategy_parameters=strategy_parameters, versions=_ExecutionVersions())


def _simulate(series, *, active, progress, include_fills, strategy_parameters=None,
              versions, daily_decisions=None):
    active()
    import pandas as pd

    req = DailyRequest.parse(series.request.payload())
    parameters = None
    if strategy_parameters is not None and daily_decisions is not None:
        raise DailyError("DAILY_STRATEGY_BINDING_CONFLICT")
    if strategy_parameters is not None:
        from .strategy_search import candidate
        parameters = candidate(strategy_parameters)
        expected = {'buy_hold':'buy_hold','sma':'sma20','sma_cross':'sma_cross_5_20'}[parameters['template']]
        if expected != req.strategy:
            raise DailyError('SEARCH_TEMPLATE_BINDING_MISMATCH')
    days = [bar.session_date for bar in series.bars]
    if (not days or days != sorted(set(days))
            or any(not req.start_date <= d <= req.end_date for d in days)
            or any(date.fromisoformat(d) >= datetime.now(timezone.utc).date() for d in days)):
        raise DailyError("DAILY_SESSION_ORDER_INVALID")
    prices = []
    for index, bar in enumerate(series.bars):
        if index % 64 == 0:
            active()
        row = [float(getattr(bar, k)) for k in ('open','high','low','close')]
        if (any(not math.isfinite(x) or x <= 0 for x in row)
                or row[2] > min(row[0], row[3]) or row[1] < max(row[0], row[3])):
            raise DailyError("DAILY_OHLC_INVALID")
        prices.append(row)

    minimum, per = float(req.minimum_fee), float(req.fee_per_share)
    spread = float(req.spread_bps) / 10000
    initial = cash = float(req.initial_cash)
    def commission(quantity):
        return max(minimum, abs(quantity) * per)

    # Retain the existing built-in decision schedule: buy/hold starts on bar 1;
    # averages first decide one bar after their first complete rolling window.
    # Saved SDK strategies receive bar 0 at its close, before any execution.
    first = 0 if daily_decisions is not None else 1
    slow, fast = None, None
    if daily_decisions is None and req.strategy != 'buy_hold':
        window = parameters['slow'] if parameters else 20
        closes = pd.Series([p[3] for p in prices])
        slow = closes.rolling(window).mean().to_numpy()
        if req.strategy == 'sma_cross_5_20':
            fast = closes.rolling(parameters['fast'] if parameters else 5).mean().to_numpy()
        first = window if len(prices) >= window else 1

    decisions, orders, trades, opened, fills, curve = [], [], [], [], [], []
    pending = None
    position = None
    last_progress = 44
    for index, row in enumerate(prices):
        if index % 64 == 0 or index == len(prices) - 1:
            active()
        if pending is not None:
            order = pending
            pending = None
            if order['side'] == 'buy':
                price = row[0] * (1 + spread)
                if order['size_fraction'] is not None:
                    # Historical all-cash policy reserves commission per unit
                    # during sizing. Keep that conservative allocation explicit.
                    quantity = int((cash * .9999) // (price + commission(.9999) / .9999))
                else:
                    quantity = int(order['quantity'])
                fee = commission(quantity)
                if quantity > 0 and quantity * price + fee <= cash:
                    cash -= quantity * price + fee
                    position = {'quantity':quantity,'entry_price':price,'entry_fee':fee,
                                'entry_bar':index,'tag':order['order_id']}
                    fills.append({'at':days[index], 'side':'buy', 'quantity':str(quantity),
                                  'price':_text(price), 'fee':_text(fee)})
                    order['status'] = 'ENGINE_FILLED'
                else:
                    order['status'] = 'ENGINE_ENTRY_NOT_FILLED'
            elif position is not None:
                quantity = position['quantity']
                price, fee = row[0], commission(quantity)
                cash += quantity * price - fee
                paid = position['entry_fee'] + fee
                pnl = quantity * (price - position['entry_price']) - paid
                trades.append({'entry_session_date':days[position['entry_bar']],
                    'exit_session_date':days[index], 'quantity':str(quantity),
                    'entry_price':_text(position['entry_price']), 'exit_price':_text(price),
                    'pnl':_text(pnl), 'fee':_text(paid),
                    'return_fraction':_text(pnl / (quantity * position['entry_price'])),
                    'entry_bar':position['entry_bar'],'exit_bar':index,'tag':position['tag']})
                fills.append({'at':days[index], 'side':'sell','quantity':str(quantity),
                              'price':_text(price), 'fee':_text(fee)})
                position = None
                order['status'] = 'ENGINE_FILLED'
        quantity = position['quantity'] if position else 0
        equity = cash + quantity * row[3]
        if not math.isfinite(equity) or not math.isfinite(cash) or equity < 0:
            raise DailyError('DAILY_EQUITY_INVALID')
        curve.append({'at':days[index],'equity':_text(equity),'cash':None,'quantity':None})
        if index < first:
            continue
        step = 45 + (index+1) * 45 // len(days)
        if step > last_progress:
            last_progress = step
            progress(step)
        following = days[index+1] if index+1 < len(days) else None
        decision = {'session_date':days[index], 'input_through_session':days[index],
                    'next_execution_session':following}
        if daily_decisions is not None:
            size, output = daily_decisions(series.bars[index], quantity, equity)
            if size is not None and (type(size) not in (int, bool) or size < 0 or size > 10**9):
                raise DailyError('DAILY_TARGET_QUANTITY_INVALID')
            decision.update(sdk_output_id=output.output_id if output else None,
                sdk_output_kind=output.kind.value if output else None,
                sdk_output_value=str(output.value) if output else None,
                decision_at=output.produced_at.isoformat() if output else None)
        else:
            size = (True if req.strategy == 'buy_hold' else
                    bool(row[3] > slow[index]) if req.strategy == 'sma20' else
                    bool(fast[index] > slow[index]))
            decision['signal'] = 'LONG' if size else 'CASH'
        decisions.append(decision)
        # The saved daily SDK deliberately has no in-place position resizing.
        if size is None or bool(size) == bool(quantity):
            continue
        pending = {'order_id':'intent-'+str(index),'decision_session_date':days[index],
            'session_date':days[index], 'side':'buy' if size else 'close',
            'quantity':None if type(size) is bool else str(size),
            'size_fraction':'0.9999' if type(size) is bool and size else None,
            'status':'STRATEGY_INTENT_SUBMITTED_TO_ENGINE'}
        orders.append(pending)

    if position is not None:
        opened.append({'quantity':str(position['quantity']), 'entry_price':_text(position['entry_price']),
            'entry_session_date':days[position['entry_bar']],
            'mark_pnl_before_commissions':_text(position['quantity']*(prices[-1][3]-position['entry_price'])),
            'entry_fee':_text(position['entry_fee']), 'tag':position['tag']})
    pending_orders = []
    if pending is not None:
        size = (.9999 if pending['size_fraction'] else int(pending['quantity'])) if pending['side']=='buy' else -quantity
        pending_orders.append({'size':_text(size), 'tag':pending['order_id'] if pending['side']=='buy' else position['tag'],
            'status':'ENGINE_PENDING_AT_END','reason':'END_OF_DATA_NO_FOLLOWING_OBSERVED_BAR'})
    closed_fees = sum(float(t['fee']) for t in trades)
    open_entry_fees = sum(float(t['entry_fee']) for t in opened)
    fees = closed_fees + open_entry_fees
    realized = sum(float(t['pnl']) for t in trades)
    unrealized = sum(float(t['mark_pnl_before_commissions'])-float(t['entry_fee']) for t in opened)
    profit = equity - initial
    if not math.isclose(profit, realized+unrealized, rel_tol=1e-9, abs_tol=1e-6):
        raise DailyError('DAILY_FULL_PNL_RECONCILIATION_FAILED')
    accounting = {'net_profit':_text(profit),'realized_pnl':_text(realized),'unrealized_pnl':_text(unrealized),
        'fees':_text(fees),'closed_trade_fees':_text(closed_fees),'open_entry_fees':_text(open_entry_fees),
        'fill_count':len(fills),'basis':'ALL_EXECUTED_ENTRY_EXIT_FEES_NO_DOUBLE_DEDUCTION',
        'reconciliation_residual':_text(profit-realized-unrealized)}
    if include_fills:
        accounting['fills'] = fills
    final = {'cash':None,'available_cash':None,'unsettled_cash':None,'quantity':str(quantity),
        'equity':_text(equity),'fees':_text(fees),'fees_basis':'ALL_EXECUTED_ENTRIES_AND_EXITS',
        'realized_pnl':_text(realized),'unrealized_pnl':_text(unrealized),'mark_session_date':days[-1],
        'mark_price':str(series.bars[-1].close),'orders':orders}
    runtime = versions.report()
    cost_model = {'version':'financeanalyze-daily-costs-1','spread_bps':req.spread_bps,
        'display_name':'按成交数量收费；开仓计入价差，不做分币或价格刻度取整',
        'fee_report_basis':'ALL_EXECUTED_ENTRIES_AND_EXITS'}
    peak, drawdown = initial, 0.0
    for point in curve:
        value = float(point['equity'])
        peak = max(peak, value)
        drawdown = max(drawdown, 1-value/peak)
    quality = (series.history or {}).get('data_quality')
    actual_first = first if first < len(days) else None
    summary = {'status':'DAILY_SIMULATION_ONLY','mode':'BACKTEST','scope':'DEVELOPMENT',
        'engine':runtime,'engine_version':ENGINE_VERSION,'cost_model':cost_model,
        'sample_hash':fingerprint(series.payload()),'net_return':_text(equity/initial-1),
        'max_drawdown':_text(drawdown),'fees':_text(fees),
        'fees_basis':'ALL_EXECUTED_ENTRIES_AND_EXITS','trade_count':len(trades),
        'trade_count_basis':'CLOSED_ROUND_TRIPS','open_trade_count':len(opened),
        'order_count':len(orders),'order_count_basis':'ADAPTER_STRATEGY_SUBMISSION_INTENTS',
        'turnover':None,'gross_traded_notional':None,
        'risk':{'annualized_volatility':None,'sharpe':None,'alpha':None,
                'reason':'Price-path simulation only; statistical or market validity is not certified.'},
        'coverage':{'sessions':len(days),'valuation_points':len(curve),
            'missing_valuations':len(quality.get('gap_dates',[])) if quality else 0,
            'market_coverage':'NOT_CERTIFIED','pending_orders':len(pending_orders)},
        'warmup':{'first_next_bar_index':actual_first,
            'first_decision_session':days[actual_first] if actual_first is not None else None,
            'basis':'SDK_FIRST_OBSERVED_CLOSE' if daily_decisions is not None else 'DECLARED_BUILTIN_ROLLING_WINDOW_START'},
        'assumptions':dict(ASSUMPTIONS),'return_basis':'PRICE_SERIES_SIMULATION_NOT_TOTAL_RETURN',
        'decision':'日线模拟；净盈亏包括买卖费用和期末持仓估值，不代表真实成交。'}
    result = {'schema_version':RESULT_FAMILY,'family':RESULT_FAMILY,'engine':runtime,'engine_version':ENGINE_VERSION,
        'strategy_version':STRATEGY_VERSION,'calculation_version':CALCULATION_VERSION,'cost_model':cost_model,
        'mode':'BACKTEST','status':'DAILY_SIMULATION_ONLY','scope':'DEVELOPMENT',
        'ticker':req.ticker,'provider':req.provider,'strategy':req.strategy,'request':req.payload(),
        'assumptions':dict(ASSUMPTIONS),'report':summary,'equity_curve':curve,'trades':trades,
        'full_accounting':accounting,'orders':orders,'decisions':decisions,'rejections':[],
        'unsubmitted_outputs':pending_orders,'open_trades':opened,'pending_orders':pending_orders,'final':final,
        'engine_parameters':{'commission':'max(minimum_fee,abs(size)*fee_per_share)',
            'fee_per_share':req.fee_per_share,'minimum_fee':req.minimum_fee,'spread_bps':req.spread_bps,
            'trade_on_close':False,'finalize_trades':False,'margin':'1','buy_size_fraction':'0.9999'}}
    if parameters is not None:
        result['search_template'] = {'version':'controlled-long-cash-parameters-1','parameters':parameters}
    if quality is not None:
        result['data_quality'] = quality
    active()
    progress(90)
    return result
