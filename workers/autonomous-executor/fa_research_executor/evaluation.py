"""Trusted evaluation of causal intents/factor numbers, never generated source.

All native imports are lazy; used in independent Linux evaluator, not Main.
Initial scope: daily synthetic USD long/cash, one equal-capital sleeve/symbol,
one-session forward open label, trained median factor threshold. No Alpha158.
"""
import math
from .protocol import require, digest

NORMALIZATION = {'return_fraction': 1.0, 'max_drawdown_fraction': 1.0,
                 'cs_ic': 1.0, 'ts_ic': 1.0, 'turnover': 1.0}
REPLAY_ENGINE = 'fa-sealed-intent-replay-1'
COST_MODEL = 'fa-proportional-entry-spread-next-open-1'


def sleeve_options(options, symbols):
    from decimal import Decimal
    cents=Decimal(str(options['cash']))*100
    require(cents==cents.to_integral_value(), 'INITIAL_CASH_CENTS_INVALID')
    base,remainder=divmod(int(cents),len(symbols))
    return {symbol:{**options,'cash':(base+(i<remainder))/100} for i,symbol in enumerate(sorted(symbols))}


def score(metrics, scoring):
    require(type(scoring) is list and bool(scoring), 'UNSUPPORTED_OBJECTIVE')
    parts = []
    for item in scoring:
        require(set(item) == {'name', 'direction', 'weight'} and item['name'] in NORMALIZATION, 'UNSUPPORTED_OBJECTIVE')
        require(item['direction'] in {'maximize', 'minimize'} and type(item['weight']) is int and 1 <= item['weight'] <= 10000, 'OBJECTIVE_INVALID')
        value = metrics.get(item['name'])
        if value is None:
            return {'value': None, 'reason': 'REQUESTED_METRIC_INSUFFICIENT', 'components': parts,
                    'protocol': 'weighted-fixed-normalization-1', 'normalization': NORMALIZATION}
        require(type(value) in {int, float} and math.isfinite(value), 'METRIC_INVALID')
        signed = 1 if item['direction'] == 'maximize' else -1
        contribution = item['weight'] * signed * value / NORMALIZATION[item['name']]
        parts.append({**item, 'metric': value, 'contribution': contribution})
    return {'value': sum(p['contribution'] for p in parts), 'reason': None, 'components': parts,
            'protocol': 'weighted-fixed-normalization-1', 'normalization': NORMALIZATION}


