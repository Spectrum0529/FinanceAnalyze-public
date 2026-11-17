"""Trusted subprocess bootstrap. Invoked with -I -S; no platform services imported."""

import importlib.util
import json
from pathlib import Path
import sys


def main():
    try:
        package = Path(sys.argv[1])
        module, function = sys.argv[2].split(":")
        request = json.loads(sys.stdin.buffer.read(256_001))
        spec = importlib.util.spec_from_file_location("trusted_plugin", package / (module + ".py"))
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        result = getattr(plugin, function)(request)
        raw = json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > 256_000:
            raise ValueError()
        sys.stdout.buffer.write(raw)
    except BaseException:
        # Never echo code, paths, exception messages or plugin supplied input.
        sys.stderr.write("PLUGIN_EXECUTION_FAILED")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
