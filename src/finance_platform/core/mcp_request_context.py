"""Carry an authenticated MCP request's approval to product transactions."""
from contextlib import contextmanager
from contextvars import ContextVar

_approval = ContextVar("financeanalyze_mcp_request_approval", default=None)


@contextmanager
def request_approval(scope):
    binding = scope.get("state", {}).get("mcp_transaction_approval")
    token = _approval.set(binding)
    try:
        yield
    finally:
        _approval.reset(token)


def require_transaction_approval(database, connection):
    binding = _approval.get()
    if binding is not None and binding[0] == database.path:
        binding[1](connection)
