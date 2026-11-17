"""Pure typed boundary shared by trusted dispatcher and Linux guest.

Content hashes bind values; they do not attest that guest claims are truthful.
"""
import hashlib
import json
import math
import re

MAX_REQUEST = 4 * 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_CODE = 48 * 1024
OHLCV_BINDING = 'fa-ohlcv-by-name-1'
BOOTSTRAP_STDERR_LIMIT = 4096
BOOTSTRAP_DIAGNOSTIC_PREFIX = b'FA_NATIVE_BOOTSTRAP_V1 '
BOOTSTRAP_DIAGNOSTIC_CODES = frozenset({
    'EXECUTOR_BOOTSTRAP_FAILED',
    'EXECUTOR_DAEMON_PROFILE_MISMATCH',
    'EXECUTOR_DAEMON_READY_TIMEOUT',
    'EXECUTOR_SOCKET_IDENTITY_MISMATCH',
    'EXECUTOR_UNIT_IDENTITY_MISMATCH',
    'EXECUTOR_UNIT_OUTPUT_LIMIT',
    'EXECUTOR_UNIT_START_FAILED',
    'EXECUTOR_USER_BUS_IDENTITY_MISMATCH',
    'QA_DAEMON_SOCKET_MISMATCH',
    'QA_DIRECTORY_INVALID',
})


class Rejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Rejected(code)


def validate_ohlcv_binding(value):
    require(type(value) is dict and value.get('ohlcv_binding') == OHLCV_BINDING, 'OHLCV_BINDING_MISMATCH')


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def strict_json(raw, maximum):
    require(type(raw) is bytes and len(raw) <= maximum, 'ENVELOPE_TOO_LARGE')
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda v: require(False, 'NONFINITE_JSON'))
    except (RecursionError, UnicodeError, json.JSONDecodeError) as exc:
        raise Rejected('JSON_INVALID') from exc
    def visit(v, depth=0):
        require(depth <= 24, 'JSON_DEPTH_EXCEEDED')
        if isinstance(v, dict):
            for k, item in v.items():
                require(type(k) is str, 'JSON_KEY_INVALID')
                visit(item, depth + 1)
        elif isinstance(v, list):
            for item in v:
                visit(item, depth + 1)
        elif type(v) is float:
            require(math.isfinite(v), 'NONFINITE_JSON')
    visit(value)
    return value


def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys.split()), 'SCHEMA_INVALID')


def identifier(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) is not None, 'ID_INVALID')


def sha(value):
    require(type(value) is str and re.fullmatch('[a-f0-9]{64}', value) is not None, 'HASH_INVALID')


REQUEST_FIELDS = ('schema session_id experiment_id attempt_id job_id run_id fencing_token reservation_id '
                  'phase kind source source_sha256 input input_sha256 options runtime_sha256')


