"""Trusted native-Qlib adapter under the existing core job; no scheduler.

A server-created immutable binding and a staged FA014 export are mandatory.
Default Runtime composition does not configure this adapter or release training.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
import time

from .errors import PlatformError
from .qlib_process import QlibOwnedJob, QlibProcessProfile, profile_limits

# FA015 supplement: intersect the generic profile ceiling with safe_json's
# existing native primitive-result protocol. Export profile remains unchanged.
NATIVE_RESULT_BYTES = 2 * 1024**2


def _fail(code):
    raise PlatformError(code)


def _hex(value):
    if type(value) is not str or re.fullmatch("[0-9a-f]{64}", value) is None:
        _fail("QLIB_BINDING_INVALID")
    return value


def _regular(base, relative, *, directory=False):
    base = Path(base).absolute()
    if (type(relative) is not str or not relative or "\\" in relative or ":" in relative
            or any(p in {"", ".", ".."} for p in relative.split("/"))
            or PurePosixPath(relative).is_absolute()):
        _fail("QLIB_PATH_INVALID")
    for part in relative.split("/"):
        stem = part.split(".", 1)[0].upper()
        if (part.endswith((".", " ")) or any(c in part for c in '<>"|?*')
                or stem in {"CON", "PRN", "AUX", "NUL"}
                or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)):
            _fail("QLIB_PATH_ALIAS_REJECTED")
    # Check ancestors before resolving. Never follow aliases to another partition.
    for ancestor in (base, *base.parents):
        if ancestor.exists() and (ancestor.is_symlink()
                or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400):
            _fail("QLIB_PATH_ALIAS_REJECTED")
    target = base.joinpath(*relative.split("/"))
    for candidate in (target, *target.parents):
        if candidate.exists():
            stat = candidate.lstat()
            if candidate.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                _fail("QLIB_PATH_ALIAS_REJECTED")
            if candidate.is_file() and stat.st_nlink != 1:
                _fail("QLIB_PATH_ALIAS_REJECTED")
        if candidate == base:
            break
    if not target.resolve().is_relative_to(base.resolve()):
        _fail("QLIB_PATH_INVALID")
    if target.exists() and (target.is_dir() if directory else target.is_file()) is not True:
        _fail("QLIB_PATH_INVALID")
    return target


def _digest_bytes(path, limit):
    size = path.stat().st_size
    if not 0 <= size <= limit:
        _fail("QLIB_FILE_BUDGET_EXCEEDED")
    raw = path.read_bytes()
    if len(raw) != size:
        _fail("QLIB_FILE_CHANGED")
    return raw, hashlib.sha256(raw).hexdigest()


def _json_bytes(value, limit):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        _fail("QLIB_PROTOCOL_INVALID")
    if len(raw) > limit:
        _fail("QLIB_PROTOCOL_TOO_LARGE")
    return raw


def _checked_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("QLIB_RESULT_DUPLICATE_KEY")
            result[key] = value
        return result
    def constant(value):
        _fail("QLIB_RESULT_NONFINITE")
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except PlatformError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        _fail("QLIB_RESULT_INVALID")
    # JSON source can encode 1e999 without a NaN token.
    stack, count = [(result, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 64 or count > 200000:
            _fail("QLIB_RESULT_COMPLEXITY")
        if type(item) is dict:
            stack.extend((v, depth + 1) for v in item.values())
        elif type(item) is list:
            stack.extend((v, depth + 1) for v in item)
        elif type(item) is float and not math.isfinite(item):
            _fail("QLIB_RESULT_NONFINITE")
        elif item is not None and type(item) not in (str, int, float, bool):
            _fail("QLIB_RESULT_INVALID")
    return result


@dataclass(frozen=True)
class FilePin:
    path: Path
    bytes: int
    sha256: str

    def __post_init__(self):
        if not Path(self.path).is_absolute() or type(self.bytes) is not int or self.bytes < 0:
            _fail("QLIB_BINDING_INVALID")
        _hex(self.sha256)
        object.__setattr__(self, "path", Path(self.path).absolute())

    def metadata(self):
        return {"path": str(self.path), "bytes": self.bytes, "sha256": self.sha256}

    def read_verified(self):
        path = _regular(self.path.parent, self.path.name)
        raw, digest = _digest_bytes(path, 64 * 1024**2)
        if len(raw) != self.bytes or digest != self.sha256:
            _fail("QLIB_BOUND_FILE_CHANGED")
        return raw


def _evidence_pin_matches(row, pin):
    return (type(row) is dict and set(row) == {"path", "bytes", "sha256"}
            and type(row["path"]) is str and Path(row["path"]).is_absolute()
            and Path(row["path"]).absolute() == pin.path
            and type(row["bytes"]) is int and row["bytes"] == pin.bytes
            and row["sha256"] == pin.sha256)


@dataclass(frozen=True)
class QlibProbeCompletion:
    """Independent measured-probe evidence, never a native training approval.

    No executable probe runs here. The trusted integrator supplies exact pins
    for the already completed probe; output bytes must link to this identity.
    """
    worker_root: Path
    result: FilePin
    request: FilePin
    stdout: FilePin
    stderr: FilePin
    worker_spec: FilePin
    identity_probe: FilePin

    def __post_init__(self):
        if (not isinstance(self.worker_root, Path) or not self.worker_root.is_absolute()
                or self.worker_root.name != "qlib" or self.worker_root.parent.name != "workers"
                or any(type(pin) is not FilePin for pin in self.files)):
            _fail("QLIB_PROBE_COMPLETION_INVALID")
        object.__setattr__(self, "worker_root", self.worker_root.absolute())

    @property
    def files(self):
        return (self.result, self.request, self.stdout, self.stderr,
                self.worker_spec, self.identity_probe)

    def verify(self, binding, record, identity):
        raw = {pin.path: pin.read_verified() for pin in self.files}
        result = _checked_json(raw[self.result.path])
        request = _checked_json(raw[self.request.path])
        output = _checked_json(raw[self.stdout.path])
        spec = _checked_json(raw[self.worker_spec.path])
        if any(type(value) is not dict for value in (result, request, output, spec)):
            _fail("QLIB_PROBE_COMPLETION_MISMATCH")
        operation = result.get("operation_id")
        root = self.worker_root
        prefix = root / ".venv"
        site = prefix / "Lib/site-packages"
        directory = self.result.path.parent / "operations" / operation if type(operation) is str else None
        if (type(operation) is not str or re.fullmatch(r"qlib-probe-[0-9a-f]{32}", operation) is None
                or result.get("schema") != "FA010_WORKER_DEPLOYMENT_STAGE_ACTUAL_1"
                or result.get("status") != "PASS_FRESH_PROJECT_LOCAL_RUNTIME_IDENTITY"
                or result.get("worker") != "qlib" or result.get("stage") != "probe"
                or type(result.get("exit_code")) is not int or result["exit_code"] != 0
                or result.get("timeout") is not False or result.get("native_workflow_activated") is not False
                or result.get("operation_dir") != str(directory)
                or self.request.path != directory / "request.json"
                or self.stdout.path != directory / "stdout.log" or self.stderr.path != directory / "stderr.log"
                or binding.runtime_identity.path != directory / "actual_identity.json"
                or not _evidence_pin_matches(result.get("request"), self.request)
                or not _evidence_pin_matches(result.get("stdout"), self.stdout)
                or not _evidence_pin_matches(result.get("stderr"), self.stderr)
                or raw[self.stderr.path] != b""
                or _json_bytes(output, 1048576) != _json_bytes(identity, 1048576)):
            _fail("QLIB_PROBE_COMPLETION_MISMATCH")
        if (request.get("schema") != "FA010_WORKER_DEPLOYMENT_STAGE_REQUEST_1"
                or request.get("worker") != "qlib" or request.get("stage") != "probe"
                or request.get("operation_id") != operation
                or request.get("session_run_id") != self.result.path.parent.name
                or request.get("cwd") != str(root)
                or request.get("command") != [str(binding.worker_python.path), "-I", "-B", "-X", "utf8", str(self.identity_probe.path)]
                or request.get("UV_PROJECT_ENVIRONMENT") != str(prefix)
                or type(request.get("timeout_seconds")) is not int or not 1 <= request["timeout_seconds"] <= 60
                or type(result.get("elapsed_seconds")) not in (int, float)
                or not math.isfinite(result["elapsed_seconds"])
                or not 0 <= result["elapsed_seconds"] <= request["timeout_seconds"]
                or request.get("automatic_retry") is not False):
            _fail("QLIB_PROBE_COMPLETION_MISMATCH")
        if (binding.worker_python.path != prefix / "Scripts/python.exe"
                or binding.worker_manifest.path != root / "pyproject.toml" or binding.worker_lock.path != root / "uv.lock"
                or self.worker_spec.path != root / "worker_spec.json" or self.identity_probe.path != root / "inspect_runtime.py"
                or record.get("worker") != "qlib" or record.get("worker_root") != str(root)
                or record.get("site_root") != str(site) or record.get("reports_venv_fallback_allowed") is not False
                or record.get("native_workflow_activated") is not False
                or record.get("status") != "MEASURED_DEPENDENCY_DEPLOYMENT_ONLY_NOT_ACTIVE_BUSINESS_BINDING"
                or identity.get("worker_kind") != "qlib" or identity.get("worker_root") != str(root)
                or identity.get("site_root") != str(site) or identity.get("reports_venv_fallback_allowed") is not False
                or identity.get("business_or_auth_import_or_execution") is not False
                or identity.get("status") != "MEASURED_IDENTITY_ONLY_NOT_BUSINESS_ACTIVATION"
                or spec.get("schema") != "FINANCE_ANALYZE_WORKER_PACKAGE_SPEC_1" or spec.get("worker_kind") != "qlib"
                or spec.get("reports_venv_fallback_allowed") is not False):
            _fail("QLIB_PROJECT_WORKER_MISMATCH")
        if type(spec.get("control_file_sha256")) is not dict:
            _fail("QLIB_PROJECT_WORKER_MISMATCH")
        for key, pin in (("python", binding.worker_python), ("manifest", binding.worker_manifest),
                         ("lock", binding.worker_lock), ("worker_spec", self.worker_spec), ("identity_probe", self.identity_probe)):
            if not _evidence_pin_matches(record.get(key), pin) or not _evidence_pin_matches(identity.get(key), pin):
                _fail("QLIB_PROJECT_WORKER_MISMATCH")
        for pin in (binding.worker_manifest, binding.worker_lock, self.identity_probe):
            if spec.get("control_file_sha256", {}).get(pin.path.name) != pin.sha256:
                _fail("QLIB_PROJECT_WORKER_MISMATCH")
        for key in ("base_python", "actual_loaded_python_DLL", "venv_config"):
            pin = next((pin for pin in binding.runtime_files if _evidence_pin_matches(identity.get(key), pin)), None)
            if pin is None or not _evidence_pin_matches(record.get(key), pin):
                _fail("QLIB_PROJECT_WORKER_MISMATCH")
        primary = identity.get("primary_distribution_source_files")
        if (type(primary) is not list or _json_bytes(primary, 1048576) != _json_bytes(spec.get("primary_distribution_source_files"), 1048576)
                or type(identity.get("installed_distribution_versions")) is not dict
                or identity["installed_distribution_versions"] != spec.get("expected_installed_distribution_versions")
                or type(record.get("primary_source_file_count")) is not int or record["primary_source_file_count"] != len(primary)
                or type(record.get("installed_distribution_count")) is not int
                or record["installed_distribution_count"] != len(identity["installed_distribution_versions"])):
            _fail("QLIB_PROJECT_WORKER_MISMATCH")
        source_hash = hashlib.sha256(_json_bytes(primary, 1048576)).hexdigest()
        if (identity.get("primary_distribution_source_digest") != source_hash
                or record.get("primary_distribution_source_digest") != source_hash):
            _fail("QLIB_PROJECT_WORKER_MISMATCH")


@dataclass(frozen=True)
class QlibBinding:
    worker_python: FilePin
    worker_manifest: FilePin
    worker_lock: FilePin
    worker_binding: FilePin
    runtime_identity: FilePin
    entry: FilePin
    contract_source: FilePin
    bootstrap: FilePin
    runtime_files: tuple[FilePin, ...]
    security_receipt: FilePin
    financial_receipt: FilePin
    source_manifest_sha256: str
    native_manifest_hash: str
    systemroot: Path = Path("C:/Windows")
    probe_completion: QlibProbeCompletion | None = None

    def __post_init__(self):
        for value in (self.source_manifest_sha256, self.native_manifest_hash):
            _hex(value)
        if (type(self.runtime_files) is not tuple or len(self.runtime_files) > 32
                or self.entry.path.parent != self.contract_source.path.parent
                or self.entry.path.name != "qlib_child.py"
                or self.contract_source.path.name != "qlib_workflow.py"
                or self.bootstrap.path.name != "qlib_bootstrap.py"
                or not Path(self.systemroot).is_absolute()):
            _fail("QLIB_BINDING_INVALID")
        if self.probe_completion is not None and type(self.probe_completion) is not QlibProbeCompletion:
            _fail("QLIB_PROBE_COMPLETION_INVALID")

    @property
    def release(self):
        return {
            "security_receipt_sha256": self.security_receipt.sha256,
            "financial_receipt_sha256": self.financial_receipt.sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
            "native_manifest_hash": self.native_manifest_hash,
        }

    def verify(self):
        for pin in (self.worker_python, self.worker_manifest, self.worker_lock,
                    self.worker_binding, self.runtime_identity, self.entry, self.contract_source, self.bootstrap,
                    self.security_receipt, self.financial_receipt, *self.runtime_files):
            if type(pin) is not FilePin:
                _fail("QLIB_BINDING_INVALID")
            pin.read_verified()
        record = _checked_json(self.worker_binding.read_verified())
        identity = _checked_json(self.runtime_identity.read_verified())
        def matches(row, pin):
            return (type(row) is dict and type(row.get("path")) is str
                    and Path(row["path"]).absolute() == pin.path
                    and type(row.get("bytes")) is int and row["bytes"] == pin.bytes
                    and row.get("sha256") == pin.sha256)
        if type(record) is not dict or type(identity) is not dict:
            _fail("QLIB_RUNTIME_BINDING_MISMATCH")
        schema = record.get("schema")
        if schema == "FA010_INDEPENDENT_WINDOWS_QLIB_WORKER_BINDING_3":
            if (self.probe_completion is not None or type(identity.get("exit_code")) is not int or identity["exit_code"] != 0):
                _fail("QLIB_RUNTIME_BINDING_MISMATCH")
            python_key, identity_key = "worker_python", "runtime_identity"
        elif schema == "FA010_MEASURED_PROJECT_WORKER_BINDING_EVIDENCE_1":
            if (type(self.probe_completion) is not QlibProbeCompletion
                    or identity.get("schema") != "FINANCE_ANALYZE_WORKER_ACTUAL_RUNTIME_IDENTITY_1"
                    or "exit_code" in identity):
                _fail("QLIB_PROBE_COMPLETION_INVALID")
            python_key, identity_key = "python", "identity"
            self.probe_completion.verify(self, record, identity)
        else:
            _fail("QLIB_RUNTIME_BINDING_MISMATCH")
        if (not matches(record.get(python_key), self.worker_python)
                or not matches(record.get("manifest"), self.worker_manifest)
                or not matches(record.get("lock"), self.worker_lock)
                or not matches(record.get(identity_key), self.runtime_identity)
                or identity.get("version") != [3, 12, 14]
                or identity.get("platform") != "win32" or identity.get("pointer_bits") != 64
                or not matches(identity.get("python"), self.worker_python)
                or type(identity.get("venv_prefix")) is not str
                or Path(identity["venv_prefix"]).absolute() != self.worker_python.path.parents[1]):
            _fail("QLIB_RUNTIME_BINDING_MISMATCH")
        by_path = {pin.path: pin for pin in self.runtime_files}
        for key in ("base_python", "actual_loaded_python_DLL", "venv_config"):
            row = identity.get(key)
            if type(row) is not dict or type(row.get("path")) is not str:
                _fail("QLIB_RUNTIME_BINDING_MISMATCH")
            pin = by_path.get(Path(row["path"]).absolute())
            if pin is None or not matches(row, pin):
                _fail("QLIB_RUNTIME_BINDING_MISMATCH")
        if (self.worker_python.path.name.lower() != "python.exe"
                or Path(identity["venv_config"]["path"]).absolute()
                != self.worker_python.path.parents[1] / "pyvenv.cfg"):
            _fail("QLIB_RUNTIME_BINDING_MISMATCH")
        if self.probe_completion is not None:
            if len(by_path) != len(self.runtime_files):
                _fail("QLIB_RUNTIME_BINDING_MISMATCH")
            base, dll, _, _ = self.launch_files()
            if (base.path.name != "python.exe" or dll.path != base.path.parent / "python312.dll"
                    or not _evidence_pin_matches(record.get("identity"), self.runtime_identity)):
                _fail("QLIB_RUNTIME_BINDING_MISMATCH")


    def launch_files(self):
        record = _checked_json(self.worker_binding.read_verified())
        identity = _checked_json(self.runtime_identity.read_verified())
        by_path = {pin.path: pin for pin in self.runtime_files}
        base = by_path[Path(identity["base_python"]["path"]).absolute()]
        dll = by_path[Path(identity["actual_loaded_python_DLL"]["path"]).absolute()]
        prefix = self.worker_python.path.parents[1]
        site = _regular(prefix, "Lib/site-packages", directory=True)
        if (type(record.get("site_root")) is not str
                or Path(record["site_root"]).absolute() != site
                or type(identity.get("base_prefix")) is not str
                or Path(identity["base_prefix"]).absolute() != base.path.parent):
            _fail("QLIB_RUNTIME_BINDING_MISMATCH")
        return base, dll, site, prefix


def _protect(path):
    from finance_platform.operations.paths import protect_private
    protect_private(path)


def _artifact_files(root, limit):
    """Bound enumeration, without traversing a directory link/junction."""
    observed, pending, visited, total = {}, [("", root)], 0, 0
    while pending:
        prefix, directory = pending.pop()
        for path in directory.iterdir():
            visited += 1
            if visited > 8192:
                _fail("QLIB_ARTIFACT_BUDGET_EXCEEDED")
            relative = prefix + path.name
            if path.is_dir():
                checked = _regular(root, relative, directory=True)
                pending.append((relative + "/", checked))
            else:
                checked = _regular(root, relative)
                raw, digest = _digest_bytes(checked, limit)
                total += len(raw)
                if len(observed) >= 4096 or total > limit:
                    _fail("QLIB_ARTIFACT_BUDGET_EXCEEDED")
                observed[relative] = {"path": relative, "bytes": len(raw), "sha256": digest}
    return observed


def _native_disk_budget(attempt, limit):
    # Live metadata polling only; post-exit _regular and hashes remain strict.
    from .qlib_live_budget import rotating_disk_budget
    return rotating_disk_budget(attempt, limit)


@dataclass(frozen=True)
class QlibNativeLauncher:
    binding: QlibBinding
    attempts_root: Path
    contract: object  # Exact FA015 pure contract module, trusted composition only.

    def __post_init__(self):
        if (type(self.binding) is not QlibBinding or not Path(self.attempts_root).is_absolute()
                or Path(getattr(self.contract, "__file__", "")).absolute() != self.binding.contract_source.path):
            _fail("QLIB_BINDING_INVALID")
        object.__setattr__(self, "attempts_root", Path(self.attempts_root).absolute())

    @property
    def worker_binding_sha256(self):
        return self.binding.worker_binding.sha256

    @property
    def source_manifest_sha256(self):
        return self.binding.source_manifest_sha256

    @property
    def release(self):
        return self.binding.release

    @property
    def worker_lock_bytes(self):
        return self.binding.worker_lock.read_verified()

    def _attempt(self, context, supplied):
        owner, run_id = context.run.owner_id, context.run.run_id
        if (type(owner) is not str or not owner or type(run_id) is not str
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", run_id) is None
                or ":" in run_id):
            _fail("QLIB_ATTEMPT_BINDING_MISMATCH")
        namespace = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        expected = _regular(self.attempts_root, namespace + "/" + run_id, directory=True)
        if Path(supplied).absolute() != expected.absolute() or not expected.is_dir():
            _fail("QLIB_ATTEMPT_BINDING_MISMATCH")
        return expected

    def prepare_attempt(self, context, attempt_root):
        """FA015 calls BEFORE FA014 stages files; no native process or export."""
        context.require_active()
        attempt = self._attempt(context, attempt_root)
        if any(attempt.iterdir()):
            _fail("QLIB_ATTEMPT_NOT_FRESH")
        _protect(attempt)
        context.require_active()
        # Marker is exclusive and binds this one server-generated owner/run.
        marker = {"owner_hash": hashlib.sha256(context.run.owner_id.encode()).hexdigest(),
                  "run_id": context.run.run_id, "job_id": context.job_id,
                  "fencing_token": context.fencing_token}
        with (attempt / "parent-prepared.json").open("xb") as stream:
            stream.write(_json_bytes(marker, 4096))

    def _verify_prepared(self, context, attempt):
        if not (attempt / "parent-prepared.json").is_file():
            _fail("QLIB_ATTEMPT_NOT_PREPARED")
        raw, _ = _digest_bytes(_regular(attempt, "parent-prepared.json"), 4096)
        expected = {"owner_hash": hashlib.sha256(context.run.owner_id.encode()).hexdigest(),
                    "run_id": context.run.run_id, "job_id": context.job_id,
                    "fencing_token": context.fencing_token}
        if _checked_json(raw) != expected:
            _fail("QLIB_ATTEMPT_BINDING_MISMATCH")
        if set(path.name for path in attempt.iterdir()) != {"parent-prepared.json", "provider"}:
            _fail("QLIB_ATTEMPT_NOT_FRESH")
        _regular(attempt, "provider", directory=True)

    def _environment(self, attempt):
        # A new dictionary, never os.environ/copy/PATH/credential inheritance.
        return {
            "SYSTEMROOT": str(self.binding.systemroot),
            "HOME": str(attempt / "home"), "USERPROFILE": str(attempt / "home"),
            "TEMP": str(attempt / "tmp"), "TMP": str(attempt / "tmp"),
            "APPDATA": str(attempt / "cache/Roaming"),
            "LOCALAPPDATA": str(attempt / "cache/Local"),
            "MLFLOW_DISABLE_TELEMETRY": "true", "MLFLOW_ENABLE_ASYNC_LOGGING": "false",
            # Qlib datetime_groupby_apply otherwise requests joblib n_jobs=-1.
            "JOBLIB_MULTIPROCESSING": "0",
            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
            "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _validate_request(self, context, request):
        if (type(request) is not dict or request.get("owner_id") != context.run.owner_id
                or request.get("run_id") != context.run.run_id
                or request.get("job_id") != context.job_id
                or request.get("fencing_token") != context.fencing_token
                or request.get("worker_binding_sha256") != self.worker_binding_sha256
                or request.get("source_manifest_sha256") != self.source_manifest_sha256
                or request.get("release") != self.release
                or request.get("data", {}).get("native_manifest_hash") != self.binding.native_manifest_hash):
            _fail("QLIB_REQUEST_BINDING_MISMATCH")
        self.contract.validate_request(request, native=True)

    def _validate_result(self, result, request, attempt):
        # Final peer is FA015 revisions/r02, not the earlier r01 schema.
        # Both audit counters are exact integer zero, never bool/default/missing.
        if type(result) is not dict:
            _fail("QLIB_DENIAL_COUNTERS_REQUIRED_ZERO")
        for key in ("network_attempts", "process_attempts"):
            if type(result.get(key)) is not int or result[key] != 0:
                _fail("QLIB_DENIAL_COUNTERS_REQUIRED_ZERO")
        self.contract.validate_result(result, request)
        relative = result.get("native_artifact_root")
        if type(relative) is not str or not relative.startswith("recorders/"):
            _fail("QLIB_ARTIFACT_SCOPE_INVALID")
        root = _regular(attempt, relative, directory=True)
        observed = _artifact_files(root, profile_limits(QlibProcessProfile.TRAIN).artifact_bytes)
        rows = result.get("native_artifacts")
        if (type(rows) is not list or not rows or len(rows) > 4096
                or any(type(row) is not dict or set(row) != {"path", "bytes", "sha256"} for row in rows)):
            _fail("QLIB_ARTIFACT_MANIFEST_INVALID")
        expected = {row["path"]: row for row in rows}
        if len(expected) != len(rows) or observed != expected:
            _fail("QLIB_NATIVE_ARTIFACT_CHANGED")
        return result

    def _packet(self, context, request, attempt, process, handle):
        base, dll, site, prefix = self.binding.launch_files()
        packet = {
            "schema": "qlib-admission-2", "request": request, "attempt_root": str(attempt),
            "expected_pid": process.pid, "job_handle": handle,
            "worker_python": self.binding.worker_python.metadata(),
            "base_python": base.metadata(), "base_dll": dll.metadata(),
            "bootstrap": self.binding.bootstrap.metadata(),
            "entry": self.binding.entry.metadata(), "contract": self.binding.contract_source.metadata(),
            "site_root": str(site), "venv_prefix": str(prefix),
        }
        packet["binding"] = hashlib.sha256(_json_bytes(packet, 524284)).hexdigest()
        return packet

    def _ready(self, ready, packet, process):
        fields = {"schema", "binding", "actual_pid", "actual_image", "actual_python_dll",
                  "actual_self_in_held_job", "duplicated_job_handle_closed",
                  "actual_python_dll_sha256", "version", "site_root", "prefix",
                  "actual_base_prefix", "qlib_imported", "automatic_site_startup"}
        paths = ("actual_image", "actual_python_dll", "site_root", "prefix", "actual_base_prefix")
        if (type(ready) is not dict or set(ready) != fields
                or any(type(ready[key]) is not str for key in paths)
                or ready["schema"] != "qlib-ready-2" or ready["binding"] != packet["binding"]
                or type(ready["actual_pid"]) is not int or ready["actual_pid"] != process.pid
                or ready["version"] != [3, 12, 14]
                or ready["actual_self_in_held_job"] is not True
                or ready["duplicated_job_handle_closed"] is not True
                or ready["qlib_imported"] is not False or ready["automatic_site_startup"] is not False
                or ready["actual_python_dll_sha256"] != packet["base_dll"]["sha256"]
                or Path(ready["actual_image"]).absolute() != Path(packet["base_python"]["path"]).absolute()
                or Path(ready["actual_python_dll"]).absolute() != Path(packet["base_dll"]["path"]).absolute()
                or Path(ready["site_root"]).absolute() != Path(packet["site_root"]).absolute()
                or Path(ready["prefix"]).absolute() != Path(packet["venv_prefix"]).absolute()
                or Path(ready["actual_base_prefix"]).absolute() != Path(packet["base_python"]["path"]).parent.absolute()):
            _fail("QLIB_READY_IDENTITY_MISMATCH")
        return ready

    def execute(self, context, request, attempt_root):
        context.require_active()
        if os.name != "nt":
            _fail("QLIB_WINDOWS_TARGET_REQUIRED")
        limits = profile_limits(QlibProcessProfile.TRAIN)
        start = time.monotonic()
        attempt = self._attempt(context, attempt_root)
        self._verify_prepared(context, attempt)
        with (attempt / "parent-execution.json").open("xb") as stream:
            stream.write(_json_bytes({"job_id": context.job_id,
                                      "fencing_token": context.fencing_token}, 4096))
        self._validate_request(context, request)
        encoded = _json_bytes(request, limits.request_bytes)
        self.binding.verify()
        # FA014 staged export only; no path from the user request is opened.
        self.contract.verify_provider(attempt, request["data"])
        provider = _regular(attempt, "provider", directory=True)
        actual_provider = _artifact_files(provider, limits.artifact_bytes)
        provider_rows = request["data"]["provider_files"]
        if (len(provider_rows) > 256 or len({row["path"] for row in provider_rows}) != len(provider_rows)
                or actual_provider != {row["path"]: row for row in provider_rows}):
            _fail("QLIB_PROVIDER_MANIFEST_MISMATCH")
        if (attempt / "result.json").exists():
            _fail("QLIB_ATTEMPT_NOT_FRESH")
        process = job = writer = None
        readers = []
        counts = [0, 0]
        io_error, overflow = threading.Event(), threading.Event()
        admitted = False
        primary = None
        proof = None
        outcome = "FAILED"
        result_hash = None
        ready = None
        ready_bytes = 0
        packet_encoded = None

        def active():
            context.require_active()
            if time.monotonic() - start >= limits.wall_seconds:
                _fail("QLIB_NATIVE_TIMEOUT")

        def drain(pipe, target, index, budget):
            try:
                with target.open("xb") as stream:
                    while chunk := pipe.read(4096):
                        counts[index] += len(chunk)
                        remaining = budget - stream.tell()
                        if remaining > 0:
                            stream.write(chunk[:remaining])
                        if counts[index] > budget:
                            overflow.set()
                            break
            except Exception:
                io_error.set()
            finally:
                try:
                    pipe.close()
                except Exception:
                    io_error.set()

        def send_packet():
            try:
                active()
                process.stdin.write(b"GO\n" + packet_encoded + b"\n")
                process.stdin.flush()
            except Exception:
                io_error.set()

        def send_exec():
            nonlocal admitted
            try:
                active()
                process.stdin.write(b"EXEC\n")
                process.stdin.flush()
                admitted = True
            except Exception:
                io_error.set()
            finally:
                try:
                    process.stdin.close()
                except Exception:
                    io_error.set()

        try:
            active()
            base, dll, site, prefix = self.binding.launch_files()
            argv = [str(base.path), "-I", "-S", "-B", "-X", "utf8", str(self.binding.bootstrap.path)]
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=attempt, env=self._environment(attempt),
                creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
            job = QlibOwnedJob(process, QlibProcessProfile.TRAIN)
            proof = job.verify_before_admission()
            packet = self._packet(context, request, attempt, process, job.duplicate_into(process))
            packet_encoded = _json_bytes(packet, limits.request_bytes - 4)
            # stderr is drained even during the no-third-party READY phase.
            stderr_thread = threading.Thread(
                target=drain, args=(process.stderr, attempt / "stderr.bin", 1, limits.stderr_bytes),
                daemon=True)
            stderr_thread.start()
            readers.append(stderr_thread)
            ready_lines = []
            def read_ready():
                try:
                    ready_lines.append(process.stdout.readline(8193))
                except Exception:
                    io_error.set()
            admission = threading.Thread(target=read_ready, daemon=True)
            readers.append(admission)
            admission.start()
            writer = threading.Thread(target=send_packet, daemon=True)
            active()
            writer.start()
            while admission.is_alive():
                active()
                if io_error.is_set() or overflow.is_set() or process.poll() is not None:
                    _fail("QLIB_ADMISSION_FAILED")
                time.sleep(.01)
            writer.join(timeout=2)
            if writer.is_alive() or io_error.is_set() or not ready_lines or not 0 < len(ready_lines[0]) <= 8192:
                _fail("QLIB_ADMISSION_FAILED")
            ready_bytes = len(ready_lines[0])
            ready = self._ready(_checked_json(ready_lines[0]), packet, process)
            # Only after actual pid/image/DLL/site and held membership matched.
            proof = job.verify_before_admission()
            active()
            stdout_thread = threading.Thread(
                target=drain, args=(process.stdout, attempt / "stdout.bin", 0,
                                   limits.stdout_bytes - ready_bytes), daemon=True)
            readers.append(stdout_thread)
            stdout_thread.start()
            writer = threading.Thread(target=send_exec, daemon=True)
            writer.start()
            while process.poll() is None:
                active()
                if overflow.is_set():
                    _fail("QLIB_PIPE_LIMIT_EXCEEDED")
                if io_error.is_set():
                    _fail("QLIB_PIPE_FAILED")
                running = job.snapshot()
                if running["pids"] != [process.pid] and process.poll() is None:
                    _fail("QLIB_JOB_MEMBERSHIP_UNPROVEN")
                _native_disk_budget(attempt, limits.artifact_bytes)
                time.sleep(.02)
            process.wait(timeout=2)
            for thread in readers:
                thread.join(timeout=2)
            writer.join(timeout=2)
            active()
            if any(thread.is_alive() for thread in [writer, *readers]):
                _fail("QLIB_PIPE_JOIN_FAILED")
            if overflow.is_set():
                _fail("QLIB_PIPE_LIMIT_EXCEEDED")
            if io_error.is_set():
                _fail("QLIB_PIPE_FAILED")
            if process.returncode != 0:
                _fail("QLIB_NATIVE_CHILD_FAILED")
            proof = job.require_empty()
            _native_disk_budget(attempt, limits.artifact_bytes)
            target = _regular(attempt, "result.json")
            raw, result_hash = _digest_bytes(target, min(limits.result_bytes, NATIVE_RESULT_BYTES))
            result = _checked_json(raw)
            self._validate_result(result, request, attempt)
            active()
            outcome = "STAGED_NATIVE_RESULT_NOT_CORE_SUCCESS"
            return result
        except BaseException as error:
            primary = error.code if isinstance(error, PlatformError) else "QLIB_EXECUTOR_FAILED"
            if isinstance(error, PlatformError):
                raise
            raise PlatformError("QLIB_EXECUTOR_FAILED") from None
        finally:
            cleanup_failed = False
            if job is not None:
                try:
                    if process.poll() is None:
                        job.terminate()
                    process.wait(timeout=5)
                    proof = job.require_empty()
                except Exception:
                    cleanup_failed = True
                finally:
                    job.close()
            elif process is not None:
                # Assignment failure: only the just-created, unadmitted child.
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                except Exception:
                    cleanup_failed = True
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    try:
                        pipe.close()
                    except Exception:
                        cleanup_failed = True
            for thread in ([writer] if writer is not None else []) + readers:
                if thread.ident is not None:
                    thread.join(timeout=2)
                cleanup_failed |= thread.is_alive()
            evidence = {
                "outcome": outcome, "primary_error_code": primary, "admission_sent": admitted,
                "child_exit_code": None if process is None else process.returncode,
                "child_reaped": process is not None and process.poll() is not None,
                "stdout_bytes": counts[0] + ready_bytes, "stderr_bytes": counts[1],
                "ready": ready, "ready_bytes": ready_bytes,
                "configured_worker_python": str(self.binding.worker_python.path),
                "launch_pattern": "DIRECT_QLIB_BASE_MINUS_S_EXPLICIT_QLIB_SITE",
                "cleanup_failed": bool(cleanup_failed), "held_job_final": proof,
                "result_sha256": result_hash,
                "entry_sha256": self.binding.entry.sha256,
                "denied_process_guard": "PINNED_FA015_ALL_DENIALS_GATE_AND_CHILD_EXIT0",
                "result_authority": "CORE_JOB_SUCCEEDED_REQUIRED",
            }
            with (attempt / "parent-outcome.json").open("xb") as stream:
                stream.write(_json_bytes(evidence, 16384))
            if cleanup_failed:
                _fail("QLIB_OWNED_CLEANUP_UNPROVEN")
