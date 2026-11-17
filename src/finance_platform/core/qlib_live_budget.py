"""Live metadata budget for the owned, protected native working directory.

Zero-link entries are pending deletion, not accepted artifacts. Charge their
observed sizes and defer them. Hardlinks (>1), reparse points and symlinks fail.
Post-exit artifact validation still requires exactly one link and exact bytes.
"""
import stat
import re
from pathlib import Path,PurePosixPath
from finance_platform.core.errors import PlatformError
from finance_platform.core.qlib_native import _fail

def _alias(info):
    return stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or stat.S_ISREG(info.st_mode) and info.st_nlink>1

def _identity(info):
    return info.st_dev,info.st_ino,stat.S_IFMT(info.st_mode)

def rotating_disk_budget(attempt,limit):
    pending,visited,total=[attempt],0,0
    while pending:
        directory=pending.pop()
        try:
            for path in directory.iterdir():
                if directory==attempt and path.name=='provider':continue
                visited+=1
                if visited>8192:_fail('QLIB_ARTIFACT_BUDGET_EXCEEDED')
                try:
                    before=path.lstat()
                    if _alias(before):_fail('QLIB_PATH_ALIAS_REJECTED')
                    is_directory=stat.S_ISDIR(before.st_mode)
                    if not is_directory:
                        if not stat.S_ISREG(before.st_mode):_fail('QLIB_PATH_INVALID')
                        total+=before.st_size
                        if total>limit:_fail('QLIB_ARTIFACT_BUDGET_EXCEEDED')
                        if before.st_nlink==0:continue
                    try:
                        checked=_live_regular(attempt,path.relative_to(attempt).as_posix(),directory=is_directory)
                    except PlatformError as error:
                        if error.code!='QLIB_PATH_INVALID':raise
                        try:after=path.lstat()
                        except FileNotFoundError:continue
                        if _alias(after):_fail('QLIB_PATH_ALIAS_REJECTED')
                        if stat.S_ISREG(after.st_mode) and after.st_nlink==0:continue
                        if _identity(before)==_identity(after):raise
                        if stat.S_ISREG(after.st_mode):
                            total+=max(0,after.st_size-(0 if is_directory else before.st_size))
                            if total>limit:_fail('QLIB_ARTIFACT_BUDGET_EXCEEDED')
                        continue
                    if is_directory:pending.append(checked)
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            # The pending directory itself was atomically removed.
            continue
    return total


# Exact FA013 _regular, except the live-only zero-link deletion state.
def _live_regular(base, relative, *, directory=False):
    base = Path(base).absolute()
    if type(relative) is not str or not relative or '\\' in relative or (':' in relative) or any((p in {'', '.', '..'} for p in relative.split('/'))) or PurePosixPath(relative).is_absolute():
        _fail('QLIB_PATH_INVALID')
    for part in relative.split('/'):
        stem = part.split('.', 1)[0].upper()
        if part.endswith(('.', ' ')) or any((c in part for c in '<>"|?*')) or stem in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch('(?:COM|LPT)[1-9]', stem):
            _fail('QLIB_PATH_ALIAS_REJECTED')
    for ancestor in (base, *base.parents):
        if ancestor.exists() and (ancestor.is_symlink() or getattr(ancestor.lstat(), 'st_file_attributes', 0) & 1024):
            _fail('QLIB_PATH_ALIAS_REJECTED')
    target = base.joinpath(*relative.split('/'))
    for candidate in (target, *target.parents):
        if candidate.exists():
            stat = candidate.lstat()
            if candidate.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 1024:
                _fail('QLIB_PATH_ALIAS_REJECTED')
            if candidate.is_file() and stat.st_nlink > 1:
                _fail('QLIB_PATH_ALIAS_REJECTED')
        if candidate == base:
            break
    if not target.resolve().is_relative_to(base.resolve()):
        _fail('QLIB_PATH_INVALID')
    if target.exists() and (target.is_dir() if directory else target.is_file()) is not True:
        _fail('QLIB_PATH_INVALID')
    return target
