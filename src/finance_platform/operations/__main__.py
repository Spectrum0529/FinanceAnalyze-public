"""Standalone operations CLI until the core CLI delegates these same services."""
import argparse
import json
from pathlib import Path
import sys

from finance_platform.core.database import Database
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import safe_json
from .backup import BackupService
from .config import load_config, doctor


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config")
    parser.add_argument("action", choices=("doctor", "backup", "verify", "restore", "list"))
    parser.add_argument("--backup-id")
    parser.add_argument("--restore-id")
    parser.add_argument("--include-config", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        config = load_config(args.root, args.config)
        if args.action == "doctor":
            result = doctor(args.root, config)
        else:
            db = Database(args.root / config["database"])
            if not db.path.is_file():
                raise PlatformError("DATABASE_UNAVAILABLE", 503)
            service = BackupService(args.root, db)
            if args.action == "backup":
                result = service.create(args.backup_id, config_paths=args.include_config)
                result.pop("validation", None)
            elif args.action == "verify":
                manifest = service.verify(args.backup_id)
                result = {"backup_id": args.backup_id, "integrity": "ok", "relations": "ok",
                          "account_replay": manifest["validation"]["account_replay"]}
            elif args.action == "restore":
                result = service.restore(args.backup_id, args.restore_id)
                result.pop("validation", None)
            else:
                result = service.list()
        print(safe_json(result))
        return 2 if args.action == "doctor" and result["verification"] == "FAILED" else 0
    except (PlatformError, ValueError, OSError, TypeError, KeyError) as error:
        # Never echo raw path, malformed config or arbitrary exception values.
        print(json.dumps({"code": error.code if isinstance(error, PlatformError) else "OPERATIONS_FAILED"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
