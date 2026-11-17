"""Rebuild usage from owned immutable facts; no independent running counter."""
from contextlib import closing
from collections import defaultdict
import json
from .database import Database
from .errors import PlatformError
from .model_usage_models import Observation, TraceRow, SPEED
from .ta_protocol import encoded, digest

TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'total_tokens', 'cached_input_tokens', 'reasoning_output_tokens')
OBSERVATION = 'MODEL_CALL_OBSERVATION'
TRACE = 'MODEL_TRACE_CAPTURE'
OPTIONAL_BYTES = 261120


def observation(value):
    if type(value) is not dict or len(encoded(value)) > 8192:
        raise ValueError('MODEL_OBSERVATION_INVALID')
    return Observation.model_validate(value).model_dump(by_alias=True)


def _fenced(context, con):
    # Append-only numeric evidence may survive cancellation. The original live
    # lease, owner, run and fencing token must still be current. No job update.
    row = context.service._active(con, context.job_id, context.worker_id, context.fencing_token)
    if row['owner_id'] != context.run.owner_id or row['run_id'] != context.run.run_id:
        raise PlatformError('LEASE_LOST', 409)


def retain_observation(context, value):
    try:
        value = observation(value)
        # Separate connection options only, same trusted project DB; contention
        # drops optional telemetry immediately rather than delaying research.
        with Database(context.service.database.path,timeout=0).transaction() as con:
            _fenced(context, con)
            rows = con.execute('SELECT metadata_json FROM job_events WHERE job_id=? AND run_id=? AND kind=?',
                               (context.job_id, context.run.run_id, OBSERVATION)).fetchall()
            used = sum(len(r[0].encode()) for r in con.execute(
                'SELECT metadata_json FROM job_events WHERE job_id=? AND run_id=? AND kind=?',
                (context.job_id, context.run.run_id, TRACE)))
            for row in rows:
                used += len(row[0].encode())
                old = json.loads(row[0]).get('model_observation')
                if old == value:
                    return True
            if used + len(encoded({'model_observation': value})) > OPTIONAL_BYTES:
                return False
            # Conflicting same-call evidence is kept as a separate immutable
            # fact; the reader excludes the identity rather than double counts.
            context.service._event(con, context.job_id, context.run.run_id, OBSERVATION,
                                   {'model_observation': value})
        return True
    except Exception:
        return False


def _label(value, maximum=256):
    return value if type(value) is str and len(value) <= maximum else None


def retain_completed_ta_observation(context, payload, *, snapshot, seen):
    """One admitted child's completed numeric DTO; no child authority fields."""
    try:
        if type(payload)is not dict or set(payload)!={'observation'}:
            raise ValueError('schema')
        value=observation(payload['observation'])
        if (type(snapshot)is not dict or snapshot.get('owner_id')!=context.run.owner_id
                or type(snapshot.get('resolved_roles'))is not dict):raise ValueError('binding')
        role=snapshot['resolved_roles'].get(value['role'])
        if (type(role)is not dict or type(role.get('model'))is not dict
                or any(value[k]!=role['model'].get(k)for k in ('provider','model'))
                or value['profile_id']is not None or value['revision']is not None
                or value['status']not in {'SUCCEEDED','FAILED','CANCELLED'}
                or value['started_at']is None or value['ended_at']is None):raise ValueError('binding')
        call_id=value['call_id']
        if len(call_id)!=32 or any(c not in '0123456789abcdef'for c in call_id):raise ValueError('identity')
        if call_id not in seen and len(seen)>=32:raise ValueError('count')
    except (ValueError,TypeError,KeyError):
        raise PlatformError('TA_OBSERVATION_INVALID',409) from None
    seen.add(call_id)
    return retain_observation(context,value)


def retain_trace(context, trace, *, source):
    """Trusted parent only; never retain the raw model config or prompt text."""
    try:
        if type(trace) is not list or len(trace) > 4096:
            return False
        rows, missing, dropped, used = [], 0, 0, 2
        for ordinal, original in enumerate(trace):
            if type(original) is not dict:
                dropped += 1
                continue
            model = original.get('model') if type(original.get('model')) is dict else {}
            item = {'ordinal': ordinal, 'role': _label(original.get('role')),
                    'provider': _label(model.get('provider')), 'model': _label(model.get('model')),
                    'status': _label(original.get('status'), 64) or 'UNKNOWN', 'model_observation': None}
            try:
                item['model_observation'] = observation(original.get('model_observation'))
            except Exception:
                missing += 1
            size=len(encoded(item))+1
            if used + size > OPTIONAL_BYTES - 2048:
                dropped += 1
            else:
                rows.append(item)
                used += size
        value = {'source': source, 'rows': rows, 'missing_observation_rows': missing,
                 'dropped_trace_rows': dropped, 'recorded_source_rows': len(trace),
                 'coverage': 'INCOMPLETE' if missing or dropped else 'COMPLETE_FOR_RECORDED_SOURCES'}
        with Database(context.service.database.path,timeout=0).transaction() as con:
            _fenced(context, con)
            # A native STAGED or FAILURE publication is at most once per run.
            prior = con.execute('SELECT 1 FROM job_events WHERE job_id=? AND run_id=? AND kind=?',
                                (context.job_id, context.run.run_id, TRACE)).fetchone()
            if prior:
                return True
            already=sum(len(row[0].encode()) for row in con.execute(
                'SELECT metadata_json FROM job_events WHERE job_id=? AND run_id=? AND kind=?',
                (context.job_id, context.run.run_id, OBSERVATION)))
            while rows and already + len(encoded(value)) > OPTIONAL_BYTES:
                rows.pop();value['dropped_trace_rows']+=1;value['coverage']='INCOMPLETE'
            if already + len(encoded(value)) > OPTIONAL_BYTES:
                return False
            context.service._event(con, context.job_id, context.run.run_id, TRACE, value)
        return True
    except Exception:
        return False


