"""Fixed local server composition; no client paths, backend or native dispatch."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import math
import re
import stat

from predictor.access import AccessPolicy, AccessError
from .errors import PlatformError
from .source_binding import checked_regular_file, source_digest, BindingError

CONFIG_PATH = ".local/platform/native-research.release.json"
SCHEMA = "finance-research-server-1"
PROFILE = "FA014-QLIB-US-OFFLINE-V2"
METADATA_PREFIXES = ("reports/platform_acceptance/", "docs/platform/", ".local/platform/research/")


def _invalid():
    raise PlatformError("RESEARCH_SERVER_CONFIG_INVALID")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _json(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: _invalid())
    except (ValueError, UnicodeError, RecursionError):
        _invalid()
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 32:
            _invalid()
        if type(node) is dict:
            pending.extend((v, depth + 1) for v in node.values())
        elif type(node) is list:
            pending.extend((v, depth + 1) for v in node)
        elif type(node) is float and math.isfinite(node):
            continue
        elif node is not None and type(node) not in (str, bool, int):
            _invalid()
    return value


@dataclass(frozen=True)
class MetadataPin:
    path: str
    bytes: int
    sha256: str

    def __post_init__(self):
        path, size, sha = self.path, self.bytes, self.sha256
        if (type(path) is not str or not path.endswith(".json") or not path.startswith(METADATA_PREFIXES)
                or "\\" in path or ":" in path or any(p in {"", ".", ".."} for p in path.split("/"))
                or type(size) is not int or not 0 <= size <= 8 * 1024**2
                or type(sha) is not str or re.fullmatch("[0-9a-f]{64}", sha) is None):
            _invalid()

    @classmethod
    def parse(cls, value):
        if type(value) is not dict or set(value) != {"path", "bytes", "sha256"}:
            _invalid()
        path, size, sha = value["path"], value["bytes"], value["sha256"]
        if (type(path) is not str or not path.endswith(".json") or not path.startswith(METADATA_PREFIXES)
                or "\\" in path or ":" in path or any(p in {"", ".", ".."} for p in path.split("/"))
                or type(size) is not int or not 0 <= size <= 8 * 1024**2
                or type(sha) is not str or re.fullmatch("[0-9a-f]{64}", sha) is None):
            _invalid()
        return cls(path, size, sha)


@dataclass(frozen=True)
class QlibServerMaterial:
    """Static inputs only. Each owner/export/run grant remains data-owned."""
    root: Path
    profile: str
    source_digest: str
    worker_binding: MetadataPin
    probe_completion: MetadataPin
    source_record: MetadataPin
    security_review: MetadataPin | None
    financial_review: MetadataPin | None
    fixture_catalog: MetadataPin | None
    dump_review: MetadataPin | None = None

    @property
    def state(self):
        return "STATIC_REFERENCES_VERIFIED" if self.security_review and self.financial_review else "STATIC_REVIEW_INCOMPLETE"


def _read(root, policy, relative, maximum):
    path = checked_regular_file(root, relative)
    path = policy.authorize(path, operation="config_read")
    if path.stat().st_size > maximum:
        _invalid()
    raw = path.read_bytes()
    if len(raw) > maximum:
        _invalid()
    return raw


def read_metadata(root, pin):
    """Owners re-read static pins through the same guarded fixed namespace."""
    if type(pin) is not MetadataPin:
        _invalid()
    root = Path(root).resolve()
    raw = _read(root, AccessPolicy(root), pin.path, 8 * 1024**2)
    if len(raw) != pin.bytes or hashlib.sha256(raw).hexdigest() != pin.sha256:
        _invalid()
    return _json(raw)


def _configured_file(root):
    current = root
    parts = CONFIG_PATH.split("/")
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or (index < len(parts)-1 and not stat.S_ISDIR(info.st_mode))):
            _invalid()
    return True


def load_server_composition(root, *, core_only=False, native_loopback_accounts=False):
    """Shared normal API/worker entry. Absence disables; bad config rejects.

    This revision installs verified static material and optional data catalog.
    FA013/FA015 consume material in their pending per-export immutable factory;
    no singleton native-data hash, fixture model, training GO or ready is made.
    """
    root = Path(root).resolve()
    if core_only:
        return {}
    try:
        if not _configured_file(root):
            return {}
        document = _json(_read(root, AccessPolicy(root), CONFIG_PATH, 65536))
        if type(document) is not dict or set(document) != {"schema", "qlib", "tradingagents"} or document["schema"] != SCHEMA:
            _invalid()
        ta_kwargs = {}
        if document["tradingagents"] is not None:
            from .ta_team_deployment import DESCRIPTOR, NATIVE_ACCOUNTS_DESCRIPTOR
            if document["tradingagents"] != {"descriptor": DESCRIPTOR}:
                _invalid()
            ta_kwargs = {"tradingagents_team_descriptor":
                         NATIVE_ACCOUNTS_DESCRIPTOR if native_loopback_accounts else DESCRIPTOR}
        value = document["qlib"]
        if value is None:
            return ta_kwargs
        fields = {"profile", "source_digest", "worker_binding", "probe_completion", "source_record", "security_review", "financial_review", "fixture_catalog", "dump_review"}
        if type(value) is dict and "dump_review" not in value:
            value = {**value, "dump_review": None}
        if type(value) is not dict or set(value) != fields or value["profile"] != PROFILE:
            _invalid()
        sha = value["source_digest"]
        if type(sha) is not str or re.fullmatch("[0-9a-f]{64}", sha) is None or source_digest(root) != sha:
            raise PlatformError("RESEARCH_SERVER_SOURCE_MISMATCH")
        pins = {}
        records = {}
        for name in fields - {"profile", "source_digest"}:
            if name in {"security_review", "financial_review", "fixture_catalog", "dump_review"} and value[name] is None:
                pins[name] = None
                continue
            pins[name] = MetadataPin.parse(value[name])
            records[name] = read_metadata(root, pins[name])
            if type(records[name]) is not dict:
                _invalid()
        from finance_platform.research.qlib_workflow import WORKER_BINDING_SHA256, WORKER_LOCK_SHA256
        worker = records["worker_binding"]
        if (pins["worker_binding"].sha256 != WORKER_BINDING_SHA256
                or worker.get("schema") != "FA010_MEASURED_PROJECT_WORKER_BINDING_EVIDENCE_1"
                or worker.get("worker") != "qlib" or worker.get("worker_root") != str(root / "workers/qlib")
                or worker.get("site_root") != str(root / "workers/qlib/.venv/Lib/site-packages")
                or worker.get("python", {}).get("path") != str(root / "workers/qlib/.venv/Scripts/python.exe")
                or worker.get("lock", {}).get("sha256") != WORKER_LOCK_SHA256
                or worker.get("reports_venv_fallback_allowed") is not False
                or worker.get("native_workflow_activated") is not False):
            _invalid()
        completion = records["probe_completion"]
        if (completion.get("schema") != "FA013_QLIB_TYPED_PROBE_COMPLETION_ARGUMENTS_1"
                or completion.get("arguments", {}).get("worker_root") != str(root / "workers/qlib")
                or completion.get("binding_mapping", {}).get("worker_binding") != {
                    "path": str(root / pins["worker_binding"].path), "bytes": pins["worker_binding"].bytes,
                    "sha256": pins["worker_binding"].sha256}
                or records["source_record"].get("standard_python_source_digest") != sha):
            _invalid()
        material = QlibServerMaterial(root, PROFILE, sha, **pins)
        kwargs = {"qlib_server_material": material, "qlib_native_launcher_factory": None, "qlib_input_preparation": None}
        if material.fixture_catalog is not None:
            from finance_platform.data.qlib_input_bridge import ReviewedFixtureCatalog
            kwargs["qlib_reviewed_catalog"] = ReviewedFixtureCatalog(records["fixture_catalog"])
        return {**kwargs, **ta_kwargs}
    except PlatformError:
        raise
    except ImportError:
        raise PlatformError('RESEARCH_COMPONENT_UNAVAILABLE') from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, BindingError, AccessError):
        raise PlatformError("RESEARCH_SERVER_CONFIG_INVALID") from None
