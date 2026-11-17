"""Thin SDK-snapshot projection; never parse HTTP, decode JS or compute qfq.

Numbers describe the SDK result, not independently certified source rows.
The trusted parent recomputes this projection before any capture is archived.
"""
from datetime import date
from decimal import Decimal
import hashlib
import math
import re

from finance_platform.core.contracts import parse_instant
from finance_platform.data.contracts import DataError
from .contracts import number
from .history_contracts import (NATIVE_INTERFACE, NATIVE_SOURCE_ID,
                                NATIVE_PRICE_POLICY, QUANTITY_BASIS)

COLUMNS = ('date', 'open', 'high', 'low', 'close', 'volume')
FLAGS = ['SDK_NATIVE_QFQ_FLOAT64_ROUND4', 'UPSTREAM_FULL_FRAME_FFILL_THEN_BFILL',
         'NO_INDEPENDENT_RAW_ROW_OR_FACTOR_CHAIN_CERTIFICATION',
         'PRICE_PATH_ONLY_NO_DIVIDEND_CREDIT_NO_DOUBLE_SPLIT',
         'VOLUME_UNIT_AND_COMPANY_ACTION_BASIS_UNVERIFIED']


def invalid():
    raise DataError('AKSHARE_NATIVE_CAPTURE_INVALID')


