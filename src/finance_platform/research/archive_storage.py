"""Durable archive blobs through registered storage, retaining legacy readers.

No native working directories or executable package roots are allocated here.
Complete but unreferenced blobs survive a failed metadata transaction.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from finance_platform.core.contracts import payload
from finance_platform.core.database import immutable
from finance_platform.strategies.contracts import canonical, digest
from .data import ResearchError
from .payload_storage import storage, MAXIMUM

MIGRATION = ("""
CREATE TABLE research_stored_archives(run_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,binding_json TEXT NOT NULL);
CREATE TABLE research_stored_archive_files(run_id TEXT NOT NULL REFERENCES research_stored_archives(run_id),
 name TEXT NOT NULL,reference_json TEXT NOT NULL,PRIMARY KEY(run_id,name));
""",)


class StoredExperimentArchive:
    def __init__(self, runtime, legacy):
        self.runtime, self.database, self.legacy = runtime, runtime.database, legacy
        self.root = legacy.root  # Compatibility only; never a new write target.
        self.database.apply_module_migrations('research_stored_archives', MIGRATION)
        with self.database.transaction() as con:
            immutable(con, 'research_stored_archives', ('run_id',))
            immutable(con, 'research_stored_archive_files', ('run_id', 'name'))

    def _row(self, run_id, con=None):
        if type(run_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', run_id):
            raise ResearchError('EXPERIMENT_RUN_ID_INVALID')
        if con is not None:
            return con.execute('SELECT * FROM research_stored_archives WHERE run_id=?', (run_id,)).fetchone()
        with closing(self.database.connect()) as connection:
            return self._row(run_id, connection)

    def _put(self, binding, name, raw):
        if len(raw) > MAXIMUM:
            raise ResearchError('EXPERIMENT_FILE_TOO_LARGE')
        category = 'reports' if name == 'result.json' else 'artifacts'
        return storage(self.runtime).put(binding, category, raw, media_type='application/json' if name.endswith('.json') else 'application/octet-stream')

    def begin(self, run_id, spec, *, sample=None, data=None, configuration=None, code_paths, dependency_bytes, storage_binding):
        self._row(run_id)
        if self.legacy._path(run_id).exists():
            raise ResearchError('EXPERIMENT_RUN_ID_REUSED')
        paths = list(code_paths)
        code_root = Path(os.path.commonpath([str(Path(p).resolve().parent) for p in paths]))
        code = {Path(p).resolve().relative_to(code_root).as_posix(): Path(p).read_bytes() for p in paths}
        snapshot = payload(sample) if sample is not None else data
        if not isinstance(snapshot, dict) or snapshot.get('scope') not in {'SYNTHETIC', 'DEVELOPMENT'}:
            raise ResearchError('EXPERIMENT_SCOPE_INVALID')
        declaration = {'run_id': run_id, 'registered_at': datetime.now(timezone.utc).isoformat(),
                       'spec': payload(spec), 'sample_hash': digest(snapshot),
                       'code_hashes': {k: hashlib.sha256(v).hexdigest() for k, v in code.items()},
                       'source_root': code_root.name, 'dependency_hash': hashlib.sha256(dependency_bytes).hexdigest(),
                       'scope': snapshot['scope'], 'formal_research_approved': False}
        files = {'declaration.json': canonical(declaration), 'sample.json': canonical(snapshot),
                 'data_snapshot.json': canonical(data if data is not None else snapshot),
                 'configuration.json': canonical(configuration or {}), 'dependencies.lock': dependency_bytes,
                 'uv.lock': dependency_bytes, **{'code/' + k: v for k, v in code.items()}}
        references = {name: self._put(storage_binding, name, raw) for name, raw in files.items()}
        with self.database.transaction() as con:
            con.execute('INSERT INTO research_stored_archives VALUES(?,?,?)', (run_id, storage_binding['owner_id'], json.dumps(storage_binding)))
            for name, reference in references.items():
                con.execute('INSERT INTO research_stored_archive_files VALUES(?,?,?)', (run_id, name, json.dumps(reference)))
        return declaration

    def _append(self, run_id, files, *, terminal=False):
        row = self._row(run_id)
        if row is None:
            raise ResearchError('EXPERIMENT_NEW_ARCHIVE_BINDING_REQUIRED')
        binding = json.loads(row['binding_json'])
        references = {name: self._put(binding, name, raw) for name, raw in files.items()}
        with self.database.transaction() as con:
            if con.execute("SELECT 1 FROM research_stored_archive_files WHERE run_id=? AND name='terminal.json'", (run_id,)).fetchone():
                raise ResearchError('EXPERIMENT_TERMINAL_IMMUTABLE')
            for name, reference in references.items():
                con.execute('INSERT INTO research_stored_archive_files VALUES(?,?,?)', (run_id, name, json.dumps(reference)))

    def append_attempt(self, run_id, attempt):
        index = attempt['attempt']
        if type(index) is not int or not 1 <= index <= 16:
            raise ResearchError('EXPERIMENT_ATTEMPT_INVALID')
        self._append(run_id, {'attempts/%02d.json' % index: canonical(attempt)})

    def finish(self, run_id, result):
        self._append(run_id, {'result.json': canonical(result), 'terminal.json': canonical(
            {'status': 'SUCCEEDED', 'result_hash': digest(result), 'at': datetime.now(timezone.utc).isoformat()})}, terminal=True)

    def fail(self, run_id, code, *, interrupted=False):
        if type(code) is not str or not re.fullmatch('[A-Z][A-Z0-9_]{0,63}', code):
            code = 'RESEARCH_FAILED'
        self._append(run_id, {'terminal.json': canonical({'status': 'INTERRUPTED' if interrupted else 'FAILED',
                     'code': code, 'at': datetime.now(timezone.utc).isoformat()})}, terminal=True)

    def read(self, run_id):
        row = self._row(run_id)
        if row is None:
            return self.legacy.read(run_id)
        with closing(self.database.connect()) as con:
            files = con.execute('SELECT * FROM research_stored_archive_files WHERE run_id=?', (run_id,)).fetchall()
        refs = {v['name']: json.loads(v['reference_json']) for v in files}
        def get(name):
            category = 'reports' if name == 'result.json' else 'artifacts'
            return json.loads(storage(self.runtime).read(row['owner_id'], refs[name], category=category, maximum_bytes=MAXIMUM))
        declaration = get('declaration.json')
        terminal = get('terminal.json') if 'terminal.json' in refs else {'status': 'INTERRUPTED_OR_RUNNING'}
        result = get('result.json') if 'result.json' in refs else None
        if result is not None and terminal.get('status') == 'SUCCEEDED' and terminal.get('result_hash') != digest(result):
            raise ResearchError('EXPERIMENT_RESULT_HASH_MISMATCH')
        return {'declaration': declaration, 'terminal': terminal, 'result': result}
