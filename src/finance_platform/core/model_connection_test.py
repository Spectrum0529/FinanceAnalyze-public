"""A separately confirmed, single-call check of a saved official model target."""

from contextlib import closing
import re

from .auth import AccountDelegation, Principal, require_writer
from .contracts import identifier
from .errors import PlatformError


HANDLER = 'model_profile.connection_test'
LIMITS = {'max_calls': 1, 'input_tokens': 256, 'output_tokens': 64, 'timeout_seconds': 15}
OFFICIAL_TARGETS = {'openai': 'https://api.openai.com/v1', 'deepseek': 'https://api.deepseek.com'}
PROMPT = 'Reply with OK.'


def invoke_model(choice, target, secret, check, *, injected=None):
    import httpx
    from langchain_core.messages import HumanMessage
    from langsmith import tracing_context

    from finance_platform.research.model_call_observation import prepare_openai_responses_invoke
    from finance_platform.research.provider_runtime import FencedTransport, create_client

    bounded_choice = {**choice, 'effort': None, 'native_parameters': {}}
    blocked = None

    def before_transport():
        nonlocal blocked
        try:
            check()
        except PlatformError as error:
            blocked = error
            raise

    try:
        with httpx.Client(transport=FencedTransport(before_transport, injected), trust_env=False,
                          follow_redirects=False, timeout=LIMITS['timeout_seconds']) as transport:
            with tracing_context(enabled=False):
                client = create_client(bounded_choice, target=target, secret=secret, transport=transport,
                                       output_tokens=LIMITS['output_tokens'], timeout_seconds=LIMITS['timeout_seconds'], retry=0)
                options, _ = prepare_openai_responses_invoke(client)
                if target.provider == 'openai':
                    options['store'] = False
                response = client.invoke([HumanMessage(content=PROMPT)], **options)
    except PlatformError:
        raise
    except Exception as error:
        if blocked is not None:
            raise blocked from None
        upstream_code = getattr(error, 'status_code', None)
        reason = {401: 'MODEL_TEST_KEY_REJECTED', 403: 'MODEL_TEST_ACCESS_DENIED', 404: 'MODEL_TEST_MODEL_NOT_FOUND',
                  429: 'MODEL_TEST_RATE_OR_QUOTA_LIMIT'}.get(upstream_code, 'MODEL_TEST_PROVIDER_ERROR')
        if isinstance(error, (TimeoutError, httpx.TimeoutException)) or type(error).__name__ == 'APITimeoutError':
            reason = 'MODEL_TEST_TIMEOUT'
        raise PlatformError(reason, 502) from None

    check()
    if not isinstance(response.text, str) or not response.text.strip():
        raise PlatformError('MODEL_TEST_NO_REPLY', 502)
    reported = response.usage_metadata or {}
    result = {'model_responded': True}
    for field in ('input_tokens', 'output_tokens'):
        value = reported.get(field)
        if type(value) is int and value >= 0:
            if value > LIMITS[field]:
                raise PlatformError('MODEL_TEST_TOKEN_LIMIT_EXCEEDED', 502)
            result[field] = value
        else:
            result[field] = None
    return result


def validate_job(parameters):
    if (type(parameters) is not dict
            or set(parameters) != {'profile_id', 'revision', 'profile_sha256', 'credential_generation'}
            or type(parameters['revision']) is not int or parameters['revision'] < 1
            or any(type(parameters[field]) is not str or not re.fullmatch(r'[a-f0-9]{64}', parameters[field])
                   for field in ('profile_sha256', 'credential_generation'))):
        raise PlatformError('INVALID_INPUT', 422)
    identifier(parameters['profile_id'], 'profile id')


