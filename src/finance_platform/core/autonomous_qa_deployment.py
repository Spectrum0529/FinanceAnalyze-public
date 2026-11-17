"""Private launcher binding for the frozen FA010 validation window."""

from dataclasses import dataclass
from pathlib import Path
import time

from .autonomous_qa_scope_contract import QA_BATCH, ScopeRejected, parse_scope, valid_run
from .errors import PlatformError
from .source_binding import checked_regular_file


QA_PARENT = (Path(__file__).resolve().parents[3] / '.local' / 'qa' / QA_BATCH / 'runs')
QA_CONFIG = "configs/autonomous_qa_scope.json"


def require(condition, code):
    if not condition:
        raise PlatformError(code, 409)


def qa_root(root):
    root = Path(root)
    name = root.parent.name
    run_id = name.removeprefix("combined-")
    require(name == "combined-" + run_id and valid_run(run_id)
            and root == QA_PARENT / name / "app", "QA_WINDOWS_ROOT_INVALID")
    try:
        canonical = root.resolve(strict=True)
    except OSError as error:
        raise PlatformError("QA_WINDOWS_ROOT_INVALID", 409) from error
    require(canonical == root, "QA_WINDOWS_ROOT_INVALID")
    return root, run_id


def read_scope(root, pin):
    root, run_id = qa_root(root)
    try:
        path = checked_regular_file(root, QA_CONFIG)
        require(path.stat().st_size <= 8192, "QA_SCOPE_INVALID")
        return parse_scope(path.read_bytes(), run_id, pin, now=time.time())
    except ScopeRejected as error:
        raise PlatformError(str(error), 409) from error
    except (OSError, ValueError) as error:
        raise PlatformError("QA_SCOPE_UNAVAILABLE", 409) from error


@dataclass(frozen=True)
class QABridgeDeployment:
    root: Path
    scope_sha256: str
    runtime_sha256: str
    main_source_sha256: str
    image_digest: str
    paths_sha256: str

    def verified_scope(self):
        scope = read_scope(self.root, self.scope_sha256)
        require(scope.runtime_sha256 == self.runtime_sha256
                and scope.main_source_sha256 == self.main_source_sha256
                and scope.image_digest == self.image_digest, "QA_SCOPE_RUNTIME_MISMATCH")
        return scope

    def remaining_seconds(self):
        return self.verified_scope().expires_at - time.time()
