"""Numeric-only diagnostics after causal execution; no generated code loading."""
import math
from .protocol import digest, require


def training_signature(causal, train_end):
    values = {s: v['outputs'][:train_end] for s, v in sorted(causal.items())}
    return digest(values)


def redundancy(signature, history):
    matches = [h.get('source_sha256') for h in history
               if h.get('training_output_sha256') == signature and h.get('source_sha256')]
    return {'method': 'EXACT_TRAIN_OUTPUT_SHA256', 'training_output_sha256': signature,
            'matching_source_sha256': sorted(set(matches)), 'exact_duplicate': bool(matches),
            'selection_action': 'INFORMATION_ONLY_NO_BUDGET_REFUND_OR_AUTOMATIC_PROMOTION',
            'correlation_redundancy': 'NOT_COMPUTED'}


def quantile_returns(rows_by_symbol, causal, train_end, *, quantiles=5):
    require(type(quantiles) is int and 2<=quantiles<=5, 'QUANTILES_INVALID')
    dates = {}
    for symbol, rows in rows_by_symbol.items():
        outputs = causal[symbol]['outputs']
        for i in range(train_end, len(rows)-2):
            value = outputs[i]['values'][0]
            if value is None or not math.isfinite(value): continue
            label = rows[i+2]['open']/rows[i+1]['open']-1
            dates.setdefault(rows[i]['at'], []).append((value, symbol, label))
    daily = []
    for at, observations in sorted(dates.items()):
        observations.sort()
        if len(observations)<quantiles or len({v[0] for v in observations})<quantiles:
            daily.append({'at':at,'returns':None,'reason':'INSUFFICIENT_MEMBERS_OR_DISTINCT_FACTORS'})
            continue
        groups = [[] for _ in range(quantiles)]
        # Equal-valued factors never receive different ranks based on ticker.
        ranks = {v:i for i,v in enumerate(sorted({x[0] for x in observations}))}
        for value, _, label in observations:
            group = min(quantiles-1, ranks[value]*quantiles//len(ranks))
            groups[group].append(label)
        returns = [sum(g)/len(g) if g else None for g in groups]
        daily.append({'at':at,'returns':returns,'counts':[len(g) for g in groups],'reason':None})
    eligible = [d for d in daily if d['returns'] is not None and all(v is not None for v in d['returns'])]
    means = [sum(d['returns'][i] for d in eligible)/len(eligible) for i in range(quantiles)] if len(eligible)>=20 else None
    return {'schema':'factor-quantile-diagnostics-1','quantiles':quantiles,'daily':daily,'mean_returns':means,
            'eligible_dates':len(eligible),'null_reason':None if means is not None else 'MINIMUM20_ELIGIBLE_DATES_REQUIRED',
            'label':'NEXT_OPEN_TO_FOLLOWING_OPEN','scope':'DEVELOPMENT_ONLY','costs':'GROSS_FORWARD_LABEL_NOT_TRADABLE_NET_PORTFOLIO',
            'long_short_spread':means[-1]-means[0] if means is not None else None}
