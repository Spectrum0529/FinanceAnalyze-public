"""Thin Massive workflow using existing SDK, queue, chunk cache and library."""
from datetime import datetime, timezone
from types import SimpleNamespace

from .massive_control import MassiveError as DataError
from .massive_daily import security_metadata, stream_daily
from .massive_control import PAGE_VALIDATION, daily_units
from .simple_chunks import DatasetChunks, MAX_CHUNKS, MAX_ROWS, FORMAT
from .resume_chunks import promote_unit


def probe(client, context, request):
    """Explicit user-requested diagnostic; not a whole-account plan audit."""
    overview = security_metadata(client, [request['ticker']], request['end'], context=context)
    count, first, last = 0, None, None
    for row in client.list_aggs(ticker=request['ticker'], multiplier=1, timespan='day',
            from_=request['start'], to=request['end'], adjusted=request['adjustment'] == 'split_adjusted', sort='asc', limit=50):
        context.require_active()
        if count >= 7:
            raise DataError('MASSIVE_PROBE_LIMIT')
        stamp = row['timestamp'] if isinstance(row, dict) else row.timestamp
        from zoneinfo import ZoneInfo
        day = datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        if not request['start'] <= day <= request['end'] or (last and day <= last):
            raise DataError('IMPORT_DUPLICATE_OR_UNSORTED_BAR')
        first, last, count = first or day, day, count + 1
    if not count:
        raise DataError('MASSIVE_PROBE_EMPTY')
    return {'provider': 'massive', 'status': 'REQUESTED_PROBE_RETURNED_DATA',
            'requested': {k: request[k] for k in ('ticker', 'start', 'end', 'adjustment')},
            'actual_coverage': {'rows': count, 'first': first, 'last': last},
            'currency': overview['metadata']['currency'], 'other_entitlements': 'UNKNOWN',
            'historical_coverage': 'ONLY_THIS_PROBE', 'market_performance': 'UNVERIFIED'}


def download_daily(runtime, library, control, family, client, context, parameters):
    """Commit only completed bounded units; retries keep family work/budget."""
    from .history_store import HistoryStore
    chunks = DatasetChunks(library.policy, context.run.owner_id)
    overview = control.unit(family, 'overview')
    if overview is None:
        history = HistoryStore(SimpleNamespace(db=runtime.database))
        identities, currencies = [], set()
        for symbol in parameters['universe']['tickers']:
            unit = control.unit(family, 'overview:' + symbol)
            if unit is None:
                unit = security_metadata(client, [symbol], parameters['end'], context=context)
                security = unit['securities'][0]
                saved = history.save_security(context, {**security, 'instrument': dict(security),
                    'observed_received_at': datetime.now(timezone.utc).isoformat()},
                    evidence={'provider': 'massive', 'interface': 'RESTClient.get_ticker_details', 'source_date': parameters['end'],
                              'mapping_policy': 'NEW_SNAPSHOT_NO_TICKER_JOIN'}, namespace='library-massive')
                unit = {**unit, 'identity': {**security, 'identity_reference': {k: saved[k] for k in ('security_id', 'content_hash')}}}
                control.save_unit(family, 'overview:' + symbol, unit, context)
            identities.append(unit['identity']); currencies.add(unit['metadata']['currency'])
        if len(currencies) != 1:
            raise DataError('MULTI_CURRENCY_DATASET_UNSUPPORTED')
        overview = {'metadata': unit['metadata'], 'identities': identities}
        control.save_unit(family, 'overview', overview, context)
    metadata = parameters['metadata'] or overview['metadata']
    if metadata['adjustment'] not in {'raw', 'split_adjusted'} or metadata['price_unit'] != 'per_share' or metadata['volume_unit'] != 'shares':
        raise DataError('IMPORT_UNITS_AMBIGUOUS')
    if metadata['currency'] != overview['metadata']['currency']:
        raise DataError('IMPORT_CURRENCY_MISMATCH')
    if metadata['timezone'] != 'America/New_York':
        raise DataError('IMPORT_PROVIDER_TIMEZONE_MISMATCH')
    units = daily_units(parameters)
    descriptors, coverage, total = [], {}, 0
    source_receipts=[]
    for index, (symbol, start, end) in enumerate(units):
        context.require_active()
        key = symbol + ':' + start + ':' + end
        saved = control.unit(family, key)
        if saved is None:
            saved = stream_daily(client, [symbol], start, end, adjusted=metadata['adjustment'] == 'split_adjusted', context=context, chunks=chunks, allow_empty=True)
            saved['page_validation'] = client.client.require_complete_aggregates()
            saved['observed_received_at']=datetime.now(timezone.utc).isoformat()
            control.save_unit(family, key, saved, context)
        else:
            if saved.get('page_validation') != PAGE_VALIDATION:
                raise DataError('MASSIVE_RESUME_PAGE_UNVERIFIED')
            if saved['row_count']:
                saved=promote_unit(library,context,saved,control.unit_scope(context.run.owner_id,family,key),start=start,end=end,provider='massive')
        # Re-read the exact cached descriptors on every attempt, including reuse.
        binding=library.market.storage.lookup_binding(context.run.owner_id,'job:'+context.job_id)
        read_chunks=DatasetChunks(library.policy,context.run.owner_id,binding=binding)
        observed = [] if saved['row_count'] == 0 and saved['chunks'] == [] else list(read_chunks.stream(saved, start=start, end=end))
        if len(observed) != saved['row_count'] or any(r['ticker'] != symbol for r in observed):
            raise DataError('DATASET_HASH_MISMATCH')
        actual = {'ticker': symbol, 'rows': len(observed), 'first': observed[0]['session_date'] if observed else None, 'last': observed[-1]['session_date'] if observed else None}
        if saved['securities'] != [actual]:
            raise DataError('DATASET_HASH_MISMATCH')
        source_receipts.append({'ticker':symbol,'start':start,'end':end,'observed_received_at':saved.get('observed_received_at'),'published_at':None,'available_at':None})
        total += len(observed); descriptors.extend(saved['chunks'])
        if total > MAX_ROWS or len(descriptors) > MAX_CHUNKS:
            raise DataError('IMPORT_SERVER_ROWS_LIMIT')
        if observed:
            previous = coverage.get(symbol)
            coverage[symbol] = {'ticker': symbol, 'rows': (previous['rows'] if previous else 0) + actual['rows'],
                                'first': previous['first'] if previous else actual['first'], 'last': actual['last']}
        context.checkpoint(min(98, 5 + int(90 * (index + 1) / len(units))),
                           {'resume_id': family, 'completed_units': index + 1, 'total_units': len(units)})
    if sorted(coverage) != parameters['universe']['tickers']:
        raise DataError('IMPORT_PARTIAL_SYMBOL_FAILURE')
    manifest = {'format': FORMAT, 'chunks': descriptors, 'row_count': total, 'securities': list(coverage.values()),
                'source_observations':source_receipts,'status': 'COMPLETE_ITERATORS_EXHAUSTED', 'completeness': 'UNVERIFIED_PROVIDER_RESPONSE', 'missing_sessions': 'NOT_INFERRED'}
    body = {**parameters, 'metadata': metadata, 'rows': None,
            'source': {'provider': 'massive', 'reference': 'RESTClient.list_aggs/day', 'universe_as_of': None},
            'received_at': datetime.now(timezone.utc).isoformat(), 'scope': 'public'}
    return library.import_dataset(context.run.owner_id, body, 'job:' + context.run.run_id,
                                  context=context, security_metadata=overview['identities'], manifest=manifest)
