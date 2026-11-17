"""Small, fixed-endpoint NYSE reference evaluation captures (not S02.04 storage)."""
import json
import re
import time
from uuid import uuid4

from predictor.data.acquisition import AcquisitionError, _immutable, _target, http_transport, json_bytes, sha
from predictor.logging import utc_timestamp

ACTION_BASE = 'https://ftp.nyse.com/Corporate%20Actions%20Data%20Samples/CORPORATE%20ACTIONS%20OF%20NYSE%20GROUP%20LISTINGS/'
MASTER_BASE = 'https://ftp.nyse.com/Reference%20Data%20Samples/NYSE%20GROUP%20SECURITY%20MASTER/'


def validate_request(request):
    if set(request) != {'kind', 'filename', 'sample_date'}:
        raise AcquisitionError('Explicit reference request required')
    from datetime import date
    day = date.fromisoformat(request['sample_date'])
    patterns = {
        'master': r'NYSEGROUP_US_REF_SECURITYMASTER_EQUITY_4\.0_([0-9]{8})\.txt',
        'ticker': r'EQY_US_NYSE-MKT_REF_TICKERNOTICES_([0-9]{8})\.xml',
        'info': r'EQY_US_NYSE_MKT_REF_INFONOTICES_([0-9]{8})\.xml',
        'distribution': r'EQY_US_NYSEGROUP_DISTRIBUTIONS_([0-9]{8})_154500\.xml'}
    match = re.fullmatch(patterns.get(request['kind'], '(?!)'), request['filename'])
    if not match or match[1] != day.strftime('%Y%m%d'):
        raise AcquisitionError('Reference sample endpoint/date rejected')
    return (MASTER_BASE if request['kind'] == 'master' else ACTION_BASE) + request['filename']


def capture_reference(policy, directory, request, *, transport=http_transport, sleep=time.sleep):
    url = validate_request(request)
    directory = _target(policy, directory)
    context = {**request, 'source_url': url, 'scope': 'PUBLIC_EVALUATION_ONLY', 'max_bytes': 16 * 1024 * 1024}
    _immutable(policy, directory / 'request.json', json_bytes(context))
    if (directory / 'complete.json').exists():
        return replay_reference(policy, directory)[1]
    for attempt in range(3):
        sleep(1 if attempt == 0 else 2 ** attempt)
        event = {'attempt': attempt + 1, 'requested_at_utc': utc_timestamp()}
        try:
            response = transport(url, {'User-Agent': 'FinanceAnalyze reference sample evaluation',
                'Accept-Encoding': 'identity'}, max_bytes=context['max_bytes'], timeout=30)
            event.update(status=response.status, received_at_utc=utc_timestamp())
        except TimeoutError:
            event.update(status=None, received_at_utc=utc_timestamp())
            response = None
        _immutable(policy, directory / ('attempt-' + uuid4().hex + '.json'), json_bytes(event))
        if response is None or response.status in {408, 429, 500, 502, 503, 504}:
            if attempt == 2:
                raise AcquisitionError('Reference retry budget exhausted')
            continue
        if response.status != 200 or not response.body or len(response.body) > context['max_bytes']:
            raise AcquisitionError('Invalid reference HTTP response')
        body = response.body
        _immutable(policy, directory / (sha(body) + '.bin'), body)
        length = response.headers.get('content-length')
        if length is not None and (not length.isdigit() or int(length) != len(body)):
            raise AcquisitionError('Reference length mismatch')
        manifest = {**context, 'raw_response_hash': sha(body), 'byte_count': len(body),
            'response_received_at': event['received_at_utc'], 'source_publication_at': None,
            'historical_first_available_at': None, 'response_headers': {k: response.headers[k] for k in
                ('etag', 'last-modified', 'content-length', 'content-type') if k in response.headers},
            'license_status': 'PUBLIC_SAMPLE_EVALUATION_BROADER_RIGHTS_UNVERIFIED'}
        _immutable(policy, directory / 'response.bin', body)
        _immutable(policy, directory / 'complete.json', json_bytes(manifest))
        return manifest


def replay_reference(policy, directory):
    directory = policy.authorize(directory)
    context = json.loads(policy.read_bytes(directory / 'request.json'))
    manifest = json.loads(policy.read_bytes(directory / 'complete.json'))
    request = {key: context[key] for key in ('kind', 'filename', 'sample_date')}
    url = validate_request(request)
    if context != {**request, 'source_url': url, 'scope': 'PUBLIC_EVALUATION_ONLY', 'max_bytes': 16 * 1024 * 1024}:
        raise AcquisitionError('Reference context rejected')
    raw = policy.read_bytes(directory / 'response.bin')
    if (sha(raw) != manifest['raw_response_hash'] or len(raw) != manifest['byte_count']
            or not 0 < len(raw) <= context['max_bytes'] or any(manifest.get(k) != v for k, v in context.items())
            or policy.read_bytes(directory / (sha(raw) + '.bin')) != raw):
        raise AcquisitionError('Reference replay integrity failed')
    return raw, manifest
