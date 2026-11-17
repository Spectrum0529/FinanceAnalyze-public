"""Bounded owner result reader for engine-derived autonomous backtest views."""
from contextlib import closing
import hashlib
import json
from types import SimpleNamespace
from finance_platform.core.errors import PlatformError
from finance_platform.core.result_views import ResultViews, digest
from finance_platform.data.autonomous_receipts import CN_SIMULATION


class AutonomousBacktestResults:
    def __init__(self, sessions):
        self.sessions = sessions

    def read(self, principal, session_id, attempt_id):
        with closing(self.sessions.database.connect()) as con:
            session = self.sessions._row(con, principal, session_id)
            attempt = con.execute("SELECT a.phase,a.status,j.status AS job_status FROM autonomous_attempts a JOIN jobs j ON j.job_id=a.job_id WHERE a.session_id=? AND a.attempt_id=?", (session_id,attempt_id)).fetchone()
            if attempt is None or attempt['status']!='SUCCEEDED' or attempt['job_status']!='SUCCEEDED':
                raise PlatformError('NOT_FOUND',404)
            logical = 'sealed-final-evaluation' if attempt['phase']=='final_test' else 'full-experiment'
            cursor = None; rows=[]
            for _ in range(3):
                page = self.sessions.page(principal,session_id,'artifacts',limit=100,cursor=cursor,attempt_id=attempt_id,connection=con)
                rows.extend(page['items'])
                cursor = page['next_cursor'] if page['has_more'] else None
                if cursor is None:break
            if cursor is not None or len(rows)>256:raise PlatformError('RESULT_PAGE_TOO_LARGE',413)
            parts=[self.sessions._artifact(con,session['owner_id'],session_id,r['artifact_id'],storage_connection=con)
                   for r in rows if r['logical_id']==logical]
        parts.sort(key=lambda p:p['part_index'])
        if not parts:raise PlatformError('AUTONOMOUS_BACKTEST_VIEW_UNAVAILABLE',404)
        sha=parts[0]['full_sha256']
        if ([p['part_index'] for p in parts]!=list(range(len(parts)))
                or any(p['part_count']!=len(parts) or p['full_sha256']!=sha or p['media_type']!='application/json'
                       or hashlib.sha256(p['content'].encode()).hexdigest()!=p['sha256'] for p in parts)):
            raise PlatformError('CONFLICT',409)
        raw=''.join(p['content'] for p in parts).encode()
        if len(raw)>16777216:raise PlatformError('RESULT_PAGE_TOO_LARGE',413)
        if hashlib.sha256(raw).hexdigest()!=sha:raise PlatformError('CONFLICT',409)
        value=json.loads(raw);view=value.get('backtest')
        # Historical reports without this projection are not silently replaced
        # with empty curves or reconstructed AI explanations.
        if type(view) is not dict or view.get('schema')!='autonomous-backtest-visuals-1':
            raise PlatformError('AUTONOMOUS_BACKTEST_VIEW_UNAVAILABLE',404)
        original=dict(view);expected=original.pop('engine_projection_sha256',None)
        if digest(original)!=expected:raise PlatformError('CONFLICT',409)
        receipt_rows = value.get('input_receipt')
        cn_receipts = [row for row in receipt_rows if type(row) is dict and 'research_market' in row] if type(receipt_rows) is list else []
        market = json.loads(session['resolved_json']).get('capability_limits', {}).get('market_semantics')
        if market == CN_SIMULATION['market_semantics'] or cn_receipts or 'market_semantics' in view:
            evidence = cn_receipts[0]['research_market'] if len(cn_receipts) == 1 else None
            if (attempt['phase'] != 'development' or market != CN_SIMULATION['market_semantics']
                    or type(evidence) is not dict or len(receipt_rows) != 1
                    or any(view.get(key) != expected for key, expected in CN_SIMULATION.items())
                    or evidence.get('schema') != market
                    or evidence.get('corporate_actions') not in ('UNVERIFIED', 'VERIFIED_NONE')
                    or view.get('corporate_actions') != evidence['corporate_actions']
                    or view.get('input_receipt_sha256') != digest(receipt_rows)):
                raise PlatformError('CONFLICT',409)
        return {**view,'session_id':session_id,'attempt_id':attempt_id,'artifact_full_sha256':sha,
                'evaluation_scope':'SEALED_FINAL' if attempt['phase']=='final_test' else 'DEVELOPMENT_ONLY'}

    def _views(self, principal, session_id, attempt_id):
        parent=self
        class Index:
            def read(self, actor, result_id):
                return parent.read(actor,session_id,attempt_id)
            def _source(self, result_id):
                return SimpleNamespace(series=None),attempt_id
        return ResultViews(Index())

    def summary(self, principal, session_id, attempt_id):
        return self._views(principal,session_id,attempt_id).summary(principal,'autonomous:'+attempt_id)

    def section(self, principal, session_id, attempt_id, section='', limit=500, offset=0, expected_sha256=None):
        return self._views(principal,session_id,attempt_id).section(principal,'autonomous:'+attempt_id,
                          section,limit,offset,expected_sha256)
