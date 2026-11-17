"""Consume explicit, byte-bound local acceptance metadata without changing gates."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re

from .contracts import digest, identifier, parse_instant
from .security import clean, now_text
from .errors import PlatformError
from .source_binding import BindingError, SRC_DIGEST_ALGORITHM, checked_regular_file, file_digest, source_digest

DECLARATION_PATH = "docs/platform/runtime_acceptance.json"
MATRIX_PATH = "docs/platform/capability_matrix.json"
MODULE_KEYS = {"data", "strategies", "research", "trading", "operations"}
IMPLEMENTATIONS = {"NOT_STARTED", "IN_PROGRESS", "IMPLEMENTED"}
VERIFICATIONS = {"NOT_RUN", "FAILED", "LOCAL_VERIFIED", "PROVIDER_VERIFIED", "TARGET_ENV_VERIFIED"}
DEPENDENCIES = {"NONE", "WAITING_SECRET", "WAITING_ACCOUNT", "WAITING_DATA_RIGHTS", "WAITING_HOST", "WAITING_USER_DECISION", "WAITING_OBSERVATION"}
MAX_DECLARATION_BYTES = 65536
MAX_REPORT_BYTES = 8*1024*1024
MAX_REPORT_BUDGET = 32*1024*1024


class AcceptanceError(ValueError):
    def __init__(self, code="INVALID_DECLARATION"):
        self.code=code
        super().__init__(code)


def normalize_architecture(value):
    if type(value) is not str:
        raise AcceptanceError()
    aliases={"amd64":"x86_64","x86_64":"x86_64","x64":"x86_64",
             "arm64":"aarch64","aarch64":"aarch64","x86":"x86","i386":"x86","i686":"x86"}
    normalized=aliases.get(value.casefold())
    if normalized is None: raise AcceptanceError()
    return normalized


def normalize_environment(value):
    if type(value) is not dict or set(value)!={"os","python","architecture","target_id"}:
        raise AcceptanceError()
    if value["os"] not in {"Windows","Linux","Darwin"} or type(value["python"]) is not str or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+",value["python"]):
        raise AcceptanceError()
    result=dict(value)
    result["architecture"]=normalize_architecture(value["architecture"])
    if value["target_id"] is not None: identifier(value["target_id"])
    return result


def runtime_environment(*, target_id=None):
    if target_id is None: target_id=os.environ.get("FINANCE_PLATFORM_TARGET_ID") or None
    return normalize_environment({"os":platform.system(),"python":platform.python_version(),"architecture":platform.machine(),"target_id":target_id})


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise AcceptanceError()
        result[key]=value
    return result


def _invalid_constant(value):
    raise AcceptanceError()


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8-sig"),object_pairs_hook=_unique,parse_constant=_invalid_constant)
    except (ValueError,UnicodeError,RecursionError):
        raise AcceptanceError() from None


def validate_declaration(value):
    """Pure producer validation; returns a defensive normalized copy."""
    try:
        if type(value) is not dict or set(value)!={"schema_version","declaration_id","recorded_at_utc","bindings","modules"}:
            raise AcceptanceError()
        if type(value["schema_version"]) is not int or value["schema_version"]!=1: raise AcceptanceError()
        identifier(value["declaration_id"])
        parse_instant(value["recorded_at_utc"])
        if datetime.fromisoformat(value["recorded_at_utc"]).utcoffset().total_seconds()!=0: raise AcceptanceError()
        binding=value["bindings"]
        if type(binding) is not dict or set(binding)!={"src_algorithm","src_sha256","uv_lock_sha256","capability_matrix_sha256"} or binding["src_algorithm"]!=SRC_DIGEST_ALGORITHM:
            raise AcceptanceError()
        for key in ("src_sha256","uv_lock_sha256","capability_matrix_sha256"): digest(binding[key])
        modules=value["modules"]
        if type(modules) is not dict or set(modules)-MODULE_KEYS: raise AcceptanceError()
        result=deepcopy(value)
        for name,entry in modules.items():
            if type(entry) is not dict or set(entry)!={"component_ids","implementation","verification","external_dependency","verified_scope","verification_environment","evidence"}:
                raise AcceptanceError()
            if entry["implementation"] not in IMPLEMENTATIONS or entry["verification"] not in VERIFICATIONS or entry["external_dependency"] not in DEPENDENCIES:
                raise AcceptanceError()
            if type(entry["verified_scope"]) is not str or not entry["verified_scope"].strip() or len(entry["verified_scope"])>1024: raise AcceptanceError()
            ids=entry["component_ids"]
            if type(ids) is not list or not 1<=len(ids)<=32 or len(set(ids))!=len(ids): raise AcceptanceError()
            for item in ids: identifier(item)
            refs=entry["evidence"]
            if type(refs) is not list or not 1<=len(refs)<=32: raise AcceptanceError()
            for ref in refs:
                if type(ref) is not dict or set(ref)!={"path","sha256"}: raise AcceptanceError()
                if type(ref["path"]) is not str or not 1<=len(ref["path"])<=512: raise AcceptanceError()
                digest(ref["sha256"])
            result["modules"][name]["verification_environment"]=normalize_environment(entry["verification_environment"])
        clean(result)
        if len(json.dumps(result,ensure_ascii=False).encode())>MAX_DECLARATION_BYTES: raise AcceptanceError()
        return result
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,PlatformError):
        raise AcceptanceError() from None


def declaration_schema():
    """Portable producer shape; validate_declaration adds semantic/byte checks."""
    def obj(properties):
        return {"type":"object","additionalProperties":False,"required":list(properties),"properties":properties}
    ident={"type":"string","pattern":r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"}
    sha={"type":"string","pattern":r"^[0-9a-f]{64}$"}
    environment=obj({"os":{"enum":["Windows","Linux","Darwin"]},
        "python":{"type":"string","pattern":r"^[0-9]+\.[0-9]+\.[0-9]+$"},
        "architecture":{"type":"string","enum":["x86_64","aarch64","x86"],"description":"Canonical producer output. Validator also normalizes case-insensitive AMD64/x64, arm64, i386/i686 aliases."},
        "target_id":{"anyOf":[ident,{"type":"null"}]}})
    module=obj({"component_ids":{"type":"array","minItems":1,"maxItems":32,"uniqueItems":True,"items":ident},
        "implementation":{"enum":sorted(IMPLEMENTATIONS)},"verification":{"enum":sorted(VERIFICATIONS)},
        "external_dependency":{"enum":sorted(DEPENDENCIES)},"verified_scope":{"type":"string","minLength":1,"maxLength":1024},
        "verification_environment":environment,
        "evidence":{"type":"array","minItems":1,"maxItems":32,"items":obj({"path":{"type":"string","minLength":1,"maxLength":512},"sha256":sha})}})
    schema=obj({"schema_version":{"type":"integer","const":1},"declaration_id":ident,
        "recorded_at_utc":{"type":"string","maxLength":40,"format":"date-time","description":"Explicit UTC offset required; producer validator checks offset zero."},
        "bindings":obj({"src_algorithm":{"const":SRC_DIGEST_ALGORITHM},"src_sha256":sha,"uv_lock_sha256":sha,"capability_matrix_sha256":sha}),
        "modules":{"type":"object","additionalProperties":False,"properties":{name:deepcopy(module) for name in sorted(MODULE_KEYS)}}})
    schema["$schema"]="https://json-schema.org/draft/2020-12/schema"
    schema["description"]="Operator-reviewed metadata only. Also call build_declaration/validate_declaration; total size, unique JSON keys, safe evidence paths, byte bindings, component membership and environment semantics are runtime checks."
    return schema


def acceptance_bindings(root: Path):
    """FA-010 producer API: actual current source/lock/matrix byte bindings."""
    root=Path(root).resolve()
    return {"src_algorithm":SRC_DIGEST_ALGORITHM,"src_sha256":source_digest(root),
            "uv_lock_sha256":file_digest(checked_regular_file(root,"uv.lock")),
            "capability_matrix_sha256":file_digest(checked_regular_file(root,MATRIX_PATH))}


def _matrix_components(root):
    path=checked_regular_file(root,MATRIX_PATH)
    if path.stat().st_size>2*1024*1024: raise AcceptanceError("MATRIX_MISMATCH")
    matrix=_json(path.read_bytes())
    if type(matrix) is not dict or type(matrix.get("components")) is not list:
        raise AcceptanceError("MATRIX_MISMATCH")
    return {item["id"] for item in matrix["components"] if type(item) is dict and type(item.get("id")) is str}


def _evidence_path(root, relative):
    if type(relative) is not str or not relative.startswith("reports/platform_acceptance/"):
        raise AcceptanceError("EVIDENCE_PATH_REJECTED")
    parts=relative.split("/")
    forbidden={".local",".git","secrets","credentials","authorized_raw","candidate_holdout","prospective","scratch","test_runtime"}
    if any(p.startswith(".") or p.casefold() in forbidden or any(word in p.casefold() for word in ("private","backup","audit")) for p in parts):
        raise AcceptanceError("EVIDENCE_PATH_REJECTED")
    if Path(relative).suffix.casefold() not in {".json",".md",".txt",".log"}:
        raise AcceptanceError("EVIDENCE_PATH_REJECTED")
    try:
        return checked_regular_file(root,relative)
    except BindingError:
        raise AcceptanceError("EVIDENCE_PATH_REJECTED") from None


def _evidence_digest(root, ref, cache, budget):
    path=_evidence_path(root,ref["path"])
    key=ref["path"]
    if key not in cache:
        size=path.stat().st_size
        if size>MAX_REPORT_BYTES or budget[0]+size>MAX_REPORT_BUDGET:
            raise AcceptanceError("EVIDENCE_PATH_REJECTED")
        budget[0]+=size
        with path.open("rb") as stream:
            raw=stream.read(MAX_REPORT_BYTES+1)
        if len(raw)>MAX_REPORT_BYTES or b"\0" in raw or raw.startswith(b"SQLite format 3"):
            raise AcceptanceError("EVIDENCE_PATH_REJECTED")
        try: raw.decode("utf-8-sig")
        except UnicodeError: raise AcceptanceError("EVIDENCE_PATH_REJECTED") from None
        cache[key]=hashlib.sha256(raw).hexdigest()
    if cache[key]!=ref["sha256"]: raise AcceptanceError("EVIDENCE_MISMATCH")


def build_declaration(root, declaration_id, modules, *, recorded_at_utc=None):
    """Caller supplies reviewed states/scope/environments; never inferred from files."""
    result=validate_declaration({"schema_version":1,"declaration_id":declaration_id,"recorded_at_utc":recorded_at_utc or now_text(),
        "bindings":acceptance_bindings(root),"modules":modules})
    cache,budget={},[0]
    component_ids=_matrix_components(Path(root).resolve())
    for entry in result["modules"].values():
        if not set(entry["component_ids"])<=component_ids: raise AcceptanceError("MATRIX_COMPONENT_UNKNOWN")
        for ref in entry["evidence"]: _evidence_digest(Path(root).resolve(),ref,cache,budget)
    return result


class AcceptanceDeclarations:
    def __init__(self, root, *, environment=None):
        self.root=Path(root).resolve()
        # environment injection is for trusted construction/tests only, never HTTP.
        try: self.environment=normalize_environment(environment) if environment is not None else runtime_environment()
        except (ValueError,TypeError): self.environment=None

    @staticmethod
    def _fallback(observed, reason):
        facts={key:bool(observed.get(key,False)) for key in ("router","handlers","initialized")}
        return {**facts,"implementation":"IN_PROGRESS" if any(facts.values()) else "NOT_STARTED", "verification":"NOT_RUN",
                "external_dependency":None,"acceptance_status":"UNVERIFIED","acceptance_reason":reason}

    def _load(self):
        if not (self.root/DECLARATION_PATH).exists() and not (self.root/DECLARATION_PATH).is_symlink():
            raise AcceptanceError("NO_DECLARATION")
        try:
            path=checked_regular_file(self.root,DECLARATION_PATH)
            if path.stat().st_size>MAX_DECLARATION_BYTES: raise AcceptanceError()
            with path.open("rb") as stream: raw=stream.read(MAX_DECLARATION_BYTES+1)
            if len(raw)>MAX_DECLARATION_BYTES: raise AcceptanceError()
            declaration=validate_declaration(_json(raw))
            current=acceptance_bindings(self.root)
            for key,reason in (("src_sha256","SOURCE_MISMATCH"),("uv_lock_sha256","LOCK_MISMATCH"),("capability_matrix_sha256","MATRIX_MISMATCH")):
                if current[key]!=declaration["bindings"][key]: raise AcceptanceError(reason)
            return declaration,_matrix_components(self.root)
        except AcceptanceError: raise
        except (BindingError,OSError,KeyError,TypeError,ValueError,RecursionError):
            raise AcceptanceError("BINDING_UNAVAILABLE") from None

    def project(self, observed_modules):
        observed={name:entry for name,entry in observed_modules.items() if name in MODULE_KEYS}
        try: declaration,component_ids=self._load()
        except AcceptanceError as error:
            return {name:self._fallback(entry,error.code) for name,entry in observed.items()}
        cache,budget={},[0]
        result={}
        for name,entry in observed.items():
            result[name]=self._fallback(entry,"NO_MODULE_DECLARATION")
            claim=declaration["modules"].get(name)
            if not claim: continue
            if not entry.get("router"):
                result[name]=self._fallback(entry,"MODULE_UNAVAILABLE")
                continue
            try:
                if not set(claim["component_ids"])<=component_ids: raise AcceptanceError("MATRIX_COMPONENT_UNKNOWN")
                for ref in claim["evidence"]: _evidence_digest(self.root,ref,cache,budget)
                projection={**result[name],"implementation":claim["implementation"],"verification":claim["verification"],
                    "external_dependency":claim["external_dependency"],"verified_scope":claim["verified_scope"],
                    "verification_environment":claim["verification_environment"],"evidence":deepcopy(claim["evidence"]),
                    "declaration_id":declaration["declaration_id"],"component_ids":list(claim["component_ids"]),
                    "acceptance_status":"MATCHED","acceptance_reason":None}
                env=claim["verification_environment"]
                if self.environment is None or any(env[key]!=self.environment[key] for key in ("os","python","architecture")):
                    projection.update(verification="NOT_RUN",declared_verification=claim["verification"],acceptance_status="UNVERIFIED",acceptance_reason="ENVIRONMENT_MISMATCH")
                elif claim["verification"]=="TARGET_ENV_VERIFIED" and (not env["target_id"] or env["target_id"]!=self.environment["target_id"]):
                    projection.update(verification="NOT_RUN",declared_verification=claim["verification"],acceptance_status="UNVERIFIED",acceptance_reason="TARGET_UNBOUND")
                result[name]=projection
            except (AcceptanceError,BindingError,OSError,ValueError) as error:
                result[name]=self._fallback(entry,getattr(error,"code","EVIDENCE_UNAVAILABLE"))
        return result
