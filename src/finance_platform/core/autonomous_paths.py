"""Optional local executor paths, authenticated by the native manifest digest."""
import hashlib
from pathlib import Path
import re
from .autonomous_path_contract import parse_paths, root_from_prefix
from .source_binding import checked_regular_file

CONFIG = 'configs/autonomous_paths.json'

def read_paths(root, expected_sha256):
    if type(expected_sha256) is not str or not re.fullmatch('[a-f0-9]{64}',expected_sha256):
        raise ValueError('EXECUTOR_PATHS_PIN_REQUIRED')
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError('EXECUTOR_PATHS_INVALID')
    path = checked_regular_file(root,CONFIG)
    if path.stat().st_size > 4096: raise ValueError('EXECUTOR_PATHS_INVALID')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('EXECUTOR_PATHS_CHANGED')
    return parse_paths(raw)
