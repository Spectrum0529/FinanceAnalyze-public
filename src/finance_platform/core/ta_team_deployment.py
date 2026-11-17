"""Read source metadata for the existing offline Windows team deployment."""
from pathlib import Path
import sys
from .errors import PlatformError
from .ta_native import FilePin
from .ta_product_binding import NAMES, PersonalTeamBinding
from .ta_protocol import checked
from .ta_team_launcher import compose_team_launcher


DESCRIPTOR = 'configs/ta_team.actual.json'
NATIVE_ACCOUNTS_DESCRIPTOR = object()
EVIDENCE_SLOTS = {
    'wheel': ('workers/tradingagents/vendor/wheels/tradingagents-0.7.0-py3-none-any.whl',
              242392, '44ff72729b717a2f4098f75e5b96d6339b017f073e0f77e0b397bc51d5762dec'),
    'probe_stdout': ('.local/platform/research/tradingagents/evidence/probe.stdout.log',
                     18906, '8123db3ab9a20fe27032e87702c1f8b0b4de624a9c559e9b00781c342b23c881'),
    'probe_stderr': ('.local/platform/research/tradingagents/evidence/probe.stderr.log',
                     0, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
}


def matches_evidence_slot(root, name, row):
    """Only these measured slots, not an extension exemption or conversion."""
    if name not in EVIDENCE_SLOTS or type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'}:
        return False
    relative, size, sha = EVIDENCE_SLOTS[name]
    return Path(row['path']) == root / relative and type(row['bytes']) is int and row['bytes'] == size and row['sha256'] == sha


def fail():
    raise PlatformError('TA_TEAM_DEPLOYMENT_INVALID')


def within(path, roots):
    return any(path.is_relative_to(root) for root in roots)


def load_personal_team_launcher(runtime, relative):
    # A trusted native launcher opt-in cannot be obtained from JSON config,
    # request input, the hosted CLI or the container entry.
    if relative is NATIVE_ACCOUNTS_DESCRIPTOR and runtime.access_mode == 'accounts' and sys.platform == 'win32':
        from .local_personal import validate_mode
        validate_mode('local-personal', runtime.allowed_origins)
        relative = DESCRIPTOR
    elif runtime.access_mode != 'local-personal':
        fail()
    if relative != DESCRIPTOR:
        fail()
    root = runtime.root
    path = root / DESCRIPTOR
    from .source_binding import checked_regular_file
    checked_regular_file(root, DESCRIPTOR)
    if path.stat().st_size > 262144:
        fail()
    document = checked(path.read_bytes())
    if (type(document) is not dict or set(document) != {'schema', 'source_identity', 'project_root', 'pins'}
            or document['schema'] != 'FINANCE_PERSONAL_TEAM_DEPLOYMENT_1'
            or document['project_root'] != str(root) or type(document['pins']) is not dict or set(document['pins']) != NAMES):
        fail()
    metadata = (root / 'configs', root / '.local/platform/research/tradingagents')
    code = (root / 'src',)
    worker = root / 'workers/tradingagents'
    base = Path(sys.base_prefix).resolve(strict=True)
    fixed = {'worker_python': worker / '.venv/Scripts/python.exe', 'manifest': worker / 'pyproject.toml',
             'lock': worker / 'uv.lock', 'worker_spec': worker / 'worker_spec.json', 'identity_probe': worker / 'inspect_runtime.py',
             'venv_config': worker / '.venv/pyvenv.cfg', 'base_python': base / 'python.exe',
             'base_dll': base / 'python312.dll'}
    sources = {'bootstrap', 'identity', 'protocol', 'guard', 'child', 'base_controls', 'native_entry', 'workflow', 'model_port'}
    pins = {}
    for name, row in document['pins'].items():
        if type(row) is not dict or set(row) != {'path', 'bytes', 'sha256'} or type(row['path']) is not str:
            fail()
        target = Path(row['path'])
        if not target.is_absolute() or '..' in target.parts:
            fail()
        if name in EVIDENCE_SLOTS:
            if not matches_evidence_slot(root, name, row): fail()
        elif name in fixed:
            if target != fixed[name]: fail()
        elif name in sources:
            if not within(target, code) or target.suffix != '.py': fail()
        elif not within(target, metadata) or target.suffix != '.json':
            fail()
        pins[name] = FilePin(target, row['bytes'], row['sha256'])
    # Inspect only source-manifest metadata before reading any declared source.
    manifest = checked(pins['sources'].read_verified())
    public_code = (*code, worker / '.venv/Lib/site-packages', base / 'Lib')
    for row in manifest.get('rows', []):
        target = Path(row['path'])
        if not target.is_absolute() or '..' in target.parts or target.suffix != '.py' or target.name.startswith('.') or not within(target, public_code):
            fail()
    binding = PersonalTeamBinding(tuple(pins.items()), document['source_identity'],
                                  root / '.local/platform/native_research', '0' * 64, root)
    return compose_team_launcher(runtime, binding)
