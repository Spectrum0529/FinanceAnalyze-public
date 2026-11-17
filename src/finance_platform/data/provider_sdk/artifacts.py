"""Exact own-child file protocol; caches/control payloads are never artifacts."""
import hashlib
import json
from pathlib import Path
import shutil
import stat

from finance_platform.data.contracts import DataError
from .contracts import LIMITS
from .history_contracts import artifact_limit, output_limits


def regular_owned(base, relative):
    if type(relative) is not str or not relative or ":" in relative or "\\" in relative:
        raise DataError("SDK_OUTPUT_PATH_INVALID")
    path = Path(base)/relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise DataError("SDK_OUTPUT_PATH_INVALID")
    base = Path(base).resolve(strict=True)
    for item in (path,*path.parents):
        if item == base:
            break
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400 or (hasattr(item,"is_junction") and item.is_junction()):
            raise DataError("SDK_OUTPUT_PATH_INVALID")
    if not path.resolve(strict=True).is_relative_to(base):
        raise DataError("SDK_OUTPUT_PATH_INVALID")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DataError("SDK_OUTPUT_PATH_INVALID")
    return path


def encoded(value, limit):
    raw = json.dumps(value,ensure_ascii=True,separators=(",",":"),allow_nan=False).encode()
    if len(raw)>limit:
        raise DataError("SDK_OUTPUT_LIMIT_EXCEEDED")
    return raw


def write_artifact(directory, name, raw, *, call=None):
    if not isinstance(raw,bytes) or len(raw)>artifact_limit(name, call):
        raise DataError("SDK_OUTPUT_LIMIT_EXCEEDED")
    target=Path(directory)/name
    with target.open("xb") as stream:
        stream.write(raw)
    return {"path":name,"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}


def cleanup_cache(work):
    work=Path(work).absolute()
    for ancestor in (work,*work.parents):
        info=ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400 or (hasattr(ancestor,"is_junction") and ancestor.is_junction()):
            raise DataError("SDK_CACHE_CLEANUP_FAILED")
    work=work.resolve(strict=True)
    cache=work/"cache"
    if not cache.exists():
        return
    if cache.is_symlink() or (hasattr(cache,"is_junction") and cache.is_junction()) or not cache.resolve().is_relative_to(work):
        raise DataError("SDK_CACHE_CLEANUP_FAILED")
    for path in cache.rglob("*"):
        info=path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400 or (stat.S_ISREG(info.st_mode) and info.st_nlink!=1):
            raise DataError("SDK_CACHE_CLEANUP_FAILED")
    # Absolute, fresh own-job path has been checked, never computed outside work.
    shutil.rmtree(cache)


def read_artifacts(policy, work, manifest, binding, *, call=None):
    if set(manifest)!={"schema_version","binding","status","error_code","files","entities","stats","sdk_ref"} or manifest["schema_version"]!="sdk-child-output-1" or manifest["binding"]!=binding:
        raise DataError("SDK_OUTPUT_INVALID")
    if type(manifest["files"]) is not list or len(manifest["files"])>LIMITS["requests"]+2:
        raise DataError("SDK_OUTPUT_INVALID")
    directory=Path(work)/"output"
    names=[x["path"] for x in manifest["files"]]
    allowed={"sdk-frame.json","observations.json"}|{"entity-"+str(i)+".json" for i in range(LIMITS["requests"])}
    if len(set(names))!=len(names) or any(n not in allowed for n in names) or {p.name for p in directory.iterdir()}!=set(names):
        raise DataError("SDK_OUTPUT_PATH_INVALID")
    total=0
    results={}
    for entry in manifest["files"]:
        if set(entry)!={"path","bytes","sha256"} or type(entry["bytes"])is not int or not 0<=entry["bytes"]<=artifact_limit(entry["path"], call):
            raise DataError("SDK_OUTPUT_INVALID")
        path=regular_owned(directory,entry["path"])
        if path.stat().st_size!=entry["bytes"]:
            raise DataError("SDK_OUTPUT_INVALID")
        raw=policy.read_bytes(path)  # Guarded development read; never direct raw/holdout access.
        if len(raw)!=entry["bytes"] or hashlib.sha256(raw).hexdigest()!=entry["sha256"]:
            raise DataError("SDK_OUTPUT_INVALID")
        total+=len(raw)
        if total>output_limits(call)["total"]:
            raise DataError("SDK_OUTPUT_LIMIT_EXCEEDED")
        results[entry["path"]]=raw
    return results
