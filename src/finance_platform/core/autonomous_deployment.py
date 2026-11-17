"""Trusted local deployment binding; never starts WSL during Main startup."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .errors import PlatformError
from .source_binding import checked_regular_file, canonical_manifest, source_digest

CONFIG = 'configs/autonomous_executor.json'
CODE = 'AUTONOMOUS_DEPLOYMENT_UNAVAILABLE'


def _unavailable():
    raise PlatformError(CODE, 503)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _unavailable()
        result[key] = value
    return result


class InstalledEvaluator:
    """A descriptor of the exact installed Linux runtime, never an evaluator.

    Runtime code/image bytes are checked by the Linux entrypoint/controller.
    This Windows descriptor checks its own installed source/lock and the
    canonical native manifest, without launching a helper or reading keys.
    """

    def __init__(self, root, value):
        self.root = Path(root)
        self.value = deepcopy(value)
        if set(value) != {'schema', 'main_source_sha256', 'windows_lock_sha256', 'runtime_manifest', 'evaluator'}:
            _unavailable()
        if value['schema'] != 'fa-autonomous-installed-deployment-1':
            _unavailable()
        for key in ('main_source_sha256', 'windows_lock_sha256'):
            if type(value[key]) is not str or not re.fullmatch('[a-f0-9]{64}', value[key]):
                _unavailable()
        manifest = value['runtime_manifest']
        if type(manifest) is not dict or set(manifest) != {'schema', 'package_files', 'dependencies', 'image_digest'}:
            _unavailable()
        if manifest['schema'] != 'fa-native-runtime-manifest-1' or type(manifest['package_files']) is not dict or not manifest['package_files']:
            _unavailable()
        if type(manifest['image_digest']) is not str or not re.fullmatch(r'(?:sha256:[a-f0-9]{64}|[a-z0-9./:_-]+@sha256:[a-f0-9]{64})', manifest['image_digest']):
            _unavailable()
        capabilities = value['evaluator']
        if type(capabilities) is not dict or type(manifest['dependencies']) is not dict:
            _unavailable()
        from fa_research_executor.protocol import OHLCV_BINDING
        if capabilities.get('ohlcv_binding') != OHLCV_BINDING:
            raise PlatformError('OHLCV_BINDING_MISMATCH',503)
        expected = manifest['dependencies'].get('evaluator_capabilities_sha256')
        if hashlib.sha256(canonical_manifest(capabilities)).hexdigest() != expected:
            _unavailable()
        self.paths_sha256 = manifest['dependencies'].get('executor_paths_sha256')
        if type(self.paths_sha256) is not str or not re.fullmatch('[a-f0-9]{64}',self.paths_sha256):
            _unavailable()
        self.runtime_sha256 = hashlib.sha256(canonical_manifest(manifest)).hexdigest()
        self.image_id = manifest['image_digest']
        if '@' in self.image_id:
            # The repository digest identifies the manifest. Controller receipts
            # carry Docker's image ID, pinned separately in build provenance.
            provenance = manifest['dependencies'].get('image_build_provenance')
            self.image_id = provenance.get('SDK_image_id') if type(provenance) is dict else None
            if type(self.image_id) is not str or not re.fullmatch(r'sha256:[a-f0-9]{64}', self.image_id):
                _unavailable()

    def _binding(self):
        try:
            from .autonomous_paths import read_paths
            read_paths(self.root, self.paths_sha256)
            lock = checked_regular_file(self.root, 'uv.lock')
            return (hashlib.sha256(lock.read_bytes()).hexdigest() == self.value['windows_lock_sha256']
                    and source_digest(self.root) == self.value['main_source_sha256'])
        except (OSError, ValueError):
            return False

    def capabilities(self):
        if not self._binding():
            _unavailable()
        return deepcopy(self.value['evaluator'])

    def readiness(self):
        ready = self._binding()
        return {'ready': ready, 'code': None if ready else CODE,
                'runtime_sha256': self.runtime_sha256,
                'startup': 'DEDICATED_EXECUTOR_ON_EXPLICIT_RESEARCH_ATTEMPT',
                'meaning': 'INSTALLED_BINDING_ONLY_NOT_A_LIVE_DAEMON_OR_REAL_MODEL_CHECK'}


def install_autonomous(runtime):
    """Bind the actual nine-port Windows bridge only when deployment exists.

    Missing/stale optional research installation does not prevent ordinary
    Windows workflows starting. No helper/daemon/provider probe happens here.
    """
    from .errors import MESSAGES
    MESSAGES[CODE] = '自主研究执行环境尚未安装完成或版本绑定已变化，请完成本地更新后重试；普通工作台仍可使用。'
    MESSAGES['OHLCV_BINDING_MISMATCH'] = '自主研究执行器的行情字段版本不匹配，请更新本地运行环境。'
    MESSAGES['EXECUTOR_PATHS_UNAVAILABLE'] = '自主研究的本机运行目录尚未设置或已经变化，请重新完成执行环境设置；普通工作台仍可使用。'
    try:
        path = checked_regular_file(runtime.root, CONFIG)
        if path.stat().st_size > 131072:
            _unavailable()
        value = json.loads(path.read_bytes(), object_pairs_hook=_object,
                           parse_constant=lambda _: _unavailable())
        if type(value) is not dict:
            _unavailable()
        evaluator = InstalledEvaluator(runtime.root, value)
        if not evaluator.readiness()['ready']:
            _unavailable()
    except (OSError, ValueError, TypeError, ImportError, PlatformError) as error:
        code = 'OHLCV_BINDING_MISMATCH' if getattr(error, 'code', None) == 'OHLCV_BINDING_MISMATCH' else CODE
        runtime.autonomous_deployment_status = {'ready': False, 'code': code, 'history_available': True}
        from .autonomous_availability import install_unavailable_readers
        return install_unavailable_readers(runtime, code=code)
    from .autonomous_bridge import BridgeDeployment, install_windows_bridge
    service = install_windows_bridge(runtime, evaluator=evaluator,
                                     deployment=BridgeDeployment(runtime_sha256=evaluator.runtime_sha256,
                                                                 root=Path(runtime.root),paths_sha256=evaluator.paths_sha256))
    runtime.autonomous_deployment_status = evaluator.readiness()
    return service


def install_qa_autonomous(runtime, *, scope_sha256, transports=None):
    """Explicit trusted QA bootstrap. Production installation never selects QA."""
    from .autonomous_qa_deployment import QABridgeDeployment, read_scope
    from .autonomous_bridge import install_windows_bridge

    scope = read_scope(runtime.root, scope_sha256)
    path = checked_regular_file(runtime.root, CONFIG)
    if path.stat().st_size > 131072:
        _unavailable()
    value = json.loads(path.read_bytes(), object_pairs_hook=_object,
                       parse_constant=lambda _: _unavailable())
    if type(value) is not dict:
        _unavailable()
    evaluator = InstalledEvaluator(runtime.root, value)
    if not evaluator.readiness()['ready']:
        _unavailable()
    deployment = QABridgeDeployment(Path(runtime.root), scope.sha256, evaluator.runtime_sha256,
                                    value['main_source_sha256'], value['runtime_manifest']['image_digest'],evaluator.paths_sha256)
    deployment.verified_scope()
    service = install_windows_bridge(runtime, evaluator=evaluator, deployment=deployment, transports=transports)
    runtime.autonomous_deployment_status = {**evaluator.readiness(), 'qa_run_id': scope.run_id,
                                             'qa_scope_sha256': scope.sha256}
    return service
