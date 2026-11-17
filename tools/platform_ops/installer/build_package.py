"""Build an isolated Windows setup pair from explicitly selected, reviewed inputs."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import stat
import subprocess
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile


MACHINE_BINDING = "src/finance_platform/data/provider_sdk/runtime_binding.json"
MCP_STDIO = "tools/platform_ops/mcp_stdio.py"


PROGRAM_FILES = (
    "FinanceAnalyze.exe", "README.md", "README.zh-CN.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
    "pyproject.toml", "uv.lock", ".python-version",
    "configs/access_policy.example.json", "configs/protocol.json", "configs/protocol.calendar.json",
    "configs/calendar.lock.json", "configs/split_policy.json", "configs/data_sources.lock.json",
    "configs/data_quality.baseline.json",
    "configs/autonomous_paths.example.json", "samples/public/synthetic_market/events.json",
    "resources/prediction_protocol.json", "resources/data_requirements.json",
    "docs/GETTING_STARTED.md", "docs/README.md", "docs/BUILDING.md",
    "tools/platform_ops/launcher/README.txt",
    "docs/USER_GUIDE.md", "docs/OPERATIONS.md", "docs/AI_SETUP_GUIDE.md",
    "docs/platform/MCP_CONNECTION.md",
    "tools/platform_ops/start.ps1", "tools/platform_ops/stop.ps1",
    "tools/platform_ops/source-install.ps1", "tools/platform_ops/launcher/bridge.ps1", MCP_STDIO,
    "vendor/wheels/AKShare-MIT-LICENSE",
)
PYTHON_FILES = (
    "LICENSE.txt", "python.exe", "python3.dll", "python312.dll",
    "pythonw.exe", "vcruntime140.dll", "vcruntime140_1.dll",
)
CACHE_TREES = ("archive-v0", "builds-v0", "sdists-v9", "simple-v24", "wheels-v6")


def check(path: Path) -> Path:
    for part in (path, *path.parents):
        if part.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Reparse input: {path.name}")
    return path


def files(directory: Path, ignored=()):
    if directory.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Reparse input: {directory.name}")
    for item in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
        if item.name in ignored or item.suffix == ".pyc":
            continue
        if item.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"Reparse input: {item.name}")
        if item.is_dir():
            yield from files(item, ignored)
        elif item.is_file():
            yield item


def locked_wheels(project, lockfile):
    """Select only vendored dependency wheels that are pinned by the current lock."""
    packages = tomllib.loads(lockfile.read_text(encoding="utf-8"))["package"]
    result = {}
    vendor_directories = {
        PurePosixPath("vendor/wheels"),
        PurePosixPath("workers/tradingagents/vendor/wheels"),
    }
    for package in packages:
        value = package["source"].get("path")
        if value is None:
            continue
        relative = PurePosixPath(value)
        if (relative.parent not in vendor_directories or relative.suffix != ".whl"
                or value != relative.as_posix() or ":" in value or "\\" in value):
            raise ValueError("Local dependency wheels must use a packaged vendor directory")
        source = check(project / relative)
        with source.open("rb") as stream:
            sha = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
        entries = [entry for entry in package.get("wheels", ()) if entry.get("filename") == relative.name]
        if not any(entry.get("hash") == sha for entry in entries):
            raise ValueError(f"Locked dependency wheel hash mismatch: {relative.name}")
        result[value] = source
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "python", "uv", "cache", "out"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--uv-licenses", type=Path)
    parser.add_argument("--python-licenses", type=Path)
    parser.add_argument("--engineering-fixture", action="store_true")
    args = parser.parse_args()
    if not args.engineering_fixture and (args.uv_licenses is None or args.python_licenses is None):
        parser.error("Release members require the upstream uv and Python runtime license files")
    project = check(args.project.absolute())
    python = check(args.python.absolute())
    out = args.out.absolute()
    if out.is_relative_to(project):
        parser.error("Output must be outside Main")
    check(out.parent)
    out.mkdir()
    binding = runpy.run_path(str(project / "src/finance_platform/core/source_binding.py"))
    digest = binding["file_digest"]
    source_hash = binding["source_digest"](project)
    lock_hash = digest(project / "uv.lock")
    members = {name: check(project / name) for name in PROGRAM_FILES}
    members.update(locked_wheels(project, project / "uv.lock"))
    members["runtime/uv.exe"] = check(args.uv.absolute())

    def add_tree(folder, archive_name, ignored=()):
        check(folder)
        for item in files(folder, ignored):
            name = f"{archive_name}/{item.relative_to(folder).as_posix()}"
            if name.casefold() != MACHINE_BINDING.casefold():
                previous = members.get(name)
                if previous is not None and digest(previous) != digest(item):
                    raise ValueError(f'Conflicting package inputs: {name}')
                members[name] = item

    for name in ("src", "web/dist"):
        add_tree(project / name, name, {"__pycache__"})
    add_tree(project / "licenses", "licenses")
    for name in PYTHON_FILES:
        members[f"runtime/python/{name}"] = check(python / name)
    for name in ("DLLs", "Lib", "tcl"):
        ignored = {"site-packages", "__pycache__"}
        if name == "Lib":
            ignored.add("test")
        add_tree(python / name, f"runtime/python/{name}", ignored)
    for name in CACHE_TREES:
        folder = args.cache.absolute() / name
        if folder.is_dir():
            add_tree(folder, f"runtime/uv-cache/{name}", {"__pycache__", ".tmp"})
    if args.python_licenses is not None:
        check(args.python_licenses / "LICENSE.cpython.txt")
        check(args.python_licenses / "LICENSE.openssl-3.txt")
        add_tree(args.python_licenses, "licenses/python-build-standalone-20260924")
    if args.uv_licenses is not None:
        for name in ("LICENSE-APACHE", "LICENSE-MIT"):
            members[f"licenses/uv/{name}"] = check(args.uv_licenses.absolute() / name)

    archive_path = out / "FinanceAnalyze.payload.zip"
    manifest = []
    with ZipFile(archive_path, "x", compression=ZIP_DEFLATED, compresslevel=4) as archive:
        for name, item in sorted(members.items()):
            before = digest(item)
            archive.write(item, name)
            if digest(item) != before:
                raise RuntimeError(f"Input changed during packaging: {name}")
            manifest.append({"path": name, "sha256": before, "bytes": item.stat().st_size})
    with ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        if any(name.casefold() == MACHINE_BINDING.casefold() for name in names) or MCP_STDIO not in names:
            raise RuntimeError("Archive must omit the build-machine SDK binding and include the MCP stdio bridge")
    if binding["source_digest"](project) != source_hash or digest(project / "uv.lock") != lock_hash:
        raise RuntimeError("Source or lock changed during packaging")
    package = digest(archive_path)
    generated = out / "SetupPins.cs"
    generated.write_text(
        "namespace FinanceAnalyzeSetup { internal static class SetupPins {\n"
        f'internal const string Package = "{package}";\n'
        f'internal const string Source = "{source_hash}";\n'
        f'internal const string Lock = "{lock_hash}";\n'
        "} }\n",
        encoding="utf-8",
    )
    candidate = Path(__file__).resolve().parent
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    exe = out / "FinanceAnalyze-Setup.exe"
    result = subprocess.run([
        str(compiler), "/nologo", "/utf8output", "/codepage:65001", "/optimize+",
        "/target:winexe", "/platform:x64", f"/out:{exe}",
        f"/win32manifest:{project / 'tools/platform_ops/launcher/launcher.manifest'}",
        *("/reference:" + reference + ".dll" for reference in (
            "System.Windows.Forms", "System.Drawing", "System.Web.Extensions", "System.IO.Compression",
        )),
        *(str(candidate / name) for name in ("PackageInstaller.cs", "SetupRuntime.cs", "SetupForm.cs")),
        str(generated),
    ], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    (out / "compile.log").write_bytes(result.stdout + result.stderr)
    pins = {"engineering_fixture": args.engineering_fixture, "payload_sha256": package,
            "payload_bytes": archive_path.stat().st_size, "source_sha256": source_hash,
            "lock_sha256": lock_hash, "compiler_exit": result.returncode,
            "setup_sha256": digest(exe) if exe.exists() else None, "members": manifest}
    (out / "package.json").write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in pins.items() if key != "members"}))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