def validate_request(value):
    exact(value, REQUEST_FIELDS)
    require(value['schema'] == 'fa-experiment-request-1', 'PROTOCOL_UNSUPPORTED')
    for key in ('session_id', 'experiment_id', 'attempt_id', 'job_id', 'run_id', 'reservation_id'):
        identifier(value[key])
    require(type(value['fencing_token']) is int and value['fencing_token'] > 0, 'FENCE_INVALID')
    # Final evaluation is intentionally a DIFFERENT trusted port, never this adaptive worker.
    require(value['phase'] == 'development', 'FINAL_TEST_NOT_EXPORTED_TO_ADAPTIVE_EXECUTOR')
    require(value['kind'] in {'strategy', 'factor'}, 'KIND_UNSUPPORTED')
    require(type(value['source']) is str and 0 < len(value['source'].encode()) <= MAX_CODE, 'SOURCE_SIZE_INVALID')
    sha(value['source_sha256']); sha(value['input_sha256']); sha(value['runtime_sha256'])
    require(hashlib.sha256(value['source'].encode()).hexdigest() == value['source_sha256'], 'SOURCE_HASH_MISMATCH')
    require(digest(value['input']) == value['input_sha256'], 'INPUT_HASH_MISMATCH')
    exact(value['input'], 'scope decision_at rows state')
    require(value['input']['scope'] == 'AUTHORIZED_DEVELOPMENT_PREFIX', 'INPUT_SCOPE_INVALID')
    rows = value['input']['rows']
    require(type(rows) is list and 1 <= len(rows) <= 20000, 'INPUT_ROWS_INVALID')
    previous = None
    from datetime import date
    for row in rows:
        exact(row, 'at open high low close volume')
        require(type(row['at']) is str, 'SESSION_INVALID')
        try:
            require(date.fromisoformat(row['at']).isoformat() == row['at'], 'SESSION_INVALID')
        except ValueError as exc:
            raise Rejected('SESSION_INVALID') from exc
        require(previous is None or previous < row['at'], 'SESSION_ORDER_INVALID')
        previous = row['at']
        for key in ('open', 'high', 'low', 'close', 'volume'):
            require(type(row[key]) in {int, float} and math.isfinite(row[key]), 'NUMBER_INVALID')
            require(row[key] >= 0 if key == 'volume' else row[key] > 0, 'PRICE_INVALID')
        require(row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high'], 'OHLC_INVALID')
    require(value['input']['decision_at'] == rows[-1]['at'], 'DECISION_PREFIX_MISMATCH')
    exact(value['input']['state'], 'cash equity quantity')
    for v in value['input']['state'].values():
        require(type(v) in {int, float} and math.isfinite(v) and v >= 0, 'ACCOUNT_STATE_INVALID')
    exact(value['options'], 'cash fee_rate spread_rate seed')
    for key, low, high in (('cash', 1, 1e9), ('fee_rate', 0, .05), ('spread_rate', 0, .05)):
        v = value['options'][key]
        require(type(v) in {int, float} and math.isfinite(v) and low <= v <= high, 'OPTIONS_INVALID')
    require(type(value['options']['seed']) is int and 0 <= value['options']['seed'] < 2**32, 'SEED_INVALID')
    require(len(encoded(value)) <= MAX_REQUEST, 'ENVELOPE_TOO_LARGE')
    return value


def validate_response(raw, request):
    value = strict_json(raw, MAX_RESPONSE)
    exact(value, 'schema request_sha256 status payload error_code')
    require(value['schema'] == 'fa-experiment-response-1' and value['request_sha256'] == digest(request), 'RECEIPT_BINDING_MISMATCH')
    require(value['status'] in {'SUCCEEDED', 'FAILED'}, 'STATUS_INVALID')
    require(type(value['payload']) is dict, 'PAYLOAD_INVALID')
    if value['status'] == 'FAILED':
        require(value['payload'] == {} and value['error_code'] in {'GENERATED_EXPERIMENT_FAILED', 'OUTPUT_INVALID'}, 'ERROR_INVALID')
        return value
    require(value['error_code'] is None, 'ERROR_INVALID')
    if request['kind'] == 'factor':
        exact(value['payload'], 'values')
        values = value['payload']['values']
        require(type(values) is list and len(values) == 1, 'FACTOR_SHAPE_INVALID')
        for v in values:
            require(v is None or type(v) in {int, float} and math.isfinite(v), 'FACTOR_VALUE_INVALID')
    else:
        exact(value['payload'], 'target_fraction')
        v = value['payload']['target_fraction']
        require(type(v) in {int, float} and math.isfinite(v) and 0 <= v <= 1, 'TARGET_FRACTION_INVALID')
    return value


def validate_final_request(value):
    # Different schema/entry; adaptive validate_request still rejects final.
    require(type(value) is dict and value.get('schema')=='fa-sealed-final-request-1'
            and value.get('phase')=='final_test', 'FINAL_REQUEST_REQUIRED')
    require(type(value.get('input')) is dict, 'FINAL_INPUT_SCOPE_INVALID')
    exact(value, REQUEST_FIELDS + ' seal_sha256')
    sha(value['seal_sha256'])
    require(value['input'].get('scope')=='AUTHORIZED_SEALED_FINAL_PREFIX', 'FINAL_INPUT_SCOPE_INVALID')
    projected=dict(value);projected.pop('seal_sha256')
    projected['schema']='fa-experiment-request-1';projected['phase']='development'
    projected['input']=dict(value['input'],scope='AUTHORIZED_DEVELOPMENT_PREFIX')
    # Reuse numeric/shape/caps validation only, never dispatch to adaptive code.
    require(value['input_sha256']==digest(value['input']), 'INPUT_HASH_MISMATCH')
    projected['input_sha256']=digest(projected['input'])
    validate_request(projected)
    return value
