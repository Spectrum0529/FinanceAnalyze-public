"""Optional local facts from the existing SDK boundary, never a model gateway."""
from copy import deepcopy
from datetime import datetime, timezone
from time import monotonic_ns
from uuid import uuid4
from .model_call_metrics import project_metrics


_SDK_SOURCE_PINS = {
    'langchain_openai.chat_models.base': '50aec54e3bdec7e049a579eb53a1cdb999011889714a4f8b8b6be409b1cd025a',
    'langchain_core.language_models.chat_models': '7377a3e0d534d6fe9835cb35e39fc2bf7614ae2be26eb5664a0767dfb002cb03',
    'langchain_core.runnables.base': '4cdcf9f699860ef49c02fe75339cdd8f302f050fa108b625f9a18d84cf3367ba',
    'tradingagents.llm_clients.openai_client': '5d4e993ccea48351051d7daeda48ca2756ba12018a65df30c5893afe3b1a2703',
    'tradingagents.llm_clients.base_client': '5af145a100b42e5de2c3baffcde0f3975fd826b14c1659c4ee44a300affa0c38',
}


def _sdk_sources_match():
    from hashlib import sha256
    from importlib import import_module
    from pathlib import Path
    return all(sha256(Path(import_module(name).__file__).read_bytes()).hexdigest() == expected
               for name, expected in _SDK_SOURCE_PINS.items())


class _ResponsesProvenance:
    __slots__ = ('client', 'invoke_kwargs')

    def __init__(self, client, invoke_kwargs):
        self.client = client
        self.invoke_kwargs = invoke_kwargs


def openai_responses_provenance(client, *, invoke_kwargs=None):
    """Optional host-only proof for this exact synchronous invocation.

    Caller must pass the SAME actual client and actual kwargs used by invoke.
    Explicit stream=False is mandatory; the default streaming=False alone does
    not disable implicit streaming callbacks. No provider/metadata strings,
    JSON proof, unknown Runnable wrapper or changed SDK source is accepted.
    This reads pinned SDK source, never client credentials or model content.
    """
    try:
        from langchain_core.runnables.base import RunnableBinding
        from langchain_core.language_models.chat_models import _ChatModelBinding
        if (type(invoke_kwargs) is not dict or set(invoke_kwargs) != {'stream'}
                or invoke_kwargs.get('stream') is not False):
            return None
        actual, bound_kwargs = client, {}
        for _ in range(4):
            if type(actual) not in (RunnableBinding, _ChatModelBinding):
                break
            if actual.config_factories:
                return None
            if 'stream' in actual.kwargs and actual.kwargs['stream'] is not False:
                return None # never override an existing requested stream
            bound_kwargs = {**actual.kwargs, **bound_kwargs}
            actual = actual.bound
        # Negative prefilter only: offline/proxy clients must not import a
        # vendor module just to discover that optional usage proof is absent.
        # Spoofed metadata still cannot satisfy the exact official type and
        # pinned SDK source checks below; these names never establish proof.
        kind = type(actual)
        if (kind.__module__, kind.__name__) != (
                'tradingagents.llm_clients.openai_client', 'NormalizedChatOpenAI'):
            return None
        from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI
        if type(actual) is not NormalizedChatOpenAI:
            return None
        effective = {**bound_kwargs, **invoke_kwargs}
        if (actual.use_responses_api is not True or actual.cache is not False
                or actual.streaming is not False or effective.get('stream') is not False
                or actual.model_kwargs or actual.extra_body or effective.get('extra_body')):
            return None
        if not _sdk_sources_match():
            return None
        return _ResponsesProvenance(client, dict(invoke_kwargs))
    except Exception:
        return None


def prepare_openai_responses_invoke(client, *, invoke_kwargs=None):
    """For existing complete-response .invoke only; preserve explicit options.

    Returns (actual_options, opaque_provenance_or_None). Stream/astream entry
    points never use this helper. An explicit stream=True, unknown option,
    client or source keeps the original options and the r01 unknown fallback.
    """
    options = {} if invoke_kwargs is None else invoke_kwargs
    if type(options) is not dict:
        return options, None
    if 'stream' in options and options['stream'] is not False:
        return dict(options), None
    tentative = {**options, 'stream': False}
    proof = openai_responses_provenance(client, invoke_kwargs=tentative)
    return (tentative, proof) if proof is not None else (dict(options), None)


def _proven_responses_usage(response, provenance):
    """Invert only non-derived fields in the pinned Responses conversion."""
    if type(provenance) is not _ResponsesProvenance:
        return {}, []
    if openai_responses_provenance(provenance.client, invoke_kwargs=provenance.invoke_kwargs) is None:
        return {}, []
    from langchain_core.messages import AIMessage
    if type(response) is not AIMessage:
        return {}, []
    normalized = _mapping(response.usage_metadata)
    canonical, confirmed = {}, []
    for key in ('input_tokens', 'output_tokens'):
        value = normalized.get(key)
        # Missing source counters become0. Only a positive integer rules out
        # that default. total_tokens may be derived and is never inverted.
        if type(value) is int and 0 < value <= 2**63 - 1:
            canonical[key] = value
            confirmed.append(key)
    metadata = _mapping(response.response_metadata)
    tier = metadata.get('service_tier')
    prefix = tier + '_' if tier in ('priority', 'flex') else ''
    for field, target, detail, key in (
        ('input_token_details', 'input_token_details', 'cached_input_tokens', prefix + 'cache_read'),
        ('output_token_details', 'output_token_details', 'reasoning_output_tokens', prefix + 'reasoning'),
    ):
        value = _mapping(normalized.get(field)).get(key)
        # Unlike top-level counts these keys are filtered out when None;
        # an explicitly retained0 therefore proves original detail presence.
        if type(value) is int and 0 <= value <= 2**63 - 1:
            canonical[target] = {('cache_read' if detail == 'cached_input_tokens' else 'reasoning'): value}
            confirmed.append(detail)
    return canonical, confirmed


