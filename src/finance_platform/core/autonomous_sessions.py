"""Research execution journal, separate from formal research Ledger.

Only trusted adapters invoke dispatch/begin/complete/recover. Public interfaces
control intention; they cannot report fake work, choose handlers or load files.
Reservations are conservative consumption: never refunded, including crashes.
"""
from contextlib import closing, nullcontext
import base64
import hashlib
import json
import sqlite3
import time
import uuid

from .auth import Principal, require_owner, require_writer
from .contracts import RunMode, identifier
from .database import immutable
from .errors import PlatformError
from .security import now_text, safe_json
from .autonomous_models import SessionSpec, Control, Outcome, ResolvedInputs


HANDLER = 'autonomous_research_experiment'
MIGRATIONS = ("""
CREATE TABLE autonomous_sessions(session_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id),
 spec_json TEXT NOT NULL, spec_hash TEXT NOT NULL,resolved_json TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
 generation INTEGER NOT NULL, spent_json TEXT NOT NULL, active_attempt TEXT, candidate_hash TEXT,
 final_attempt TEXT, deadline REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE autonomous_keys(owner_id TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
 request_hash TEXT NOT NULL, response_json TEXT NOT NULL, PRIMARY KEY(owner_id,scope,key));
CREATE TABLE autonomous_attempts(attempt_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES autonomous_sessions(session_id),
 ordinal INTEGER NOT NULL, generation INTEGER NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES jobs(job_id), run_id TEXT NOT NULL REFERENCES runs(run_id),
 reservation_json TEXT NOT NULL, outcome_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(session_id,ordinal));
CREATE TABLE autonomous_events(event_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES autonomous_sessions(session_id),
 kind TEXT NOT NULL, metadata_json TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE INDEX autonomous_events_lookup ON autonomous_events(session_id,event_id);
CREATE TABLE autonomous_artifacts(artifact_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES autonomous_sessions(session_id),
 attempt_id TEXT NOT NULL REFERENCES autonomous_attempts(attempt_id), phase TEXT NOT NULL,
 kind TEXT NOT NULL, media_type TEXT NOT NULL, content TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
 logical_id TEXT NOT NULL,part_index INTEGER NOT NULL,part_count INTEGER NOT NULL,full_sha256 TEXT NOT NULL,
 UNIQUE(attempt_id,logical_id,part_index));
CREATE TABLE autonomous_exposures(input_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES autonomous_sessions(session_id),
 candidate_hash TEXT NOT NULL,attempt_id TEXT NOT NULL,recorded_at TEXT NOT NULL,
 source_id TEXT NOT NULL,security_id TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL);
CREATE TABLE autonomous_model_calls(call_id TEXT PRIMARY KEY,attempt_id TEXT NOT NULL REFERENCES autonomous_attempts(attempt_id),
 request_hash TEXT NOT NULL,status TEXT NOT NULL,tokens_reserved INTEGER NOT NULL,cost_reserved INTEGER NOT NULL,
 request_json TEXT NOT NULL,response_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(attempt_id,call_id));
CREATE TABLE autonomous_prefixes(attempt_id TEXT NOT NULL REFERENCES autonomous_attempts(attempt_id),symbol TEXT NOT NULL,
 ordinal INTEGER NOT NULL,request_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(attempt_id,symbol,ordinal));
""",)

# Separate append sequence avoids UUID lexical insertion and SQLite rowid
# renumbering during VACUUM. Revision1 bytes/hashes are intentionally unchanged.
MIGRATIONS += ("""
CREATE TABLE autonomous_artifact_positions(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 artifact_id TEXT NOT NULL UNIQUE REFERENCES autonomous_artifacts(artifact_id),
 session_id TEXT NOT NULL REFERENCES autonomous_sessions(session_id),
 attempt_id TEXT NOT NULL REFERENCES autonomous_attempts(attempt_id));
CREATE INDEX autonomous_artifact_position_lookup ON autonomous_artifact_positions(session_id,sequence);
CREATE INDEX autonomous_artifact_attempt_position ON autonomous_artifact_positions(session_id,attempt_id,sequence);
CREATE TRIGGER autonomous_artifact_assign_position AFTER INSERT ON autonomous_artifacts
BEGIN INSERT INTO autonomous_artifact_positions(artifact_id,session_id,attempt_id)
 VALUES (NEW.artifact_id,NEW.session_id,NEW.attempt_id); END;
""",)

MIGRATIONS += ("""
CREATE TABLE autonomous_artifact_payload_refs(
 artifact_id TEXT PRIMARY KEY REFERENCES autonomous_artifacts(artifact_id),
 owner_id TEXT NOT NULL,reference_json TEXT NOT NULL);
""",)

MIGRATIONS += ("""
CREATE INDEX autonomous_events_attempt_lookup
 ON autonomous_events(session_id,kind,json_extract(metadata_json,'$.attempt_id'),event_id)
 WHERE kind='CONTROLLER_RECORD' OR kind='STAGE';
""",)


