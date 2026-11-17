"""CONTAINER ONLY arbitrary-code guest. External reviewed isolation mandatory."""
import sys
from .protocol import validate_final_request, validate_request, strict_json, encoded, digest, MAX_REQUEST, validate_response


def execute_generated(request):
    if sys.platform != 'linux':
        raise RuntimeError('LINUX_GUEST_REQUIRED')
    import numpy as np
    import pandas as pd
    from .inputs import ohlcv_frame
    frame = ohlcv_frame(request['input']['rows'])
    np.random.seed(request['options']['seed'])
    scope = {'__name__': 'generated_experiment', '__file__': '/tmp/generated.py'}
    exec(compile(request['source'], '/tmp/generated.py', 'exec'), scope)
    if request['kind'] == 'strategy':
        return {'target_fraction': float(scope['decide'](frame.copy(deep=True), dict(request['input']['state'])))}
    values = scope['compute'](frame.copy(deep=True))
    if len(values) != len(frame):
        raise ValueError('FACTOR_SHAPE_INVALID')
    last = list(values)[-1]
    return {'values': [None if pd.isna(last) else float(last)]}


def main():
    if sys.platform != 'linux':
        raise RuntimeError('LINUX_GUEST_REQUIRED')
    with open('/input/request.json', 'rb') as stream:
        value = strict_json(stream.read(MAX_REQUEST+1), MAX_REQUEST)
        validator = validate_final_request if value.get('schema')=='fa-sealed-final-request-1' else validate_request
        request = validator(value)
    try:
        response = {'schema': 'fa-experiment-response-1', 'request_sha256': digest(request),
                    'status': 'SUCCEEDED', 'payload': execute_generated(request), 'error_code': None}
        validate_response(encoded(response), request)
    except Exception:
        response = {'schema': 'fa-experiment-response-1', 'request_sha256': digest(request),
                    'status': 'FAILED', 'payload': {}, 'error_code': 'GENERATED_EXPERIMENT_FAILED'}
    # Prints by arbitrary code corrupt strict transport and MUST fail closed.
    sys.stdout.buffer.write(encoded(response))


if __name__ == '__main__':
    main()
