"""Validated raw-store inventory and guarded import of existing sample spools."""
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET
import json

from predictor.access import AccessError
from predictor.data.acquisition import replay_response
from predictor.data.reference_acquisition import replay_reference
from predictor.data.raw_store import RawStore, RawStoreError, _load, _page_meta, _time


def build_manifest(store):
    batches, versions, referenced = [], [], set()
    with store._connection() as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise RawStoreError('Raw index integrity failed')
        for source_batch in connection.execute('SELECT batch_id FROM batches ORDER BY batch_id').fetchall():
            batch, context = store._batch(connection, source_batch['batch_id'])
            pages = []
            for row in connection.execute('SELECT * FROM pages WHERE batch_id=? ORDER BY ordinal', (batch['batch_id'],)):
                raw = store._blob(row['body_hash'])
                if len(raw) != row['byte_count'] or not 0 <= row['ordinal'] < context['expected_pages']:
                    raise RawStoreError('Indexed page count/ordinal mismatch')
                meta = _load(row['reception'])
                _page_meta(meta)
                _time(row['stored_at'])
                if context['source_version'] is not None and meta['response_headers'].get('etag') != context['source_version']:
                    raise RawStoreError('Indexed source validator mismatch')
                pages.append({**dict(row), 'reception': meta})
                referenced.add(row['body_hash'])
            completed = connection.execute('SELECT * FROM completions WHERE batch_id=?', (batch['batch_id'],)).fetchone()
            if completed:
                assembled = store._assembled(connection, batch['batch_id'], context)
                store._validate_completion(connection, completed, batch, context, assembled)
                referenced.add(completed['body_hash'])
            batches.append({'batch_id': batch['batch_id'], 'request_key': batch['request_key'], 'context': context,
                'started_at': batch['started_at'], 'pages': pages, 'completion': dict(completed) if completed else None,
                'remaining_pages': context['expected_pages'] - len(pages), 'status': 'complete' if completed else 'incomplete'})
        for row in connection.execute('SELECT * FROM versions ORDER BY request_key,version').fetchall():
            origin = next((batch for batch in batches if batch['batch_id'] == row['first_batch_id']), None)
            if not origin or not origin['completion'] or origin['completion']['version'] != row['version'] or origin['request_key'] != row['request_key']:
                raise RawStoreError('Version origin mismatch')
            counts = {b['completion']['record_count'] for b in batches if b['request_key'] == row['request_key']
                and b['completion'] and b['completion']['version'] == row['version'] and b['completion']['record_count'] is not None}
            if len(counts) > 1:
                raise RawStoreError('Version record counts conflict')
            versions.append({**dict(row), 'revision_detected': row['version'] > 1, 'record_count': next(iter(counts), None)})
        groups = {}
        for version in versions:
            groups.setdefault(version['request_key'], []).append(version['version'])
        if any(numbers != list(range(1, len(numbers) + 1)) for numbers in groups.values()):
            raise RawStoreError('Noncontiguous source version sequence')
        objects = []
        for path in sorted(store._path('objects').glob('*.bin')):
            raw = store._blob(path.stem)
            objects.append({'body_hash': path.stem, 'byte_count': len(raw), 'indexed': path.stem in referenced})
        counts = {'batches': len(batches), 'completed_batches': sum(b['status'] == 'complete' for b in batches),
            'incomplete_batches': sum(b['status'] == 'incomplete' for b in batches),
            'indexed_pages': sum(len(b['pages']) for b in batches), 'unique_versions': len(versions),
            'unique_blob_objects': len(objects), 'unreferenced_blob_objects': sum(not o['indexed'] for o in objects),
            'staging_files': len(list(store._path('staging').glob('*.bin'))),
            'known_records_in_unique_versions': sum(v['record_count'] or 0 for v in versions),
            'versions_with_unknown_record_count': sum(v['record_count'] is None for v in versions),
            'bytes_in_unique_versions': sum(next(b['completion']['byte_count'] for b in batches if b['batch_id'] == v['first_batch_id']) for v in versions)}
    return {'schema_version': 1, 'scope': 'guarded_development_raw_inventory', 'counts': counts,
        'batches': batches, 'versions': versions, 'objects': objects,
        'data_certification': 'UNVERIFIED', 'formal_research_eligible': False, 'model_evaluation': 'NOT_RUN',
        'record_counts_basis': 'Declared/adapter-counted physical source rows; no trading-universe or label certification',
        'orphan_policy': 'Retain orphan blobs/staging; retries reuse valid blobs. No automatic deletion.'}


