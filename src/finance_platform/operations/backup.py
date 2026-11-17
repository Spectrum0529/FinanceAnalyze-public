"""Versioned private consistent bundles, reference validation and isolated restore.

Bundles contain the platform database (including password hashes), never process
environment values, plaintext credentials, raw/holdout/prospective partitions or
active authentication sessions. They are local private artifacts, not exports.
"""
from contextlib import closing, suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import uuid

from finance_platform.core.auth import clear_auth_proofs
from finance_platform.core.database import Database
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from finance_platform.core.security import safe_json, now_text
from predictor.access import AccessPolicy
from .paths import contained, protect_private
from .pagination import validate_page

ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")
MAX_FILE = 32 * 1024 * 1024
MAX_BUNDLE = 512 * 1024 * 1024
RESTORE_CANCELLED_STRATEGY_HANDLERS = (
    "strategy.external.activate", "strategy.external.consume", "strategy.simulation",
)
RECOVERY_MIGRATIONS = ("""
CREATE TABLE operations_restore_barriers(account_id TEXT NOT NULL, restore_id TEXT NOT NULL,
 backup_id TEXT NOT NULL, recorded_at TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(account_id,restore_id));
CREATE TABLE operations_restore_events(event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
 client_order_id TEXT, restore_id TEXT NOT NULL, recorded_at TEXT NOT NULL, body_json TEXT NOT NULL);
""",)


def digest(path):
    with Path(path).open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return sha


def _readonly(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def validate_database(path):
    """Validate persisted relationships and replay every account from ledger events."""
    with closing(_readonly(path)) as con:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or con.execute("PRAGMA foreign_key_check").fetchall():
            raise PlatformError("DATABASE_UNAVAILABLE", 503)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"platform_meta", "schema_migrations", "runs", "jobs", "users"} <= tables:
            raise PlatformError("MIGRATION_REJECTED")
        if con.execute("SELECT value FROM platform_meta WHERE key='database_kind'").fetchone()[0] != "finance_platform":
            raise PlatformError("MIGRATION_REJECTED")
        migrations = [dict(r) for r in con.execute("SELECT namespace,revision,script_hash FROM schema_migrations ORDER BY namespace,revision")]
        counts = {name: con.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
                  for name in sorted(tables) if not name.startswith("sqlite_") and re.fullmatch(r"[a-z_][a-z0-9_]*", name)}
        # These modules have logical owner/version links beyond declared SQLite FKs.
        checks = [(name, "owner_id", "users", "user_id") for name in counts
                  if "owner_id" in {row[1] for row in con.execute('PRAGMA table_info("' + name + '")')}]
        for child, key, parent, parent_key in checks:
            if child in tables and con.execute(f"SELECT 1 FROM {child} c LEFT JOIN {parent} p ON c.{key}=p.{parent_key} WHERE p.{parent_key} IS NULL LIMIT 1").fetchone():
                raise PlatformError("MIGRATION_REJECTED")
        for name, content_key, hash_key in (("runs", "spec_json", "spec_hash"), ("jobs", "parameters_json", "parameters_hash")):
            for row in con.execute(f"SELECT {content_key},{hash_key} FROM {name}"):
                if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                    raise PlatformError("MIGRATION_REJECTED")
        accounts = {}
        if "trading_accounts" in tables:
            from finance_platform.trading.storage import load_book
            for row in con.execute("SELECT * FROM trading_accounts ORDER BY account_id"):
                book = load_book(con, row)
                snapshot = book.snapshot({})
                # Unknown marks remain unknown; recovery never invents valuation.
                accounts[row["account_id"]] = snapshot
        return {"integrity": "ok", "relations": "ok", "migrations": migrations,
                "table_counts": counts, "accounts": accounts,
                "account_replay": "VERIFIED" if "trading_accounts" in tables else "MODULE_ABSENT"}


def strip_snapshot_authentication(snapshot):
    """Remove ephemeral authentication from a new snapshot, never from Main."""
    with closing(sqlite3.connect(snapshot)) as con, con:
        con.execute("PRAGMA foreign_keys=ON")
        clear_auth_proofs(con, strip_sessions=True, clear_rates=True)
    return digest(snapshot)


