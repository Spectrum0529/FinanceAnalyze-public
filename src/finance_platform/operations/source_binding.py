"""Fail-closed source-install identity, independent of an interpreter's editable install."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys

from finance_platform.core.errors import PlatformError
from finance_platform.core.source_binding import checked_regular_file, file_digest, source_digest, BindingError


def inspect_source(root):
    root = Path(root).resolve()
    source = root / "src"
    if not source.is_dir() or source.resolve() != source:
        raise PlatformError("SOURCE_INSTALL_REQUIRED", 409)
    try:
        lock = checked_regular_file(root, "uv.lock")
        for name in ("finance_platform", "predictor", "finance_platform.core.runtime", "finance_platform.worker"):
            importlib.import_module(name)
        modules = {}
        for name, module in tuple(sys.modules.items()):
            if name in {"finance_platform", "predictor"} or name.startswith(("finance_platform.", "predictor.")):
                location = getattr(module, "__file__", None)
                if location is None or not Path(location).resolve().is_relative_to(source):
                    raise PlatformError("SOURCE_BINDING_MISMATCH", 409)
                modules[name] = str(Path(location).resolve())
        return {"installation": "SOURCE", "root": str(root), "source_root": str(source),
                "root_hash": hashlib.sha256(str(root).encode("utf-8")).hexdigest(),
                "source_hash": source_digest(root), "lock_sha256": file_digest(lock), "loaded_modules": modules}
    except BindingError as error:
        raise PlatformError(error.code, 409) from None


def public_summary(binding):
    return {key: binding[key] for key in ("installation", "root_hash", "source_hash", "lock_sha256")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_source(args.root)))
        return 0
    except (PlatformError, OSError) as error:
        print(json.dumps({"code": error.code if isinstance(error, PlatformError) else "SOURCE_BINDING_FAILED"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
