"""One bounded paging contract for HTTP and direct core-service callers."""
from .errors import PlatformError

MAX_LIMIT = 100
MAX_OFFSET = 2_147_483_647


def validate_pagination(limit, offset):
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= MAX_LIMIT or not 0 <= offset <= MAX_OFFSET:
        raise PlatformError("INVALID_INPUT", 422)