def replay(rows, intents, options):
    """Replay sealed long-only intents with independent cash/lot accounting.

    Bar zero is the no-order warmup. Decisions at a close can fill only at
    the next observed open. Buys pay the opening spread and a proportional
    fee; closes receive the raw open and pay a proportional exit fee. A
    close consumes the oldest lots, attributing each lot's entry fee by units.
    No settlement rules, forced liquidation, generated code or leverage.
    """
    from datetime import date

    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    require(type(rows) is list and type(intents) is list and
            len(rows) == len(intents) and len(rows) >= 2, 'REPLAY_SHAPE_INVALID')
    require(type(options) is dict, 'REPLAY_OPTIONS_INVALID')
    for key, low, high in (('cash', 1, 1e9), ('fee_rate', 0, .05), ('spread_rate', 0, .05)):
        value = options.get(key)
        require(finite(value) and low <= value <= high, 'REPLAY_OPTIONS_INVALID')
    previous = None
    for row, signals in zip(rows, intents):
        require(type(row) is dict and set(row) == {'at', 'open', 'high', 'low', 'close', 'volume'},
                'OHLCV_FRAME_FIELDS_INVALID')
        require(type(row['at']) is str, 'REPLAY_SESSION_INVALID')
        try:
            at = date.fromisoformat(row['at']).isoformat()
        except ValueError:
            require(False, 'REPLAY_SESSION_INVALID')
        require(at == row['at'] and (previous is None or previous < at), 'REPLAY_SESSION_INVALID')
        previous = at
        require(all(finite(row[k]) and row[k] > 0 for k in ('open', 'high', 'low', 'close'))
                and row['low'] <= min(row['open'], row['close'])
                and row['high'] >= max(row['open'], row['close'])
                and finite(row['volume']) and row['volume'] >= 0, 'REPLAY_PRICE_INVALID')
        require(type(signals) is list, 'REPLAY_INTENT_INVALID')
        for intent in signals:
            require(type(intent) is dict and set(intent) == {'action', 'quantity'}
                    and intent['action'] in ('buy', 'close')
                    and type(intent['quantity']) is int and intent['quantity'] > 0,
                    'REPLAY_INTENT_INVALID')

    cash = float(options['cash'])
    fee_rate = options['fee_rate']
    opened, closed, plans, equity = [], [], [], []
    pending = None
    for index, row in enumerate(rows):
        if pending is not None:
            action, units, plan = pending
            if action == 'buy':
                price = row['open'] * (1 + options['spread_rate'])
                cost = units * price * (1 + fee_rate)
                if cost <= cash:
                    cash -= cost
                    opened.append({'size': units, 'entry_price': price, 'entry_bar': index,
                                   'tag': 'intent-' + str(index - 1)})
                    plan['filled_units'] = units
            else:
                remaining = units
                closed_at_open = []
                for lot in list(opened):
                    size = min(remaining, lot['size'])
                    if not size:
                        break
                    price = row['open']
                    commission = size * lot['entry_price'] * fee_rate + size * price * fee_rate
                    closed_at_open.append({**lot, 'size': size, 'exit_bar': index, 'exit_price': price,
                                           'commission': commission,
                                           'pnl': size * (price - lot['entry_price']) - commission})
                    cash += size * price * (1 - fee_rate)
                    lot['size'] -= size
                    remaining -= size
                    if lot['size'] == 0:
                        opened.remove(lot)
                require(remaining == 0, 'CLOSE_UNITS_EXCEED_POSITION')
                # Preserve the existing report's ordering for simultaneous legs;
                # selection and allocation above remain oldest-lot first.
                closed.extend(reversed(closed_at_open))
                plan['filled_units'] = units
            plan['reason'] = 'TRUSTED_FILL_OBSERVED' if plan['filled_units'] else 'INSUFFICIENT_CASH'
            pending = None
        quantity = sum(lot['size'] for lot in opened)
        equity.append(cash + quantity * row['close'])
        if index == 0:
            continue
        for intent in intents[index]:
            plan = {'decision_bar': index, 'decision_at': row['at'], 'decision_price': row['close'],
                    'action': intent['action'], 'planned_units': intent['quantity'], 'filled_units': 0,
                    'reason': 'END_OF_DATA_NO_NEXT_OPEN' if index + 1 == len(rows) else 'NO_TRUSTED_FILL_OBSERVED'}
            plans.append(plan)
            if pending is None and (intent['action'] == 'buy' or quantity):
                if intent['action'] == 'close':
                    require(intent['quantity'] <= quantity, 'CLOSE_UNITS_EXCEED_POSITION')
                pending = (intent['action'], intent['quantity'], plan)
    traded_notional = sum(abs(t['size']) * (t['entry_price'] + t['exit_price']) for t in closed)
    traded_notional += sum(abs(t['size']) * t['entry_price'] for t in opened)
    require(all(math.isfinite(v) and v >= 0 for v in equity), 'REPLAY_EQUITY_INVALID')
    require(cash >= 0 and math.isfinite(cash), 'TRUSTED_CASH_RECONCILIATION_FAILED')
    state = {'cash': cash, 'equity': equity[-1], 'quantity': quantity}
    return {'equity': equity, 'closed_trades': closed, 'open_positions': opened, 'state': state, 'plans':plans,'initial_cash':options['cash'],
            'bar_index_basis':'INCLUDES_ONE_NO_ORDER_WARMUP_BAR_BEFORE_VALID',
            'actual_final_weight':quantity*rows[-1]['close']/equity[-1] if equity[-1]>0 else None,
            'turnover': traded_notional / options['cash'],
            'fees': sum(t['commission'] for t in closed) + sum(t['size'] * t['entry_price'] * options['fee_rate'] for t in opened),
            'engine': REPLAY_ENGINE, 'cost_model': COST_MODEL,
            'settlement': 'INDEPENDENT_VALID_EMPTY_ACCOUNT_NEXT_OPEN_NO_FORCED_FINAL_CLOSE'}


def target_intent(target, state, close):
    """Target allocation at decision close; actual order fills next open."""
    require(type(target) in {int, float} and math.isfinite(target) and 0 <= target <= 1, 'TARGET_FRACTION_INVALID')
    desired = math.floor(target*state['equity']/close)
    difference = desired-int(state['quantity'])
    if difference > 0:
        return [{'action': 'buy', 'quantity': difference}]
    if difference < 0:
        return [{'action': 'close', 'quantity': -difference}]
    return []


def account_state(rows, outputs, states, train_end, options, index):
    initial = {'cash': options['cash'], 'equity': options['cash'], 'quantity': 0}
    if index <= train_end:
        return initial
    intentions = [[]] + [target_intent(outputs[i]['target_fraction'], states[i], rows[i]['close']) for i in range(train_end, index)] + [[]]
    return replay(rows[train_end-1:index+1], intentions, options)['state']


