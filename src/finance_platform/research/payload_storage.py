"""Internal persistence envelopes; owning services authorize before hydration.

Logical encoders and hashes stay with producers. No paths or execution ports.
Task bindings must come from the submission transaction, never a late bind().
"""
import json
from finance_platform.core.errors import PlatformError

SCHEMA = 'research-payload-envelope-1'
MAXIMUM = 32 * 1024**2


def storage(runtime):
    from finance_platform.core.storage_locations import get_storage
    return get_storage(runtime)


def task_binding(context):
    value = getattr(context, 'storage_binding', None)
    if (type(value) is not dict or value.get('schema') != 'storage-binding-1'
            or value.get('owner_id') != context.run.owner_id
            or value.get('binding_key') != 'job:' + context.job_id
            or not {'reports', 'artifacts'} <= set(value.get('locations', {}))):
        raise PlatformError('STORAGE_TASK_BINDING_REQUIRED', 409)
    return value


def persist(runtime, binding, category, encoded):
    if type(encoded) is not str or len(encoded.encode('utf-8')) > MAXIMUM:
        raise PlatformError('STORAGE_PAYLOAD_LIMIT', 422)
    value = json.loads(encoded)
    reference = storage(runtime).put(binding, category, encoded.encode('utf-8'), media_type='application/json')
    envelope = {'storage_schema': SCHEMA, 'reference': reference}
    # Existing SQL family filters remain metadata-only. Never duplicate arrays.
    if type(value) is dict:
        for key in ('family', 'schema_version', 'title', 'ticker'):
            if key in value and type(value[key]) is str and len(value[key]) <= 256:
                envelope[key] = value[key]
    return json.dumps(envelope, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def hydrate(runtime, owner_id, category, encoded):
    value = json.loads(encoded)
    if type(value) is not dict or value.get('storage_schema') != SCHEMA:
        return value  # Original embedded records remain immutable/readable.
    if set(value) - {'storage_schema', 'reference', 'family', 'schema_version', 'title', 'ticker'} or 'reference' not in value:
        raise PlatformError('STORAGE_REFERENCE_INVALID', 422)
    raw = storage(runtime).read(owner_id, value['reference'], category=category, maximum_bytes=MAXIMUM)
    result = json.loads(raw)
    for key in ('family', 'schema_version', 'title', 'ticker'):
        if key in value and (type(result) is not dict or result.get(key) != value[key]):
            raise PlatformError('STORAGE_REFERENCE_INVALID', 422)
    return result


ARTIFACT_CATEGORIES = {'proposal': 'artifacts', 'strategy': 'artifacts', 'factor': 'artifacts',
                       'checkpoint': 'artifacts', 'evaluation': 'reports', 'feedback': 'artifacts'}


def persist_artifact_part(runtime, binding, kind, media_type, content):
    """Core validates multipart logical hashes before calling this byte port.

    Individual JSON chunks need not parse as JSON. No code import/evaluation.
    """
    if kind not in ARTIFACT_CATEGORIES or media_type not in {'application/json', 'text/plain', 'text/x-python'}:
        raise PlatformError('STORAGE_REFERENCE_INVALID', 422)
    if type(content) is not str or len(content.encode('utf-8')) > 65536:
        raise PlatformError('STORAGE_PAYLOAD_LIMIT', 422)
    category = ARTIFACT_CATEGORIES[kind]
    ref = storage(runtime).put(binding, category, content.encode('utf-8'), media_type=media_type)
    return json.dumps({'storage_schema': 'research-artifact-part-1', 'reference': ref}, separators=(',', ':'))


def hydrate_artifact_part(runtime, owner_id, kind, encoded):
    # Historical content is arbitrary bounded text, including incomplete JSON.
    try:
        value = json.loads(encoded)
    except (ValueError, TypeError):
        return encoded
    if type(value) is not dict or value.get('storage_schema') != 'research-artifact-part-1':
        return encoded
    if set(value) != {'storage_schema', 'reference'} or kind not in ARTIFACT_CATEGORIES:
        raise PlatformError('STORAGE_REFERENCE_INVALID', 422)
    return storage(runtime).read(owner_id, value['reference'], category=ARTIFACT_CATEGORIES[kind],
                                 maximum_bytes=65536).decode('utf-8')
