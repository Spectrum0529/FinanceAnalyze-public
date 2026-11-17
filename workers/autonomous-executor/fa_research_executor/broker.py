"""RD-Agent model backend delegates each role to an already-reserved host broker.

No provider SDK, environment key or default RD-Agent backend is used here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import threading
from .protocol import require, encoded

_scope = ContextVar('fa_rd_role', default=None)
_serial = threading.Lock()


@contextmanager
def role_scope(broker, profile, role, *, active, max_calls, max_chars, history_groups=None):
    if history_groups is not None:
        require(role=='hypothesis' and type(history_groups) is list and len(history_groups)<=20
                and all(type(group) is str and 0<len(group)<=4096 for group in history_groups),
                'MODEL_STAGE_HISTORY_INVALID')
    frame = {'broker': broker, 'profile': deepcopy(profile), 'role': role, 'active': active,
             'remaining': max_calls, 'max_chars': max_chars,
             'history_groups': None if history_groups is None else tuple(history_groups),
             'opened': True, 'thread': threading.get_ident(), 'lock': threading.Lock()}
    token = _scope.set(frame)
    try:
        yield
    finally:
        with frame['lock']:
            frame['opened'] = False
        _scope.reset(token)


class BrokerBackend:
    chat_token_limit = 8192

    def __init__(self, *args, **kwargs):
        frame = _scope.get()
        require(frame is not None and frame['thread'] == threading.get_ident(), 'MODEL_BROKER_SCOPE_REQUIRED')
        with frame['lock']:
            require(frame['opened'], 'MODEL_BROKER_SCOPE_REQUIRED')
        self._frame = frame

    def build_messages_and_calculate_token(self, user_prompt, system_prompt='', **kwargs):
        # Conservative UTF8 byte bound, never advertise a tokenizer estimate as usage.
        return len(encoded([system_prompt, user_prompt]))

    def build_messages_and_create_chat_completion(self, user_prompt, system_prompt='', **kwargs):
        scope = self._frame
        require(_scope.get() is scope and scope['thread'] == threading.get_ident(), 'MODEL_BROKER_SCOPE_REQUIRED')
        messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}]
        groups = scope['history_groups']
        optional = {} if groups is None else {'history_groups':list(groups)}
        stage_input = messages if groups is None else {'messages':messages, **optional}
        with scope['lock']:
            require(scope['opened'], 'MODEL_BROKER_SCOPE_REQUIRED')
            scope['active']()
            require(scope['remaining'] > 0, 'MODEL_STAGE_CALL_BUDGET_EXCEEDED')
            require(len(encoded(stage_input)) <= scope['max_chars'], 'MODEL_STAGE_INPUT_EXCEEDED')
            scope['remaining'] -= 1  # including failures/unknown remote completion
        response = scope['broker'](role=scope['role'], profile=scope['profile'], messages=messages,
                                    json_mode=bool(kwargs.get('json_mode', False)), **optional)
        with scope['lock']:
            require(scope['opened'], 'MODEL_BROKER_SCOPE_REQUIRED')
            scope['active']()
        require(type(response) is str and len(response.encode()) <= 65536, 'MODEL_STAGE_OUTPUT_EXCEEDED')
        return response


@contextmanager
def native_backend_scope():
    """Linux trusted orchestration process only, serial and restored on failure."""
    from rdagent.oai.llm_conf import LLM_SETTINGS
    with _serial:
        original = LLM_SETTINGS.backend
        LLM_SETTINGS.backend = 'fa_research_executor.broker.BrokerBackend'
        try:
            yield
        finally:
            LLM_SETTINGS.backend = original
