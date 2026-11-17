"""Same fixed owner/run/attempt grammar, using real project root as short cwd."""
from pathlib import Path
import re,stat,hashlib
ATTEMPT_ENV='FA014_OWNED_ATTEMPT'
ROOT_ENV='FA014_RUNTIME_ROOT'
MAX_CWD_UTF16=240
def _fail():raise RuntimeError('QLIB_LAUNCH_DIRECTORY_INVALID')
def check_cwd(path):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts or len(str(path).encode('utf-16-le'))//2>MAX_CWD_UTF16:_fail()
    for node in (path,*path.parents):
        info=node.lstat()
        if not stat.S_ISDIR(info.st_mode) or node.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:_fail()
    if path.resolve(strict=True)!=path:_fail()
    return path
def _identifier(value):
    if type(value)is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',value)is None:_fail()
    return value
def work_directory(root,owned,attempt_id):
    root=check_cwd(root)
    parts=[_identifier(owned[key]) for key in ['owner_id','run_id','export_id']]+[_identifier(attempt_id)]
    if not attempt_id.startswith('qlib-native-'):_fail()
    key=hashlib.sha256('|'.join(parts).encode()).hexdigest()[:24]
    return root/'data/development/qlib-work'/key/'n'
def locate_attempt(root,cwd,attempt_text):
    root=check_cwd(root)
    if Path(cwd)!=root or type(attempt_text)is not str or len(attempt_text)>4096:_fail()
    attempt=Path(attempt_text)
    if not attempt.is_absolute() or '..' in attempt.parts:_fail()
    try:parts=attempt.relative_to(root).parts
    except ValueError:_fail()
    if len(parts)!=5 or parts[:3]!=('data','development','qlib-work') or re.fullmatch(r'[0-9a-f]{24}',parts[3])is None or parts[4]!='n':_fail()
    for node in (attempt,*attempt.parents):
        info=node.lstat()
        if not stat.S_ISDIR(info.st_mode) or node.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:_fail()
        if node==root:break
    if attempt.resolve(strict=True)!=attempt:_fail()
    return attempt
def assert_owned_layout(root,cwd,attempt,owned,attempt_id):
    attempt=locate_attempt(root,cwd,str(attempt))
    expected=Path(root)/'data/development/platform'/_identifier(owned['owner_id'])/'qlib-exports'/_identifier(owned['run_id'])/_identifier(owned['export_id'])
    if Path(owned['directory'])!=expected or Path(owned['CSV_directory'])!=expected/'csv' or attempt!=work_directory(root,owned,attempt_id):_fail()
    return attempt
def select_launch_cwd(root,attempt,owned,attempt_id):
    cwd=check_cwd(root);assert_owned_layout(root,cwd,attempt,owned,attempt_id);return cwd
