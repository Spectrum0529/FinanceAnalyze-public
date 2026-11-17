"""Pure deployment-only QA identity, byte-shared by Windows and native hosts."""

from dataclasses import dataclass
import hashlib
import json
import re


QA_BATCH = "full_validation_20261109_r01"
QA_LIFETIME_SECONDS = 2550
SCOPE_FILE = "scope.json"
SCOPE_OWNER = "FA010_R03"
ENTRYPOINT = "fa_research_executor.entrypoint"
DAEMON_POLICY = "EXCLUSIVE_WINDOW_PINNED_ROOTLESS"
COLD_DAEMON_POLICY = "EXCLUSIVE_ATTEMPT_COLD_ROOTLESS"
def linux_qa_base(executor_root):
    return executor_root + '/runtime/qa/' + QA_BATCH
HOST_VENV = "host-venv"


def socket_path(run_id):
    return "/run/user/1000/faq-" + run_id + "/docker.sock"


def cold_unit_name(run_id):
    require(valid_run(run_id), "QA_SCOPE_ARGUMENT_INVALID")
    return "financeanalyze-research-qa-cold-" + run_id + ".service"


class ScopeRejected(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ScopeRejected(code)


def valid_run(value):
    return type(value) is str and re.fullmatch("[a-f0-9]{32}", value) is not None


def valid_sha(value):
    return type(value) is str and re.fullmatch("[a-f0-9]{64}", value) is not None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "QA_SCOPE_INVALID")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ScopeRejected("QA_SCOPE_INVALID")


@dataclass(frozen=True)
class ScopeBase:
    run_id: str
    sha256: str
    main_source_sha256: str
    runtime_sha256: str
    config_sha256: str
    image_digest: str
    issued_at: int
    expires_at: int
    staging_device: int
    staging_inode: int


@dataclass(frozen=True)
class ScopeIdentity(ScopeBase):
    socket_device: int
    socket_inode: int


@dataclass(frozen=True)
class ColdScopeIdentity(ScopeBase):
    unit_sha256: str


def parse_scope(raw, run_id, sha256, *, now):
    require(valid_run(run_id) and valid_sha(sha256), "QA_SCOPE_ARGUMENT_INVALID")
    require(type(raw) is bytes and 0 < len(raw) <= 8192, "QA_SCOPE_INVALID")
    require(hashlib.sha256(raw).hexdigest() == sha256, "QA_SCOPE_DIGEST_MISMATCH")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ScopeRejected("QA_SCOPE_INVALID") from error
    require(type(value) is dict, "QA_SCOPE_INVALID")
    cold = value.get("schema") == "fa-native-qa-cold-scope-1"
    identity_type = ColdScopeIdentity if cold else ScopeIdentity
    fields = set(identity_type.__dataclass_fields__) - {"sha256"}
    require(type(value) is dict and set(value) == fields | {"schema", "owner", "entrypoint", "daemon_policy"},
            "QA_SCOPE_INVALID")
    expected_schema = "fa-native-qa-cold-scope-1" if cold else "fa-native-qa-scope-1"
    expected_policy = COLD_DAEMON_POLICY if cold else DAEMON_POLICY
    require(value.pop("schema") == expected_schema and value.pop("owner") == SCOPE_OWNER
            and value.pop("entrypoint") == ENTRYPOINT and value.pop("daemon_policy") == expected_policy
            and value["run_id"] == run_id,
            "QA_SCOPE_BINDING_MISMATCH")
    require(all(valid_sha(value[key]) for key in ("main_source_sha256", "runtime_sha256", "config_sha256")),
            "QA_SCOPE_INVALID")
    image = value["image_digest"]
    require(type(image) is str and re.fullmatch("sha256:[a-f0-9]{64}", image) is not None, "QA_SCOPE_INVALID")
    issued, expires = value["issued_at"], value["expires_at"]
    require(type(issued) is int and type(expires) is int and 0 < expires - issued <= QA_LIFETIME_SECONDS,
            "QA_SCOPE_INVALID")
    require(issued <= now < expires, "QA_SCOPE_EXPIRED")
    if cold:
        require(valid_sha(value["unit_sha256"]), "QA_SCOPE_INVALID")
    for prefix in (("staging",) if cold else ("staging", "socket")):
        require(type(value[prefix+"_device"]) is int and value[prefix+"_device"] >= 0
                and type(value[prefix+"_inode"]) is int and value[prefix+"_inode"] > 0, "QA_SCOPE_INVALID")
    return identity_type(sha256=sha256, **value)