def ingest_capture(policy, directory, capture, kind, batch_id):
    """Index original source bytes; no parsing into price histories or labels."""
    # Authorize every destination and input before any source response is opened.
    store = RawStore(policy, directory)
    capture_path = policy.authorize(capture)
    if not capture_path.is_relative_to(policy.partitions['development']):
        raise AccessError('Existing source spools must be in development')
    pages = []
    if kind == 'prices':
        raw, manifest = replay_response(policy, capture_path)
        request = {'source_id': manifest['source_id'], 'source_url': manifest['source_url'],
            'parameters': {'session': manifest['session'], 'transport_exchange': manifest['transport_exchange']},
            'schema_version': manifest['schema_version'], 'data_scope': 'public_provider_sample' if manifest['scope'] == 'public_provider_sample' else 'synthetic_only'}
        for marker in sorted(capture_path.glob('part-*.json')):
            part = json.loads(policy.read_bytes(marker))
            pages.append((policy.read_bytes(capture_path / (part['body_hash'] + '.bin')), {
                'received_at': part['received_at_utc'], 'http_status': part['status'], 'response_headers': part['response_headers'],
                'request_headers': part['request_headers']}))
        count = sum(bool(line.strip()) for line in raw.split(b'\n'))
        if raw.split(b'\n', 1)[0].lower().startswith(b'symbol|cusip|'):
            count -= 1
        validator = manifest['source_etag']
    elif kind == 'reference':
        raw, manifest = replay_reference(policy, capture_path)
        request = {'source_id': 'nyse_reference_' + manifest['kind'], 'source_url': manifest['source_url'],
            'parameters': {'sample_date': manifest['sample_date'], 'filename': manifest['filename']},
            'schema_version': 'observed-master-4.0-157-fields' if manifest['kind'] == 'master' else 'observed-CA-v3-family-' + manifest['kind'],
            'data_scope': 'public_provider_sample'}
        if manifest['kind'] == 'master':
            count = sum(bool(line.strip()) for line in raw.split(b'\n'))
        else:
            text = raw.decode('utf-8-sig')
            if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
                raise RawStoreError('DTD/entities forbidden in row count inspection')
            count = len(ET.fromstring(text))
        validator = manifest['response_headers'].get('etag')
        pages.append((raw, {'received_at': manifest['response_received_at'], 'http_status': 200,
            'response_headers': manifest['response_headers'], 'request_headers': None}))
    else:
        raise RawStoreError('Unknown capture adapter')
    context = {'request': request, 'license': {'id': 'public-provider-sample-evaluation',
        'status': manifest['license_status'], 'usage': 'public_sample_evaluation_only'}, 'expected_pages': len(pages),
        'expected_response_hash': manifest['raw_response_hash'], 'expected_records': count, 'source_version': validator}
    key = store.begin_batch(batch_id, context)
    for ordinal, (body, reception) in enumerate(pages):
        store.put_page(batch_id, ordinal, body, reception)
    completion = store.finish_batch(batch_id, record_count=count)
    if store.read_version(key, completion['version']) != raw:
        raise RawStoreError('Indexed replay differs from original capture')
    return {'request_key': key, 'completion': completion, 'context': context, 'original_capture': capture_path.relative_to(policy.root).as_posix()}
