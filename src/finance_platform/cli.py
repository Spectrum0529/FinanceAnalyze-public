"""Local bootstrap/serve/worker entry points; no automatic credential loading."""
import argparse
import json
import os
import re
from pathlib import Path
import signal
import threading

from .api import DEFAULT_ORIGINS, create_app
from .core.auth import AuthService, OPERATOR_RECOVERY_SECONDS
from .core.database import Database
from .core.errors import PlatformError
from .core.runtime import build_runtime
from .worker import Worker
from .core.server_composition import load_server_composition


def load_auth_mfa_key():
    encoded = os.environ.get("FINANCE_PLATFORM_AUTH_MFA_KEY")
    if encoded is None:
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{64}", encoded):
        raise PlatformError("MFA_UNAVAILABLE", 503)
    return bytes.fromhex(encoded)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="finance-platform")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path)
    parser.add_argument("--core-only", action="store_true", help="explicit core acceptance mode without feature modules")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="generate a one-time local browser setup token; token is intentionally printed only here")
    recovery = commands.add_parser("auth-recovery", help="prepare one-time local enabled-admin recovery proof")
    recovery.add_argument("--username", required=True)
    restore = commands.add_parser("invalidate-restored-auth", help="expire historical proofs in an offline restored copy")
    restore.add_argument("--confirm-offline-restored-copy", action="store_true", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--origin", action="append")
    serve.add_argument("--secure-cookies", action="store_true")
    serve.add_argument("--hosted", action="store_true")
    serve.add_argument("--trusted-proxy", choices=("127.0.0.1",))
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--hosted", action="store_true")
    commands.add_parser("health")
    tick = commands.add_parser("schedule-tick", help="one explicit scheduler tick; same persisted service as worker")
    tick.add_argument("--hosted", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "invalidate-restored-auth":
            from .operations.backup import require_restored_copy
            path = require_restored_copy(args.root, args.database)
            database = Database(path)
            database.migrate()
            print(json.dumps(AuthService(database).invalidate_restored_auth()))
            return 0
        if args.command in {"serve", "worker", "schedule-tick"} and args.database is not None:
            root = args.root.resolve()
            restores = (root / ".local/platform/restores").resolve()
            if (root / args.database).resolve().is_relative_to(restores):
                raise PlatformError("RESTORED_COPY_OFFLINE_ONLY", 403)
        composition = load_server_composition(args.root, core_only=args.core_only) if args.command in {"serve", "worker"} else {}
        if args.command == "serve":
            if args.host not in {"127.0.0.1", "localhost"} or args.hosted != bool(args.trusted_proxy):
                raise PlatformError("INVALID_INPUT", 422)
            if args.hosted and (args.host != "127.0.0.1" or not args.origin):
                raise PlatformError("INVALID_INPUT", 422)
            import uvicorn
            origins = tuple(args.origin) if args.origin else tuple(DEFAULT_ORIGINS) + (f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}")
            app = create_app(args.root, args.database, allowed_origins=origins, include_modules=not args.core_only,
                             secure_cookies=args.secure_cookies, hosted_accounts=args.hosted,
                             auth_mfa_key=load_auth_mfa_key(), **composition)
            # Access logs omit raw URLs/query strings; service logs use safe correlation only.
            uvicorn.run(app, host=args.host, port=args.port, access_log=False, proxy_headers=args.hosted,
                        forwarded_allow_ips=args.trusted_proxy or "")
            return 0
        runtime = build_runtime(args.root, args.database, include_modules=not args.core_only,
                                hosted_accounts=getattr(args, "hosted", False), **composition)
        if args.command == "init":
            token = runtime.auth.prepare_setup()
            print(json.dumps({"setup_token": token, "expires_in_seconds": 1800, "usage": "Use in local browser initialization; do not store in reports/Git/logs"}))
        elif args.command == "auth-recovery":
            proof = runtime.auth.prepare_operator_recovery(args.username)
            print(json.dumps({"recovery_code": proof, "expires_in_seconds": OPERATOR_RECOVERY_SECONDS,
                              "usage": "Enter this proof in the direct local browser recovery form."}))
        elif args.command == "health":
            print(json.dumps(runtime.database.health()))
        elif args.command == "schedule-tick":
            print(json.dumps({"occurrences": runtime.scheduler.tick()}))
        elif args.command == "backup":
            from .operations.backup import publish_database_snapshot
            print(json.dumps(publish_database_snapshot(runtime.database, args.destination)))
        elif args.command == "worker":
            runner = Worker(runtime)
            if args.once:
                print(json.dumps({"processed_job_id": runner.run_once()}))
            else:
                stop = threading.Event()
                signal.signal(signal.SIGINT, lambda *_: stop.set())
                if hasattr(signal, "SIGTERM"):
                    signal.signal(signal.SIGTERM, lambda *_: stop.set())
                runner.run(stop)
        return 0
    except PlatformError as error:
        print(json.dumps({"code": error.code, "message": error.message}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
