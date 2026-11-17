"""Selected, accepted, bounded value export to an isolated research executor.

Returns bytes/values only; caller owns fresh read-only input mount creation.
Never exports a Main path, connection credential reference or arbitrary metadata.
"""
from hashlib import sha256
from finance_platform.core.contracts import canonical, fingerprint, identifier
from . import autonomous_receipts as receipts
from .contracts import DataError
from .simple_inputs import metadata, tickers, validate_rows

FORMAT = 'finance-research-input-1'
MAX_EXPORT_ROWS = 200000
MAX_EXPORT_BYTES = 64 * 1024 * 1024
PAYLOAD_FIELDS = {'format','dataset_reference','window','metadata','rows','rows_hash','coverage','provenance','universe',
                  'pit_status','published_at','available_at','factor_publication_times','missing_sessions','return_basis',
                  'corporate_actions','external_cash_flows','limitations'}

def cn_research_market(record):
    """Read eligibility from the immutable data service receipt."""
    source = record['source']['provider']
    provenance = record.get('goal_data_provenance')
    if source == 'openbb_nasdaq':
        raise DataError('GOAL_PLAN_CAPABILITY_UNSUPPORTED')
    if source not in receipts.CN_PROVIDERS:
        if record['metadata']['currency'] != 'USD' or provenance and provenance.get('market') == 'CN':
            raise DataError('CURRENCY_UNSUPPORTED')
        return None
    if record['metadata'] != receipts.CN_METADATA:
        raise DataError('CN_RESEARCH_UNITS_UNSUPPORTED')
    if (type(provenance) is not dict or provenance.get('provider') != source
            or tuple(provenance.get(key) for key in ('market', 'asset', 'kind', 'frequency'))
            != ('CN', 'CN_EQUITY', 'bars', '1Day')):
        raise DataError('CN_RESEARCH_EVIDENCE_REQUIRED')
    try:
        market = receipts.cn_market(provenance.get('research_market'), record['universe']['tickers'])
    except receipts.ReceiptRejected as error:
        raise DataError(str(error)) from None
    if market['required_sessions_sha256'] != provenance.get('required_sessions_sha256'):
        raise DataError('CN_RESEARCH_CALENDAR_INVALID')
    total, extra = divmod(record['quality']['rows'], len(market['securities']))
    if extra or not 80 <= total * 7 // 10 <= total - 40:
        raise DataError('CN_RESEARCH_COVERAGE_INVALID')
    return market


def export_research_input(library, owner, reference, run_id, *, start=None, end=None, context,
                          parse_cache=None, allow_wrds_local=False):
    identifier(owner); identifier(run_id); context.require_active()
    if context.run.owner_id != owner or context.run.run_id != run_id:
        raise DataError('DATASET_NOT_ACCEPTED_FOR_RUN')
    # Existing pin + committed dataset verification precedes any selected read.
    record = library.get(owner, reference['dataset_id'])
    if reference != {k: record[k] for k in ('dataset_id', 'version', 'content_hash')} or run_id not in record['referenced_by']:
        raise DataError('DATASET_NOT_ACCEPTED_FOR_RUN')
    from .wrds import PROFILE as WRDS_PROFILE, SCOPE as WRDS_SCOPE
    licensed = record['scope'] == WRDS_SCOPE
    if licensed and (
            allow_wrds_local is not True or record['source']['provider'] != 'wrds'
            or record.get('row_storage', {}).get('wrds_capture', {}).get('profile')
            != WRDS_PROFILE):
        raise DataError('WRDS_LOCAL_RESEARCH_REQUIRED')
    if not licensed and record['scope'] not in {'synthetic', 'public'}:
        raise DataError('RESEARCH_INPUT_LIMIT')
    market = cn_research_market(record)
    if market is not None and (start is not None or end is not None):
        raise DataError('CN_RESEARCH_FROZEN_WINDOW_REQUIRED')
    parse_options = {} if parse_cache is None else {'parse_cache': parse_cache}
    data=library.read_for_research(owner,reference,run_id,start=start,end=end,context=context,maximum_rows=MAX_EXPORT_ROWS,
                                   **parse_options)
    context.require_active()
    if market is not None and (data['metadata'] != record['metadata']
                               or data['universe']['tickers'] != record['universe']['tickers']):
        raise DataError('CN_RESEARCH_EVIDENCE_REQUIRED')
    rows = data['rows']
    coverage = {}
    for index, row in enumerate(rows):
        if index % 2000 == 0: context.require_active()
        item = coverage.setdefault(row['ticker'], {'ticker': row['ticker'], 'rows': 0, 'first': row['session_date'], 'last': row['session_date']})
        item['rows'] += 1; item['first'] = min(item['first'], row['session_date']); item['last'] = max(item['last'], row['session_date'])
    if sorted(coverage) != data['universe']['tickers']:
        raise DataError('IMPORT_PARTIAL_SYMBOL_FAILURE')
    provider = record['source']['provider']
    if not licensed and provider not in {'csv', 'massive', 'yfinance', 'akshare', 'openbb_cboe'} | receipts.CN_PROVIDERS:
        raise DataError('RESEARCH_INPUT_SOURCE')
    payload = {'format': FORMAT, 'dataset_reference': dict(reference), 'window': data['window'],
               'metadata': data['metadata'], 'rows': rows, 'rows_hash': fingerprint(rows),
               'coverage': [coverage[k] for k in sorted(coverage)],
               'provenance': {'provider': provider, 'source_hash': fingerprint(
                   record['goal_data_provenance'] if market is not None else record['source']),
                              'identity_snapshot_hash': fingerprint(record.get('security_metadata')),
                              'acquisition_hash': fingerprint(data.get('acquisition')),
                              'received_at': record['received_at'], 'scope': record['scope'],
                              'permitted_use': 'LOCAL_PERSONAL_RESEARCH_ONLY', 'license_status': 'NOT_A_REDISTRIBUTION_GRANT'},
               'universe': {'tickers': data['universe']['tickers'], 'membership_kind': 'STATIC_SELECTED_MEMBERS',
                            'historical_constituents': False, 'publication_time': None},
               'pit_status': 'UNVERIFIED', 'published_at': None, 'available_at': None,
               'factor_publication_times': 'NOT_PROVIDED', 'missing_sessions': 'NOT_INFERRED',
               'return_basis': 'price', 'corporate_actions': None, 'external_cash_flows': None,
               'limitations': ['RECEIPT_FREEZE_IS_NOT_PIT', 'HISTORICAL_MEMBERSHIP_UNVERIFIED',
                               'FACTOR_PUBLICATION_UNAVAILABLE', 'NO_DIVIDEND_OR_ACTION_MODEL']}
    if market is not None:
        payload['research_market'] = market
    if licensed:
        payload['limitations'].append('LICENSED_LOCAL_NO_EXTERNAL_ROW_FORWARDING')
    encoded = canonical(payload).encode('utf-8')
    if len(encoded) > MAX_EXPORT_BYTES:
        raise DataError('RESEARCH_INPUT_LIMIT')
    context.require_active()
    package = {'manifest': {'format': FORMAT, 'sha256': sha256(encoded).hexdigest(), 'bytes': len(encoded),
                           'rows': len(rows), 'dataset_reference': dict(reference)}, 'payload': payload}
    if market is not None or licensed:
        verify_research_input(package, allow_cn=market is not None, allow_wrds_local=licensed)
    return package


