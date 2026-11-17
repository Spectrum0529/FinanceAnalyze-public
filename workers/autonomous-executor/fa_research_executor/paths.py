"""Dedicated executor root and OS-account directories, independent of HOME."""
import hashlib
import os
from pathlib import Path
import stat
import sys
from .path_contract import parse_paths, root_from_prefix

def executor_root():
    prefix = Path(sys.prefix)
    root = Path(root_from_prefix(prefix.as_posix()))
    for directory in (prefix, *prefix.parents):
        if directory.is_symlink() or directory.resolve() != directory:
            raise ValueError('EXECUTOR_VENV_REQUIRED')
    return root

def user_unit_directory():
    import pwd
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    if not home.is_absolute() or home.resolve() != home:
        raise ValueError('EXECUTOR_ACCOUNT_HOME_INVALID')
    directory = home / '.config/systemd/user'
    for part in (directory,*directory.parents):
        if part.is_symlink() or part.resolve() != part:
            raise ValueError('EXECUTOR_ACCOUNT_HOME_INVALID')
    return directory

def verified_paths(manifest):
    root = executor_root()
    path = root / 'deployment/executor-paths.json'
    for parent in (path.parent,*path.parent.parents):
        if parent.is_symlink() or parent.resolve() != parent:
            raise ValueError('EXECUTOR_PATHS_INVALID')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or info.st_size > 4096):
            raise ValueError('EXECUTOR_PATHS_INVALID')
        raw = stream.read(4097)
    expected = manifest.get('dependencies',{}).get('executor_paths_sha256')
    if hashlib.sha256(raw).hexdigest() != expected or Path(parse_paths(raw)) != root:
        raise ValueError('EXECUTOR_PATHS_CHANGED')
    return root
