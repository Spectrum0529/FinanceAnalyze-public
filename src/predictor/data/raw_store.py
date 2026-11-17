"""Guarded content-addressed response bytes and append-only ingestion index.

SQLite serializes index writers. A blob is fsynced then atomically published by
an exclusive hard link; index commits follow publication. Crashes can leave
unreferenced blobs/staging files, never a partial indexed blob. This is logical
immutability, not an OS-level defense against an administrator rewriting files.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from predictor.research.append_guards import install as install_identity_guards
from urllib.parse import urlsplit, parse_qsl
from uuid import uuid4

from predictor.access import AccessError
from predictor.logging import utc_timestamp
from predictor.secrets import reject_credentials, secret_values

MAX_PAGE_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024
HEADER_FIELDS = {'etag', 'last-modified', 'content-length', 'content-type', 'content-range'}


class RawStoreError(ValueError):
    """Fixed diagnostics intentionally omit source bytes and credential values."""


def canonical(value):
    reject_credentials(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require_digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise RawStoreError('SHA-256 required')
    return value


def _count(value, *, optional=False, positive=False):
    if value is None and optional:
        return
    if type(value) is not int or value < (1 if positive else 0):
        raise RawStoreError('Explicit integer count required')


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)', value):
        raise RawStoreError('Explicit UTC reception time required')
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise RawStoreError('Invalid UTC reception time') from None


def request_key(request):
    fields = {'source_id', 'source_url', 'parameters', 'schema_version', 'data_scope'}
    if not isinstance(request, dict) or set(request) != fields:
        raise RawStoreError('Explicit source/request/schema/scope required')
    if request['data_scope'] not in {'synthetic_only', 'public_provider_sample'}:
        raise RawStoreError('Unsupported development data scope')
    if not isinstance(request['parameters'], dict):
        raise RawStoreError('Request parameters must be an object')
    for field in ('source_id', 'source_url', 'schema_version'):
        if not isinstance(request[field], str) or not request[field].strip():
            raise RawStoreError('Source declaration missing')
    url = urlsplit(request['source_url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
        raise RawStoreError('Source URL must be credential-free HTTPS')
    reject_credentials(dict(parse_qsl(url.query, keep_blank_values=True)))
    return digest(canonical(request).encode('utf-8'))


def _context(value):
    fields = {'request', 'license', 'expected_pages', 'expected_response_hash', 'expected_records', 'source_version'}
    if not isinstance(value, dict) or set(value) != fields:
        raise RawStoreError('Batch context fields missing or unknown')
    request_key(value['request'])
    license_value = value['license']
    if not isinstance(license_value, dict) or set(license_value) != {'id', 'status', 'usage'} or any(
            not isinstance(v, str) or not v.strip() for v in license_value.values()):
        raise RawStoreError('Explicit license declaration required')
    _count(value['expected_pages'], positive=True)
    if value['expected_pages'] > 100000:
        raise RawStoreError('Page limit exceeded')
    _count(value['expected_records'], optional=True)
    if value['expected_response_hash'] is not None:
        require_digest(value['expected_response_hash'])
    if value['source_version'] is not None and (not isinstance(value['source_version'], str) or not value['source_version'].strip()):
        raise RawStoreError('Invalid source version declaration')
    canonical(value)


def _page_meta(value):
    if not isinstance(value, dict) or set(value) != {'received_at', 'http_status', 'response_headers', 'request_headers'}:
        raise RawStoreError('Page reception metadata required')
    _time(value['received_at'])
    if type(value['http_status']) is not int or value['http_status'] not in {200, 206}:
        raise RawStoreError('Only successful raw response pages can be indexed')
    headers = value['response_headers']
    if not isinstance(headers, dict) or set(headers) - HEADER_FIELDS or any(not isinstance(v, str) for v in headers.values()):
        raise RawStoreError('Unsupported response headers')
    if value['http_status'] == 206 and 'content-range' not in headers:
        raise RawStoreError('Partial response requires its byte range')
    request_headers = value['request_headers']
    if request_headers is not None and (not isinstance(request_headers, dict) or any(
            not isinstance(k, str) or k.lower() not in {'user-agent', 'accept-encoding', 'range', 'if-range'}
            or not isinstance(v, str) for k, v in request_headers.items())):
        raise RawStoreError('Unsupported request headers')
    canonical(value)


def _load(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RawStoreError('Duplicate stored metadata key')
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs)
        if canonical(value) != text:
            raise RawStoreError('Noncanonical stored metadata')
        return value
    except (TypeError, json.JSONDecodeError):
        raise RawStoreError('Invalid stored metadata') from None


class RawStore:
    def __init__(self, policy, directory):
        self.policy = policy
        self.directory = policy.authorize(directory)
        if not self.directory.is_relative_to(policy.partitions['development']) or self.directory == policy.partitions['development']:
            raise AccessError('Raw store output must be a development subdirectory')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self._path('index.sqlite3')
        with self._connection() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches(
                    batch_id TEXT PRIMARY KEY,request_key TEXT NOT NULL,context TEXT NOT NULL,started_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pages(
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id),ordinal INTEGER NOT NULL CHECK(ordinal>=0),
                    body_hash TEXT NOT NULL,byte_count INTEGER NOT NULL CHECK(byte_count>0),
                    reception TEXT NOT NULL,stored_at TEXT NOT NULL,PRIMARY KEY(batch_id,ordinal));
                CREATE TABLE IF NOT EXISTS versions(
                    request_key TEXT NOT NULL,version INTEGER NOT NULL CHECK(version>0),body_hash TEXT NOT NULL,
                    first_batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                    PRIMARY KEY(request_key,version),UNIQUE(request_key,body_hash));
                CREATE TABLE IF NOT EXISTS completions(
                    batch_id TEXT PRIMARY KEY REFERENCES batches(batch_id),request_key TEXT NOT NULL,version INTEGER NOT NULL,
                    body_hash TEXT NOT NULL,page_count INTEGER NOT NULL,byte_count INTEGER NOT NULL,
                    record_count INTEGER,finished_at TEXT NOT NULL,
                    FOREIGN KEY(request_key,version) REFERENCES versions(request_key,version));
                CREATE TRIGGER IF NOT EXISTS closed_batch_pages BEFORE INSERT ON pages
                    WHEN EXISTS(SELECT 1 FROM completions WHERE batch_id=NEW.batch_id)
                    BEGIN SELECT RAISE(ABORT,'Batch is already complete'); END;
            ''')
            connection.execute('BEGIN IMMEDIATE')
            if not connection.execute("SELECT 1 FROM metadata WHERE key='schema_version'").fetchone():
                connection.execute("INSERT INTO metadata VALUES('schema_version','1')")
            for table in ('metadata', 'batches', 'pages', 'versions', 'completions'):
                for action in ('UPDATE', 'DELETE'):
                    connection.execute(f'''CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                        BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Append-only raw index'); END''')
            install_identity_guards(connection, ('metadata','batches','pages','versions','completions'))
            if connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != '1':
                raise RawStoreError('Unsupported raw index schema')

    def _path(self, relative):
        target = self.policy.authorize(self.directory / relative)
        if not target.is_relative_to(self.directory):
            raise RawStoreError('Raw store child escapes its root')
        return target

    @contextmanager
    def _connection(self, *, write=False):
        self.policy.authorize(self.database)
        with closing(sqlite3.connect(self.database, timeout=30, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield connection
                if connection.in_transaction:
                    connection.execute('COMMIT')
            except BaseException:
                if connection.in_transaction:
                    connection.execute('ROLLBACK')
                raise

    def _blob(self, body_hash):
        require_digest(body_hash)
        raw = self.policy.read_bytes(self._path('objects/' + body_hash + '.bin'))
        if digest(raw) != body_hash:
            raise RawStoreError('Raw blob hash mismatch; original not replaced')
        return raw

    def _publish(self, raw):
        body_hash = digest(raw)
        destination = self._path('objects/' + body_hash + '.bin')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if self._blob(body_hash) != raw:
                raise RawStoreError('Raw hash collision/conflict')
            return body_hash
        temporary = self._path('staging/' + uuid4().hex + '.bin')
        temporary.parent.mkdir(parents=True, exist_ok=True)
        try:
            with temporary.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if self._blob(body_hash) != raw:
                    raise RawStoreError('Concurrent raw blob conflict')
        finally:
            # Only this call's verified staging path is removed, never raw originals.
            if temporary.exists():
                self._path(temporary.relative_to(self.directory)).unlink()
        return body_hash

    def begin_batch(self, batch_id, context):
        if not isinstance(batch_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', batch_id):
            raise RawStoreError('Safe unique batch ID required')
        _context(context)
        encoded = canonical(context)
        key = request_key(context['request'])
        with self._connection(write=True) as connection:
            row = connection.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if row:
                if row['request_key'] != key or row['context'] != encoded:
                    raise RawStoreError('Batch context conflict; preserve old batch and use a new ID')
            else:
                connection.execute('INSERT INTO batches VALUES(?,?,?,?)', (batch_id, key, encoded, utc_timestamp()))
        return key

    def _batch(self, connection, batch_id):
        batch = connection.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not batch:
            raise RawStoreError('Unknown raw batch')
        context = _load(batch['context'])
        _context(context)
        _time(batch['started_at'])
        if request_key(context['request']) != batch['request_key']:
            raise RawStoreError('Stored request key mismatch')
        return batch, context

    def put_page(self, batch_id, ordinal, raw, reception):
        _count(ordinal)
        _page_meta(reception)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PAGE_BYTES:
            raise RawStoreError('Empty/oversized/non-byte response rejected')
        if any(value.encode('utf-8') in raw for value in secret_values()):
            raise RawStoreError('Scoped credential value in raw response rejected')
        body_hash = digest(raw)
        encoded = canonical(reception)
        length = reception['response_headers'].get('content-length')
        if length is not None and (not length.isdigit() or int(length) != len(raw)):
            raise RawStoreError('Response length header mismatch')
        bounds = reception['response_headers'].get('content-range')
        if bounds is not None:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', bounds)
            if not match or int(match[2]) - int(match[1]) + 1 != len(raw) or int(match[2]) >= int(match[3]):
                raise RawStoreError('Response range header mismatch')
        with self._connection(write=True) as connection:
            _, context = self._batch(connection, batch_id)
            if ordinal >= context['expected_pages']:
                raise RawStoreError('Page ordinal outside declared batch')
            declared_version = context['source_version']
            if declared_version is not None and reception['response_headers'].get('etag') != declared_version:
                raise RawStoreError('Provider version changed inside batch; use a new batch')
            existing = connection.execute('SELECT * FROM pages WHERE batch_id=? AND ordinal=?', (batch_id, ordinal)).fetchone()
            if existing:
                if existing['body_hash'] != body_hash or existing['byte_count'] != len(raw) or existing['reception'] != encoded:
                    raise RawStoreError('Page conflict; old original preserved')
                if self._blob(body_hash) != raw:
                    raise RawStoreError('Page integrity failed')
                return body_hash
            total = connection.execute('SELECT COALESCE(sum(byte_count),0) FROM pages WHERE batch_id=?', (batch_id,)).fetchone()[0]
            if total + len(raw) > MAX_BATCH_BYTES:
                raise RawStoreError('Batch byte limit exceeded')
            self._publish(raw)
            connection.execute('INSERT INTO pages VALUES(?,?,?,?,?,?)',
                (batch_id, ordinal, body_hash, len(raw), encoded, utc_timestamp()))
        return body_hash

    def _assembled(self, connection, batch_id, context):
        pages = connection.execute('SELECT * FROM pages WHERE batch_id=? ORDER BY ordinal', (batch_id,)).fetchall()
        if len(pages) != context['expected_pages'] or [p['ordinal'] for p in pages] != list(range(context['expected_pages'])):
            raise RawStoreError('Batch incomplete; committed pages remain resumable')
        bodies = []
        range_offset, range_total = 0, None
        has_ranges = any('content-range' in _load(page['reception'])['response_headers'] for page in pages)
        for page in pages:
            reception = _load(page['reception'])
            _page_meta(reception)
            _time(page['stored_at'])
            if context['source_version'] is not None and reception['response_headers'].get('etag') != context['source_version']:
                raise RawStoreError('Stored provider version mismatch')
            raw = self._blob(page['body_hash'])
            if len(raw) != page['byte_count'] or not 0 < len(raw) <= MAX_PAGE_BYTES:
                raise RawStoreError('Stored page length mismatch')
            if has_ranges:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', reception['response_headers'].get('content-range', ''))
                if not match:
                    raise RawStoreError('Every range page must have exact source bounds')
                first, last, total = map(int, match.groups())
                if first != range_offset or last-first+1 != len(raw) or last >= total or (range_total is not None and total != range_total):
                    raise RawStoreError('Repeated/gapped/changed source byte ranges')
                range_offset, range_total = last+1, total
            bodies.append(raw)
        raw = b''.join(bodies)
        if has_ranges and range_offset != range_total:
            raise RawStoreError('Source byte ranges do not cover the declared response')
        if len(raw) > MAX_BATCH_BYTES or (context['expected_response_hash'] is not None and digest(raw) != context['expected_response_hash']):
            raise RawStoreError('Assembled response differs from declared source hash')
        return raw

    def finish_batch(self, batch_id, *, record_count):
        _count(record_count, optional=True)
        with self._connection(write=True) as connection:
            batch, context = self._batch(connection, batch_id)
            raw = self._assembled(connection, batch_id, context)
            body_hash = digest(raw)
            if context['expected_records'] is not None and record_count != context['expected_records']:
                raise RawStoreError('Record count differs from declared source inventory')
            existing = connection.execute('SELECT * FROM completions WHERE batch_id=?', (batch_id,)).fetchone()
            if existing:
                if existing['body_hash'] != body_hash or existing['record_count'] != record_count:
                    raise RawStoreError('Completion conflict; old completion preserved')
                self._validate_completion(connection, existing, batch, context, raw)
                return dict(existing)
            self._publish(raw)
            version = connection.execute('SELECT version FROM versions WHERE request_key=? AND body_hash=?', (batch['request_key'], body_hash)).fetchone()
            if version:
                version = version[0]
                first_count = connection.execute('''SELECT record_count FROM completions WHERE batch_id=(
                    SELECT first_batch_id FROM versions WHERE request_key=? AND version=?)''',
                    (batch['request_key'], version)).fetchone()[0]
                if first_count is not None and record_count is not None and first_count != record_count:
                    raise RawStoreError('Identical response has conflicting record counts')
            else:
                version = connection.execute('SELECT COALESCE(max(version),0)+1 FROM versions WHERE request_key=?', (batch['request_key'],)).fetchone()[0]
                connection.execute('INSERT INTO versions VALUES(?,?,?,?)', (batch['request_key'], version, body_hash, batch_id))
            connection.execute('INSERT INTO completions VALUES(?,?,?,?,?,?,?,?)',
                (batch_id, batch['request_key'], version, body_hash, context['expected_pages'], len(raw), record_count, utc_timestamp()))
            return dict(connection.execute('SELECT * FROM completions WHERE batch_id=?', (batch_id,)).fetchone())

    def _validate_completion(self, connection, completion, batch, context, raw):
        _time(completion['finished_at'])
        _count(completion['record_count'], optional=True)
        if (completion['request_key'] != batch['request_key'] or completion['body_hash'] != digest(raw)
                or completion['byte_count'] != len(raw) or completion['page_count'] != context['expected_pages']
                or (context['expected_records'] is not None and completion['record_count'] != context['expected_records'])):
            raise RawStoreError('Completion metadata mismatch')
        version = connection.execute('SELECT * FROM versions WHERE request_key=? AND version=?',
            (completion['request_key'], completion['version'])).fetchone()
        if not version or version['body_hash'] != digest(raw) or self._blob(version['body_hash']) != raw:
            raise RawStoreError('Version binding mismatch')

    def read_version(self, key, version):
        require_digest(key)
        _count(version, positive=True)
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM versions WHERE request_key=? AND version=?', (key, version)).fetchone()
            if not row:
                raise RawStoreError('Unknown response version')
            batch, context = self._batch(connection, row['first_batch_id'])
            completion = connection.execute('SELECT * FROM completions WHERE batch_id=?', (row['first_batch_id'],)).fetchone()
            if not completion or batch['request_key'] != key or completion['version'] != version:
                raise RawStoreError('Version origin binding mismatch')
            raw = self._assembled(connection, row['first_batch_id'], context)
            self._validate_completion(connection, completion, batch, context, raw)
            return raw

    def manifest(self):
        from predictor.data.manifest import build_manifest
        return build_manifest(self)