def _statistics(rows):
    counts = {key: sum((r.get('metrics') or {}).get(key) is not None for r in rows) for key in TOKEN_FIELDS}
    values = {key: sum((r.get('metrics') or {}).get(key) or 0 for r in rows) if counts[key] else None for key in TOKEN_FIELDS}
    pairs = [r['metrics'] for r in rows if r.get('metrics') and r['metrics']['output_tokens'] is not None
             and r['metrics']['elapsed_ms'] is not None and r['metrics']['elapsed_ms'] > 0]
    return {'recorded_call_count': len(rows),
            'unknown_usage_call_count': sum(not any((r.get('metrics') or {}).get(k) is not None for k in TOKEN_FIELDS) for r in rows),
            'known_subtotals': values, 'known_field_call_counts': counts,
            'complete_for_recorded_calls': {k: bool(rows) and counts[k] == len(rows) for k in TOKEN_FIELDS},
            'output_tokens_per_second': sum(r['output_tokens'] for r in pairs) * 1000 / sum(r['elapsed_ms'] for r in pairs) if pairs else None,
            'speed_pair_count': len(pairs),
            'latest_call': max(rows, key=lambda r: (r['started_at'] or '', r['run_id'], r['call_id'])) if rows else None}


class ModelUsageService:
    def __init__(self, runtime, *, sessions=None):
        self.runtime, self.sessions = runtime, sessions
        jobs=getattr(runtime,'jobs',None) or sessions.jobs
        self.database = jobs.database

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise PlatformError('INVALID_INPUT', 422)

    def _jobs(self, con, principal, kind, identity):
        if kind == 'session':
            row = con.execute('SELECT owner_id FROM autonomous_sessions WHERE session_id=?', (identity,)).fetchone()
            if not row or row['owner_id'] != principal.user_id:
                raise PlatformError('NOT_FOUND', 404)
            return [dict(row) for row in con.execute('SELECT j.* FROM autonomous_attempts a JOIN jobs j USING(job_id) WHERE a.session_id=? AND j.owner_id=? ORDER BY a.ordinal', (identity, principal.user_id))]
        current = con.execute('SELECT j.*,r.parent_run_id FROM jobs j JOIN runs r USING(run_id) WHERE j.job_id=? AND j.owner_id=?', (identity, principal.user_id)).fetchone()
        if not current:
            raise PlatformError('NOT_FOUND', 404)
        jobs, seen = [dict(current)], {current['run_id']}
        while current['handler'] == 'research.debate' and current['parent_run_id']:
            if current['parent_run_id'] in seen:
                raise PlatformError('MODEL_USAGE_LINEAGE_INVALID', 409)
            parent = con.execute('SELECT j.*,r.parent_run_id FROM jobs j JOIN runs r USING(run_id) WHERE r.run_id=? AND j.owner_id=? AND j.handler=?',
                                 (current['parent_run_id'], principal.user_id, 'research.debate')).fetchone()
            if parent is None or parent['parameters_hash'] != current['parameters_hash']:
                raise PlatformError('MODEL_USAGE_LINEAGE_INVALID', 409)
            seen.add(parent['run_id']); jobs.append(dict(parent)); current = parent
        return list(reversed(jobs))

    def _read(self, con, principal, jobs):
        from finance_platform.research.payload_storage import hydrate
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        facts, traces, coverages = [], [], []
        def add(value, job):
            try:
                safe = observation(value)
                facts.append(dict(safe, job_id=job['job_id'], run_id=job['run_id']))
            except Exception:
                coverages.append('INCOMPLETE')
        def ordinary(value, job):
            if type(value) is not dict:
                return
            if value.get('event') == OBSERVATION:
                add(value.get('model_observation'), job)
            for item in value.get('model_observations', []) if type(value.get('model_observations')) is list else []:
                add(item, job)
            note = value.get('coverage') if value.get('event') == 'MODEL_OBSERVATION_COVERAGE' else value.get('model_observations_status')
            if type(note) is dict:
                coverages.append('INCOMPLETE' if note.get('result_rows_omitted') or note.get('evidence_events_dropped') else 'UNKNOWN')
        def saved(encoded, expected, category, job):
            try:
                value = hydrate(self.runtime, principal.user_id, category, encoded)
                if digest(value) != expected:
                    raise ValueError
            except (AttributeError, KeyError, OSError, TypeError, ValueError):
                coverages.append("INCOMPLETE")
                return
            ordinary(value, job)
        for job in jobs:
            source_seen = False
            for event in con.execute('SELECT kind,metadata_json FROM job_events WHERE job_id=? AND run_id=? AND kind IN (?,?) ORDER BY event_id', (job['job_id'], job['run_id'], OBSERVATION, TRACE)):
                value = json.loads(event['metadata_json']); source_seen = True
                if event['kind'] == OBSERVATION:
                    add(value.get('model_observation'), job)
                    coverages.append('UNKNOWN')  # no proof all real calls were observed
                else:
                    coverages.append(value.get('coverage', 'UNKNOWN'))
                    for original in value.get('rows', []):
                        safe = original.get('model_observation')
                        if safe is not None:
                            add(safe, job)
                        item = {k: original.get(k) for k in ('role', 'provider', 'model', 'status')}
                        item.update(sequence=len(traces)+1, job_id=job['job_id'], run_id=job['run_id'],
                                    model_observation=None if safe is None else dict(safe, job_id=job['job_id'], run_id=job['run_id']))
                        try:
                            traces.append(TraceRow.model_validate(item).model_dump(by_alias=True))
                        except Exception:
                            coverages.append('INCOMPLETE')
            if 'research_search_evidence' in tables:
                for row in con.execute('SELECT body,sha256 FROM research_search_evidence WHERE job_id=? AND owner_id=? ORDER BY sequence', (job['job_id'], principal.user_id)):
                    saved(row["body"], row["sha256"], "artifacts", job)
            if 'research_ux_results' in tables:
                row = con.execute('SELECT result_json,result_hash FROM research_ux_results WHERE job_id=? AND owner_id=?', (job['job_id'], principal.user_id)).fetchone()
                if row:
                    saved(row["result_json"], row["result_hash"], "reports", job)
            if not source_seen:
                coverages.append('UNKNOWN')
        unique, conflicts = {}, set()
        for fact in facts:
            key = (fact['job_id'], fact['run_id'], fact['call_id'])
            if key in unique and unique[key] != fact:
                conflicts.add(key)
            else:
                unique[key] = fact
        for key in conflicts:
            unique.pop(key, None)
        coverage = ('INCOMPLETE' if conflicts or 'INCOMPLETE' in coverages else
                    'UNKNOWN' if not coverages or 'UNKNOWN' in coverages else 'COMPLETE_FOR_RECORDED_SOURCES')
        return list(unique.values()), traces, coverage, len(conflicts)

    def summary(self, principal, kind, identity, limit=50, offset=0):
        self._page(limit, offset)
        with closing(self.database.connect()) as con:
            con.execute('BEGIN')  # one consistent read snapshot across all pages' totals
            jobs = self._jobs(con, principal, kind, identity)
            rows, _, coverage, conflicts = self._read(con, principal, jobs)
        stats = _statistics(rows)
        grouped = defaultdict(list)
        for row in rows:
            grouped[(row['provider'], row['model'])].append(row)
        keys = sorted(grouped, key=lambda p: (p[0] or '', p[1] or ''))
        groups = [dict(provider=key[0], model=key[1], **_statistics(grouped[key])) for key in keys[offset:offset+limit]]
        return {'schema': 'model-usage-summary-1', 'scope_kind': kind, 'scope_id': identity,
                'availability': 'NOT_RECORDED' if not rows else 'RECORDED' if all(stats['complete_for_recorded_calls'].values()) and coverage != 'INCOMPLETE' else 'PARTIAL',
                'coverage': coverage, 'coverage_basis': 'RECORDED_UNIQUE_CALL_IDENTITIES_NOT_PROVIDER_BILLING',
                **stats, 'conflicting_call_count': conflicts, 'speed_basis': SPEED,
                'models': groups, 'models_total': len(keys), 'limit': limit, 'offset': offset,
                'next_offset': offset+len(groups) if offset+len(groups) < len(keys) else None,
                'billed_cost': None, 'billing_currency': None}

    def trace(self, principal, job_id, limit=50, offset=0):
        self._page(limit, offset)
        with closing(self.database.connect()) as con:
            con.execute('BEGIN')
            jobs = self._jobs(con, principal, 'job', job_id)
            _, rows, coverage, _ = self._read(con, principal, jobs)
        return {'schema': 'ai-model-trace-page-1', 'job_id': job_id, 'coverage': coverage, 'total': len(rows),
                'limit': limit, 'offset': offset, 'next_offset': offset+limit if offset+limit < len(rows) else None,
                'items': rows[offset:offset+limit], 'not_a_success_report': True}
