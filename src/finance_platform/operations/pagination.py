"""Shared bounded pagination for HTTP and direct operations callers."""
from finance_platform.core.errors import PlatformError

MAX_OFFSET = 2147483647


def validate_page(limit, offset):
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or not 0 <= offset <= MAX_OFFSET:
        raise PlatformError("INVALID_INPUT", 422)
