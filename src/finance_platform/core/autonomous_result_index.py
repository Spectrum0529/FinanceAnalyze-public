"""Read-only unified source over successful saved autonomous attempt reports."""
from contextlib import closing
import json

from .errors import PlatformError


class AutonomousReportSource:
    def __init__(self, sessions):
        from finance_platform.research.autonomous_results import AutonomousBacktestResults
        self.sessions = sessions
        self.reader = AutonomousBacktestResults(sessions)

    def _attempt(self, principal, attempt_id):
        with closing(self.sessions.database.connect()) as con:
            row = con.execute(
                "SELECT a.session_id,a.attempt_id,a.run_id,a.phase FROM autonomous_attempts a "
                "JOIN autonomous_sessions s ON s.session_id=a.session_id "
                "JOIN jobs j ON j.job_id=a.job_id AND j.run_id=a.run_id AND j.owner_id=s.owner_id "
                "WHERE a.attempt_id=? AND s.owner_id=? AND a.status='SUCCEEDED' "
                "AND j.status='SUCCEEDED'",
                (attempt_id, principal.user_id),
            ).fetchone()
        if row is None:
            raise PlatformError('NOT_FOUND', 404)
        self.sessions.get(principal, row['session_id'])
        return row

    def read(self, principal, attempt_id):
        row = self._attempt(principal, attempt_id)
        try:
            value = self.reader.read(principal, row['session_id'], attempt_id)
        except PlatformError as error:
            if error.code != 'AUTONOMOUS_BACKTEST_VIEW_UNAVAILABLE':
                raise
            # The original reader has checked the saved multipart document.
            # A historical document without the new engine projection remains
            # a real saved report; do not manufacture curves or zero returns.
            value = {
                'family': 'autonomous-research-report-1',
                'structured_backtest_available': False,
                'message': '这份历史报告没有结构化收益图表；请打开原研究记录查看保存产物。',
                'session_id': row['session_id'], 'attempt_id': attempt_id,
            }
        return {**value, 'result_id': 'autonomous:' + attempt_id,
                'run_id': row['run_id'], 'status': 'SUCCEEDED',
                'report_scope': 'COMPLETED_ATTEMPT_NOT_SESSION_COMPLETION'}

    def export(self, principal, attempt_id):
        value = self.read(principal, attempt_id)
        if value.get('structured_backtest_available') is False:
            raise PlatformError('RESULT_EXPORT_UNAVAILABLE', 422)
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > 8 * 1024 ** 2:
            raise PlatformError('RESULT_EXPORT_TOO_LARGE', 413)
        return raw


def result_source(runtime):
    sessions = getattr(runtime, 'autonomous_research', None)
    if sessions is None:
        return None
    from .result_index import ResultSource
    owner = AutonomousReportSource(sessions)
    return ResultSource('autonomous', owner.read, owner.export)