class ModelConnectionTest:
    def __init__(self, runtime, credential_store_factory):
        self.runtime = runtime
        self.profiles = runtime.model_profiles
        self.credential_store_factory = credential_store_factory

    def _profile(self, principal, profile_id):
        if isinstance(principal, AccountDelegation):
            raise PlatformError('FORBIDDEN', 403)
        return self.profiles.get(principal, profile_id)

    def _execution(self, principal, row):
        choice = row['configuration']['model']
        if self.runtime.hosted_accounts or choice.get('endpoint_profile') is not None or choice['provider'] not in OFFICIAL_TARGETS:
            raise PlatformError('MODEL_TEST_UNSUPPORTED_TARGET', 409)
        reference = choice.get('secret_ref')
        if not isinstance(reference, str) or not reference.startswith('credential:FA_'):
            raise PlatformError('MODEL_TEST_SAVED_LOCAL_KEY_REQUIRED', 409)
        self.profiles.prepare_execution_profile(principal, row['profile_id'], row['revision'])
        target = self.profiles.freeze_target(principal, choice)
        if target.base_url != OFFICIAL_TARGETS[choice['provider']] or target._asdict() != row['execution_target']:
            raise PlatformError('MODEL_TARGET_CHANGED', 409)
        return choice, target

    def status(self, principal, profile_id):
        row = self._profile(principal, profile_id)
        choice = row['configuration']['model']
        state = 'NOT_CONFIGURED'
        if choice['provider'] in {'offline', 'ollama'} and choice.get('secret_ref') is None:
            state = 'NOT_REQUIRED'
        elif isinstance(choice.get('secret_ref'), str) and choice['secret_ref'].startswith('credential:FA_'):
            try:
                self.profiles.prepare_execution_profile(principal, profile_id, row['revision'])
                state = 'SAVED'
            except PlatformError:
                state = 'UNVERIFIED'
        reason = None
        try:
            self._execution(principal, row)
        except PlatformError as error:
            reason = error.code
        can_write = principal.role in {'admin', 'researcher'}
        if not can_write:
            reason = 'FORBIDDEN'
        return {'profile_id': profile_id, 'revision': row['revision'], 'provider': choice['provider'],
                'model': choice['model'], 'credential_state': state, 'available': reason is None,
                'reason_code': reason, 'limits': LIMITS}

    def submit(self, principal, profile_id, revision, idempotency_key):
        require_writer(principal)
        row = self._profile(principal, profile_id)
        if revision != row['revision']:
            raise PlatformError('CONFLICT', 409)
        choice, target = self._execution(principal, row)
        store = self.credential_store_factory()
        generation = store.generation_for_provider(principal.user_id, choice['secret_ref'], target=target)
        return self.runtime.jobs.submit_internal(principal, HANDLER, {
            'profile_id': profile_id, 'revision': revision, 'profile_sha256': row['sha256'],
            'credential_generation': generation,
        }, idempotency_key=idempotency_key)

    def _active_writer(self, owner):
        with closing(self.runtime.database.connect()) as con:
            user = con.execute('SELECT username, role, enabled FROM users WHERE user_id=?', (owner,)).fetchone()
        if user is None or not user['enabled']:
            raise PlatformError('FORBIDDEN', 403)
        principal = Principal(owner, user['username'], user['role'])
        require_writer(principal)
        return principal

    def run(self, context, parameters):
        owner = context.run.owner_id
        profile_id = parameters['profile_id']
        generation = parameters['credential_generation']
        store = self.credential_store_factory()

        def current():
            context.require_active()
            principal = self._active_writer(owner)
            row = self._profile(principal, profile_id)
            if (row['revision'] != parameters['revision'] or row['sha256'] != parameters['profile_sha256']):
                raise PlatformError('CONFLICT', 409)
            choice, target = self._execution(principal, row)
            if store.generation_for_provider(owner, choice['secret_ref'], target=target) != generation:
                raise PlatformError('MODEL_TEST_CREDENTIAL_CHANGED', 409)
            return choice, target

        choice, target = current()
        secret = store.resolve_for_provider(owner, choice['secret_ref'], target=target, expected_generation=generation)
        result = invoke_model(choice, target, secret, current)
        return {**result, 'profile_id': profile_id, 'revision': parameters['revision'], 'provider': choice['provider'],
                'model': choice['model'], 'test_kind': 'provider_inference'}


def install(runtime, credential_store_factory):
    service = ModelConnectionTest(runtime, credential_store_factory)
    runtime.model_connection_test = service
    runtime.handlers.register(HANDLER, service.run, validate=validate_job, internal_only=True,
                              recoverable=False, auto_recovery_limit=0)