def _digest(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        invalid()
    return value


def _day(value):
    if type(value) is not str or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        invalid()
    return value


def _scalar(value):
    if type(value) is not dict:
        invalid()
    if set(value) == {'kind', 'hex'} and value['kind'] == 'binary64':
        if type(value['hex']) is not str or len(value['hex']) > 32:
            invalid()
        binary = float.fromhex(value['hex'])
        if not math.isfinite(binary) or binary.hex() != value['hex']:
            invalid()
        # repr is a round-tripping rendering of the SDK's binary64 result.
        # No rounding, factor adjustment or raw lexical precision claim.
        return number(repr(binary))
    if set(value) == {'kind', 'value'} and value['kind'] == 'integer':
        if type(value['value']) is not str or re.fullmatch(r'-?(?:0|[1-9][0-9]{0,23})', value['value']) is None:
            invalid()
        return number(value['value'])
    invalid()


def _entities(call, entities):
    if type(entities) is not list or len(entities) != 2:
        invalid()
    paths = {'sina_us_full_history': '/staticdata/us/' + call['provider_symbol'],
             'sina_qfq_factors': '/us_stock/company/reinstatement/' + call['provider_symbol'] + '_qfq.js'}
    refs = []
    for index, entity in enumerate(entities):
        keys = {'role', 'origin', 'path', 'parameters', 'body', 'observed_received_at',
                'entity_sha256', 'entity_bytes', 'decoded_entity_bytes', 'content_decoding', 'decoding'}
        if type(entity) is not dict or set(entity) not in (keys, keys | {'artifact'}):
            invalid()
        if 'artifact' in entity and entity['artifact'] != f'entity-{index}.json':
            invalid()
        role = entity.get('role')
        if (role not in paths or entity.get('origin') != 'finance.sina.com.cn'
                or entity.get('path') != paths[role] or entity.get('parameters') != {}):
            invalid()
        raw = entity.get('body')
        if (type(raw) is not bytes or not 0 < len(raw) <= (512*1024 if role == 'sina_qfq_factors' else 2*1024**2)
                or entity.get('entity_sha256') != hashlib.sha256(raw).hexdigest()
                or type(entity.get('entity_bytes')) is not int or entity['entity_bytes'] != len(raw)
                or type(entity.get('decoded_entity_bytes')) is not int or entity['decoded_entity_bytes'] != len(raw)):
            invalid()
        decoding = entity['decoding']
        base = {'http_framing', 'framing_control_bytes'}
        gzip = {'content_decoding', 'content_encoding', 'encoded_entity_bytes', 'decoded_entity_bytes',
                'encoded_entity_sha256', 'encoded_entity_archived'}
        if (type(decoding) is not dict or set(decoding) not in (base, base | gzip)
                or decoding['http_framing'] not in {'IDENTITY', 'CHUNKED', 'CONTENT_LENGTH', 'CONNECTION_CLOSE'}
                or type(decoding['framing_control_bytes']) is not int or not 0 <= decoding['framing_control_bytes'] <= 32768*8):
            invalid()
        if set(decoding) == base:
            if entity['content_decoding'] != 'IDENTITY_ENTITY_NOT_TLS_WIRE':
                invalid()
        else:
            if (entity['content_decoding'] != 'GZIP_DECODED_ENTITY_NOT_TLS_WIRE'
                    or decoding['content_decoding'] != entity['content_decoding'] or decoding['content_encoding'] != 'gzip'
                    or type(decoding['encoded_entity_bytes']) is not int or not 0 < decoding['encoded_entity_bytes'] <= 2*1024**2
                    or type(decoding['decoded_entity_bytes']) is not int or decoding['decoded_entity_bytes'] != len(raw)
                    or decoding['encoded_entity_archived'] is not False):
                invalid()
            _digest(decoding['encoded_entity_sha256'])
        parse_instant(entity['observed_received_at'])
        refs.append({'entity': index, 'role': role, 'entity_sha256': entity['entity_sha256']})
    if {e['role'] for e in entities} != set(paths):
        invalid()
    return refs, max((e['observed_received_at'] for e in entities), key=parse_instant)


def _rows(call, snapshot, entities):
    if (call.get('provider') != 'akshare' or call.get('interface') != NATIVE_INTERFACE
            or type(call.get('provider_symbol')) is not str
            or re.fullmatch(r'[A-Z][A-Z0-9]{0,17}', call['provider_symbol']) is None
            or call['provider_symbol'] in {'CIEN', 'AI'} or call.get('ticker') != call['provider_symbol']
            or call.get('timeframe') != '1Day'
            or type(call.get('max_rows')) is not int or not 0 < call['max_rows'] <= 9000
            or 'quality_policy_id' in call or 'quality_policy_hash' in call):
        invalid()
    start, end = _day(call['start']), _day(call['end'])
    if not start < end:
        invalid()
    refs, received = _entities(call, entities)
    if (type(snapshot) is not dict or set(snapshot) != {'schema_version', 'columns', 'dtypes', 'rows'}
            or snapshot['schema_version'] != 'sdk-frame-snapshot-1' or snapshot['columns'] != list(COLUMNS)
            or type(snapshot['dtypes']) is not dict or set(snapshot['dtypes']) != set(COLUMNS)
            or type(snapshot['rows']) is not list or not 0 < len(snapshot['rows']) <= call['max_rows']):
        invalid()
    if (type(snapshot['dtypes']['date']) is not str
            or re.fullmatch(r'datetime64\[(?:s|ms|us|ns)\]', snapshot['dtypes']['date']) is None
            or any(snapshot['dtypes'][k] not in {'float64', 'int64'} for k in COLUMNS[1:])):
        invalid()
    result = []
    previous = ''
    for index, item in enumerate(snapshot['rows']):
        if (type(item) is not dict or set(item) != {'sdk_index', 'values'} or item['sdk_index'] != str(index)
                or type(item['values']) is not dict or set(item['values']) != set(COLUMNS)):
            invalid()
        values = item['values']
        label = values['date']
        if (type(label) is not dict or set(label) != {'kind', 'value'} or label['kind'] != 'date_label'
                or type(label['value']) is not str or len(label['value']) != 19
                or not label['value'].endswith(' 00:00:00')):
            invalid()
        day = _day(label['value'][:10])
        if not start <= day < end or day <= previous:
            invalid()
        previous = day
        prices = {k: _scalar(values[k]) for k in ('open', 'high', 'low', 'close')}
        numbers = {k: Decimal(v) for k, v in prices.items()}
        volume = _scalar(values['volume'])
        if (any(v <= 0 for v in numbers.values()) or numbers['high'] < max(numbers.values())
                or numbers['low'] > min(numbers.values()) or Decimal(volume) < 0):
            invalid()
        result.append({
            'existing_instrument_id': call['existing_instrument_id'], 'provider_symbol': call['provider_symbol'],
            'session_date': day, 'source_timestamp': None, 'source_timestamp_timezone': None,
            **prices, 'volume': volume, 'source_reported_volume': volume, 'currency': 'USD',
            'price_unit': 'USD_per_adjusted_simulation_unit', 'volume_unit': 'UNVERIFIED',
            'volume_share_basis': 'UPSTREAM_UNADJUSTED_COMPANY_ACTION_BASIS_UNVERIFIED',
            'currency_and_unit_evidence': 'SDK_REQUESTED_US_PRICE_PATH_NOT_PER_ROW_CERTIFIED',
            'price_basis': 'PROVIDER_OBSERVED', 'raw_compatible': False,
            'price_policy': NATIVE_PRICE_POLICY, 'quantity_basis': QUANTITY_BASIS,
            'source_adjustment_request': {'symbol': call['provider_symbol'], 'adjust': 'qfq'},
            'provider_basis_claim': 'AKSHARE_NATIVE_PANDAS_FLOAT64_ROUND4_FULL_FRAME_FILL',
            'basis_verification': 'SDK_RESULT_ONLY_NO_INDEPENDENT_RAW_ROW_OR_FACTOR_PROOF',
            'provider_identity_verification': 'EXPLICIT_USER_BINDING_NOT_EXTERNALLY_CERTIFIED',
            'basis_reference_time': None, 'observed_received_at': received,
            'availability_basis': 'OBSERVED_RECEIPT_ONLY', 'historical_first_available_at': None,
            'completeness': 'UNKNOWN', 'sdk_transform_flags': list(FLAGS),
            'raw_row_locator': None, 'source_entity_refs': refs,
            'sdk_row_locator': {'artifact': 'sdk-frame.json', 'row': index}, 'source_adj_close': None,
        })
    return result


def native_rows(call, snapshot, entities):
    """Project already selected SDK output. Do not repair or drop any row."""
    try:
        return _rows(call, snapshot, entities)
    except DataError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError):
        invalid()


