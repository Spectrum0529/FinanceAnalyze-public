"""Owned bounded long-session projection; no scheduling, provider or artifact IO."""
from contextlib import closing
import json
import math
import re
from .errors import PlatformError


def _code(value):
    return value if type(value)is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,95}',value) else None


def session_progress(service, principal, session_id, *, limit=20, before_ordinal=None):
    if (type(limit)is not int or not 1<=limit<=100 or before_ordinal is not None
            and (type(before_ordinal)is not int or before_ordinal<1)):
        raise PlatformError('INVALID_INPUT',422)
    with closing(service.database.connect())as con:
        con.execute('BEGIN')
        session=service._row(con,principal,session_id)  # original owner gate
        spec=json.loads(session['spec_json']);spent=json.loads(session['spent_json'])
        counts={row['status']:row['n']for row in con.execute(
            'SELECT status,COUNT(*) AS n FROM autonomous_attempts WHERE session_id=? GROUP BY status',(session_id,))}
        predicate='a.session_id=?';args=[session_id]
        if before_ordinal is not None:
            predicate+=' AND a.ordinal<?';args.append(before_ordinal)
        rows=con.execute('SELECT a.attempt_id,a.ordinal,a.generation,a.phase,a.status,a.job_id,a.run_id,'
            'a.reservation_json,a.outcome_json,a.created_at,a.updated_at,j.status AS job_status,j.error_code,j.lease_until '
            'FROM autonomous_attempts a JOIN jobs j USING(job_id) WHERE '+predicate+
            ' ORDER BY a.ordinal DESC LIMIT ?',(*args,limit+1)).fetchall()
        latest=con.execute('SELECT a.attempt_id,a.ordinal,a.status,j.status AS job_status,j.error_code,j.lease_until '
            'FROM autonomous_attempts a JOIN jobs j USING(job_id) WHERE a.session_id=? ORDER BY a.ordinal DESC LIMIT 1',
            (session_id,)).fetchone()
        checkpoint=con.execute("SELECT attempt_id,logical_id,full_sha256,part_count FROM autonomous_artifacts "
            "WHERE session_id=? AND kind='checkpoint' ORDER BY rowid DESC LIMIT 1",(session_id,)).fetchone()
        # Only bounded, fixed decision fields; never return arbitrary event text.
        decision=con.execute("SELECT kind,metadata_json,recorded_at FROM autonomous_events WHERE session_id=? "
            "AND kind IN ('CONTROL','BUDGET_EXHAUSTED','ADMISSION_BLOCKED','RECOVERED_UNKNOWN','ATTEMPT_FAILED') "
            "ORDER BY event_id DESC LIMIT 1",(session_id,)).fetchone()
        progress=service._latest_execution_progress(con,session)
        controls=service._view(session)['allowed_actions']
    history=[]
    for row in rows[:limit]:
        value=dict(row);outcome=json.loads(value.pop('outcome_json'))if row['outcome_json']else None
        value['reservation']=json.loads(value.pop('reservation_json'))
        value['error_code']=_code(value['error_code'])
        value['outcome_status']=outcome.get('status')if outcome else None
        value['outcome_error_code']=_code(outcome.get('error_code'))if outcome else None
        metrics=outcome.get('metrics',{})if outcome else {}
        value['metrics']={k:v for k,v in metrics.items()if type(k)is str and
            re.fullmatch(r'[a-z][a-z0-9_]{0,79}',k) and type(v)in(int,float) and math.isfinite(v)}
        history.append(value)
    now=service.clock()
    quota={k:{'limit':v,'conservatively_consumed':spent[k],'remaining':max(0,v-spent[k])}
           for k,v in spec['budget'].items()}
    reason=None
    if session['status']=='BLOCKED':
        reason='UNKNOWN_ATTEMPT_REQUIRES_ACK'if counts.get('UNKNOWN',0)else (_code(latest['error_code'])if latest else None)
        reason=reason or 'SESSION_BLOCKED'
    elif session['status']in {'PAUSED','PAUSE_REQUESTED','CANCELLED','CANCEL_REQUESTED','BUDGET_EXHAUSTED','COMPLETED'}:
        reason='SESSION_'+session['status']
    decision_view=None
    if decision:
        metadata=json.loads(decision['metadata_json'])
        decision_view={'kind':decision['kind'],'recorded_at':decision['recorded_at'],
                       'code':_code(metadata.get('code')),'action':_code(metadata.get('action'))}
    if session['status']=='BLOCKED' and reason=='SESSION_BLOCKED' and decision_view and decision_view['code']:
        reason=decision_view['code']
    blockers=[k for k,v in quota.items()if v['remaining']<spec['experiment'][k]]
    if now>=session['deadline']:blockers.append('deadline')
    return {'schema':'autonomous-session-progress-1','session_id':session_id,'status':session['status'],
        'revision':session['revision'],'generation':session['generation'],'target':spec['target'],
        'active_attempt':session['active_attempt'],'latest_attempt':None if latest is None else
        {k:(_code(latest[k])if k=='error_code'else latest[k])for k in latest.keys()},
        'loop_policy':'HISTORY_ONLY_EXECUTOR_UNAVAILABLE' if getattr(service,'unavailable_code',None) else 'DURABLE_SCHEDULER_UNTIL_BUDGET_DEADLINE_OR_CONTROL',
        'execution_unavailable_code':getattr(service,'unavailable_code',None),
        'allowed_actions':controls,'stop_reason':reason,'last_control_or_failure':decision_view,
        'requires_unknown_charge_ack':session['status']in {'BLOCKED','PAUSED'}and counts.get('UNKNOWN',0)>0,
        'budget':quota,'budget_basis':'CONSERVATIVE_NON_REFUNDABLE_RESERVATIONS_NOT_PROVIDER_BILLING',
        'next_round_budget_blockers':blockers,'billing_cost':None,'deadline':session['deadline'],'deadline_seconds_remaining':max(0,session['deadline']-now),
        'attempt_counts':counts,'history':history,'history_limit':limit,
        'next_before_ordinal':history[-1]['ordinal']if len(rows)>limit and history else None,
        'latest_execution_progress':progress,'latest_checkpoint':None if checkpoint is None else dict(checkpoint),
        'checkpoint_semantics':'DURABLE_ARTIFACT_REFERENCE_NOT_A_PROMISE_TO_RESUME_AN_UNACKNOWLEDGED_RUN',
        'model_usage_url':'/api/v1/research/autonomous/sessions/'+session_id+'/model-usage'}
