"""Authenticated search submission/status CLI using the common durable service."""
import argparse
import json
import os
from pathlib import Path
from predictor.access import AccessPolicy
from finance_platform.core.runtime import build_runtime
from .service import ResearchService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="action", required=True)
    search = sub.add_parser("search")
    search.add_argument("--request", type=Path, required=True)
    status = sub.add_parser("search-status")
    status.add_argument("job_id")
    args = parser.parse_args()
    runtime = build_runtime(args.root)
    principal = runtime.auth.session(os.environ.get("FINANCE_PLATFORM_SESSION_TOKEN", ""))[0]
    service = ResearchService(runtime)
    if args.action == "search":
        path = AccessPolicy(args.root).authorize(args.request, operation="config_read")
        request = json.loads(path.read_bytes())
        result = service.submit_search(principal, **request)
    else:
        result = service.search_status(principal, args.job_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
