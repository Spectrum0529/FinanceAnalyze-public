"""Shared byte/canonical source binding for producer, control plane and worker."""
import hashlib
import json
import os
from pathlib import Path
import stat

SRC_DIGEST_ALGORITHM = "python-src-manifest-sha256-v1"


class BindingError(ValueError):
    """Fixed code only; file paths/contents never enter diagnostics."""
    def __init__(self, code="UNSAFE_BINDING_PATH"):
        self.code = code
        super().__init__(code)


def checked_regular_file(root: Path, relative: str) -> Path:
    """No parent escape, reparse/symlink, nonregular file or hard-link alias."""
    return _checked_regular_file(Path(root).resolve(), relative)


def _checked_regular_file(root: Path, relative: str) -> Path:
    """Validate a path beneath a root already resolved by the trusted caller."""
    if type(relative) is not str or not relative or "\\" in relative or ":" in relative or relative.startswith("/"):
        raise BindingError()
    parts = relative.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise BindingError()
    current = root
    try:
        for index, part in enumerate(parts):
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise BindingError()
            if index < len(parts)-1 and not stat.S_ISDIR(info.st_mode):
                raise BindingError()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not current.resolve().is_relative_to(root):
            raise BindingError()
    except OSError:
        raise BindingError("BINDING_FILE_UNAVAILABLE") from None
    return current


def canonical_manifest(manifest) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def file_digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            sha.update(block)
    return sha.hexdigest()


def source_manifest(root: Path, *, require_sources=True) -> list[dict]:
    root = Path(root).resolve()
    source = root / "src"
    rows = []
    if source.exists() or source.is_symlink():
        try:
            info = source.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0) & 0x400:
                raise BindingError()
            def walk_error(error):
                raise BindingError("SOURCE_UNAVAILABLE")
            for directory, folders, files in os.walk(source, followlinks=False, onerror=walk_error):
                for name in list(folders):
                    part = Path(directory) / name
                    info = part.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0) & 0x400:
                        raise BindingError()
                    if name == "__pycache__":
                        folders.remove(name)
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    relative = (Path(directory)/name).relative_to(root).as_posix()
                    path = _checked_regular_file(root, relative)
                    rows.append({"path":relative,"sha256":file_digest(path)})
        except OSError:
            raise BindingError("SOURCE_UNAVAILABLE") from None
    if require_sources and not rows:
        raise BindingError("SOURCE_UNAVAILABLE")
    return sorted(rows, key=lambda row: row["path"])


def source_digest(root: Path) -> str:
    return hashlib.sha256(canonical_manifest(source_manifest(root))).hexdigest()


def deployment_digest(root: Path, *, external_handler: Path | None = None) -> str:
    """Same canonical base, with the existing trusted external-entry extension."""
    root = Path(root).resolve()
    rows = source_manifest(root, require_sources=False)
    if external_handler is not None:
        entry = Path(external_handler).absolute()
        if not entry.resolve().is_relative_to(root / "src"):
            # Registry supplies installed trusted Python code, never API paths.
            parts = {part.casefold() for part in entry.parts}
            if entry.suffix != ".py" or parts & {".local", "secrets", "authorized_raw", "candidate_holdout", "prospective"}:
                raise BindingError()
            checked = checked_regular_file(Path(entry.anchor), entry.relative_to(entry.anchor).as_posix())
            rows.append({"path":"external-handler:"+entry.name,"sha256":file_digest(checked)})
    if not rows:
        raise BindingError("SOURCE_UNAVAILABLE")
    return hashlib.sha256(canonical_manifest(rows)).hexdigest()