def publish_database_snapshot(database, destination):
    """Stage a private DB-only snapshot, then atomically publish verified bytes."""
    requested = Path(destination).absolute()
    final = requested.parent.resolve() / requested.name
    if final == database.path or os.path.lexists(final):
        raise PlatformError("CONFLICT", 409)

    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".financeanalyze-backup-", dir=final.parent))
        # Windows does not guarantee a private DACL on mkdtemp. No DB bytes
        # are copied until the existing ACL helper verifies the directory.
        protect_private(stage)
        snapshot = stage / "platform.sqlite3"
        try:
            result = database.backup(snapshot)
            # The SQLite backup can inherit WAL mode. Normalize the private
            # copy before verification so no sidecar is needed at publication.
            with closing(sqlite3.connect(snapshot)) as con:
                if con.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise PlatformError("DATABASE_UNAVAILABLE", 503)
            checksum = strip_snapshot_authentication(snapshot)
            counts = validate_database(snapshot)["table_counts"]
            if any(counts.get(table) for table in
                   ("sessions", "setup", "auth_recovery_codes", "auth_operator_recovery")):
                raise PlatformError("DATABASE_UNAVAILABLE", 503)
            if counts.get("auth_totp"):
                with closing(_readonly(snapshot)) as con:
                    pending = con.execute("""SELECT 1 FROM auth_totp
                                             WHERE pending_secret IS NOT NULL OR pending_token_hash IS NOT NULL
                                             OR pending_expires IS NOT NULL LIMIT 1""").fetchone()
                if pending:
                    raise PlatformError("DATABASE_UNAVAILABLE", 503)
            if any(os.path.lexists(snapshot.with_name(snapshot.name + suffix))
                   for suffix in ("-journal", "-wal", "-shm")):
                raise PlatformError("DATABASE_UNAVAILABLE", 503)
            protect_private(snapshot)
            result.update(sha256=checksum, bytes=snapshot.stat().st_size)
            try:
                if os.name == "nt":
                    # A Windows same-volume rename refuses an existing destination.
                    snapshot.rename(final)
                else:
                    # POSIX rename replaces the destination. Link creates it
                    # atomically only while the name remains free.
                    os.link(snapshot, final)
            except FileExistsError:
                raise PlatformError("CONFLICT", 409) from None
        except BaseException:
            # A failed or interrupted copy stays in its verified private stage.
            with suppress(OSError):
                (stage / "FAILED.json").write_text('{"status":"FAILED","usable":false}', encoding="utf-8")
            raise
    except (OSError, sqlite3.Error):
        raise PlatformError("DATABASE_UNAVAILABLE", 503) from None

    # After publication any remaining staged file is the sanitized copy.
    with suppress(OSError):
        snapshot.unlink(missing_ok=True)
        stage.rmdir()
    return result


