"""UTF-8 JSON logs with explicit UTC timestamps, written to stderr."""

from datetime import datetime, timezone
import json
import logging
import sys
from predictor.secrets import redact, sensitive_field

class SanitizedException(Exception):
    """Safe exception carrier; the sanitized traceback lives in exc_text."""

def safe_extra(value, active=None, depth=0):
    """Never invoke an arbitrary object's str/repr while preparing a log."""
    if type(value) is str:
        return redact(value)
    if value is None or type(value) in (int, float, bool):
        return value
    if active is None: active=set()
    if depth > 32: return '[WITHHELD: depth limit]'
    if id(value) in active: return '[WITHHELD: cycle]'
    if type(value) not in (dict, list, tuple):
        return '[WITHHELD: unsupported object]'
    active.add(id(value))
    try:
        if type(value) is dict:
            result={}
            for key,item in value.items():
                if type(key) is not str:
                    return '[WITHHELD: non-string mapping key]'
                result[redact(key)]='[REDACTED]' if sensitive_field(key) else safe_extra(item,active,depth+1)
            return result
        items=[safe_extra(v,active,depth+1) for v in value]
        return tuple(items) if type(value) is tuple else items
    finally:
        active.remove(id(value))


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RedactingFilter(logging.Filter):
    """Sanitize the record before any handler on the configured logger sees it."""
    def filter(self, record):
        try:
            if type(record.msg) in (dict, list):
                message = json.dumps(safe_extra(record.msg), ensure_ascii=False)
            else:
                # Preserve %-placeholders until getMessage consumes the safe
                # arguments; label redaction before interpolation could erase %s.
                if type(record.msg) is not str:
                    record.msg = safe_extra(record.msg)
                record.args = safe_extra(record.args)
                message = record.getMessage()
            record.msg, record.args = redact(message), ()
        except (TypeError, ValueError):
            # Logging's own formatting-error diagnostics must not echo raw args.
            record.msg, record.args = "Invalid log message; original arguments withheld", ()
        if record.exc_info and not isinstance(record.exc_info[1], SanitizedException):
            try:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            except Exception:
                record.exc_text = 'Exception details withheld: invalid exception formatter'
            # Keep logging's three-item structure, but never expose raw exception
            # arguments or traceback objects to a downstream custom Formatter.
            record.exc_info = (SanitizedException, SanitizedException(record.exc_text), None)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        for key, value in list(record.__dict__.items()):
            if sensitive_field(key):
                record.__dict__[key] = "[REDACTED]"
            elif key != 'exc_info':
                record.__dict__[key] = safe_extra(value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        RedactingFilter().filter(record)
        timestamp = datetime.fromtimestamp(record.created, timezone.utc)
        content = {
            "timestamp_utc": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "event": redact(record.getMessage()),
        }
        if record.exc_text:
            content["exception"] = redact(record.exc_text)
        return json.dumps(content, ensure_ascii=False)


def configure_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("predictor")
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()
    for existing in list(logger.filters):
        if isinstance(existing, RedactingFilter):
            logger.removeFilter(existing)
    logger.addFilter(RedactingFilter())
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    add_safe_handler(logger, handler)
    return logger


def add_safe_handler(logger: logging.Logger, handler: logging.Handler):
    """Guard additional handlers, including records propagated from child loggers."""
    if not any(isinstance(existing, RedactingFilter) for existing in handler.filters):
        handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