def _page_cursor(session_id,kind,attempt_id,after):
    raw=json.dumps([1,session_id,kind,attempt_id,after],separators=(',',':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _cursor_position(cursor,session_id,kind,attempt_id):
    if cursor=='':return 0
    if type(cursor)is not str or len(cursor)>1024 or not cursor:
        raise PlatformError('INVALID_INPUT',422)
    try:
        raw=base64.b64decode(cursor+'='*(-len(cursor)%4),altchars=b'-_',validate=True)
        value=json.loads(raw)
        if (type(value)is not list or len(value)!=5 or value[:4]!=[1,session_id,kind,attempt_id]
                or type(value[4])is not int or not 0<=value[4]<=9223372036854775807):
            raise ValueError()
        if _page_cursor(session_id,kind,attempt_id,value[4])!=cursor:raise ValueError()
        return value[4]
    except (ValueError,TypeError,UnicodeError):
        raise PlatformError('INVALID_INPUT',422) from None


def digest(value):
    return hashlib.sha256(safe_json(value).encode()).hexdigest()


class AutonomousSessions:
    def __init__(self, database, jobs, *, resolve_inputs, clock=time.time, require_current_ohlcv=False):
        if not callable(resolve_inputs):
            raise PlatformError('INVALID_INPUT', 422)
        self.database, self.jobs, self.resolve_inputs, self.clock = database, jobs, resolve_inputs, clock
        self.require_current_ohlcv = require_current_ohlcv

    def migrate(self):
        self.database.apply_module_migrations('autonomous_research', MIGRATIONS)
        with self.database.transaction() as con:
            # Only new append-only sidecar records; original artifacts remain
            # immutable. Run migration/backfill before dispatching services.
            con.execute('INSERT INTO autonomous_artifact_positions(artifact_id,session_id,attempt_id) '
                        'SELECT a.artifact_id,a.session_id,a.attempt_id FROM autonomous_artifacts a '
                        'WHERE NOT EXISTS (SELECT 1 FROM autonomous_artifact_positions p WHERE p.artifact_id=a.artifact_id) ORDER BY a.rowid')
            for table, keys in (('autonomous_events', ('event_id',)),
                                ('autonomous_artifacts', ('artifact_id',)),
                                ('autonomous_exposures', ('input_hash',)),
                                ('autonomous_prefixes', ('attempt_id','symbol','ordinal')),
                                ('autonomous_keys', ('owner_id', 'scope', 'key'))):
                immutable(con, table, keys)
            immutable(con,'autonomous_artifact_positions',('sequence',))
            immutable(con,'autonomous_artifact_payload_refs',('artifact_id',))

    def _row(self, con, principal, session_id):
        row = con.execute('SELECT * FROM autonomous_sessions WHERE session_id=?', (session_id,)).fetchone()
        if not row:
            raise PlatformError('NOT_FOUND', 404)
        require_owner(principal, row['owner_id'])
        return row

    def _event(self, con, sid, kind, metadata=None):
        con.execute('INSERT INTO autonomous_events(session_id,kind,metadata_json,recorded_at) VALUES (?,?,?,?)',
                    (sid, kind, safe_json(metadata or {}), now_text()))

    def _view(self, row):
        value = dict(row)
        for name in ('spec', 'spent', 'resolved'):
            value[name] = json.loads(value.pop(name + '_json'))
        value['resolved'].pop('model_profiles',None)
        value['resolved'].pop('candidate_context',None)
        value['schema']='autonomous-session-1'
        value['evaluation_scope']='FINAL_SEPARATE' if value['spec']['final_test'] else 'DEVELOPMENT_ONLY'
        value['budget_usage']={'conservative_consumed':value['spent'],'reservation_refunded':False,'billed':'UNKNOWN','currency':None,'price_basis':'UNKNOWN','cost_policy':value['spec']['cost_policy'],'cost_limit_guaranteed':False}
        states={'DRAFT':['run','cancel'],'READY':['pause','cancel'],'RUNNING':['pause','cancel'],'PAUSED':['resume','restart','cancel'],'BLOCKED':['resume','restart','cancel'],'SEALED':['cancel']}
        value['allowed_actions']=states.get(value['status'],[])
        if value['spec']['final_test'] and value['status'] in {'READY','PAUSED'} and not value['active_attempt'] and not value['final_attempt']:
            value['allowed_actions']=[*value['allowed_actions'],'seal_final']
        if value['candidate_hash'] or value['final_attempt']:
            value['allowed_actions']=[a for a in value['allowed_actions'] if a not in {'run','resume','restart','seal_final'}]
        return value

    def _cached(self, con, owner, scope, key, request):
        identifier(key)
        row = con.execute('SELECT * FROM autonomous_keys WHERE owner_id=? AND scope=? AND key=?', (owner, scope, key)).fetchone()
        if row:
            if row['request_hash'] != digest(request):
                raise PlatformError('CONFLICT', 409)
            return json.loads(row['response_json'])

    def _remember(self, con, owner, scope, key, request, response):
        con.execute('INSERT INTO autonomous_keys VALUES (?,?,?,?,?)', (owner, scope, key, digest(request), safe_json(response)))

    def create(self, principal, spec, *, idempotency_key, goal_link=None, goal_deadline=None, goal_round=None):
        require_writer(principal)
        spec = SessionSpec.model_validate(spec).model_dump()
        if goal_round is not None and (type(goal_round) is not str or not callable(goal_link) or goal_deadline is None
                                       or not callable(getattr(self, 'goal_guard', None))
                                       or spec['final_test'] is not None or spec['reuse_policy'] != 'context'):
            raise PlatformError('GOAL_ROUND_INPUT_UNSUPPORTED',422)
        options = {'goal_round':goal_round} if goal_round is not None else {}
        command = {'spec':spec, **options} if options else spec
        # Trusted owner adapters MUST resolve/freeze all refs and verify chronological
        # disjointness, price basis, and model target ownership. No silent fallback.
        with self.database.transaction() as con:
            cached = self._cached(con, principal.user_id, 'create', idempotency_key, command)
            if cached is not None:
                return cached
        resolved = ResolvedInputs.model_validate(self.resolve_inputs(principal, SessionSpec.model_validate(spec), **options)).model_dump()
        if bool(spec['final_test'])!=bool(resolved['final_windows']):raise PlatformError('CONFLICT',409)
        if any(m['name'] not in resolved['scoring_metrics'] for m in spec['scoring']):
            raise PlatformError('UNSUPPORTED_CAPABILITY', 422)
        for dev in resolved['development_windows']:
            for final in resolved['final_windows']:
                if dev['source_id'] == final['source_id'] and set(dev['security_ids']) & set(final['security_ids']) and dev['start'] <= final['end'] and final['start'] <= dev['end']:
                    raise PlatformError('CONFLICT', 409)
        with self.database.transaction() as con:
            cached = self._cached(con, principal.user_id, 'create', idempotency_key, command)
            if cached is not None:
                return cached
            sid, stamp = str(uuid.uuid4()), now_text()
            self._unexposed(con, resolved['development_windows'])
            deadline = self.clock() + spec['deadline_seconds']
            if goal_deadline is not None:
                deadline = min(deadline, goal_deadline)
                if self.clock() >= deadline:
                    raise PlatformError('GOAL_DEADLINE_EXCEEDED',409)
            if goal_round is not None:
                budget, reservation = spec['budget'], spec['experiment']
                attempts = budget['attempts']
                if (deadline - self.clock() <= attempts * reservation['compute_seconds']
                        or any(budget[key] < attempts * cost for key,cost in reservation.items())):
                    raise PlatformError('GOAL_ROUND_BUDGET_INSUFFICIENT',422)
            con.execute('INSERT INTO autonomous_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (sid, principal.user_id, safe_json(spec), digest(spec), safe_json(resolved), 'DRAFT', 1, 1,
                         safe_json({k: 0 for k in spec['budget']}), None, None, None,
                         deadline, stamp, stamp))
            if goal_link is not None:
                goal_link(con,sid)
            self._event(con, sid, 'CREATED', {'spec_hash': digest(spec), **options})
            response = self._view(self._row(con, principal, sid))
            self._remember(con, principal.user_id, 'create', idempotency_key, command, response)
            return response

    def _latest_execution_progress(self, con, row):
        attempt_id=row['active_attempt']
        if attempt_id:
            attempt=con.execute('SELECT attempt_id FROM autonomous_attempts WHERE session_id=? AND attempt_id=?',
                                (row['session_id'],attempt_id)).fetchone()
        else:
            attempt=con.execute('SELECT attempt_id FROM autonomous_attempts WHERE session_id=? ORDER BY ordinal DESC LIMIT 1',
                                (row['session_id'],)).fetchone()
        if attempt is None:return None
        event=con.execute("SELECT event_id,recorded_at,metadata_json FROM autonomous_events WHERE session_id=? AND kind='STAGE' AND json_extract(metadata_json,'$.attempt_id')=? AND json_extract(metadata_json,'$.stage')='execute' AND json_type(metadata_json,'$.completed_prefixes')='integer' AND json_type(metadata_json,'$.total_prefixes')='integer' ORDER BY event_id DESC LIMIT 1",
                          (row['session_id'],attempt['attempt_id'])).fetchone()
        if event is None:return None
        if type(event['event_id'])is not int or event['event_id']<=0 or type(event['recorded_at'])is not str or not event['recorded_at']:return None
        value=json.loads(event['metadata_json'])
        completed,total=value.get('completed_prefixes'),value.get('total_prefixes')
        symbol,index=value.get('current_symbol'),value.get('current_index')
        if type(completed)is not int or type(total)is not int or not (0<=completed<=total and 1<=total<=2000):return None
        if completed==0:
            if symbol is not None or index is not None:return None
        elif type(symbol)is not str or type(index)is not int or not 0<=index<2000:return None
        if completed:
            try:identifier(symbol)
            except PlatformError:return None
        return {'attempt_id':attempt['attempt_id'],'event_id':event['event_id'],'recorded_at':event['recorded_at'],
                'stage':'execute','completed_prefixes':completed,'total_prefixes':total,
                'current_symbol':symbol,'current_index':index}

    def get(self, principal, session_id):
        with closing(self.database.connect()) as con:
            row=self._row(con, principal, session_id)
            value=self._view(row)
            value['latest_execution_progress']=self._latest_execution_progress(con,row)
            return value

    def list(self, principal, *, limit=50, offset=0):
        self.jobs._paging(limit, offset)
        with closing(self.database.connect()) as con:
            rows = con.execute('SELECT * FROM autonomous_sessions WHERE owner_id=? ORDER BY created_at,session_id LIMIT ? OFFSET ?',
                               (principal.user_id, limit, offset)).fetchall()
            return {'items': [self._view(r) for r in rows], 'limit': limit, 'offset': offset}

    def control(self, principal, session_id, body):
        require_writer(principal)
        control = Control.model_validate(body)
        request = control.model_dump()
        with self.database.transaction() as con:
            row = self._row(con, principal, session_id)
            cached = self._cached(con, principal.user_id, session_id, control.idempotency_key, request)
            if cached is not None:
                return cached
            if row['revision'] != control.expected_revision:
                raise PlatformError('CONFLICT', 409)
            action, state, generation = control.action, row['status'], row['generation']
            candidate = row['candidate_hash']
            if action in {'run', 'resume', 'restart'}:
                allowed = {'run': {'DRAFT', 'READY'}, 'resume': {'PAUSED', 'BLOCKED'},
                           'restart': {'CANCELLED', 'BLOCKED', 'PAUSED'}}[action]
                if state not in allowed or row['active_attempt'] or row['final_attempt'] or candidate:
                    raise PlatformError('CONFLICT', 409)
                unknown = con.execute("SELECT 1 FROM autonomous_attempts WHERE session_id=? AND status='UNKNOWN'", (session_id,)).fetchone()
                if unknown and not control.accept_unknown_charge:
                    raise PlatformError('CONFLICT', 409)
                state = 'READY'
                generation += int(action == 'restart')
            elif action in {'pause', 'cancel'}:
                if state in {'COMPLETED', 'CANCELLED', 'BUDGET_EXHAUSTED'}:
                    raise PlatformError('CONFLICT', 409)
                if row['active_attempt']:
                    # Same database transaction; no nested cancel transaction.
                    a = con.execute('SELECT * FROM autonomous_attempts WHERE attempt_id=?', (row['active_attempt'],)).fetchone()
                    j = con.execute('SELECT * FROM jobs WHERE job_id=?', (a['job_id'],)).fetchone()
                    if j['status'] == 'QUEUED':
                        con.execute("UPDATE jobs SET status='CANCELLED',cancel_requested=1,updated_at=? WHERE job_id=?", (now_text(), j['job_id']))
                        self.jobs._event(con, j['job_id'], j['run_id'], 'CANCELLED', {'reason': action})
                        con.execute("UPDATE autonomous_attempts SET status='FAILED',updated_at=? WHERE attempt_id=?", (now_text(), a['attempt_id']))
                        con.execute('UPDATE autonomous_sessions SET active_attempt=NULL WHERE session_id=?', (session_id,))
                        state = 'PAUSED' if action == 'pause' else 'CANCELLED'
                    else:
                        con.execute('UPDATE jobs SET cancel_requested=1,updated_at=? WHERE job_id=?', (now_text(), j['job_id']))
                        self.jobs._event(con, j['job_id'], j['run_id'], 'CANCEL_REQUESTED', {'reason': action})
                        state = 'PAUSE_REQUESTED' if action == 'pause' else 'CANCEL_REQUESTED'
                else:
                    state = 'PAUSED' if action == 'pause' else 'CANCELLED'
            elif action == 'seal_final':
                if not json.loads(row['spec_json'])['final_test'] or state not in {'READY', 'PAUSED'} or row['active_attempt'] or row['final_attempt'] or not control.candidate_hash:
                    raise PlatformError('CONFLICT', 409)
                artifact = con.execute("SELECT 1 FROM autonomous_artifacts WHERE session_id=? AND full_sha256=? AND phase='development' AND kind IN ('strategy','factor')", (session_id, control.candidate_hash)).fetchone()
                if not artifact:
                    raise PlatformError('NOT_FOUND', 404)
                final_windows=json.loads(row['resolved_json'])['final_windows']
                self._require_unseen_final(con, final_windows, control.candidate_hash)
                if self.require_current_ohlcv:
                    self.verified_final_origin(con, principal, session_id, control.candidate_hash)
                candidate, state = control.candidate_hash, 'SEALED'
                self._event(con, session_id, 'FINAL_SEALED', {'candidate_hash': candidate})
            con.execute('UPDATE autonomous_sessions SET status=?,generation=?,candidate_hash=?,revision=revision+1,updated_at=? WHERE session_id=?',
                        (state, generation, candidate, now_text(), session_id))
            self._event(con, session_id, 'CONTROL', {'action': action, 'state': state, 'unknown_charge_accepted': control.accept_unknown_charge})
            response = self._view(self._row(con, principal, session_id))
            self._remember(con, principal.user_id, session_id, control.idempotency_key, request, response)
            return response

    def dispatch(self, principal, session_id):
        """Trusted scheduler entry; at most one bounded job, never a direct model call."""
        require_writer(principal)
        # Handler validation/provenance stays in JobService, not invented here.
        params = {'session_id': session_id}
        mode, definition, _ = self.jobs._validate(principal, HANDLER, params, RunMode.LOCAL_SIMULATION, None)
        with self.database.transaction() as con:
            row = self._row(con, principal, session_id)
            if row['active_attempt'] or row['status'] not in {'READY', 'SEALED'}:
                return None
            guard = getattr(self,'goal_guard',None)
            if guard is not None and not guard(con,row['owner_id'],'session',session_id,dispatch=True):
                return None
            previous = con.execute('SELECT j.status FROM autonomous_attempts a JOIN jobs j USING(job_id) WHERE a.session_id=? ORDER BY a.ordinal DESC LIMIT 1', (session_id,)).fetchone()
            if previous and previous['status'] not in {'SUCCEEDED', 'FAILED', 'CANCELLED'}:
                return None
            spec, spent = json.loads(row['spec_json']), json.loads(row['spent_json'])
            resolved = json.loads(row['resolved_json'])
            self._unexposed(con, resolved['development_windows'])
            reservation = spec['experiment']
            if self.clock() >= row['deadline'] or any(spent[k] + reservation[k] > spec['budget'][k] for k in spent):
                con.execute("UPDATE autonomous_sessions SET status='BUDGET_EXHAUSTED',revision=revision+1,updated_at=? WHERE session_id=?", (now_text(), session_id))
                self._event(con, session_id, 'BUDGET_EXHAUSTED')
                return None
            created = con.execute("SELECT metadata_json FROM autonomous_events WHERE session_id=? AND kind='CREATED' ORDER BY event_id LIMIT 1",
                                  (session_id,)).fetchone()
            metadata = json.loads(created['metadata_json']) if created is not None else {}
            if metadata.get('goal_round') is not None:
                if guard is None:
                    raise PlatformError('GOAL_CHILD_BINDING_REQUIRED',409)
                if metadata.get('spec_hash') != row['spec_hash'] or digest(spec) != row['spec_hash']:
                    raise PlatformError('GOAL_RESEARCH_SPEC_CHANGED',409)
                if row['deadline'] - self.clock() <= reservation['compute_seconds']:
                    completed = con.execute("""SELECT 1 FROM autonomous_attempts a JOIN jobs j USING(job_id)
                        WHERE a.session_id=? AND a.phase='development' AND a.status='SUCCEEDED'
                            AND j.status='SUCCEEDED' LIMIT 1""", (session_id,)).fetchone()
                    if completed is not None:
                        con.execute("UPDATE autonomous_sessions SET status='BUDGET_EXHAUSTED',revision=revision+1,updated_at=? WHERE session_id=?", (now_text(), session_id))
                        self._event(con, session_id, 'BUDGET_EXHAUSTED', {'code':'GOAL_ROUND_BUDGET_INSUFFICIENT'})
                        return None
                    raise PlatformError('GOAL_ROUND_BUDGET_INSUFFICIENT',422)
            if row['status'] == 'SEALED' and row['final_attempt']:
                raise PlatformError('CONFLICT', 409)
            attempt = str(uuid.uuid4())
            phase = 'final_test' if row['status'] == 'SEALED' else 'development'
            if phase == 'final_test':
                try:
                    self._require_unseen_final(con, resolved['final_windows'], row['candidate_hash'])
                except PlatformError as error:
                    if error.code!='FINAL_DEVELOPMENT_IN_PROGRESS':
                        raise
                    return None  # Still sealed, without creating or charging an attempt.
                if self.require_current_ohlcv:
                    self.verified_final_origin(con, principal, session_id, row['candidate_hash'])
                for window in resolved['final_windows']:
                    for security in window['security_ids']:
                        identity = digest({'source': window['source_id'], 'security': security, 'start': window['start'], 'end': window['end']})
                        con.execute('INSERT INTO autonomous_exposures VALUES (?,?,?,?,?,?,?,?,?)',
                                    (identity, session_id, row['candidate_hash'], attempt, now_text(), window['source_id'], security, window['start'], window['end']))
                self._event(con, session_id, 'FINAL_EXPOSED', {'input_hash': spec['final_test']['content_hash'], 'candidate_hash': row['candidate_hash'], 'attempt_id': attempt})
            elif self.require_current_ohlcv and spec.get('reuse_policy') != 'evaluate_saved':
                self._verified_development_history(con, principal, session_id)
            # Atomic job + reservation + attempt insertion eliminates submit crash gaps.
            job = self.jobs._insert(con, row['owner_id'], definition, safe_json(params), mode)
            ordinal = con.execute('SELECT COUNT(*) FROM autonomous_attempts WHERE session_id=?', (session_id,)).fetchone()[0] + 1
            stamp = now_text()
            con.execute('INSERT INTO autonomous_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (attempt, session_id, ordinal, row['generation'], phase, 'RESERVED', job['job_id'], job['run_id'], safe_json(reservation), None, stamp, stamp))
            consumed = {k: spent[k] + reservation[k] for k in spent}
            con.execute("UPDATE autonomous_sessions SET status='RUNNING',active_attempt=?,final_attempt=?,spent_json=?,revision=revision+1,updated_at=? WHERE session_id=?",
                        (attempt, attempt if phase == 'final_test' else row['final_attempt'], safe_json(consumed), stamp, session_id))
            self._event(con, session_id, 'RESERVED', {'attempt_id': attempt, 'job_id': job['job_id'], 'phase': phase, 'reservation': reservation})
            return job

    def _unexposed(self, con, windows):
        for window in windows:
            for security in window['security_ids']:
                if con.execute("SELECT 1 FROM autonomous_exposures WHERE source_id=? AND (security_id=? OR security_id='*' OR ?='*') AND start<=? AND end>=?",
                               (window['source_id'], security, security, window['end'], window['start'])).fetchone():
                    raise PlatformError('CONFLICT', 409)

    def _require_unseen_final(self, con, windows, candidate_hash):
        # Final-to-final exposures remain global and immutable. Compare frozen
        # underlying lineage, instruments and inclusive dates, never dataset IDs.
        self._unexposed(con, windows)
        historical_overlap = """
            WITH related(session_id) AS (
                SELECT session_id FROM autonomous_artifacts
                WHERE phase='development' AND kind IN ('strategy','factor') AND full_sha256=:candidate
                UNION
                SELECT s.session_id FROM autonomous_sessions s
                WHERE EXISTS (
                    SELECT 1 FROM json_each(s.resolved_json,'$.candidate_context') seed
                    WHERE json_extract(seed.value,'$.source_sha256')=:candidate
                ) AND EXISTS (
                    SELECT 1 FROM autonomous_attempts a JOIN autonomous_events e
                      ON e.session_id=a.session_id AND e.kind='STARTED'
                      AND json_extract(e.metadata_json,'$.attempt_id')=a.attempt_id
                    WHERE a.session_id=s.session_id AND a.phase='development'
                )
            )
            SELECT 1 FROM related r JOIN autonomous_sessions s USING(session_id)
            CROSS JOIN json_each(s.resolved_json,'$.development_windows') w
            WHERE json_extract(w.value,'$.source_id')=:source
              AND json_extract(w.value,'$.start')<=:end AND json_extract(w.value,'$.end')>=:start
              AND EXISTS (SELECT 1 FROM json_each(w.value,'$.security_ids') old
                          WHERE old.value=:security OR old.value='*' OR :security='*')
            LIMIT 1
        """
        # An active overlapping development attempt may not have committed
        # source yet. Defer final admission until its source identity is known.
        # Include RESERVED attempts, so an already queued job cannot begin
        # after the final claim. New dispatches still use the original guard.
        active_overlap = """
            SELECT 1 FROM autonomous_attempts a JOIN autonomous_sessions s USING(session_id)
            CROSS JOIN json_each(s.resolved_json,'$.development_windows') w
            WHERE a.phase='development' AND a.status IN ('RESERVED','IN_FLIGHT')
              AND s.active_attempt=a.attempt_id
              AND json_extract(w.value,'$.source_id')=:source
              AND json_extract(w.value,'$.start')<=:end AND json_extract(w.value,'$.end')>=:start
              AND EXISTS (SELECT 1 FROM json_each(w.value,'$.security_ids') old
                          WHERE old.value=:security OR old.value='*' OR :security='*')
            LIMIT 1
        """
        for window in windows:
            for security in window['security_ids']:
                bound={'candidate':candidate_hash,'source':window['source_id'],
                       'start':window['start'],'end':window['end'],'security':security}
                if con.execute(historical_overlap, bound).fetchone():
                    raise PlatformError('FINAL_CANDIDATE_DEVELOPMENT_OVERLAP',409)
                if con.execute(active_overlap, bound).fetchone():
                    raise PlatformError('FINAL_DEVELOPMENT_IN_PROGRESS',409)

    def _attempt(self, con, context):
        context.assert_fenced(con)
        row = con.execute('SELECT * FROM autonomous_attempts WHERE job_id=? AND run_id=?', (context.job_id, context.run.run_id)).fetchone()
        if not row:
            raise PlatformError('NOT_FOUND', 404)
        session = con.execute('SELECT * FROM autonomous_sessions WHERE session_id=?', (row['session_id'],)).fetchone()
        if session['owner_id'] != context.run.owner_id or session['active_attempt'] != row['attempt_id']:
            raise PlatformError('LEASE_LOST', 409)
        guard = getattr(self, 'goal_guard', None)
        if guard is not None:
            guard(con, session['owner_id'], 'session', session['session_id'])
        return row, session

    def require_effect_active(self, context, attempt_id, *, call_id=None):
        """Recheck the original lease, parent binding and reservation before transport."""
        with closing(self.database.connect()) as con:
            con.execute('BEGIN')
            attempt, session = self._attempt(con, context)
            if (attempt['attempt_id'] != attempt_id or attempt['status'] != 'IN_FLIGHT'
                    or session['status'] != 'RUNNING' or self.clock() >= session['deadline']):
                raise PlatformError('LEASE_LOST', 409)
            if call_id is not None and con.execute("""SELECT 1 FROM autonomous_model_calls
                    WHERE call_id=? AND attempt_id=? AND status='REQUESTED'""", (call_id, attempt_id)).fetchone() is None:
                raise PlatformError('GOAL_MODEL_CALL_BINDING_REQUIRED', 409)

    def begin(self, context):
        with self.database.transaction() as con:
            attempt, session = self._attempt(con, context)
            if attempt['status'] != 'RESERVED' or session['status'] != 'RUNNING' or self.clock() >= session['deadline']:
                raise PlatformError('CONFLICT', 409)
            con.execute("UPDATE autonomous_attempts SET status='IN_FLIGHT',updated_at=? WHERE attempt_id=?", (now_text(), attempt['attempt_id']))
            self._event(con, session['session_id'], 'STARTED', {'attempt_id': attempt['attempt_id']})
            spec = json.loads(session['spec_json'])
            # Host loop gets no final_test reference until final sealing.
            if attempt['phase'] == 'development':
                spec.pop('final_test')
            resolved = json.loads(session['resolved_json'])
            if attempt['phase'] == 'development':
                resolved.pop('final_windows')
            committed, checkpoint, latest_outcome = [], [], None
            if attempt['phase'] == 'development':
                seeds = resolved.get('candidate_context', [])
                if self.require_current_ohlcv and spec.get('reuse_policy') != 'evaluate_saved':
                    owner = con.execute('SELECT user_id,username,role FROM users WHERE user_id=? AND enabled=1',
                                        (session['owner_id'],)).fetchone()
                    if owner is None:
                        raise PlatformError('FORBIDDEN',403)
                    committed, checkpoint, latest_outcome = self._verified_development_history(
                        con, Principal(owner['user_id'], owner['username'], owner['role']), session['session_id'])
                elif not self.require_current_ohlcv:
                    history = con.execute("SELECT attempt_id,outcome_json FROM autonomous_attempts WHERE session_id=? AND phase='development' AND outcome_json IS NOT NULL ORDER BY ordinal DESC LIMIT 20", (session['session_id'],)).fetchall()
                    latest = self._latest_iteration_checkpoint(con, session['session_id'])
                    checkpoint = [] if latest is None else self._checkpoint_refs(con, session['session_id'], latest['attempt_id'])
                    committed = [{'attempt_id': h['attempt_id'], 'outcome': json.loads(h['outcome_json'])} for h in reversed(history)]
                committed = committed[-(20-len(seeds)):]
                # The checkpoint's successful outcome is pinned evidence. Keep
                # it even when newer outcomes or explicit seeds fill the window.
                if latest_outcome is not None and latest_outcome not in committed:
                    committed.insert(0, latest_outcome)
                committed = list(seeds) + committed
            return {'session_id': session['session_id'], 'attempt_id': attempt['attempt_id'], 'phase': attempt['phase'],
                    'spec': spec, 'spec_hash': session['spec_hash'], 'resolved': resolved, 'generation': session['generation'],
                    'development_history': committed,
                    'checkpoint_refs': [dict(c) for c in checkpoint], 'history_window': 20,
                    'candidate_hash': session['candidate_hash'], 'reservation': json.loads(attempt['reservation_json'])}

    @staticmethod
    def _latest_iteration_checkpoint(con, session_id):
        # FA010 G2: formal checkpoint from the newest successful development
        # attempt, never an interrupted plan-checkpoint or final artifact.
        return con.execute("""
            SELECT a.attempt_id FROM autonomous_artifacts a
            JOIN autonomous_attempts t ON t.attempt_id=a.attempt_id AND t.session_id=a.session_id
            JOIN jobs j ON j.job_id=t.job_id AND j.run_id=t.run_id
            JOIN autonomous_sessions owner ON owner.session_id=t.session_id AND owner.owner_id=j.owner_id
            WHERE a.session_id=? AND a.phase='development' AND a.kind='checkpoint' AND a.logical_id='checkpoint'
              AND t.phase='development' AND t.status='SUCCEEDED' AND j.status='SUCCEEDED'
            ORDER BY t.ordinal DESC,a.part_index LIMIT 1
        """, (session_id,)).fetchone()

    @staticmethod
    def _checkpoint_refs(con, session_id, attempt_id):
        return con.execute("""
            SELECT artifact_id,attempt_id,sha256,full_sha256,logical_id,part_index,part_count
            FROM autonomous_artifacts WHERE session_id=? AND attempt_id=? AND phase='development'
              AND logical_id='checkpoint' ORDER BY part_index LIMIT 257
        """, (session_id,attempt_id)).fetchall()

    def _verified_iteration(self, con, principal, session_id, attempt_id):
        from fa_research_executor.protocol import OHLCV_BINDING, Rejected, strict_json

        owner = self._row(con, principal, session_id)['owner_id']

        def read_group(logical_id, media_type, kinds):
            rows = con.execute("""
                SELECT artifact_id FROM autonomous_artifacts
                WHERE session_id=? AND attempt_id=? AND phase='development' AND logical_id=?
                ORDER BY part_index LIMIT 257
            """, (session_id,attempt_id,logical_id)).fetchall()
            if not rows and logical_id == 'checkpoint':
                raise PlatformError('OHLCV_BINDING_MISMATCH',409)
            if not rows or len(rows) > 256:
                raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409)
            parts = [
                self._artifact(con, owner, session_id, row['artifact_id'], storage_connection=con)
                for row in rows
            ]
            first = parts[0]
            if first['kind'] not in kinds:
                raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409)
            chunks = []
            for index, part in enumerate(parts):
                data = part['content'].encode('utf-8')
                if (part['part_index'] != index or part['part_count'] != len(parts)
                        or part['attempt_id'] != attempt_id or part['logical_id'] != logical_id
                        or part['kind'] != first['kind'] or part['media_type'] != media_type
                        or part['full_sha256'] != first['full_sha256'] or len(data) != part['bytes']
                        or len(data) > 65536 or hashlib.sha256(data).hexdigest() != part['sha256']):
                    raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409)
                chunks.append(data)
            raw = b''.join(chunks)
            if hashlib.sha256(raw).hexdigest() != first['full_sha256']:
                raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409)
            return raw, first['full_sha256'], first['kind']

        raw, _, _ = read_group('checkpoint', 'application/json', {'checkpoint'})
        try:
            checkpoint = strict_json(raw, 16777216)
        except Rejected:
            raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409) from None
        if (type(checkpoint) is not dict or checkpoint.get('schema') != 'research-iteration-checkpoint-2'
                or checkpoint.get('ohlcv_binding') != OHLCV_BINDING):
            raise PlatformError('OHLCV_BINDING_MISMATCH',409)
        source, source_hash, kind = read_group('source', 'text/x-python', {'strategy','factor'})
        source = source.decode('utf-8')
        if (checkpoint.get('attempt_id') != attempt_id or checkpoint.get('source_sha256') != source_hash
                or checkpoint.get('source') != source or checkpoint.get('kind') != kind
                or checkpoint.get('next_kind') not in ('strategy','factor')
                or type(checkpoint.get('engine_options')) is not dict
                or type(checkpoint.get('frozen_factor_thresholds')) is not dict):
            raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409)
        return checkpoint, source

    def _verified_development_history(self, con, principal, session_id):
        rows = con.execute("""
            SELECT a.attempt_id,a.status,a.outcome_json,j.status AS job_status
            FROM autonomous_attempts a JOIN jobs j ON j.job_id=a.job_id AND j.run_id=a.run_id
            JOIN autonomous_sessions owner ON owner.session_id=a.session_id AND owner.owner_id=j.owner_id
            WHERE a.session_id=? AND a.phase='development' AND a.status IN ('SUCCEEDED','FAILED','UNKNOWN')
            ORDER BY a.ordinal DESC LIMIT 20
        """, (session_id,)).fetchall()
        latest = self._latest_iteration_checkpoint(con, session_id)
        # A completed iteration with no formal checkpoint is not a fresh start.
        newest_success = con.execute("""
            SELECT a.attempt_id,a.outcome_json FROM autonomous_attempts a JOIN jobs j ON j.job_id=a.job_id AND j.run_id=a.run_id
            JOIN autonomous_sessions owner ON owner.session_id=a.session_id AND owner.owner_id=j.owner_id
            WHERE a.session_id=? AND a.phase='development' AND a.status='SUCCEEDED' AND j.status='SUCCEEDED'
            ORDER BY a.ordinal DESC LIMIT 1
        """, (session_id,)).fetchone()
        if latest is not None and (newest_success is None or latest['attempt_id'] != newest_success['attempt_id']):
            raise PlatformError('OHLCV_BINDING_MISMATCH',409)
        if newest_success is not None and latest is None:
            raise PlatformError('OHLCV_BINDING_MISMATCH',409)

        def decode_outcome(row):
            try:
                outcome = json.loads(row['outcome_json']) if row['outcome_json'] else {}
            except (ValueError, TypeError):
                raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409) from None
            if type(outcome) is not dict:
                raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409)
            return outcome

        def verified_success(attempt_id, outcome):
            if outcome.get('status') != 'SUCCEEDED':
                raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409)
            checkpoint, _ = self._verified_iteration(con, principal, session_id, attempt_id)
            return {'attempt_id':attempt_id,'outcome':outcome,'ohlcv_binding':checkpoint['ohlcv_binding']}

        committed, latest_outcome = [], None
        for row in reversed(rows):
            attempt_id = row['attempt_id']
            # UNKNOWN and inconsistent terminal states remain in the journal.
            # They cannot become native success or failed feedback inputs.
            if row['status'] == 'UNKNOWN' or row['status'] == 'SUCCEEDED' and row['job_status'] != 'SUCCEEDED':
                continue
            outcome = decode_outcome(row)
            if row['status'] == row['job_status'] == 'SUCCEEDED':
                verified = verified_success(attempt_id, outcome)
                if latest is not None and attempt_id == latest['attempt_id']:
                    latest_outcome = verified
                committed.append(verified)
            elif row['status'] == 'FAILED':
                # Explicit failure summary accepted by the H driver. No metrics.
                status = 'CANCELLED' if row['job_status'] == 'CANCELLED' else 'FAILED'
                committed.append({'attempt_id':attempt_id,'outcome':{'status':status,'metrics':{},'error_code':outcome.get('error_code')}})
        if latest is None:
            return committed, [], None
        attempt_id = latest['attempt_id']
        if latest_outcome is None:
            latest_outcome = verified_success(attempt_id, decode_outcome(newest_success))
        return committed, self._checkpoint_refs(con, session_id, attempt_id), latest_outcome

    def verified_final_origin(self, con, principal, session_id, source_hash):
        origin = con.execute("""
            SELECT a.attempt_id FROM autonomous_artifacts a
            JOIN autonomous_attempts t ON t.attempt_id=a.attempt_id AND t.session_id=a.session_id
            JOIN jobs j ON j.job_id=t.job_id AND j.run_id=t.run_id
            JOIN autonomous_sessions owner ON owner.session_id=t.session_id AND owner.owner_id=j.owner_id
            WHERE a.session_id=? AND a.phase='development' AND a.logical_id='source'
              AND a.kind IN ('strategy','factor') AND a.full_sha256=?
              AND t.phase='development' AND t.status='SUCCEEDED' AND j.status='SUCCEEDED'
            ORDER BY t.ordinal DESC,a.part_index LIMIT 1
        """, (session_id,source_hash)).fetchone()
        if origin is None or not self._checkpoint_refs(con, session_id, origin['attempt_id']):
            raise PlatformError('OHLCV_BINDING_MISMATCH',409)
        checkpoint, source = self._verified_iteration(con, principal, session_id, origin['attempt_id'])
        if checkpoint['source_sha256'] != source_hash:
            raise PlatformError('CHECKPOINT_BINDING_MISMATCH',409)
        return checkpoint, source

    def _validated_artifacts(self, outcome):
        outcome = Outcome.model_validate(outcome)
        # Safe JSON rejects NaN, secrets and excessive nesting before commit.
        encoded = safe_json(outcome.model_dump(exclude={'artifacts'}))
        artifacts = []
        groups = {}
        for artifact in outcome.artifacts:
            raw = artifact.content.encode('utf-8')
            if len(raw) > 65536:
                raise PlatformError('INVALID_INPUT', 422)
            safe_json(artifact.model_dump())
            artifacts.append((artifact, raw, hashlib.sha256(raw).hexdigest()))
            groups.setdefault(artifact.logical_id, []).append(artifact)
        for parts in groups.values():
            first = parts[0]
            if len(parts) != first.part_count or {p.part_index for p in parts} != set(range(first.part_count)):
                raise PlatformError('INVALID_INPUT', 422)
            if any((p.kind, p.media_type, p.part_count, p.full_sha256) != (first.kind, first.media_type, first.part_count, first.full_sha256) for p in parts):
                raise PlatformError('INVALID_INPUT', 422)
            content = ''.join(p.content for p in sorted(parts, key=lambda p: p.part_index))
            if hashlib.sha256(content.encode()).hexdigest() != first.full_sha256:
                raise PlatformError('CONFLICT', 409)
            if first.media_type == 'application/json':
                # Parsing verifies JSON; individual chunks need not be valid JSON.
                json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(PlatformError('INVALID_INPUT', 422)))
        return artifacts

    def _store_artifacts(self,con,attempt,session,artifacts):
        ids=[]
        for artifact,raw,sha in artifacts:
            old=con.execute('SELECT * FROM autonomous_artifacts WHERE attempt_id=? AND logical_id=? AND part_index=?',(attempt['attempt_id'],artifact.logical_id,artifact.part_index)).fetchone()
            if old:
                if (old['sha256'],old['full_sha256'],old['part_count'],old['kind'],old['media_type'])!=(sha,artifact.full_sha256,artifact.part_count,artifact.kind,artifact.media_type):raise PlatformError('CONFLICT',409)
                ids.append(old['artifact_id']);continue
            used=con.execute('SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM autonomous_artifacts WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()
            if used[0]>=256 or used[1]+len(raw)>json.loads(attempt['reservation_json'])['artifact_bytes']:raise PlatformError('INVALID_INPUT',422)
            aid=str(uuid.uuid4())
            content=artifact.content;reference=None
            storage=getattr(self.jobs,'storage',None)
            if storage is not None:
                binding=storage.lookup_binding(session['owner_id'],'job:'+attempt['job_id'],connection=con)
                if type(binding)is not dict or binding.get('owner_id')!=session['owner_id']:
                    raise PlatformError('STORAGE_REFERENCE_INVALID',409)
                category='reports' if artifact.kind=='evaluation' else 'artifacts'
                reference=storage.put(binding,category,raw,media_type=artifact.media_type,connection=con)
                content=''
            con.execute('INSERT INTO autonomous_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(aid,session['session_id'],attempt['attempt_id'],attempt['phase'],artifact.kind,artifact.media_type,content,sha,len(raw),artifact.logical_id,artifact.part_index,artifact.part_count,artifact.full_sha256))
            if reference is not None:
                con.execute('INSERT INTO autonomous_artifact_payload_refs VALUES(?,?,?)',(aid,session['owner_id'],safe_json(reference)))
            ids.append(aid)
        return ids

    def commit_stage_artifacts(self,context,begun,artifacts):
        values=self._validated_artifacts({'status':'SUCCEEDED','artifacts':artifacts})
        with self.database.transaction() as con:
            attempt,session=self._attempt(con,context)
            if attempt['status']!='IN_FLIGHT' or begun['attempt_id']!=attempt['attempt_id']:raise PlatformError('CONFLICT',409)
            ids=self._store_artifacts(con,attempt,session,values)
            context.assert_fenced(con)
            self._event(con,session['session_id'],'ARTIFACTS_COMMITTED',{'attempt_id':attempt['attempt_id'],'artifact_ids':ids})
            return ids

    def complete(self, context, outcome):
        outcome=Outcome.model_validate(outcome)
        encoded=safe_json(outcome.model_dump(exclude={'artifacts'}))
        artifacts=self._validated_artifacts(outcome)
        with self.database.transaction() as con:
            attempt, session = self._attempt(con, context)
            if attempt['status'] != 'IN_FLIGHT' or session['status'] != 'RUNNING':
                raise PlatformError('CONFLICT', 409)
            uncertain = con.execute("SELECT 1 FROM autonomous_model_calls WHERE attempt_id=? AND status IN ('REQUESTED','UNKNOWN')", (attempt['attempt_id'],)).fetchone()
            if uncertain:
                outcome=outcome.model_copy(update={'status':'UNKNOWN','metrics':{},'error_code':'MODEL_REQUEST_UNCERTAIN'})
                encoded=safe_json(outcome.model_dump(exclude={'artifacts'}))
            self._store_artifacts(con,attempt,session,artifacts)
            context.assert_fenced(con)
            con.execute('UPDATE autonomous_attempts SET status=?,outcome_json=?,updated_at=? WHERE attempt_id=?',
                        (outcome.status, encoded, now_text(), attempt['attempt_id']))
            one_shot=attempt['phase']=='final_test' or json.loads(session['spec_json']).get('reuse_policy')=='evaluate_saved'
            state = 'BLOCKED' if outcome.status == 'UNKNOWN' or one_shot and outcome.status != 'SUCCEEDED' else ('COMPLETED' if one_shot else 'READY')
            con.execute('UPDATE autonomous_sessions SET status=?,active_attempt=NULL,revision=revision+1,updated_at=? WHERE session_id=?',
                        (state, now_text(), session['session_id']))
            self._event(con, session['session_id'], 'ATTEMPT_' + outcome.status, {'attempt_id': attempt['attempt_id'], 'phase': attempt['phase']})
            return {'session_id': session['session_id'], 'attempt_id': attempt['attempt_id'], 'status': state}

    def progress(self, context, stage, *, completed_prefixes=None, total_prefixes=None,
                 current_symbol=None, current_index=None):
        if stage not in {'hypothesis','code','execute','evaluate','feedback','final_evaluate'}:
            raise PlatformError('INVALID_INPUT',422)
        detailed = completed_prefixes is not None or total_prefixes is not None or current_symbol is not None or current_index is not None
        if detailed:
            if (type(completed_prefixes) is not int or type(total_prefixes) is not int
                    or not (0 <= completed_prefixes <= total_prefixes and 1 <= total_prefixes <= 2000)):
                raise PlatformError('INVALID_INPUT',422)
            if completed_prefixes == 0:
                if current_symbol is not None or current_index is not None:
                    raise PlatformError('INVALID_INPUT',422)
            else:
                identifier(current_symbol)
                if type(current_index) is not int or not 0 <= current_index < 2000:
                    raise PlatformError('INVALID_INPUT',422)
        with self.database.transaction() as con:
            attempt,session=self._attempt(con,context)
            if attempt['status']!='IN_FLIGHT' or (attempt['phase']=='final_test') != (stage=='final_evaluate'):
                raise PlatformError('CONFLICT',409)
            metadata={'attempt_id':attempt['attempt_id'],'stage':stage}
            if detailed:
                if total_prefixes > json.loads(attempt['reservation_json'])['prefixes']:
                    raise PlatformError('CONFLICT',409)
                # Completion comes from persisted successful STOP receipts, not
                # reservation/create counters or FAILED_RETAINED_STOPPED.
                event_scope = (session['session_id'], attempt['attempt_id'])
                receipts = con.execute("""
                    SELECT metadata_json FROM autonomous_events
                    WHERE session_id=? AND kind='CONTROLLER_RECORD'
                      AND json_extract(metadata_json,'$.attempt_id')=?
                      AND json_extract(metadata_json,'$.event')='CONTAINER_STOPPED'
                    ORDER BY event_id
                """, event_scope)
                completed = []
                for receipt in receipts:
                    request = json.loads(receipt['metadata_json'])['request_sha256']
                    if request not in completed:
                        completed.append(request)
                if completed_prefixes != len(completed):
                    raise PlatformError('CONFLICT',409)
                if completed:
                    rows=con.execute('SELECT symbol,ordinal FROM autonomous_prefixes WHERE attempt_id=? AND request_hash=?',(attempt['attempt_id'],completed[-1])).fetchall()
                    if len(rows)!=1 or rows[0]['symbol']!=current_symbol or rows[0]['ordinal']!=current_index:
                        raise PlatformError('CONFLICT',409)
                detailed_prior = con.execute("""
                    SELECT metadata_json FROM autonomous_events
                    WHERE session_id=? AND kind='STAGE'
                      AND json_extract(metadata_json,'$.attempt_id')=?
                      AND json_type(metadata_json,'$.total_prefixes') IS NOT NULL
                    ORDER BY event_id DESC LIMIT 128
                """, event_scope).fetchall()
                if len(detailed_prior) >= 128:
                    raise PlatformError('CONFLICT',409)
                last_completed_at=now_text() if completed_prefixes else None
                if detailed_prior:
                    previous = json.loads(detailed_prior[0]['metadata_json'])
                    if previous['total_prefixes'] != total_prefixes or completed_prefixes < previous['completed_prefixes']:
                        raise PlatformError('CONFLICT',409)
                    if completed_prefixes == previous['completed_prefixes']:
                        last_completed_at = previous.get('last_completed_at')
                metadata.update(completed_prefixes=completed_prefixes,total_prefixes=total_prefixes,
                                current_symbol=current_symbol,current_index=current_index,last_completed_at=last_completed_at)
            self._event(con,session['session_id'],'STAGE',metadata)

    def reserve_prefix(self, context, begun, symbol, index, request_hash):
        from .contracts import digest as require_digest
        require_digest(request_hash);identifier(symbol)
        if type(index)is not int or not 0<=index<2000:raise PlatformError('INVALID_INPUT',422)
        with self.database.transaction() as con:
            attempt,session=self._attempt(con,context)
            if attempt['status']!='IN_FLIGHT' or begun['attempt_id']!=attempt['attempt_id']:raise PlatformError('CONFLICT',409)
            # Prefix identity is an external-side-effect reservation, not a
            # replay cache. Even matching duplicate dispatch is refused.
            if con.execute('SELECT 1 FROM autonomous_prefixes WHERE attempt_id=? AND symbol=? AND ordinal=?',(attempt['attempt_id'],symbol,index)).fetchone():raise PlatformError('CONFLICT',409)
            count=con.execute('SELECT COUNT(*) FROM autonomous_prefixes WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()[0]
            if count>=json.loads(attempt['reservation_json'])['prefixes']:raise PlatformError('CONFLICT',409)
            con.execute('INSERT INTO autonomous_prefixes VALUES (?,?,?,?,?)',(attempt['attempt_id'],symbol,index,request_hash,now_text()))
            self._event(con,session['session_id'],'PREFIX_RESERVED',{'attempt_id':attempt['attempt_id'],'symbol':symbol,'index':index,'request_hash':request_hash,'reserved':count+1,'total_limit':json.loads(attempt['reservation_json'])['prefixes']})

    def recover(self, principal, session_id):
        """Reconcile ONLY terminal Jobs. Expiry/fence decisions belong to JobService.

        Called on trusted scheduler ticks after queue recovery, not on public GET.
        An in-flight request without receipt is UNKNOWN and remains fully charged.
        """
        require_writer(principal)
        with self.database.transaction() as con:
            session = self._row(con, principal, session_id)
            if not session['active_attempt']:
                return False
            attempt = con.execute('SELECT * FROM autonomous_attempts WHERE attempt_id=?', (session['active_attempt'],)).fetchone()
            job = con.execute('SELECT * FROM jobs WHERE job_id=?', (attempt['job_id'],)).fetchone()
            if job['status'] not in {'SUCCEEDED', 'FAILED', 'CANCELLED'}:
                return False
            unknown = attempt['status'] == 'IN_FLIGHT'
            status = 'UNKNOWN' if unknown else 'FAILED'
            state = {'PAUSE_REQUESTED': 'PAUSED', 'CANCEL_REQUESTED': 'CANCELLED'}.get(session['status'], 'BLOCKED')
            if unknown:
                state = 'BLOCKED'
            con.execute('UPDATE autonomous_attempts SET status=?,updated_at=? WHERE attempt_id=?', (status, now_text(), attempt['attempt_id']))
            con.execute('UPDATE autonomous_sessions SET status=?,active_attempt=NULL,revision=revision+1,updated_at=? WHERE session_id=?', (state, now_text(), session_id))
            self._event(con, session_id, 'RECOVERED_' + status, {'attempt_id': attempt['attempt_id'], 'reservation_retained': True})
            return True

    def page(self, principal, session_id, kind, *, limit=50, offset=0,cursor=None,attempt_id=None,connection=None):
        self.jobs._paging(limit, offset)
        if kind not in {'attempts', 'events', 'artifacts'}:
            raise PlatformError('INVALID_INPUT', 422)
        if cursor is not None and offset!=0 or attempt_id is not None and kind!='artifacts':
            raise PlatformError('INVALID_INPUT',422)
        if attempt_id is not None:
            try:identifier(attempt_id)
            except ValueError:raise PlatformError('INVALID_INPUT',422) from None
        table, order = {'attempts': ('autonomous_attempts a', 'a.ordinal'), 'events': ('autonomous_events a', 'a.event_id'),
                        'artifacts': ('autonomous_artifacts a JOIN autonomous_artifact_positions p ON p.artifact_id=a.artifact_id', 'p.sequence')}[kind]
        with (closing(self.database.connect()) if connection is None else nullcontext(connection)) as con:
            self._row(con, principal, session_id)
            after=None if cursor is None else _cursor_position(cursor,session_id,kind,attempt_id)
            # Fixed query identifiers only; cursor contributes a position value,
            # never SQL/table names or access authority.
            where='a.session_id=?';params=[session_id]
            if attempt_id is not None:
                if not con.execute('SELECT 1 FROM autonomous_attempts WHERE session_id=? AND attempt_id=?',(session_id,attempt_id)).fetchone():
                    raise PlatformError('NOT_FOUND',404)
                where+=' AND a.attempt_id=?';params.append(attempt_id)
            if after:
                if not con.execute(f'SELECT 1 FROM {table} WHERE {where} AND {order}=?',(*params,after)).fetchone():
                    raise PlatformError('INVALID_INPUT',422)
            columns = ','.join('a.'+s for s in ('artifact_id','attempt_id','phase','kind','media_type','sha256','bytes','logical_id','part_index','part_count','full_sha256'))+',p.sequence AS append_sequence' if kind=='artifacts' else 'a.*'
            query=f'SELECT {columns},{order} AS _page_position FROM {table} WHERE {where}'
            query_params=list(params)
            if after is not None:query+=f' AND {order}>?';query_params.append(after)
            query+=f' ORDER BY {order} LIMIT ? OFFSET ?';query_params.extend((limit+1,offset))
            rows=con.execute(query,query_params).fetchall()
            values = []
            for row in rows[:limit]:
                item = dict(row)
                item.pop('_page_position')
                for key in tuple(item):
                    if key.endswith('_json'):
                        item[key[:-5]] = json.loads(item.pop(key)) if item[key] is not None else None
                values.append(item)
            if values:position=rows[min(limit,len(rows))-1]['_page_position']
            elif after is not None:position=after
            else:position=con.execute(f'SELECT COALESCE(MAX({order}),0) FROM {table} WHERE {where}',params).fetchone()[0]
            has_more=len(rows)>limit
            return {'schema':'autonomous-page-2','session_id':session_id,'kind':kind,'attempt_id':attempt_id,'limit':limit,
                    'items':values,'next_cursor':_page_cursor(session_id,kind,attempt_id,position),'has_more':has_more,
                    'next_offset':offset+limit if has_more and cursor is None else None}

    def artifact(self, principal, session_id, artifact_id):
        with closing(self.database.connect()) as con:
            owner = self._row(con, principal, session_id)['owner_id']
            return self._artifact(con, owner, session_id, artifact_id)

    def _artifact(self, con, owner, session_id, artifact_id, *, storage_connection=None):
        """Read one part of a session whose owner was checked on this connection."""
        row = con.execute('SELECT * FROM autonomous_artifacts WHERE session_id=? AND artifact_id=?', (session_id, artifact_id)).fetchone()
        try:
            link = con.execute('SELECT * FROM autonomous_artifact_payload_refs WHERE artifact_id=?', (artifact_id,)).fetchone()
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_ERROR or str(error) != 'no such table: autonomous_artifact_payload_refs':
                raise
            link = None
        if not row:
            raise PlatformError('NOT_FOUND', 404)
        value=dict(row)
        if link is not None:
            if link['owner_id']!=owner:raise PlatformError('STORAGE_REFERENCE_INVALID',409)
            storage=getattr(self.jobs,'storage',None)
            if storage is None:raise PlatformError('STORAGE_UNAVAILABLE',503)
            category='reports' if row['kind']=='evaluation' else 'artifacts'
            reference=json.loads(link['reference_json'])
            if storage_connection is None:
                raw=storage.read(owner,reference,category=category,maximum_bytes=65536)
            else:
                raw=storage._read(owner,reference,category=category,maximum_bytes=65536,connection=storage_connection)
            if len(raw)!=row['bytes']or hashlib.sha256(raw).hexdigest()!=row['sha256']:
                raise PlatformError('STORAGE_HASH_MISMATCH',409)
            value['content']=raw.decode('utf-8')
        return value
