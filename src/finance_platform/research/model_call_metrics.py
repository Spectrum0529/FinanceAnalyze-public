"""Pure projection of existing SDK-normalized usage; no calls or persistence.

Core supplies one confirmed response's AIMessage.usage_metadata, frozen budgets
and monotonic elapsed time. Core owns identities, status, durability and dedup.
No prompt, private context, model choice object, secret or reservation is input.
"""


def _count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _mapping(value):
    return value if type(value) is dict else {}


def project_metrics(usage_metadata, *, elapsed_ns, configured_limits,
                    confirmed_usage_fields=(),
                    known_model_context_window=None,
                    known_model_context_window_source=None):
    """Unknown stays None, including missing SDK usage and model window.

    output_tokens_per_second is end-to-end output throughput, including provider
    wait/network/reasoning. It is not streaming decode speed or visible-text
    speed. A missing/zero/invalid elapsed time gives no throughput.
    """
    usage = _mapping(usage_metadata)
    # Some adapters fill missing fields with zero or derive total_tokens.
    # Core confirms field presence from this response's original SDK usage;
    # normalized values alone are insufficient evidence of reported counts.
    confirmed = (confirmed_usage_fields
                 if type(confirmed_usage_fields) in (tuple, list, set, frozenset) else ())
    counts = {key: _count(usage.get(key)) if key in confirmed else None for key in
              ('input_tokens', 'output_tokens', 'total_tokens')}
    incoming, outgoing = counts['input_tokens'], counts['output_tokens']
    if (incoming is not None and outgoing is not None
            and counts['total_tokens'] is not None
            and counts['total_tokens'] != incoming + outgoing):
        counts['total_tokens'] = None
    cached = (_count(_mapping(usage.get('input_token_details')).get('cache_read'))
              if 'cached_input_tokens' in confirmed else None)
    if cached is not None and incoming is not None and cached > incoming:
        cached = None
    reasoning = (_count(_mapping(usage.get('output_token_details')).get('reasoning'))
                 if 'reasoning_output_tokens' in confirmed else None)
    if reasoning is not None and outgoing is not None and reasoning > outgoing:
        reasoning = None
    elapsed = _count(elapsed_ns)
    budgets = _mapping(configured_limits)
    window = _count(known_model_context_window)
    source = known_model_context_window_source
    if not window or type(source) is not str or not source.strip():
        window, source = None, None
    return {
        **counts,
        'cached_input_tokens': cached,
        'reasoning_output_tokens': reasoning,
        'usage_source': ('SDK_NORMALIZED_USAGE_METADATA'
                         if any(v is not None for v in (*counts.values(), cached, reasoning))
                         else None),
        'elapsed_ms': elapsed / 1_000_000 if elapsed is not None else None,
        'output_tokens_per_second': (outgoing * 1_000_000_000 / elapsed
                                     if outgoing is not None and elapsed else None),
        'speed_basis': 'CONFIRMED_OUTPUT_TOKENS_OVER_FULL_REQUEST_ELAPSED_INCLUDING_WAIT',
        'configured_input_limit': _count(budgets.get('input_tokens')),
        'configured_output_limit': _count(budgets.get('output_tokens')),
        'configured_history_messages': _count(budgets.get('history_messages')),
        'context_policy': (budgets.get('context_policy')
                           if budgets.get('context_policy') in ('reject', 'truncate_oldest') else None),
        'known_model_context_window': window,
        'known_model_context_window_source': source,
    }
