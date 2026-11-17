"""Windows MCP entry: attach to the existing local API, no service spawning.

Stdout belongs exclusively to the official SDK. Root and module are fixed by
the installed script location. Client supplies only the selected API port.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--client', default='codex')
    parser.add_argument('--access-mode', choices=('accounts', 'local-personal'), default='accounts')
    parser.add_argument('--pair', action='store_true')
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error('API port must be between 1024 and 65535')
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / 'src'))
    from finance_platform.core.errors import PlatformError
    from finance_platform.operations.source_binding import inspect_source
    try:
        binding = inspect_source(root)
    except (OSError, ValueError):
        print('MCP_CORE_NOT_INSTALLED', file=sys.stderr)
        return 2
    if importlib.util.find_spec('finance_platform.core.mcp_server') is None:
        raise SystemExit('MCP_CORE_NOT_INSTALLED')
    # Process-local native resolution and payload telemetry opt-out only.
    os.environ['PATHEXT'] = '.EXE;.COM'
    os.environ['OTEL_SDK_DISABLED'] = 'true'
    os.environ['LANGSMITH_TRACING'] = 'false'
    os.environ['LANGCHAIN_TRACING_V2'] = 'false'
    from finance_platform.core.mcp_server import main as serve
    forwarded = ['--api-url', f'http://127.0.0.1:{args.port}', '--client', args.client,
                 '--access-mode', args.access_mode]
    return serve(forwarded + (['--pair'] if args.pair else []), root=root, binding=binding)


if __name__ == '__main__':
    raise SystemExit(main())
