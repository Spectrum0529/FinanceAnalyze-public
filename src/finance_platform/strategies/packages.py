"""Bounded package ingestion and immutable content-addressed version storage."""

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from .contracts import Manifest, StrategyError, canonical, digest

MAX_FILES, MAX_FILE, MAX_TOTAL = 64, 256_000, 1_000_000
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def safe_name(name):
    if not isinstance(name, str) or len(name) > 160 or "\\" in name or ":" in name:
        raise StrategyError("PACKAGE_PATH_INVALID")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or len(path.parts) > 4 or any(
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", x) or x in {".", ".."} or x.endswith((".", " ")) or x.split(".")[0].upper() in RESERVED
            for x in path.parts):
        raise StrategyError("PACKAGE_PATH_INVALID")
    if path.suffix not in {".py", ".json", ".txt"}:
        raise StrategyError("PACKAGE_FORMAT_INVALID")
    return path


def inspect_package(source):
    source = Path(source)
    files = {}
    if source.is_dir():
        root = source.resolve()
        for entry in source.rglob("*"):
            if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()) or not entry.resolve().is_relative_to(root):
                raise StrategyError("PACKAGE_LINK_DENIED")
            if entry.is_file():
                name = entry.relative_to(source).as_posix()
                safe_name(name)
                if entry.stat().st_size > MAX_FILE:
                    raise StrategyError("PACKAGE_SIZE_EXCEEDED")
                files[name] = entry.read_bytes()
                _limits(files)
    elif source.suffix == ".zip":
        if source.stat().st_size > MAX_TOTAL:
            raise StrategyError("PACKAGE_SIZE_EXCEEDED")
        try:
            with zipfile.ZipFile(source) as archive:
                for entry in archive.infolist():
                    if entry.is_dir():
                        continue
                    name = entry.filename
                    safe_name(name)
                    if name in files or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                        raise StrategyError("PACKAGE_LINK_OR_DUPLICATE_DENIED")
                    if entry.file_size > MAX_FILE or len(files) >= MAX_FILES or sum(map(len, files.values())) + entry.file_size > MAX_TOTAL:
                        raise StrategyError("PACKAGE_SIZE_EXCEEDED")
                    files[name] = archive.read(entry)
                    _limits(files)
        except (zipfile.BadZipFile, RuntimeError, OSError):
            raise StrategyError("PACKAGE_ARCHIVE_INVALID") from None
    else:
        raise StrategyError("PACKAGE_DIRECTORY_OR_ZIP_REQUIRED")
    if len({x.casefold() for x in files}) != len(files):
        raise StrategyError("PACKAGE_CASE_COLLISION")
    try:
        manifest = Manifest.parse(json.loads(files["manifest.json"]))
        lock_bytes = files["dependencies.lock.json"]
        lock = json.loads(lock_bytes)
        if lock != {"python": "3.12.14", "packages": []}:
            raise StrategyError("PLUGIN_DEPENDENCIES_UNSUPPORTED")
        if hashlib.sha256(lock_bytes).hexdigest() != manifest.raw["dependency_lock"]["sha256"]:
            raise StrategyError("DEPENDENCY_LOCK_HASH_MISMATCH")
        module, function = manifest.raw["entrypoint"].split(":")
        tree = ast.parse(files[module + ".py"].decode("utf-8"))
        if not any(isinstance(node, ast.FunctionDef) and node.name == function for node in tree.body):
            raise StrategyError("ENTRYPOINT_MISSING")
        for name, data in files.items():
            if name.endswith(".py"):
                ast.parse(data.decode("utf-8"))
    except (KeyError, UnicodeError, SyntaxError, json.JSONDecodeError):
        raise StrategyError("PACKAGE_CONTENT_INVALID") from None
    file_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    return manifest, files, digest(file_hashes)


def _limits(files):
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL:
        raise StrategyError("PACKAGE_SIZE_EXCEEDED")


class PackageStore:
    """Local files only; metadata ownership/auth/transactions belong to core Store."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def freeze(self, source):
        manifest, files, sha = inspect_package(source)
        destination = self.root / sha
        if destination.exists():
            self.verify(sha)
        else:
            # Exclusive mkdir prevents concurrent writers silently replacing another package.
            destination.mkdir()
            try:
                for name, data in files.items():
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as stream:
                        stream.write(data)
            except Exception:
                # Preserve partial evidence; it will fail verification and cannot execute.
                raise StrategyError("PACKAGE_FREEZE_INTERRUPTED") from None
        return {"id": manifest.raw["id"], "version": manifest.raw["version"], "sha256": sha,
                "manifest": manifest.raw}

    def path(self, sha):
        if not re.fullmatch(r"[a-f0-9]{64}", str(sha)):
            raise StrategyError("PACKAGE_HASH_INVALID")
        path = (self.root / sha).resolve()
        if not path.is_relative_to(self.root):
            raise StrategyError("PACKAGE_PATH_INVALID")
        return path

    def verify(self, sha):
        manifest, _, actual = inspect_package(self.path(sha))
        if actual != sha:
            raise StrategyError("FROZEN_PACKAGE_CHANGED")
        return manifest

    def discover(self):
        return [{"sha256": p.name, "manifest": self.verify(p.name).raw}
                for p in sorted(self.root.iterdir()) if p.is_dir()]
