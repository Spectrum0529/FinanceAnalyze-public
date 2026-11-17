"""Keep owned research history available without admitting executor work."""
from .auth import require_writer
from .autonomous_api import create_router
from .autonomous_models import Control
from .autonomous_sessions import AutonomousSessions
from .errors import PlatformError


class UnavailableInputs:
    def __init__(self, code):
        self.code = code

    def capabilities(self, principal):
        return {
            'schema': 'autonomous-capabilities-1',
            'executor': {'ready': False, 'code': self.code, 'reason': self.code},
            'evaluation': {'metrics': []},
            'supports_development_only': True,
            'supports_final_test': False,
            'final_test': 'UNAVAILABLE',
        }

    def prepare(self, principal, request):
        raise PlatformError(self.code, 503)


class UnavailableSessions(AutonomousSessions):
    """Use the original journal/readers; execution remains unavailable.

    Pause/cancel persist user intent through the original owner/CAS/key path.
    No handler or scheduler is registered, and no old job is recovered here.
    """

    def __init__(self, database, jobs, *, code):
        self.unavailable_code = code
        super().__init__(database, jobs, resolve_inputs=self._unavailable)

    def _unavailable(self, *args, **kwargs):
        raise PlatformError(self.unavailable_code, 503)

    @staticmethod
    def _limited_view(value):
        return {**value, 'allowed_actions': [
            action for action in value['allowed_actions']
            if action in {'pause', 'cancel'}
        ]}

    def _view(self, row):
        return self._limited_view(super()._view(row))

    def create(self, principal, spec, *, idempotency_key):
        require_writer(principal)
        return self._unavailable()

    def control(self, principal, session_id, body):
        require_writer(principal)
        control = Control.model_validate(body)
        if control.action not in {'pause', 'cancel'}:
            return self._unavailable()
        # A cached response may predate unavailable mode; filter it too.
        return self._limited_view(super().control(principal, session_id, body))

    def dispatch(self, principal, session_id):
        return self._unavailable()

    def begin(self, context):
        return self._unavailable()


def install_unavailable_readers(runtime, *, code):
    if getattr(runtime, 'autonomous_research', None) is not None:
        raise PlatformError('INVALID_INPUT', 422)
    service = UnavailableSessions(runtime.database, runtime.jobs, code=code)
    service.migrate()
    from finance_platform.research.autonomous_library import ResearchCandidateLibrary
    runtime.research_candidates = ResearchCandidateLibrary(runtime, service)
    runtime.autonomous_research = service
    runtime.routers.append(create_router(
        runtime, service=service, inputs=UnavailableInputs(code),
    ))
    return service
