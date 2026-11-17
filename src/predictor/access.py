"""Development data access gate and transactional value-free audit events."""

import hashlib
import json
from pathlib import Path
import sqlite3
from predictor.research.append_guards import install as install_identity_guards
from contextlib import closing

from predictor.logging import utc_timestamp
from predictor.secrets import reject_credentials


PARTITIONS = {"public_samples", "authorized_raw", "development", "candidate_holdout", "prospective", "credentials"}
DEVELOPMENT_ALLOWED = {"public_samples", "development"}
DEFAULT_PARTITIONS = {
    "public_samples": "samples/public", "authorized_raw": ".local/data/authorized_raw",
    "development": "data/development", "candidate_holdout": ".local/data/candidate_holdout",
    "prospective": ".local/prospective", "credentials": ".local/secrets",
}


class AccessError(ValueError):
    pass


class AccessPolicy:
    def __init__(self, root: Path, policy_path: Path | None = None):
        self.root = root.resolve()
        self.audit = self.root / ".local/audit/access.sqlite3"
        path = self.root / policy_path if policy_path is not None else self.root / "configs/access_policy.example.json"
        if path.exists():
            resolved_policy = path.resolve()
            if not resolved_policy.is_relative_to((self.root / "configs").resolve()) or any(
                    resolved_policy.is_relative_to((self.root / relative).resolve())
                    for name, relative in DEFAULT_PARTITIONS.items() if name not in DEVELOPMENT_ALLOWED):
                raise AccessError("Policy files must be in the repository configs directory")
            raw = path.read_bytes()
            policy = json.loads(raw)
        elif policy_path is not None:
            raise AccessError("Requested policy file is missing")
        else:
            policy = {"schema_version": 1, "partitions": DEFAULT_PARTITIONS,
                      "development_allowed": sorted(DEVELOPMENT_ALLOWED)}
            raw = json.dumps(policy, sort_keys=True).encode("utf-8")
        reject_credentials(policy)
        if not isinstance(policy, dict) or policy.get("schema_version") != 1:
            raise AccessError("Unsupported access policy")
        partitions = policy.get("partitions")
        if not isinstance(partitions, dict) or set(partitions) != PARTITIONS:
            raise AccessError("Access policy must define all six data partitions")
        self.partitions = {}
        for name, relative in partitions.items():
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                raise AccessError("Partition paths must be repository-relative")
            resolved = (self.root / relative).resolve()
            if not resolved.is_relative_to(self.root) or resolved == self.root:
                raise AccessError("Partition root escapes the repository")
            self.partitions[name] = resolved
        for name, path in self.partitions.items():
            for other, other_path in self.partitions.items():
                if name != other and (path.is_relative_to(other_path) or other_path.is_relative_to(path)):
                    raise AccessError("Partition roots overlap or alias each other")
        allowed = policy.get("development_allowed")
        if not isinstance(allowed, list) or not allowed or any(name not in DEVELOPMENT_ALLOWED for name in allowed):
            raise AccessError("Development may mount only public samples and development data")
        self.allowed = set(allowed)
        self.policy_hash = hashlib.sha256(raw).hexdigest()

    def log(self, *, request_hash: str, partition: str, operation: str, outcome: str, reason: str):
        # Logging must commit before a read is permitted; failure is fail-closed.
        destination = self.audit.resolve()
        if not destination.is_relative_to(self.root) or any(destination.is_relative_to(p) for p in self.partitions.values()):
            raise AccessError("Audit destination escapes the repository")
        self.audit.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.audit, timeout=30)) as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("""CREATE TABLE IF NOT EXISTS access_events (
                event_id INTEGER PRIMARY KEY, recorded_at_utc TEXT NOT NULL,
                request_hash TEXT NOT NULL, partition TEXT NOT NULL, operation TEXT NOT NULL,
                outcome TEXT NOT NULL, reason TEXT NOT NULL, policy_hash TEXT NOT NULL)""")
            for action in ("UPDATE", "DELETE"):
                connection.execute(f"""CREATE TRIGGER IF NOT EXISTS access_immutable_{action}
                    BEFORE {action} ON access_events BEGIN
                    SELECT RAISE(ABORT, 'Append-only access audit'); END""")
            install_identity_guards(connection, ('access_events',))
            connection.execute("INSERT INTO access_events(recorded_at_utc,request_hash,partition,operation,outcome,reason,policy_hash) VALUES (?,?,?,?,?,?,?)",
                               (utc_timestamp(), request_hash, partition, operation, outcome, reason, self.policy_hash))

    def authorize(self, requested: str | Path, *, operation: str = "read") -> Path:
        if operation not in {"read", "configuration", "config_read"}:
            raise AccessError("Unsupported access operation")
        path = (self.root / requested).resolve()
        request_hash = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        partition = next((name for name, directory in self.partitions.items() if path.is_relative_to(directory)), "unclassified")
        allowed = partition in self.allowed or (operation == "config_read" and partition == "unclassified" and path.is_relative_to(self.root))
        self.log(request_hash=request_hash, partition=partition, operation=operation,
                 outcome="allowed" if allowed else "denied",
                 reason="development_partition" if allowed else "partition_not_mounted")
        if not allowed:
            raise AccessError(f"Development access denied: {partition}")
        return path

    def read_bytes(self, requested: str | Path) -> bytes:
        path = self.authorize(requested)
        try:
            data = path.read_bytes()
        except OSError:
            request_hash = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
            self.log(request_hash=request_hash, partition="development_read", operation="read",
                     outcome="failed", reason="read_failed")
            raise AccessError("Authorized data read failed") from None
        return data

    def check_inputs(self, requested: tuple[str, ...]):
        for path in requested:
            self.authorize(path, operation="configuration")
