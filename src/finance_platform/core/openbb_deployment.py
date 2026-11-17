"""Server-owned optional SDK binding; no OpenBB import or provider request."""
import hashlib
import json
from pathlib import Path

from .source_binding import BindingError, checked_regular_file


def optional_source_kwargs(root):
    root = Path(root).resolve()
    relative = 'configs/openbb_runtime.json'
    if not (root / relative).exists():
        return {}
    unavailable = {'openbb_cboe_enabled': True, 'openbb_runtime_binding': None,
                   'openbb_adapter_enabled': True}
    try:
        config = checked_regular_file(root, relative)
        if config.stat().st_size > 65536:
            return unavailable
        value = json.loads(config.read_bytes())
        if (set(value) != {'schema', 'runtime_binding', 'optional_lock_sha256'}
                or value['schema'] != 'finance-openbb-optional-runtime-1'):
            return unavailable
        binding = value['runtime_binding']
        if (type(binding) is not dict
                or binding.get('schema_version') != 'sdk-direct-base-runtime-binding-1'
                or binding.get('expected_version') != [3, 12, 14]):
            return unavailable
        project = '.local/runtimes/openbb-cboe-20261107/'
        lock = checked_regular_file(root, project + 'uv.lock')
        if hashlib.sha256(lock.read_bytes()).hexdigest() != value['optional_lock_sha256']:
            return unavailable
        for key, name in (('venv_python', '.venv/Scripts/python.exe'),
                          ('venv_config', '.venv/pyvenv.cfg')):
            path = checked_regular_file(root, project + name)
            row = binding[key]
            if (str(path.resolve()).casefold() != row['path'].casefold()
                    or path.stat().st_size != row['bytes']
                    or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']):
                return unavailable
        expected_site = (root / project / '.venv/Lib/site-packages').resolve()
        if str(expected_site).casefold() != binding['site_root'].casefold():
            return unavailable
        # Reuse the established trusted Windows base; a config cannot choose
        # another executable/DLL or fall back to the Main dependency tree.
        original = json.loads(checked_regular_file(
            root, 'src/finance_platform/data/provider_sdk/runtime_binding.json',
        ).read_bytes())
        for key in ('base_python', 'base_dll'):
            if binding[key] != original[key]:
                return unavailable
        return {**unavailable, 'openbb_runtime_binding': binding}
    except (OSError, BindingError, ValueError, TypeError, KeyError, AttributeError):
        return unavailable
