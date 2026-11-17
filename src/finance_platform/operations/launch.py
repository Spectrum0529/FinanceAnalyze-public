"""Thin native/container entry point with a kernel-held single-worker lock."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from finance_platform.core.errors import PlatformError
from finance_platform.core.source_binding import BindingError, checked_regular_file
from .paths import contained, protect_private


def _record_binding(root, role, binding):
    directory = contained(root, Path(root) / ".local/platform/processes")
    directory.mkdir(parents=True, exist_ok=True)
    protect_private(directory)
    target = directory / (role + ".binding.json")
    temporary = directory / (role + ".binding." + str(os.getpid()) + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump({"process_id": os.getpid(), "recorded_at": time.time(), "role": role, **binding}, stream, sort_keys=True)
    temporary.replace(target)


@contextmanager
def _process_lock(path, *, busy_code="CONFLICT"):
    """OS-released lock on a local filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PlatformError(busy_code, 409) from None
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def single_worker(root):
    """Single-host worker lock. Durable job fencing still governs side effects."""
    return _process_lock(contained(root, Path(root) / ".local/platform/sending-worker.lock"))


def _auth_database_binding(root, database):
    relative = database.relative_to(root).as_posix()
    path = checked_regular_file(root, relative)
    identity = path.stat()
    body = json.dumps([relative, identity.st_dev, identity.st_ino], separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _require_bound_api(root, database):
    try:
        checked_regular_file(root, ".local/platform/processes/native.json")
        record = checked_regular_file(root, ".local/platform/processes/api.binding.json")
        # Match the launcher's metadata limit, including the full source manifest.
        with record.open("rb") as stream:
            raw = stream.read(524289)
        if len(raw) > 524288:
            raise ValueError
        binding = json.loads(raw)
        expected = _auth_database_binding(root, database)
        if type(binding) is dict and binding.get("auth_database_binding") == expected:
            return
    except (OSError, ValueError):
        pass
    raise PlatformError("ACCOUNT_DB_CHANGED", 409)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("api", "worker", "healthcheck", "init", "prepare-mfa-key"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--console-port", type=int, default=5173)
    parser.add_argument("--container", action="store_true")
    parser.add_argument("--personal", action="store_true", help="Local single-user mode; loopback only")
    parser.add_argument("--require-bound-api", action="store_true", help="Bind native setup to the running API's database")
    args = parser.parse_args(argv)
    try:
        args.root = args.root.resolve()
        if args.personal and args.container:
            raise PlatformError("INVALID_INPUT")
        access = {"access_mode": "local-personal"} if args.personal else {}
        native_accounts = not args.container and not args.personal and sys.platform == "win32"
        if args.require_bound_api and (args.action != "init" or not native_accounts):
            raise PlatformError("INVALID_INPUT")
        from .source_binding import inspect_source, public_summary
        source_binding = inspect_source(args.root)
        if not 1024 <= args.port <= 65535 or not 1024 <= args.console_port <= 65535:
            raise PlatformError("INVALID_INPUT")
        from finance_platform.core.runtime import build_runtime
        from .service import OperationsService
        from .config import load_config
        config_path = args.root / ".local/platform/operations.json"
        config = load_config(args.root, config_path) if config_path.exists() else load_config(args.root)
        database_path = args.root / config["database"]
        loopback_origins = (f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}")
        runtime_access = dict(access, allowed_origins=loopback_origins) if args.personal or native_accounts else access
        if args.action == "healthcheck":
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=3)
            connection.request("GET", "/api/v1/health/ready")
            result = connection.getresponse()
            connection.close()
            return 0 if result.status == 200 else 2
        if args.action == "prepare-mfa-key":
            if not native_accounts:
                raise PlatformError("INVALID_INPUT")
            from .native_mfa import prepare_native_mfa_key
            process_dir = args.root / ".local/platform/processes"
            lease = contained(args.root, process_dir / "native-operation.lock")
            with _process_lock(lease, busy_code="MFA_STOP_REQUIRED"):
                if (process_dir / "native.json").exists():
                    raise PlatformError("MFA_STOP_REQUIRED", 409)
                result = prepare_native_mfa_key(args.root, database_path)
            print(json.dumps({"mfa_key": result}))
            return 0
        if args.action == "api" and native_accounts:
            from .native_mfa import read_native_mfa_key
            try:
                mfa_key = read_native_mfa_key(args.root)
            except PlatformError as error:
                if error.code != "MFA_UNAVAILABLE":
                    raise
                # Keep local operator recovery reachable. Enrolled login still
                # requires MFA and fails closed in the account service.
                print('{"code":"MFA_UNAVAILABLE"}', file=sys.stderr)
                mfa_key = None
            if mfa_key is not None:
                access["auth_mfa_key"] = mfa_key
        if args.action == "api":
            from finance_platform.api import create_app
            import uvicorn
            origins = (f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}", "http://127.0.0.1:5173", "http://localhost:5173",
                       "http://127.0.0.1:8080", "http://localhost:8080", f"http://127.0.0.1:{args.console_port}", f"http://localhost:{args.console_port}")
            if args.personal or native_accounts:
                origins = loopback_origins
            from finance_platform.core.server_composition import load_server_composition
            app = create_app(args.root, database_path=database_path, allowed_origins=origins,
                             **load_server_composition(args.root, native_loopback_accounts=native_accounts), **access)
            source_binding = inspect_source(args.root)
            @app.get("/api/v1/health/source")
            def source_health():
                current = inspect_source(args.root)
                if current["source_hash"] != source_binding["source_hash"] or current["lock_sha256"] != source_binding["lock_sha256"]:
                    raise PlatformError("SOURCE_CHANGED_RESTART_REQUIRED", 503)
                return {"status": "bound", **public_summary(source_binding)}
            # create_app may already mount the built console at '/'. Insert this
            # specific API route ahead of that catch-all static mount.
            from starlette.routing import Mount
            source_route = app.router.routes.pop()
            index = next((i for i, route in enumerate(app.router.routes) if isinstance(route, Mount)), len(app.router.routes))
            app.router.routes.insert(index, source_route)
            process_binding = dict(source_binding)
            if native_accounts:
                process_binding["auth_database_binding"] = _auth_database_binding(args.root, database_path)
            _record_binding(args.root, "api", process_binding)
            # Container port is mapped to host loopback; internal plaintext is single-host only.
            uvicorn.run(app, host="0.0.0.0" if args.container else "127.0.0.1", port=args.port,
                        access_log=False, proxy_headers=False)
        elif args.action == "init":
            if args.require_bound_api:
                _require_bound_api(args.root, database_path)
            runtime = build_runtime(args.root, database_path, **runtime_access)
            if args.require_bound_api:
                _require_bound_api(args.root, database_path)
            _record_binding(args.root, "init", inspect_source(args.root))
            if args.personal:
                print(json.dumps({"initialized": True, "access_mode": "local-personal", "interactive_login_required": False}))
            else:
                status = runtime.auth.setup_status()
                if status["account_state"] == "ready":
                    print('{"initialized":true}')
                elif status["setup_kind"] in {"first_admin", "upgrade_personal"}:
                    # One-time token is only printed to local operator, never stored in logs.
                    print(json.dumps({"setup_token": runtime.auth.prepare_setup(), "setup_kind": status["setup_kind"],
                                      "expires_in_seconds": 1800}))
                else:
                    raise PlatformError("SETUP_REJECTED", 403)
        else:
            from finance_platform.worker import Worker
            with single_worker(args.root):
                from finance_platform.core.server_composition import load_server_composition
                runtime = build_runtime(args.root, database_path,
                                        **load_server_composition(args.root, native_loopback_accounts=native_accounts),
                                        **runtime_access)
                process_binding = inspect_source(args.root)
                if native_accounts:
                    process_binding["auth_database_binding"] = _auth_database_binding(args.root, database_path)
                _record_binding(args.root, "worker", process_binding)
                service = OperationsService(runtime)
                worker = Worker(runtime)
                stop = threading.Event()
                signal.signal(signal.SIGINT, lambda *_: stop.set())
                signal.signal(signal.SIGTERM, lambda *_: stop.set())
                def monitor():
                    while not stop.is_set():
                        try:
                            service.heartbeat("worker")
                            service.evaluate_alerts()
                        except Exception:
                            # Persisted heartbeat becomes stale; don't report fake health.
                            pass
                        stop.wait(5)
                monitor_thread = threading.Thread(target=monitor, name="operations-health", daemon=True)
                monitor_thread.start()
                try:
                    while not stop.is_set():
                        worker.run_once()
                        stop.wait(0.5)
                finally:
                    stop.set()
                    monitor_thread.join(timeout=3)
        return 0
    except (PlatformError, OSError, BindingError) as error:
        print(json.dumps({"code": error.code if isinstance(error, PlatformError) else "OPERATIONS_FAILED"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
