"""Immutable per-run local artifacts; failures retained without touching research ledger."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import traceback
import os

from finance_platform.core.contracts import payload
from finance_platform.strategies.contracts import canonical, digest
from .data import ResearchError


class ExperimentArchive:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
            raise ResearchError("EXPERIMENT_RUN_ID_INVALID")
        return self.root/run_id

    def begin(self, run_id, spec, *, sample=None, data=None, configuration=None, code_paths, dependency_bytes):
        path = self._path(run_id)
        path.mkdir()  # Run IDs are never reused, including failed/interrupted runs.
        code_root = Path(os.path.commonpath([str(Path(p).resolve().parent) for p in code_paths]))
        code_bytes = {Path(p).resolve().relative_to(code_root).as_posix(): Path(p).read_bytes() for p in code_paths}
        code = {name: hashlib.sha256(raw).hexdigest() for name, raw in code_bytes.items()}
        snapshot = payload(sample) if sample is not None else data
        if not isinstance(snapshot, dict) or snapshot.get("scope") not in {"SYNTHETIC", "DEVELOPMENT"}:
            raise ResearchError("EXPERIMENT_SCOPE_INVALID")
        declaration = {"run_id": run_id, "registered_at": datetime.now(timezone.utc).isoformat(),
                       "spec": payload(spec), "sample_hash": digest(snapshot), "code_hashes": code, "source_root": code_root.name,
                       "dependency_hash": hashlib.sha256(dependency_bytes).hexdigest(), "scope": snapshot["scope"],
                       "formal_research_approved": False}
        self._write(path/"declaration.json", declaration)
        self._write(path/"sample.json", snapshot)
        self._write(path/"data_snapshot.json", data if data is not None else snapshot)
        self._write(path/"configuration.json", configuration or {})
        with (path/"dependencies.lock").open("xb") as stream:
            stream.write(dependency_bytes)
        with (path/"uv.lock").open("xb") as stream:
            stream.write(dependency_bytes)
        source = path/"code"
        source.mkdir()
        for name, raw in code_bytes.items():
            file = source/name
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open("xb") as stream:
                stream.write(raw)
        return declaration

    def finish(self, run_id, result):
        path = self._path(run_id)
        if (path/"terminal.json").exists():
            raise ResearchError("EXPERIMENT_TERMINAL_IMMUTABLE")
        self._write(path/"result.json", result)
        self._write(path/"terminal.json", {"status": "SUCCEEDED", "result_hash": digest(result), "at": datetime.now(timezone.utc).isoformat()})

    def fail(self, run_id, code, *, interrupted=False):
        path = self._path(run_id)
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            code = "RESEARCH_FAILED"
        self._write(path/"terminal.json", {"status": "INTERRUPTED" if interrupted else "FAILED", "code": code,
                                            "at": datetime.now(timezone.utc).isoformat()})

    def read(self, run_id):
        path = self._path(run_id)
        declaration = json.loads((path/"declaration.json").read_bytes())
        terminal = json.loads((path/"terminal.json").read_bytes()) if (path/"terminal.json").exists() else {"status": "INTERRUPTED_OR_RUNNING"}
        result = json.loads((path/"result.json").read_bytes()) if (path/"result.json").exists() else None
        if result is not None and terminal.get("status") == "SUCCEEDED" and terminal.get("result_hash") != digest(result):
            raise ResearchError("EXPERIMENT_RESULT_HASH_MISMATCH")
        return {"declaration": declaration, "terminal": terminal, "result": result}

    @staticmethod
    def _write(path, value):
        with Path(path).open("xb") as stream:
            stream.write(canonical(value))


def parameter_search(sample, space, *, budget, evaluate, before_attempt=None, record_attempt=None):
    """Finite preregistered dev-only attempts; invalid parameters remain failed attempts."""
    if sample.scope.value not in {"SYNTHETIC", "DEVELOPMENT"} or not isinstance(space, list) or not space or type(budget) is not int or not 1 <= budget <= 16 or len(space) > budget:
        raise ResearchError("SEARCH_SCOPE_OR_BUDGET_INVALID")
    declaration = {"space": space, "budget": budget, "sample_hash": sample.sha256, "scope": sample.scope.value,
                   "purpose": "DEVELOPMENT_ONLY", "failed_attempts_retained": True}
    canonical(declaration)
    attempts = []
    for i, params in enumerate(space):
        if before_attempt:
            before_attempt()
        try:
            result = evaluate(params, i)
            attempts.append({"attempt": i+1, "parameters": params, "status": "SUCCEEDED", "result": result})
        except Exception:
            attempts.append({"attempt": i+1, "parameters": params, "status": "FAILED", "code": "PARAMETER_ATTEMPT_FAILED"})
        if record_attempt:
            record_attempt(attempts[-1])
    return {"declaration": declaration, "attempt_count": len(attempts), "failure_count": sum(a["status"] == "FAILED" for a in attempts),
            "attempts": attempts, "decision": "DEVELOPMENT_ONLY_NO_RELIABLE_ALPHA_CLAIM"}