def validate_native_capture(call, manifest, artifacts, snapshot, rows):
    """Parent-side metadata/row binding; this does not execute or certify SDK."""
    try:
        from .sina_native import RELEASE, UPSTREAM_RELEASE, GUARDED_MODULE_SHA256, CONS_SHA256, WHEEL_SHA256, ADAPTATION
        sdk = manifest['sdk_ref']
        expected = {'name': 'akshare', 'provider': 'akshare', 'version': RELEASE,
                    'upstream_version': UPSTREAM_RELEASE, 'source_id': NATIVE_SOURCE_ID,
                    'interface': NATIVE_INTERFACE, 'price_policy': NATIVE_PRICE_POLICY,
                    'adaptation': ADAPTATION, 'official_wheel_sha256': WHEEL_SHA256,
                    'guarded_module_sha256': GUARDED_MODULE_SHA256, 'cons_sha256': CONS_SHA256,
                    'calculation': 'UPSTREAM_PANDAS_FLOAT64_ROUND4_V1',
                    'fill': 'UPSTREAM_FULL_FRAME_FFILL_THEN_BFILL', 'volume_unit': 'UNVERIFIED',
                    'volume_adjustment': 'UPSTREAM_UNADJUSTED',
                    'requested_window': {key: call[key] for key in ('start', 'end')}}
        rest = {'artifact_sha256', 'runtime', 'pandas_version', 'full_history_rows',
                'selected_window_rows', 'full_history_start', 'full_history_end'}
        if type(sdk) is not dict or set(sdk) != set(expected) | rest or any(sdk[k] != v for k, v in expected.items()):
            invalid()
        _digest(sdk['artifact_sha256'])
        runtime = sdk['runtime']
        hash_fields = {'artifact_sha256', 'modules_manifest_sha256', 'native_dll_sha256', 'icu_sha256'}
        if (type(runtime) is not dict or set(runtime) != hash_fields | {'name', 'version'}
                or runtime['name'] != 'mini-racer' or runtime['version'] != '0.14.1'):
            invalid()
        for field in hash_fields:
            _digest(runtime[field])
        if type(sdk['pandas_version']) is not str or re.fullmatch(r'[0-9][0-9A-Za-z.+-]{0,31}', sdk['pandas_version']) is None:
            invalid()
        if (type(sdk['full_history_rows']) is not int or not len(rows) <= sdk['full_history_rows'] <= 40000
                or type(sdk['selected_window_rows']) is not int or sdk['selected_window_rows'] != len(rows)):
            invalid()
        a, b = _day(sdk['full_history_start']), _day(sdk['full_history_end'])
        entities = []
        if manifest.get('status') != 'SUCCEEDED' or manifest.get('error_code') is not None:
            invalid()
        for entity in manifest['entities']:
            entities.append({**entity, 'body': artifacts[entity['artifact']]})
        expected_rows = native_rows(call, snapshot, entities)
        if not a <= expected_rows[0]['session_date'] <= expected_rows[-1]['session_date'] <= b or rows != expected_rows:
            invalid()
        return rows
    except DataError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError):
        invalid()