def require_restored_copy(root, database_path):
    """Recognize an exact registered, still-offline restore before any SQLite open."""
    from .config import load_config

    try:
        root = Path(root).resolve()
        if database_path is None:
            raise ValueError()
        recovery = contained(root, root / ".local/platform/restores")
        target = contained(recovery, root / database_path)
        parts = target.relative_to(recovery).parts
        if len(parts) != 2 or not ID.fullmatch(parts[0]) or parts[1] != "platform.sqlite3":
            raise ValueError()
        if not target.is_file() or target.stat().st_nlink != 1:
            raise ValueError()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = target.with_name(target.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                raise ValueError()
        receipt = contained(target.parent, target.parent / "RESTORE.json")
        if not receipt.is_file() or receipt.stat().st_nlink != 1 or receipt.stat().st_size > 2 * 1024 * 1024:
            raise ValueError()
        record = json.loads(receipt.read_bytes())
        if (record["restore_id"] != parts[0] or not ID.fullmatch(record["backup_id"])
                or record["session_restore"] != "INVALIDATED"):
            raise ValueError()
        identity = record["database_identity"]
        if (set(identity) != {"format", "bytes", "sha256", "source_database"}
                or identity["format"] != "platform-restored-copy-1"):
            raise ValueError()
        live = contained(root / ".local/platform", root / load_config(root)["database"])
        source = contained(root / ".local/platform", root / identity["source_database"])
        if source.suffix != ".sqlite3":
            raise ValueError()
        if any(target == active or active.is_file() and target.samefile(active) for active in (live, source)):
            raise ValueError()
        if (type(identity["bytes"]) is not int or not 0 < identity["bytes"] <= MAX_BUNDLE
                or identity["bytes"] != target.stat().st_size or digest(target) != identity["sha256"]):
            raise ValueError()
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        raise PlatformError("RESTORED_DATABASE_REQUIRED", 422) from None
    return target


class BackupService:
    def __init__(self, root, database):
        self.root, self.database = Path(root).resolve(), database
        self.base = contained(self.root, self.root / ".local/platform/backups")
        self.recovery_base = contained(self.root, self.root / ".local/platform/restores")
        # The provided core Database must point to the platform area, never research/audit stores.
        contained(self.root / ".local/platform", self.database.path)

    def _target(self, base, name):
        if type(name) is not str or not ID.fullmatch(name):
            raise PlatformError("INVALID_INPUT")
        return contained(base, base / name)

    def _private_new(self, base, name):
        target = self._target(base, name)
        base.mkdir(parents=True, exist_ok=True)
        protect_private(base)
        target.mkdir(exist_ok=False)
        protect_private(target)
        return target

    def create(self, backup_id=None, *, config_paths=()):
        backup_id = backup_id or "backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        target = self._private_new(self.base, backup_id)
        snapshot = target / "platform.sqlite3"
        try:
            self.database.backup(snapshot)
            # Strip ephemeral authentication; password hashes remain for restored users.
            checksum = strip_snapshot_authentication(snapshot)
            validation = validate_database(snapshot)
            files = [{"path": "platform.sqlite3", "sha256": checksum, "bytes": snapshot.stat().st_size}]
            policy = AccessPolicy(self.root)
            # Configuration uses the existing guard and rejects inline/scoped credentials.
            for index, relative in enumerate(config_paths):
                path = policy.authorize(relative, operation="config_read")
                if path.suffix != ".json" or path.stat().st_size > MAX_FILE:
                    raise PlatformError("INVALID_INPUT")
                content = safe_json(json.loads(path.read_bytes())).encode("utf-8")
                name = "config/" + str(index) + ".json"
                destination = target / name
                destination.parent.mkdir(exist_ok=True)
                destination.write_bytes(content)
                files.append({"path": name, "sha256": digest(destination), "bytes": len(content),
                              "source": path.relative_to(self.root).as_posix()})
            with closing(_readonly(snapshot)) as con:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                hashes = {r[0] for r in con.execute("SELECT package_hash FROM strategy_versions")} if "strategy_versions" in tables else set()
                references = []
                if "data_datasets" in tables:
                    for row in con.execute("SELECT dataset_id,body,content_hash FROM data_datasets"):
                        body = json.loads(row["body"])
                        if body.get("scope") not in {"SYNTHETIC", "DEVELOPMENT"}:
                            raise PlatformError("FORBIDDEN", 403)
                        references.append({"dataset_id": row["dataset_id"], "content_hash": row["content_hash"],
                                           "scope": body["scope"], "content": "IN_DATABASE"})
                if "data_collection_pages" in tables:
                    for row in con.execute("SELECT collection_id,ordinal,body FROM data_collection_pages"):
                        body = json.loads(row["body"])
                        references.append({"collection_id": row["collection_id"], "ordinal": row["ordinal"],
                                           "raw_reference": body.get("raw"), "content": "EXTERNAL_DEVELOPMENT_STORE_NOT_COPIED"})
                experiment_runs = {r[0] for r in con.execute("SELECT run_id FROM research_results")} if "research_results" in tables else set()
            from finance_platform.strategies.packages import inspect_package
            for sha in sorted(hashes):
                if not re.fullmatch(r"[0-9a-f]{64}", sha):
                    raise PlatformError("INVALID_INPUT")
                source = contained(self.root / ".local/platform/strategy-packages", self.root / ".local/platform/strategy-packages" / sha)
                _, package_files, actual = inspect_package(source)
                if actual != sha:
                    raise PlatformError("MIGRATION_REJECTED")
                for name, content in package_files.items():
                    relative = "strategy-packages/" + sha + "/" + name
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                    files.append({"path": relative, "sha256": digest(destination), "bytes": len(content)})
            for run_id in sorted(experiment_runs):
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
                    raise PlatformError("INVALID_INPUT")
                archive = contained(self.root / ".local/platform/experiments", self.root / ".local/platform/experiments" / run_id)
                if not archive.is_dir():
                    raise PlatformError("MIGRATION_REJECTED")
                for entry in sorted(archive.rglob("*")):
                    entry = contained(archive, entry)
                    if not entry.is_file():
                        continue
                    if entry.stat().st_size > MAX_FILE:
                        raise PlatformError("INVALID_INPUT")
                    content = entry.read_bytes()
                    if entry.suffix == ".json":
                        value = json.loads(content)
                        safe_json(value)
                        if entry.name == "declaration.json" and value.get("scope") not in {"SYNTHETIC", "DEVELOPMENT"}:
                            raise PlatformError("FORBIDDEN", 403)
                    relative = "experiments/" + run_id + "/" + entry.relative_to(archive).as_posix()
                    destination = contained(target, target / relative)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                    files.append({"path": relative, "sha256": digest(destination), "bytes": len(content)})
            if sum(item["bytes"] for item in files) > MAX_BUNDLE:
                raise PlatformError("INVALID_INPUT")
            manifest = {"schema_version": 1, "backup_id": backup_id, "created_at": now_text(),
                        "private_local_only": True, "sessions": "EXCLUDED", "files": files,
                        "data_references": references, "validation": validation}
            (target / "manifest.json").write_text(safe_json(manifest), encoding="utf-8")
            for path in target.rglob("*"):
                protect_private(path)
            return {"backup_id": backup_id, "manifest_sha256": digest(target / "manifest.json"),
                    "bytes": sum(f["bytes"] for f in files), "validation": validation}
        except BaseException:
            # Preserve failed backup evidence in its private unique directory.
            (target / "FAILED.json").write_text('{"status":"FAILED","usable":false}', encoding="utf-8")
            raise

    def verify(self, backup_id):
        target = self._target(self.base, backup_id)
        manifest_path = contained(target, target / "manifest.json")
        if manifest_path.stat().st_size > MAX_FILE or (target / "FAILED.json").exists():
            raise PlatformError("INVALID_INPUT")
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("schema_version") != 1 or manifest.get("backup_id") != backup_id or manifest.get("sessions") != "EXCLUDED":
            raise PlatformError("INVALID_INPUT")
        seen, total = set(), 0
        for item in manifest["files"]:
            name = item["path"]
            parsed = PurePosixPath(name)
            if parsed.is_absolute() or str(parsed) != name or ".." in parsed.parts or "\\" in name or ":" in name or name in seen:
                raise PlatformError("INVALID_INPUT")
            if name != "platform.sqlite3" and not name.startswith(("config/", "strategy-packages/", "experiments/")):
                raise PlatformError("INVALID_INPUT")
            seen.add(name)
            path = contained(target, target / name)
            size = path.stat().st_size
            total += size
            if size != item["bytes"] or total > MAX_BUNDLE or digest(path) != item["sha256"]:
                raise PlatformError("MIGRATION_REJECTED")
        if "platform.sqlite3" not in seen:
            raise PlatformError("INVALID_INPUT")
        actual = validate_database(target / "platform.sqlite3")
        if actual != manifest["validation"]:
            raise PlatformError("MIGRATION_REJECTED")
        with closing(_readonly(target / "platform.sqlite3")) as con:
            if con.execute("SELECT count(*) FROM sessions").fetchone()[0] or con.execute("SELECT count(*) FROM setup").fetchone()[0]:
                raise PlatformError("MIGRATION_REJECTED")
        return manifest

    def restore(self, backup_id, restore_id=None):
        manifest = self.verify(backup_id)
        restore_id = restore_id or "restore-" + uuid.uuid4().hex
        target = self._private_new(self.recovery_base, restore_id)
        source = self._target(self.base, backup_id)
        for item in manifest["files"]:
            destination = contained(target, target / item["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Copy only listed, verified bytes; no overwrite of an active path.
            with contained(source, source / item["path"]).open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            if digest(destination) != item["sha256"]:
                raise PlatformError("MIGRATION_REJECTED")
        restored = Database(target / "platform.sqlite3")
        restored.apply_module_migrations("operations_recovery", RECOVERY_MIGRATIONS)
        # Install only schema here, before the restore transaction. Do not build
        # RuntimeContext or register modules: that could enqueue staged work.
        from finance_platform.strategies.restore import install_restore_schema, suspend_for_restore
        install_restore_schema(restored)
        with closing(restored.connect()) as con:
            trading_present = con.execute("SELECT 1 FROM sqlite_master WHERE name='trading_accounts'").fetchone() is not None
        trading_restore = None
        if trading_present:
            # Trading owns the authoritative barrier/resolution semantics and all send gates.
            # Call even for same-path activation; relocation detection is only an extra guard.
            from finance_platform.trading.storage import mark_restored_paper
            trading_restore = mark_restored_paper(restored, restore_id)
        cancelled_strategy_jobs = []
        with restored.transaction() as con:
            immutable(con, "operations_restore_barriers", ("account_id", "restore_id"))
            immutable(con, "operations_restore_events", ("event_id",))
            clear_auth_proofs(con, strip_sessions=True, clear_rates=True)
            # Session projections, their global execution gate and core task
            # cancellation commit together. Clearing the gate later cannot make
            # an old activation/consume/simulation job automatically claimable.
            suspend_for_restore(restored, restore_id, connection=con)
            placeholders = ",".join("?" for _ in RESTORE_CANCELLED_STRATEGY_HANDLERS)
            pending_strategy_jobs = con.execute(
                f"SELECT job_id,run_id,handler,status FROM jobs WHERE handler IN ({placeholders}) AND status IN ('QUEUED','RUNNING')",
                RESTORE_CANCELLED_STRATEGY_HANDLERS).fetchall()
            for job in pending_strategy_jobs:
                recorded_at = now_text()
                con.execute("UPDATE jobs SET status='CANCELLED',cancel_requested=1,error_code='RESTORE_REQUIRES_EXPLICIT_RESUME',lease_owner=NULL,lease_until=NULL,fencing_token=fencing_token+1,updated_at=? WHERE job_id=?",
                            (recorded_at, job["job_id"]))
                con.execute("INSERT INTO job_events(job_id,run_id,kind,recorded_at,metadata_json) VALUES(?,?,?,?,?)",
                            (job["job_id"], job["run_id"], "CANCELLED", recorded_at,
                             safe_json({"code": "RESTORE_REQUIRES_EXPLICIT_RESUME", "restore_id": restore_id,
                                        "previous_status": job["status"]})))
                cancelled_strategy_jobs.append({"job_id": job["job_id"], "run_id": job["run_id"],
                                                "handler": job["handler"], "previous_status": job["status"]})
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='schedules'").fetchone():
                for row in con.execute("SELECT schedule_id FROM schedules WHERE paused=0 AND archived=0").fetchall():
                    con.execute("UPDATE schedules SET paused=1,updated_at=? WHERE schedule_id=?", (now_text(), row[0]))
                    con.execute("INSERT INTO schedule_events(schedule_id,kind,recorded_at,details_json) VALUES(?,?,?,?)",
                                (row[0], "RESTORE_PAUSED", now_text(), '{"reason":"isolated_restore"}'))
            # Stale worker fences are never restored as active permissions.
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='trading_leases'").fetchone():
                con.execute("UPDATE trading_leases SET expires_at='1970-01-01T00:00:00Z'")
                con.execute("UPDATE trading_accounts SET frozen=1,freeze_reason='RESTORE_RECONCILIATION_REQUIRED'")
                # A stale backup is not proof that a queued intention was never sent.
                # All paper accounts get an immutable recovery barrier; every nonterminal
                # intention and outbound action is uncertain until broker evidence resolves it.
                for account in con.execute("SELECT account_id FROM trading_accounts WHERE mode='BROKER_PAPER'").fetchall():
                    account_id = account[0]
                    con.execute("INSERT INTO operations_restore_barriers VALUES(?,?,?,?,?)",
                                (account_id, restore_id, backup_id, now_text(), "RESTORE_UNCERTAIN_PAPER_SEND"))
                    con.execute("UPDATE trading_accounts SET freeze_reason='RESTORE_UNCERTAIN_PAPER_SEND' WHERE account_id=?", (account_id,))
                    pending = con.execute("SELECT o.client_order_id,o.status FROM trading_orders o JOIN trading_intents i USING(client_order_id) WHERE i.account_id=? AND o.status NOT IN ('filled','cancelled','rejected','expired','replaced')", (account_id,)).fetchall()
                    for order in pending:
                        body = safe_json({"event": "restore_uncertain", "previous_status": order["status"],
                                          "status": "unknown", "restore_id": restore_id, "broker_evidence": "NONE"})
                        event_id = "restore-" + restore_id + "-" + uuid.uuid4().hex
                        con.execute("UPDATE trading_orders SET status='unknown' WHERE client_order_id=?", (order["client_order_id"],))
                        con.execute("UPDATE trading_outbox SET status='unknown' WHERE client_order_id=? AND status IN ('pending','sending')", (order["client_order_id"],))
                        con.execute("INSERT INTO operations_restore_events VALUES(?,?,?,?,?,?)", (event_id, account_id, order["client_order_id"], restore_id, now_text(), body))
                        con.execute("INSERT INTO trading_order_events(account_id,event_id,client_order_id,body,at) VALUES(?,?,?,?,?)", (account_id, event_id, order["client_order_id"], body, now_text()))
            # Preserve runs, let core recovery fail expired jobs with new retry IDs.
            con.execute("UPDATE jobs SET lease_until=0 WHERE status='RUNNING'")
        actual = validate_database(restored.path)
        if actual["accounts"] != manifest["validation"]["accounts"]:
            raise PlatformError("MIGRATION_REJECTED")
        report = {"restore_id": restore_id, "backup_id": backup_id, "restored_at": now_text(),
                  "session_restore": "INVALIDATED", "trading_resume": "REQUIRES_RECONCILIATION",
                  "schedules_and_running_strategies": "PAUSED",
                  "paper_send": "IMMUTABLE_RESTORE_BARRIER_NONTERMINAL_INTENTS_UNKNOWN",
                  "strategy_restore": {"global_gate": "ACTIVE", "sessions": "SUSPENDED",
                                       "cancelled_handlers": list(RESTORE_CANCELLED_STRATEGY_HANDLERS),
                                       "cancelled_jobs": cancelled_strategy_jobs,
                                       "pending_work": "RETAINED_SUSPENDED"},
                  "trading_restore": trading_restore,
                  "validation": actual}
        report["database_identity"] = {
            "format": "platform-restored-copy-1", "bytes": restored.path.stat().st_size,
            "sha256": digest(restored.path), "source_database": self.database.path.relative_to(self.root).as_posix(),
        }
        (target / "RESTORE.json").write_text(safe_json(report), encoding="utf-8")
        for path in target.rglob("*"):
            protect_private(path)
        return report

    def list(self, *, limit=50, offset=0):
        validate_page(limit, offset)
        rows = []
        if self.base.exists():
            for child in sorted(self.base.iterdir()):
                if ID.fullmatch(child.name) and child.is_dir():
                    rows.append({"backup_id": child.name, "status": "FAILED" if (child / "FAILED.json").exists() else
                                 "CREATED" if (child / "manifest.json").exists() else "INTERRUPTED"})
        return {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}
