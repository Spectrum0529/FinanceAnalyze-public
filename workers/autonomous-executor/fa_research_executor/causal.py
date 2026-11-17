"""Trusted Linux controller: one fresh closed guest per decision prefix.

Whole development snapshot stays HERE. Generated source never sees future rows,
labels or trusted evaluator memory. No code execution happens in this module.
"""
from copy import deepcopy
from .protocol import validate_request, validate_response, digest, require


def collect(base_request, full_rows, execute, *, active, reserve_prefix, state_provider,on_completed=None):
    require(type(full_rows) is list and len(full_rows) <= 2000, 'INITIAL_CAUSAL_ROW_CAP')
    output = []
    prefix_receipts = []; states = []
    for index, row in enumerate(full_rows):
        active()
        request = deepcopy(base_request)
        state = state_provider(index, output, states)
        request['input'] = {'scope': 'AUTHORIZED_DEVELOPMENT_PREFIX', 'decision_at': row['at'], 'rows': full_rows[:index+1], 'state': state}
        request['input_sha256'] = digest(request['input'])
        validate_request(request)
        # Must be durable before every container call, including failures.
        reserve_prefix(index, digest(request))
        raw, closure = execute(request, active)
        require(closure == {'request_sha256': digest(request), 'writers_closed': True, 'owned_tree_empty': True}, 'EXECUTOR_CLOSURE_UNPROVEN')
        response = validate_response(raw, request)
        require(response['status'] == 'SUCCEEDED', 'PREFIX_EXECUTION_FAILED')
        output.append(response['payload'])
        states.append(state)
        prefix_receipts.append({'decision_at': row['at'], 'request_sha256': digest(request), 'response_sha256': digest(response)})
        if on_completed is not None:on_completed(index)
        active()
    return {'schema': 'fa-causal-experiment-1', 'kind': base_request['kind'],
            'rows_hash': digest(full_rows), 'outputs': output, 'states': states, 'prefix_receipts': prefix_receipts,
            'warmup': 'UNAVAILABLE_FACTOR_VALUES_REMAIN_NULL', 'claimed_market_validity': 'NONE'}