def evaluate(rows_by_symbol, causal_by_symbol, *, kind, train_end, options, scoring, frozen_thresholds=None, final=False):
    require(type(final) is bool and (not final or type(frozen_thresholds) is dict), "FINAL_FROZEN_PREPROCESSING_REQUIRED")
    from importlib.metadata import version
    require(version('pyqlib') == '0.9.8', 'QLIB_VERSION_MISMATCH')
    import pandas as pd
    import numpy as np
    from qlib.contrib.eva.alpha import calc_ic
    require(set(rows_by_symbol) == set(causal_by_symbol) and 1 <= len(rows_by_symbol) <= 8, 'SYMBOL_BINDING_INVALID')
    results = {}; baselines = {}; pairs = []; per_symbol = []; valid_calendar = None
    sleeves=sleeve_options(options,rows_by_symbol)
    for symbol, rows in sorted(rows_by_symbol.items()):
        causal = causal_by_symbol[symbol]
        require(causal['rows_hash'] == digest(rows) and len(causal['outputs']) == len(rows), 'CAUSAL_RECEIPT_MISMATCH')
        require(80 <= train_end <= len(rows)-40, 'INSUFFICIENT_TRAIN_VALID_HISTORY')
        dates = [r['at'] for r in rows[train_end:]]
        require(valid_calendar is None or dates == valid_calendar, 'COMPARISON_CALENDAR_MISMATCH')
        valid_calendar = dates
        if kind == 'factor':
            values = [p['values'][0] for p in causal['outputs']]
            finite_train = [v for v in values[:train_end] if v is not None and math.isfinite(v)]
            require(final or len(finite_train) >= 40, 'INSUFFICIENT_TRAIN_FACTOR_VALUES')
            if final:
                require(symbol in frozen_thresholds and type(frozen_thresholds[symbol]) in {int,float}
                        and math.isfinite(frozen_thresholds[symbol]), 'FINAL_THRESHOLD_MISSING')
                threshold = float(frozen_thresholds[symbol])
            else:
                threshold = float(np.median(finite_train))  # train only, no labels
            # Factor targets also use the same trusted account feedback and
            # integer-unit planner as policy targets, never fake proportion fills.
            targets = [.999 if v is not None and v > threshold else 0.0 for v in values]
            states = []; intents = []
            for i, target in enumerate(targets):
                state = account_state(rows, [{'target_fraction':t} for t in targets[:i]], states, train_end,
                                      sleeves[symbol], i)
                states.append(state)
                if i >= train_end:
                    intents.append(target_intent(target,state,rows[i]['close']))
            ts_x = []; ts_y = []
            # Label starts next open and matures at following open. Last two
            # observations excluded, no cross-boundary train labels exported.
            for i in range(train_end, len(rows)-2):
                if values[i] is not None and math.isfinite(values[i]):
                    label = rows[i+2]['open'] / rows[i+1]['open'] - 1
                    pairs.append((rows[i]['at'], symbol, values[i], label))
                    ts_x.append(values[i]); ts_y.append(label)
            reason = None
            if len(ts_x) < 20 or len(set(ts_x)) < 2 or len(set(ts_y)) < 2:
                ts_ic = ts_rank = None; reason = 'INSUFFICIENT_PAIRS_OR_ZERO_VARIANCE'
            else:
                ts_ic = float(pd.Series(ts_x).corr(pd.Series(ts_y)))
                ts_rank = float(pd.Series(ts_x).corr(pd.Series(ts_y), method='spearman'))
            per_symbol.append({'symbol': symbol, 'axis': 'TIME_SERIES', 'count': len(ts_x), 'ic': ts_ic,
                               'rank_ic': ts_rank, 'null_reason': reason, 'train_threshold': threshold})
        else:
            intents = [target_intent(p['target_fraction'], causal['states'][i], rows[i]['close'])
                       for i, p in enumerate(causal['outputs']) if i >= train_end]
        sleeve = sleeves[symbol]
        results[symbol] = replay(rows[train_end-1:], [[]]+intents, sleeve)
        baseline_intents = [[] for _ in dates]
        baseline_intents[0] = target_intent(.999, {'cash':sleeve['cash'],'equity':sleeve['cash'],'quantity':0},rows[train_end]['close'])
        baselines[symbol] = replay(rows[train_end-1:], [[]]+baseline_intents, sleeve)
        results[symbol]['equity'] = results[symbol]['equity'][1:]
        baselines[symbol]['equity'] = baselines[symbol]['equity'][1:]
    ic = rank = None; by_date = []
    if kind == 'factor' and pairs:
        table = pd.DataFrame(pairs, columns=['datetime', 'instrument', 'factor', 'label'])
        table['datetime'] = pd.to_datetime(table['datetime'])
        table = table.set_index(['datetime', 'instrument']).sort_index()
        # No pooled time/asset correlation masquerading as daily CS IC.
        valid_dates = []
        for at, group in table.groupby(level='datetime'):
            valid = len(group) >= 3 and group['factor'].nunique() > 1 and group['label'].nunique() > 1
            by_date.append({'at': at.date().isoformat(), 'count': len(group), 'eligible': valid,
                            'ic':None,'rank_ic':None,
                            'null_reason': None if valid else 'CS_MINIMUM3_OR_ZERO_VARIANCE'})
            if valid:
                valid_dates.append(at)
        selected = table.loc[table.index.get_level_values('datetime').isin(valid_dates)]
        if not selected.empty:
            daily_ic, daily_rank = calc_ic(selected['factor'], selected['label'])
            for row in by_date:
                at = pd.Timestamp(row['at'])
                row['ic'] = float(daily_ic.loc[at]) if at in daily_ic.index else None
                row['rank_ic'] = float(daily_rank.loc[at]) if at in daily_rank.index else None
            if len(daily_ic.dropna()) >= 20:
                ic = float(daily_ic.mean()); rank = float(daily_rank.mean())
    equity = [sum(r['equity'][i] for r in results.values()) for i in range(len(valid_calendar))]
    benchmark = [sum(r['equity'][i] for r in baselines.values()) for i in range(len(valid_calendar))]
    peaks = np.maximum.accumulate(equity)
    drawdown = max((p-v)/p for p, v in zip(peaks, equity) if p > 0)
    returns = equity[-1]/options['cash']-1
    ts_available = [r['ic'] for r in per_symbol if r['ic'] is not None]
    metrics = {'return_fraction': returns, 'max_drawdown_fraction': drawdown, 'cs_ic': ic,
               'ts_ic': sum(ts_available)/len(ts_available) if ts_available else None,
               'turnover': sum(r['turnover']*r['initial_cash'] for r in results.values())/options['cash']}
    excess_ratio = [v/b for v, b in zip(equity, benchmark)]
    excess_peak = np.maximum.accumulate(excess_ratio)
    native_metrics = {'1day.excess_return_with_cost.annualized_return': excess_ratio[-1]**(252/(len(equity)-1))-1,
                      '1day.excess_return_with_cost.max_drawdown': -max((p-v)/p for p, v in zip(excess_peak, excess_ratio))}
    if ic is not None:
        native_metrics['IC'] = ic
    return {'schema': 'fa-trusted-sealed-final-evaluation-1' if final else 'fa-trusted-development-evaluation-1',
            'engine': REPLAY_ENGINE, 'cost_model': COST_MODEL, 'metrics': metrics, 'score': score(metrics, scoring),
            'cs_ic': {'axis': 'CROSS_SECTIONAL', 'ic': ic, 'rank_ic': rank, 'daily': by_date,
                      'null_reason': None if ic is not None else 'INSUFFICIENT_VALID_DATES_OR_MEMBERS'},
            'ts_ic': per_symbol, 'equity': equity, 'sleeves': results, 'benchmark_equity': benchmark,
            'native_feedback_metrics': native_metrics,
            'native_feedback_mapping':'KEY_COMPATIBILITY_FOR_RDAGENT_IMPORTANT_METRICS; geometric excess is locally adapted, not Qlib native cumulative-sum portfolio backtest',
            'annualization': '252_OBSERVED_SESSION_GEOMETRIC_EXCESS_RATIO_NOT_QLIB_CUMSUM_CHART',
            'training': {'last': train_end-1, 'preprocessing': 'FROZEN_DEVELOPMENT_THRESHOLD_NO_FINAL_FIT' if final else 'TRAIN_MEDIAN_ONLY_FACTOR_THRESHOLD', 'labels_exported_to_generation': False},
            'development': {'first': valid_calendar[0], 'last': valid_calendar[-1], 'adaptive_exposed': not final},
            'label': {'entry': 'NEXT_OPEN', 'exit': 'FOLLOWING_OPEN', 'horizon_sessions': 1, 'purge_tail': 2},
            'claim': 'INDEPENDENT_SEALED_FINAL_ENGINEERING_NOT_FORMAL_OR_PIT' if final else 'SYNTHETIC_ENGINEERING_OR_EXPLORATORY_DEVELOPMENT_NOT_FINAL_OR_PIT'}
