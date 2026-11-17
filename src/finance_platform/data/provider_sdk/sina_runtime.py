"""Existing pure native-runtime pin verifier, detached from the old engine."""
import hashlib
from importlib import metadata
import json
from pathlib import Path

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def verify_runtime(pins, budget):
    """No mini-racer/AKShare import before every Python/DLL/ICU pin is checked."""
    budget.check()
    pin = pins["sina"]["runtime"]
    try:
        dist = metadata.distribution("mini-racer")
    except metadata.PackageNotFoundError:
        budget.abort("SDK_DEPENDENCY_MISSING")
    if dist.version != pin["version"] or pin["name"] != "mini-racer":
        budget.abort("SDK_VERSION_UNREVIEWED")
    # Reject extra executable/module files as well as missing or changed files.
    installed = {str(f).replace("\\", "/") for f in dist.files or ()
                 if str(f).replace("\\", "/").startswith("py_mini_racer/")
                 and not str(f).endswith(".pyc") and "__pycache__" not in str(f)}
    if installed != set(pin["modules"]):
        budget.abort("SDK_INTERFACE_CHANGED")
    for relative, digest in pin["modules"].items():
        budget.check()
        path = Path(dist.locate_file(relative))
        raw = path.read_bytes()
        if sha(raw.replace(b"\r\n", b"\n") if path.suffix == ".py" else raw) != digest:
            budget.abort("SDK_INTERFACE_CHANGED")
    budget.check()
    return {**{k: pin[k] for k in ("name", "version", "artifact_sha256")},
            "modules_manifest_sha256": sha(json.dumps(pin["modules"], sort_keys=True, separators=(",", ":")).encode()),
            "native_dll_sha256": pin["modules"]["py_mini_racer/mini_racer.dll"],
            "icu_sha256": pin["modules"]["py_mini_racer/icudtl.dat"]}