def _mapping(value):
    return value if type(value) is dict else {}


def _label(value):
    return value if type(value) is str and len(value) <= 256 else None


def retained_usage(response, provider, *, provenance=None):
    """Only already-retained raw SDK metadata proves original field presence.

    Optional pinned actual-client provenance also admits non-derived Responses
    fields. No content, normalized zero defaults, transport hooks or extra calls.
    """
    metadata = _mapping(getattr(response, 'response_metadata', None))
    raw = _mapping(metadata.get('token_usage')) or _mapping(metadata.get('usage'))
    canonical, confirmed = {}, []
    chat = 'prompt_tokens' in raw or 'completion_tokens' in raw
    names = (('input_tokens', 'prompt_tokens' if chat else 'input_tokens'),
             ('output_tokens', 'completion_tokens' if chat else 'output_tokens'),
             ('total_tokens', 'total_tokens'))
    for target, source in names:
        if source in raw:
            canonical[target] = raw[source]
            confirmed.append(target)
    details = _mapping(raw.get('prompt_tokens_details' if chat else 'input_tokens_details'))
    cached = details.get('cached_tokens')
    if 'cached_tokens' in details:
        canonical['input_token_details'] = {'cache_read': cached}
        confirmed.append('cached_input_tokens')
    elif provider == 'deepseek' and 'prompt_cache_hit_tokens' in raw:
        canonical['input_token_details'] = {'cache_read': raw['prompt_cache_hit_tokens']}
        confirmed.append('cached_input_tokens')
    details = _mapping(raw.get('completion_tokens_details' if chat else 'output_tokens_details'))
    if 'reasoning_tokens' in details:
        canonical['output_token_details'] = {'reasoning': details['reasoning_tokens']}
        confirmed.append('reasoning_output_tokens')
    if provider == 'openai':
        proven, mask = _proven_responses_usage(response, provenance)
        for field in mask:
            if field in confirmed:
                continue # original raw usage remains authoritative
            if field == 'cached_input_tokens':
                canonical['input_token_details'] = proven['input_token_details']
            elif field == 'reasoning_output_tokens':
                canonical['output_token_details'] = proven['output_token_details']
            else:
                canonical[field] = proven[field]
            confirmed.append(field)
    return canonical, confirmed


class ModelCallObservation:
    """One SDK invocation; only its coordinator publishes optional evidence.

    invoke may run in the existing bounded SDK thread. It only captures local
    values; finish is called once by the coordinator after existing cleanup.
    Sealing prevents a late SDK return from creating a later published record.
    """
    def __init__(self, choice, budgets, *, role, selected=None):
        selected = _mapping(selected)
        self.choice = {key: _label(_mapping(choice).get(key)) for key in ('provider', 'model')}
        self.budgets = {key: _mapping(budgets).get(key) for key in
                        ('input_tokens', 'output_tokens', 'history_messages', 'context_policy')}
        self.row = {'schema': 'ai-model-call-observation-1', 'call_id': uuid4().hex,
                    **self.choice, 'role': _label(role),
                    'profile_id': _label(selected.get('profile_id')),
                    'revision': selected.get('revision') if type(selected.get('revision')) is int else None}
        self.elapsed = None
        self.usage, self.confirmed = {}, ()
        self.sealed = False
        self.finished_row = None
        self.row.update(started_at=None, ended_at=None)

    def invoke(self, call, *, provenance=None):
        start = monotonic_ns()
        self.row['started_at'] = datetime.now(timezone.utc).isoformat()
        try:
            result = call()
            if not self.sealed:
                self.elapsed = max(0, monotonic_ns() - start)
                self.row['ended_at'] = datetime.now(timezone.utc).isoformat()
                try:
                    raw = result.get('raw') if type(result) is dict and 'raw' in result else result
                    self.usage, self.confirmed = retained_usage(raw, self.choice['provider'], provenance=provenance)
                except Exception:
                    self.usage, self.confirmed = {}, ()
            return result
        finally:
            end = monotonic_ns()
            if not self.sealed and self.elapsed is None:
                self.elapsed = max(0, end - start)
                self.row['ended_at'] = datetime.now(timezone.utc).isoformat()

    def finish(self, status, sink=lambda value: None):
        if self.finished_row is not None:
            return deepcopy(self.finished_row)
        self.sealed = True
        try:
            row = {**self.row, 'status': status,
                   'metrics': project_metrics(self.usage, elapsed_ns=self.elapsed,
                                              configured_limits=self.budgets,
                                              confirmed_usage_fields=self.confirmed)}
        except Exception:
            row = {**self.row, 'status': status, 'metrics': None}
        self.finished_row = deepcopy(row)
        try:
            sink(deepcopy(row))
        except Exception:
            # Optional evidence failure cannot change the original outcome.
            pass
        return row

