"""Canonical local deployment paths; no shell, environment or request lookup."""
import json
from pathlib import PurePosixPath
import re

SCHEMA = 'financeanalyze-executor-paths-1'
QA_BATCH = 'full_validation_20261109_r01'

def validate_linux_root(value):
    if type(value) is not str or not 1 <= len(value) <= 1024:
        raise ValueError('EXECUTOR_PATHS_INVALID')
    parts = PurePosixPath(value).parts
    if (len(parts) < 4 or parts[0] != '/' or parts[1] not in ('home','srv','opt')
            or str(PurePosixPath(value)) != value
            or any(p in ('.','..') or re.fullmatch(r'[A-Za-z0-9._-]+',p) is None for p in parts[1:])):
        raise ValueError('EXECUTOR_PATHS_INVALID')
    return value

def parse_paths(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 4096:
        raise ValueError('EXECUTOR_PATHS_INVALID')
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result: raise ValueError('EXECUTOR_PATHS_INVALID')
            result[key] = value
        return result
    try:
        value = json.loads(raw,object_pairs_hook=pairs)
    except (UnicodeError,ValueError,RecursionError):
        raise ValueError('EXECUTOR_PATHS_INVALID') from None
    if type(value) is not dict or set(value) != {'schema','linux_root'} or value['schema'] != SCHEMA:
        raise ValueError('EXECUTOR_PATHS_INVALID')
    return validate_linux_root(value['linux_root'])

def root_from_prefix(value):
    prefix = PurePosixPath(value)
    if str(prefix) != value:
        raise ValueError('EXECUTOR_VENV_REQUIRED')
    if prefix.name == '.venv':
        return validate_linux_root(str(prefix.parent))
    if (prefix.name == 'host-venv' and re.fullmatch('[a-f0-9]{32}',prefix.parent.name)
            and prefix.parent.parent.name == QA_BATCH
            and prefix.parent.parent.parent.name == 'qa'
            and prefix.parent.parent.parent.parent.name == 'runtime'):
        return validate_linux_root(str(prefix.parents[4]))
    raise ValueError('EXECUTOR_VENV_REQUIRED')
