"""Reject inline credentials and redact explicitly scoped environment secrets."""

import os
import re
import json

SENSITIVE_FIELDS = {"apikey", "secret", "password", "token", "accesstoken",
                    "refreshtoken", "clientsecret", "privatekey", "authorization"}
_LABEL = re.compile(
    r'''(?P<label>(?<![\w])["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|private[_-]?key|client[_-]?secret|token)["']?\s*[:=]\s*)'''
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:Bearer|Basic)\s+[^\s,;}\]]+|[^\s,;}\]]+)''',
    re.IGNORECASE,
)

def sensitive_field(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower()) in SENSITIVE_FIELDS

def redact_structure(value):
    if isinstance(value, dict):
        return {redact(key) if isinstance(key, str) else key: "[REDACTED]" if sensitive_field(key) else redact_structure(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_structure(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


class SensitiveInputError(ValueError):
    pass


def secret_values() -> list[str]:
    return [value for name, value in os.environ.items()
            if name.startswith("PREDICTOR_")
            and name.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
            and value]


def redact(text: str) -> str:
    # Preserve a parseable serialized JSON object/list and redact nested fields.
    try:
        parsed = json.loads(text)
    except RecursionError:
        return '[WITHHELD: JSON depth limit]'
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        try:
            return json.dumps(redact_structure(parsed), ensure_ascii=False)
        except RecursionError:
            return '[WITHHELD: JSON depth limit]'
    text = _LABEL.sub(lambda match: match.group("label") + "[REDACTED]", text)
    for value in sorted(secret_values(), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text


def reject_credentials(value):
    """Reject using a fresh snapshot captured at this call's first string.

    Keep inputs and credential environment stable from capture to completion.
    Rotate credentials between drained calls or replace the configured worker;
    this per-invocation view does not synchronize live environment mutations.
    Capture failures propagate. Clearing references below is not zeroization.
    """
    snapshot = None

    def walk(value):
        nonlocal snapshot
        if isinstance(value, dict):
            for key, item in value.items():
                if sensitive_field(key) and item:
                    raise SensitiveInputError("Inline credentials are forbidden; use a scoped environment variable")
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            if snapshot is None:
                snapshot = tuple(secret_values())
            if any(secret in value for secret in snapshot):
                raise SensitiveInputError("Sensitive environment value is forbidden in configuration or records")

    try:
        walk(value)
    finally:
        snapshot = None