def verify_research_input(package, *, allow_cn=False, allow_wrds_local=False):
    """Host-side input verification before passing typed JSON to the executor."""
    if type(package) is not dict or set(package) != {'manifest', 'payload'}:
        raise DataError('RESEARCH_INPUT_INVALID')
    body, manifest = package['payload'], package['manifest']
    fields = PAYLOAD_FIELDS | {'research_market'} if type(body) is dict and 'research_market' in body else PAYLOAD_FIELDS
    if type(body) is not dict or set(body) != fields or body.get('format') != FORMAT or type(body.get('rows')) is not list or not 0 < len(body['rows']) <= MAX_EXPORT_ROWS:
        raise DataError('RESEARCH_INPUT_INVALID')
    is_cn = 'research_market' in body
    try:
        metadata(body['metadata'])
        universe = body['universe']
        if (type(universe) is not dict or set(universe) != {'tickers','membership_kind','historical_constituents','publication_time'}
                or type(universe['tickers']) is not list or not universe['tickers']):
            raise DataError('RESEARCH_INPUT_INVALID')
        if not is_cn and tickers(' '.join(universe['tickers'])) != universe['tickers']:
            raise DataError('RESEARCH_INPUT_INVALID')
        if universe['historical_constituents'] is not False or universe['publication_time'] is not None or body['pit_status'] != 'UNVERIFIED' or body['published_at'] is not None or body['available_at'] is not None:
            raise DataError('RESEARCH_INPUT_INVALID')
        previous = None
        for offset in range(0, len(body['rows']), 2000):
            batch = body['rows'][offset:offset+2000]
            if any(row.get('ticker') not in universe['tickers'] for row in batch):
                raise DataError('RESEARCH_INPUT_INVALID')
            validated = validate_rows(batch, body['window']['start'], body['window']['end'], sorted({row['ticker'] for row in batch}),
                                       allow_missing_volume=body['metadata']['volume_unit']=='provider_reported_unverified')
            if validated != batch:
                raise DataError('RESEARCH_INPUT_INVALID')
            for row in batch:
                key = (row['ticker'], row['session_date'])
                if previous is not None and key <= previous:
                    raise DataError('RESEARCH_INPUT_INVALID')
                previous = key
        if sorted({row['ticker'] for row in body['rows']}) != universe['tickers']:
            raise DataError('RESEARCH_INPUT_INVALID')
        encoded = canonical(body).encode('utf-8')
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise DataError('RESEARCH_INPUT_INVALID') from None
    if len(encoded) > MAX_EXPORT_BYTES:
        raise DataError('RESEARCH_INPUT_LIMIT')
    expected = {'format': FORMAT, 'sha256': sha256(encoded).hexdigest(), 'bytes': len(encoded),
                'rows': len(body['rows']), 'dataset_reference': body['dataset_reference']}
    if manifest != expected or body['rows_hash'] != fingerprint(body['rows']):
        raise DataError('DATASET_HASH_MISMATCH')
    provenance = body.get("provenance", {})
    if type(provenance) is not dict:
        raise DataError('RESEARCH_INPUT_INVALID')
    if is_cn or provenance.get("scope") == "licensed_local" or provenance.get("provider") == "wrds":
        try:
            receipts.checked_package(package, allow_cn=allow_cn, allow_wrds_local=allow_wrds_local)
        except receipts.ReceiptRejected as error:
            raise DataError(str(error)) from None
    return encoded
