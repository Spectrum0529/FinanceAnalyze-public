"""Template/validation CLI. Registered lifecycle uses the common core service."""

import argparse
import json
import os
from pathlib import Path

from .contracts import StrategyError
from .packages import PackageStore, inspect_package
from .templates import template


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m finance_platform.strategies")
    sub = parser.add_subparsers(dest="action", required=True)
    new = sub.add_parser("template")
    new.add_argument("destination", type=Path)
    new.add_argument("--id", required=True)
    new.add_argument("--style", choices=["event", "batch", "target", "model", "external"], default="event")
    new.add_argument("--model-reference", type=Path)
    check = sub.add_parser("validate")
    check.add_argument("source", type=Path)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("source", type=Path)
    freeze.add_argument("--store", type=Path, required=True)
    find = sub.add_parser("discover")
    find.add_argument("--store", type=Path, required=True)
    instance = sub.add_parser("instance")
    instance.add_argument("instance_id")
    instance.add_argument("--action", dest="lifecycle_action", choices=["status", "validate", "pause", "stop", "archive"], default="status")
    instance.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        if args.action == "template":
            if args.model_reference is not None:
                from predictor.access import AccessPolicy
                allowed = AccessPolicy(Path.cwd()).authorize(args.model_reference, operation="config_read")
                model = json.loads(allowed.read_text(encoding="utf-8"))
            else:
                model = None
            result = template(args.destination, args.id, args.style, model=model)
        elif args.action == "validate":
            manifest, _, sha = inspect_package(args.source)
            result = {"valid": True, "manifest": manifest.raw, "sha256": sha}
        elif args.action == "freeze":
            result = PackageStore(args.store).freeze(args.source)
        elif args.action == "discover":
            result = PackageStore(args.store).discover()
        else:
            from finance_platform.core.runtime import build_runtime
            from finance_platform.strategies.service import StrategyService
            runtime = build_runtime(args.root)
            principal = runtime.auth.session(os.environ.get("FINANCE_PLATFORM_SESSION_TOKEN", ""))[0]
            service = StrategyService(runtime)
            if args.lifecycle_action == "status":
                result = service.instance(principal, args.instance_id)
            else:
                target = {"validate": "VALIDATED", "pause": "PAUSED", "stop": "STOPPED", "archive": "ARCHIVED"}[args.lifecycle_action]
                from contextlib import closing
                with closing(runtime.database.connect()) as con:
                    external=con.execute("SELECT 1 FROM strategy_signal_bindings WHERE instance_id=?",(args.instance_id,)).fetchone()
                    simulation=con.execute("SELECT 1 FROM strategy_simulation_sessions WHERE instance_id=?",(args.instance_id,)).fetchone()
                if external and target!="VALIDATED":
                    from .sessions import ExternalSessions
                    result=ExternalSessions(runtime).stop(principal,args.instance_id,target)
                elif simulation and target!="VALIDATED":
                    from .simulation import LocalSimulation
                    result=LocalSimulation(runtime).stop(principal,args.instance_id,target)
                else:
                    result = service.transition(principal, args.instance_id, target)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (StrategyError, OSError, ValueError, TypeError):
        print(json.dumps({"code": "STRATEGY_PACKAGE_REJECTED", "message": "策略包或参数校验失败"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
