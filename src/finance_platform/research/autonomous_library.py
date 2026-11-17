"""Named immutable research candidates, stored as data, never host programs.

Only successful development attempt artifacts may enter the library. References
are resolved through owning session getters; no client code/path is admitted.
"""
from contextlib import closing
import hashlib
import json
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import safe_json, now_text
from finance_platform.core.storage_locations import get_storage

MIGRATIONS = ("""
CREATE TABLE research_candidate_library(
 candidate_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,body_sha256 TEXT NOT NULL,reference_json TEXT NOT NULL,
 session_id TEXT NOT NULL,attempt_id TEXT NOT NULL,logical_id TEXT NOT NULL,created_at TEXT NOT NULL);
""",)


def reject(code='RESEARCH_CANDIDATE_INVALID'):
    raise PlatformError(code, 422)


class ResearchCandidateLibrary:
    def __init__(self, runtime, sessions):
        self.runtime, self.sessions, self.db = runtime, sessions, runtime.database
        self.db.apply_module_migrations('research_candidate_library', MIGRATIONS)
        with self.db.transaction() as con:
            immutable(con, 'research_candidate_library', ('candidate_id',))

    def save(self, principal, session_id, attempt_id, logical_id, name):
        self.runtime.require_owner(principal, principal.user_id, write=True)
        if type(name) is not str or not 1 <= len(name.strip()) <= 80 or type(logical_id) is not str:
            reject()
        session = self.sessions.get(principal, session_id)
        with closing(self.db.connect()) as con:
            attempt = con.execute('SELECT status,phase FROM autonomous_attempts WHERE session_id=? AND attempt_id=?', (session_id,attempt_id)).fetchone()
        if attempt is None or attempt['status'] != 'SUCCEEDED' or attempt['phase'] != 'development':
            reject('RESEARCH_CANDIDATE_SUCCESS_REQUIRED')
        all_rows = []; cursor = None
        for _ in range(3):
            page = self.sessions.page(principal, session_id, 'artifacts', attempt_id=attempt_id, limit=100, cursor=cursor)
            all_rows.extend(page['items'])
            cursor = page.get('next_cursor') if page.get('has_more') else None
            if cursor is None: break
        if cursor is not None or len(all_rows)>256: reject()
        rows = [r for r in all_rows if r['logical_id'] == logical_id]
        if not rows or len(rows) > 256:
            reject()
        parts = [self.sessions.artifact(principal, session_id, r['artifact_id']) for r in rows]
        parts.sort(key=lambda p: p['part_index'])
        first = parts[0]
        if (first['kind'] not in {'factor', 'strategy'} or first['media_type'] != 'text/x-python'
                or [p['part_index'] for p in parts] != list(range(len(parts)))):
            reject()
        for p in parts:
            if (p['part_count'] != len(parts) or p['full_sha256'] != first['full_sha256']
                    or p['kind'] != first['kind'] or p['phase'] != 'development'
                    or p['attempt_id'] != attempt_id or p['logical_id'] != logical_id
                    or p['media_type'] != first['media_type']
                    or hashlib.sha256(p['content'].encode()).hexdigest() != p['sha256']):
                reject('RESEARCH_CANDIDATE_HASH_MISMATCH')
        source = ''.join(p['content'] for p in parts)
        if not 0 < len(source.encode()) <= 49152 or hashlib.sha256(source.encode()).hexdigest() != first['full_sha256']:
            reject('RESEARCH_CANDIDATE_HASH_MISMATCH')
        body = {'schema': 'research-reusable-candidate-1', 'kind': first['kind'], 'source': source,
                'source_sha256': first['full_sha256'], 'abi': 'compute(frame)' if first['kind']=='factor' else 'decide(frame,state)',
                'origin': {'session_id': session_id, 'attempt_id': attempt_id, 'logical_id': logical_id,
                           'artifact_ids': [p['artifact_id'] for p in parts]},
                'engine_options': session['spec']['engine_options'], 'scoring': session['spec']['scoring'],
                'development': session['spec']['development'], 'scope': 'DEVELOPMENT_ONLY',
                'executable_on_host': False, 'name': name.strip()}
        raw = safe_json(body).encode(); body_hash = hashlib.sha256(raw).hexdigest()
        candidate_id = 'candidate-' + hashlib.sha256((principal.user_id+':'+body_hash).encode()).hexdigest()
        store = get_storage(self.runtime)
        with self.db.transaction() as con:
            old = con.execute('SELECT * FROM research_candidate_library WHERE candidate_id=?', (candidate_id,)).fetchone()
            if old:
                return self._metadata(old)
            binding = store.bind(principal.user_id, candidate_id, categories=('artifacts',), connection=con)
            ref = store.put(binding, 'artifacts', raw, media_type='application/json', connection=con)
            con.execute('INSERT INTO research_candidate_library VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (candidate_id, principal.user_id, name.strip(), first['kind'], first['full_sha256'],
                         body_hash, safe_json(ref), session_id, attempt_id, logical_id, now_text()))
            return self._metadata(con.execute('SELECT * FROM research_candidate_library WHERE candidate_id=?', (candidate_id,)).fetchone())

    @staticmethod
    def _metadata(row):
        return {k: row[k] for k in ('candidate_id','name','kind','source_sha256','session_id','attempt_id','logical_id','created_at')}

    def list(self, principal, *, kind=None, limit=50, offset=0):
        self.runtime.require_owner(principal, principal.user_id)
        if kind not in {None, 'factor', 'strategy'} or type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or not 0<=offset<=10000:
            reject()
        where = 'owner_id=?'; args = (principal.user_id,)
        if kind is not None: where += ' AND kind=?'; args += (kind,)
        with closing(self.db.connect()) as con:
            total = con.execute('SELECT count(*) FROM research_candidate_library WHERE '+where, args).fetchone()[0]
            rows = con.execute('SELECT * FROM research_candidate_library WHERE '+where+' ORDER BY created_at DESC,candidate_id LIMIT ? OFFSET ?', args+(limit,offset)).fetchall()
        return {'items':[self._metadata(r) for r in rows], 'total':total, 'limit':limit, 'offset':offset}

    def get(self, principal, candidate_id):
        self.runtime.require_owner(principal, principal.user_id)
        with closing(self.db.connect()) as con:
            row = con.execute('SELECT * FROM research_candidate_library WHERE candidate_id=? AND owner_id=?', (candidate_id,principal.user_id)).fetchone()
        if row is None: raise PlatformError('NOT_FOUND',404)
        # Recheck original ownership/source access; saved copies do not grant
        # rights to another owner's session or override revocation.
        self.sessions.get(principal,row['session_id'])
        raw = get_storage(self.runtime).read(principal.user_id,json.loads(row['reference_json']),category='artifacts',maximum_bytes=131072)
        if hashlib.sha256(raw).hexdigest()!=row['body_sha256']:reject('RESEARCH_CANDIDATE_HASH_MISMATCH')
        value = json.loads(raw)
        if value['source_sha256']!=row['source_sha256'] or hashlib.sha256(value['source'].encode()).hexdigest()!=row['source_sha256']:
            reject('RESEARCH_CANDIDATE_HASH_MISMATCH')
        return {**self._metadata(row),'candidate':value,'host_execution_allowed':False}

    def context(self, principal, candidate_ids):
        if (type(candidate_ids) is not list or len(candidate_ids)>4
                or any(type(v) is not str or len(v)>128 for v in candidate_ids)
                or len(set(candidate_ids))!=len(candidate_ids)):reject()
        values = [self.get(principal, ident) for ident in candidate_ids]
        result = [{'reusable_candidate':v['candidate'], 'source_sha256':v['source_sha256'],
                   'meaning':'PRIOR_DEVELOPMENT_CODE_AS_MODEL_CONTEXT_NOT_EXECUTION_PERMISSION'} for v in values]
        if len(safe_json(result).encode())>262144:reject('RESEARCH_CANDIDATE_CONTEXT_TOO_LARGE')
        return result

    def backtest(self, principal, candidate_id, request, *, prepare, idempotency_key):
        """Create/run ONE new isolated saved-source development session.

        API composition supplies WindowsInputs.prepare, never a client-chosen
        callback. Actual execution remains the existing Core scheduler/worker
        and Linux guest. This method never imports or executes candidate code.
        """
        self.runtime.require_owner(principal, principal.user_id, write=True)
        if (type(request) is not dict or not callable(prepare)
                or type(idempotency_key) is not str or not 1<=len(idempotency_key)<=128):
            reject()
        candidate = self.get(principal, candidate_id)
        if request.get('final_test_id') is not None:
            reject('FINAL_DATA_NOT_ALLOWED_IN_SAVED_BACKTEST')
        body = dict(request)
        body.update(target=candidate['kind'],saved_candidate_ids=[candidate_id],reuse_policy='evaluate_saved',final_test_id=None)
        if type(body.get('budget')) is not dict: reject()
        body['budget']={**body['budget'],'attempts':1,'model_requests':0,'tokens':0,'cost_microunits':0}
        body['models']={};body.pop('default_model',None);body.pop('team_snapshot_id',None)
        prepared = prepare(principal,body)
        spec = prepared['spec']
        if (spec.get('reuse_policy')!='evaluate_saved' or spec.get('saved_candidate_ids')!=[candidate_id]
                or spec.get('target')!=candidate['kind'] or spec.get('budget',{}).get('attempts')!=1
                or spec.get('final_test') is not None
                or any(spec.get('experiment',{}).get(k)!=0 for k in ('model_requests','tokens','cost_microunits'))
                or any(spec.get('budget',{}).get(k)!=0 for k in ('model_requests','tokens','cost_microunits'))
                or spec.get('models')!={}):
            reject('SAVED_BACKTEST_BINDING_MISMATCH')
        key = 'saved-backtest:'+hashlib.sha256(idempotency_key.encode()).hexdigest()
        session = self.sessions.create(principal,spec,idempotency_key=key)
        # create() replay may return its original DRAFT response. Always get
        # current state, and never restart a running/completed prior request.
        current = self.sessions.get(principal,session['session_id'])
        if current['status']=='DRAFT':
            current = self.sessions.control(principal,current['session_id'],{
                'action':'run','expected_revision':current['revision'],
                'idempotency_key':key+':run'})
        return {'schema':'saved-candidate-backtest-1','candidate_id':candidate_id,
                'source_sha256':candidate['source_sha256'],'session':current,
                'execution':'LINUX_ISOLATED_CAUSAL_PREFIX','maximum_attempts':1,'model_requests':0,'model_required':False,
                'validation_status':'NOT_RUN','not_a_completed_backtest':True}

    def final_descriptor(self, principal, session_id, attempt_id, *, warmup_rows=80):
        """Trusted composition only, AFTER Core's one-time exposure commit.

        Freeze source, checkpoint and preprocessing BEFORE authorized final
        export. This descriptor is not a runnable seal or an export permission.
        """
        import re
        session = self.sessions.get(principal, session_id)
        source_hash = session.get('candidate_hash')
        if (session.get('active_attempt') != attempt_id or session.get('final_attempt') != attempt_id
                or type(warmup_rows) is not int or not 80<=warmup_rows<=1960):
            reject('FINAL_SEAL_BINDING_MISMATCH')
        with closing(self.db.connect()) as con:
            attempt=con.execute("SELECT phase,status FROM autonomous_attempts WHERE attempt_id=? AND session_id=?",(attempt_id,session_id)).fetchone()
            exposed=con.execute("SELECT 1 FROM autonomous_events WHERE session_id=? AND kind='FINAL_EXPOSED' AND json_extract(metadata_json,'$.attempt_id')=? AND json_extract(metadata_json,'$.candidate_hash')=?",(session_id,attempt_id,source_hash)).fetchone()
            if not attempt or attempt['phase']!='final_test' or attempt['status']!='IN_FLIGHT' or not exposed:
                reject('FINAL_SEAL_BINDING_MISMATCH')
            checkpoint, source = self.sessions.verified_final_origin(con,principal,session_id,source_hash)
        options=checkpoint['engine_options'];thresholds=checkpoint['frozen_factor_thresholds']
        # Executor digest uses sorted compact JSON, finite primitive fields.
        digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        return {'schema':'research-final-seal-1','session_id':session_id,'attempt_id':attempt_id,
                'candidate_hash':source_hash,'source':source,'kind':checkpoint['kind'],'exposure_recorded':True,
                'ohlcv_binding':checkpoint['ohlcv_binding'],
                'engine_options':options,'engine_options_sha256':digest(options),'factor_thresholds':thresholds,
                'factor_thresholds_sha256':digest(thresholds),'scoring':session['spec']['scoring'],
                'warmup_rows':warmup_rows,
                'development_origin_attempt':checkpoint['attempt_id'],'checkpoint_sha256':digest(checkpoint)}


    def final_seal(self,principal,session_id,attempt_id,*,final_input_receipts_sha256,warmup_rows=80):
        import re
        if type(final_input_receipts_sha256)is not str or not re.fullmatch('[0-9a-f]{64}',final_input_receipts_sha256):
            reject('FINAL_SEAL_BINDING_MISMATCH')
        descriptor=self.final_descriptor(principal,session_id,attempt_id,warmup_rows=warmup_rows)
        return dict(descriptor,final_input_receipts_sha256=final_input_receipts_sha256)

