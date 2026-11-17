"""Transfer verified product objects to the destination's signed-in account."""
import base64
import binascii
from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from uuid import uuid4
import zipfile

from fastapi import APIRouter, Request, Response
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from finance_platform.data.simple_inputs import metadata, tickers, validate_rows
from finance_platform.operations.launch import _process_lock
from finance_platform.operations.paths import contained
from finance_platform.strategies.contracts import StrategyError
from finance_platform.strategies.packages import MAX_FILE, MAX_FILES, MAX_TOTAL, inspect_package, safe_name
from finance_platform.strategies.service import StrategyService
from .account_api import COOKIE
from .auth import AccountDelegation
from .contracts import digest, fingerprint, identifier, parse_instant
from .database import immutable
from .errors import PlatformError
from .local_personal import require_direct_local_scope
from .mcp_data_policy import require_dataset
from . import onboarding_yahoo_transfer as yahoo
from .result_lifecycle import active_result
from .security import clean, now_text
from .source_binding import checked_regular_file
from .team_profiles import Strict


FORMAT = "financeanalyze-transfer/1"
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 100
REFERENCE = ("dataset_id", "version", "content_hash")
ObjectID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
TaskResultID = Annotated[str, Field(pattern=r"^tasks:[A-Za-z0-9][A-Za-z0-9_.-]{0,121}$")]
MIGRATIONS = ("""
CREATE TABLE onboarding_migrated_objects(
    owner_id TEXT NOT NULL REFERENCES users(user_id),
    kind TEXT NOT NULL,
    origin_id TEXT NOT NULL,
    origin_version TEXT NOT NULL,
    origin_content_hash TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    target_json TEXT NOT NULL,
    archive_id TEXT NOT NULL UNIQUE,
    archive_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner_id,kind,origin_id,origin_version)
);
""",)


class DataReference(Strict):
    dataset_id: ObjectID
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class StrategyVersion(Strict):
    strategy_id: ObjectID
    version: ObjectID


class MigrationSelection(Strict):
    dataset_references: list[DataReference] = Field(default_factory=list, max_length=MAX_ITEMS)
    strategy_versions: list[StrategyVersion] = Field(default_factory=list, max_length=MAX_ITEMS)
    result_ids: list[TaskResultID] = Field(default_factory=list, max_length=MAX_ITEMS)


class StrategyApproval(Strict):
    package_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class TransferObject(Strict):
    kind: Literal["dataset", "yahoo_dataset", "strategy", "report"]
    origin_id: str = Field(min_length=1, max_length=128)
    origin_version: str = Field(min_length=1, max_length=128)
    origin_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    payload: dict


class TransferBundle(Strict):
    format: Literal["financeanalyze-transfer/1"]
    items: list[TransferObject] = Field(min_length=1, max_length=MAX_ITEMS)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PortableDataset(Strict):
    name: str = Field(min_length=1, max_length=120)
    scope: Literal["public", "synthetic"]
    start: str
    end: str
    received_at: str
    symbols: list[str] = Field(min_length=1, max_length=1000)
    metadata: dict
    rows: list[dict] = Field(min_length=1, max_length=10000)


def _invalid():
    return PlatformError("MIGRATION_PACKAGE_INVALID", 422)


def _restricted():
    return PlatformError("MIGRATION_OBJECT_RESTRICTED", 403)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise _invalid()
    return result


def _origin(item):
    return item.kind, item.origin_id, item.origin_version


def _matches_yahoo(item, request):
    return (request["origin_content_hash"] == item.origin_content_hash
            and fingerprint(request["payload"]) == item.payload_sha256)


def _report_payload(report):
    if report.get("family") != "dataset-portfolio-result-1":
        raise _restricted()
    values = [report]
    while values:
        value = values.pop()
        if isinstance(value, dict):
            if (value.get("scope") == "licensed_local" or str(value.get("provider", "")).casefold() == "wrds"
                    or {"wrds_capture", "session_token", "mcp_grants", "auth_totp", "password_hash"} & value.keys()):
                raise _restricted()
            values.extend(value.values())
        elif isinstance(value, list):
            values.extend(value)
    clean(report)
    return report


class Migration:
    def __init__(self, runtime):
        self.runtime = runtime
        self.database = runtime.database
        self.library = runtime.dataset_library
        self.research = runtime.research_task_service
        self.database.apply_module_migrations("onboarding_migration", MIGRATIONS)
        with self.database.transaction() as con:
            immutable(con, "onboarding_migrated_objects", ("owner_id", "kind", "origin_id", "origin_version"))

    def _owner(self, principal, *, write=False):
        if self.runtime.hosted_accounts or isinstance(principal, AccountDelegation):
            raise PlatformError("FORBIDDEN", 403)
        self.runtime.require_owner(principal, principal.user_id, write=write)
        return principal.user_id

    def _write_lock(self):
        root = Path(self.runtime.root)
        relative = ".local/platform/onboarding-migration.lock"
        path = contained(root, root / relative)
        if path.exists():
            checked_regular_file(root, relative)
        return _process_lock(path)

    def _verified_bundle(self, raw, expected):
        actual = sha256(raw).hexdigest()
        if actual != expected:
            raise PlatformError("MIGRATION_PREVIEW_CHANGED", 409)
        return actual, self._parse(raw)

    @contextmanager
    def _stage_package(self, files):
        if (type(files) is not dict or not 1 <= len(files) <= MAX_FILES
                or len({name.casefold() for name in files}) != len(files)):
            raise _invalid()
        root = Path(self.runtime.root)
        staging = contained(root, root / ".local/platform/strategy-staging")
        staging.mkdir(exist_ok=True, parents=True)
        with TemporaryDirectory(dir=staging, prefix="migration-") as temporary:
            archive_path = Path(temporary) / "strategy.zip"
            total = 0
            with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, encoded in files.items():
                    safe_name(name)
                    if type(encoded) is not str or len(encoded) > 4 * ((MAX_FILE + 2) // 3):
                        raise _invalid()
                    data = base64.b64decode(encoded, validate=True)
                    total += len(data)
                    if len(data) > MAX_FILE or total > MAX_TOTAL:
                        raise _invalid()
                    archive.writestr(name, data)
            yield archive_path

    @staticmethod
    def _entry(kind, origin_id, version, checksum, payload):
        identifier(origin_id)
        identifier(version)
        digest(checksum)
        return {"kind": kind, "origin_id": origin_id, "origin_version": version,
                "origin_content_hash": checksum, "payload_sha256": fingerprint(payload), "payload": payload}

    def _export_dataset(self, owner, reference):
        current = self.library.get(owner, reference.dataset_id, selectable=True)
        require_dataset(current)
        if (current["kind"] != "dataset" or current.get("market_provenance")
                or {field: current[field] for field in REFERENCE} != reference.model_dump()):
            raise _restricted()
        provider = current["source"]["provider"]
        if provider == "yfinance":
            if current["scope"] != "public":
                raise _restricted()
            return self._export_yahoo(owner, reference, current)
        if provider != "csv" or current.get("row_storage"):
            raise _restricted()
        material = self.library.get(owner, reference.dataset_id, include_rows=True, selectable=True)
        if {field: material[field] for field in REFERENCE} != reference.model_dump():
            raise PlatformError("MIGRATION_VERSION_CONFLICT", 409)
        payload = {field: current[field] for field in ("name", "scope", "start", "end", "received_at", "metadata")}
        payload["rows"] = material["rows"]
        payload["symbols"] = current["universe"]["tickers"]
        clean(payload)
        return self._entry("dataset", reference.dataset_id, str(reference.version), reference.content_hash, payload)

    def _export_yahoo(self, owner, reference, current):
        with closing(self.database.connect()) as con:
            producer = con.execute("""SELECT handler,status,result_json FROM jobs
                WHERE owner_id=? AND job_id=?""", (owner, current.get("origin_job_id"))).fetchone()
        if (producer is None or producer["status"] != "SUCCEEDED"
                or producer["handler"] not in {"data.library-download", yahoo.HANDLER}
                or json.loads(producer["result_json"])["dataset_id"] != reference.dataset_id):
            raise _restricted()
        material = self.library.get(owner, reference.dataset_id, include_rows=True, selectable=True)
        if {field: material[field] for field in REFERENCE} != reference.model_dump():
            raise PlatformError("MIGRATION_VERSION_CONFLICT", 409)
        payload = {"name": current["name"], "start": current["start"], "end": current["end"],
                   "symbols": current["universe"]["tickers"],
                   "capture": yahoo.classify_capture(current, current["row_storage"], current["security_metadata"])}
        yahoo.validate_payload(payload)
        clean(payload)
        return self._entry("yahoo_dataset", reference.dataset_id, str(reference.version), reference.content_hash, payload)

    def _export_report(self, principal, result_id):
        source, separator, run = result_id.partition(":")
        if not separator or source != "tasks":
            raise _restricted()
        identifier(run)
        report = self.research.result(principal, run)
        self.research.require_mcp_report(principal, run, report)
        return self._entry("report", result_id, "1", fingerprint(report), _report_payload(report))

    def _export_strategy(self, principal, selection):
        service = StrategyService(self.runtime)
        record = service.version(principal, selection.strategy_id, selection.version)
        checksum = record["package_hash"]
        manifest, files, actual = inspect_package(service.packages.path(checksum))
        if (record["owner_id"] != principal.user_id or actual != checksum
                or manifest.raw["id"] != selection.strategy_id or manifest.raw["version"] != selection.version):
            raise _restricted()
        packages = {name: base64.b64encode(data).decode("ascii") for name, data in files.items()}
        return self._entry("strategy", selection.strategy_id, selection.version, checksum, {"files": packages})

    def export(self, principal, selection):
        owner = self._owner(principal, write=True)
        count = len(selection.dataset_references) + len(selection.strategy_versions) + len(selection.result_ids)
        if not 1 <= count <= MAX_ITEMS:
            raise _invalid()
        items = [self._export_dataset(owner, reference) for reference in selection.dataset_references]
        items.extend(self._export_strategy(principal, version) for version in selection.strategy_versions)
        items.extend(self._export_report(principal, result_id) for result_id in selection.result_ids)
        raw = _json_bytes({"format": FORMAT, "items": items, "manifest_sha256": fingerprint(items)})
        self._parse(raw)
        return raw

    def _parse(self, raw):
        try:
            if len(raw) > MAX_BYTES:
                raise PlatformError("MIGRATION_PACKAGE_TOO_LARGE", 413)
            if not raw:
                raise _invalid()
            bundle = TransferBundle.model_validate(json.loads(raw, object_pairs_hook=_unique_fields))
            items = bundle.items
            if (fingerprint([item.model_dump() for item in items]) != bundle.manifest_sha256
                    or len({_origin(item) for item in items}) != len(items)):
                raise _invalid()
            for item in items:
                identifier(item.origin_id)
                identifier(item.origin_version)
                if fingerprint(item.payload) != item.payload_sha256:
                    raise _invalid()
            return items
        except PlatformError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise _invalid() from None

    def _validate(self, item):
        if item.kind == "report":
            if (not item.origin_id.startswith("tasks:") or item.origin_version != "1"
                    or item.origin_content_hash != item.payload_sha256
                    or item.payload.get("run_id") != item.origin_id.removeprefix("tasks:")):
                raise _invalid()
            _report_payload(item.payload)
        elif item.kind == "yahoo_dataset":
            yahoo.validate_payload(item.payload)
            clean(item.payload)
        elif item.kind == "dataset":
            data = PortableDataset.model_validate(item.payload)
            clean(item.payload)
            metadata(data.metadata)
            parse_instant(data.received_at)
            if not item.origin_version.isascii() or not item.origin_version.isdecimal() or int(item.origin_version) < 1:
                raise _invalid()
            if tickers(" ".join(data.symbols)) != data.symbols:
                raise _invalid()
            validate_rows(data.rows, data.start, data.end, data.symbols)
        else:
            if set(item.payload) != {"files"}:
                raise _invalid()
            with self._stage_package(item.payload["files"]) as archive:
                manifest, _, actual = inspect_package(archive)
                if (actual != item.origin_content_hash or manifest.raw["id"] != item.origin_id
                        or manifest.raw["version"] != item.origin_version):
                    raise _invalid()

    def _previous(self, owner, item):
        with closing(self.database.connect()) as con:
            return con.execute("""SELECT * FROM onboarding_migrated_objects
                WHERE owner_id=? AND kind=? AND origin_id=? AND origin_version=?""",
                (owner, *_origin(item))).fetchone()

    def _disposition(self, principal, item):
        self._validate(item)
        existing = self._previous(principal.user_id, item)
        if existing is not None:
            if (existing["origin_content_hash"] != item.origin_content_hash
                    or existing["payload_sha256"] != item.payload_sha256):
                return {"status": "conflict", "reason_code": "MIGRATION_VERSION_CONFLICT"}
            return {"status": "reused", "target": json.loads(existing["target_json"])}
        if item.kind == "yahoo_dataset":
            job = yahoo.job_record(self.runtime, principal.user_id, _origin(item))
            if job is not None:
                request = json.loads(job["parameters_json"])
                if not _matches_yahoo(item, request):
                    return {"status": "conflict", "reason_code": "MIGRATION_VERSION_CONFLICT"}
                if job["status"] == "SUCCEEDED":
                    return {"status": "ready_to_link", "job_id": job["job_id"]}
                if job["status"] in {"FAILED", "CANCELLED"}:
                    return {"status": "rejected", "job_id": job["job_id"],
                            "retry_available": True,
                            "reason_code": job["error_code"] or "MIGRATION_YAHOO_JOB_CANCELLED"}
                return {"status": "pending", "job_id": job["job_id"]}
        if item.kind == "strategy":
            try:
                old = StrategyService(self.runtime).version(principal, item.origin_id, item.origin_version)
            except StrategyError as error:
                if str(error) != "STRATEGY_VERSION_NOT_FOUND":
                    raise
            else:
                if old["package_hash"] != item.origin_content_hash:
                    return {"status": "conflict", "reason_code": "MIGRATION_VERSION_CONFLICT"}
        return {"status": "importable"}

    @staticmethod
    def _project(item, outcome):
        return {**item.model_dump(exclude={"payload"}), **outcome}

    def _inspect(self, principal, item):
        try:
            return self._disposition(principal, item)
        except PlatformError as error:
            return {"status": "rejected", "reason_code": error.code}
        except StrategyError as error:
            return {"status": "rejected", "reason_code": PlatformError(str(error), 422).code}
        except (ValidationError, ValueError, TypeError, KeyError, binascii.Error):
            return {"status": "rejected", "reason_code": "MIGRATION_PACKAGE_INVALID"}

    def preview(self, principal, raw):
        self._owner(principal, write=True)
        validated = [self._project(item, self._inspect(principal, item)) for item in self._parse(raw)]
        return {"bundle_sha256": sha256(raw).hexdigest(), "items": validated,
                "can_import": any(item["status"] in {"importable", "reused", "ready_to_link", "pending"}
                                  for item in validated)}

    def _import_dataset(self, owner, item):
        data = item.payload
        key = "migration-" + fingerprint(_origin(item))
        connection = self.library.save_connection(owner, {"name": "迁入 CSV 行情", "provider": "csv",
                                                  "credential_ref": None}, "migration-csv")
        selection = self.library.resolve_universe(owner, {"manual": " ".join(data["symbols"])})
        restored = self.library.import_dataset(owner, {
            **{field: data[field] for field in ("name", "scope", "start", "end", "received_at", "metadata", "rows")},
            "connection_id": connection["connection_id"], "kind": "dataset", "benchmark": None,
            "universe": selection, "source": {"provider": "csv",
                "reference": "FinanceAnalyze transfer " + item.origin_content_hash, "universe_as_of": None},
        }, key)
        return {field: restored[field] for field in REFERENCE}

    def _import_strategy(self, principal, item):
        service = StrategyService(self.runtime)
        try:
            current = service.version(principal, item.origin_id, item.origin_version)
        except StrategyError as error:
            if str(error) != "STRATEGY_VERSION_NOT_FOUND":
                raise
            with self._stage_package(item.payload["files"]) as archive:
                current = service.register(principal, archive, trusted=False)
        current_hash = current.get("package_hash", current.get("sha256"))
        if current_hash != item.origin_content_hash:
            raise PlatformError("MIGRATION_VERSION_CONFLICT", 409)
        return {"strategy_id": item.origin_id, "version": item.origin_version,
                "package_hash": current_hash, "trusted": bool(current["trusted"])}

    def _import_one(self, principal, item, bundle_sha256):
        owner = principal.user_id
        archive_id = uuid4().hex
        if item.kind == "yahoo_dataset":
            job = yahoo.job_record(self.runtime, owner, _origin(item))
            if job is None:
                receipt = yahoo.submit(self.runtime, principal, item, _origin(item), bundle_sha256)
                return {"status": "pending", "job_id": receipt["job_id"]}
            result = json.loads(job["result_json"])
            target = {field: result[field] for field in REFERENCE}
            saved = self.library.get(owner, target["dataset_id"], selectable=True)
            if (saved["origin_job_id"] != job["job_id"] or saved["source"]["provider"] != "yfinance"
                    or {field: saved[field] for field in REFERENCE} != target):
                raise PlatformError("MIGRATION_VERSION_CONFLICT", 409)
            target["job_id"] = job["job_id"]
        elif item.kind == "dataset":
            target = self._import_dataset(owner, item)
        elif item.kind == "strategy":
            target = self._import_strategy(principal, item)
        else:
            target = {"archive_id": archive_id, "report_sha256": item.payload_sha256,
                      "status": "imported_historical",
                      "result_id": "migration:" + archive_id,
                      "read_url": "/api/v1/onboarding/migration/reports/" + archive_id}
        with self.database.transaction() as con:
            con.execute("INSERT INTO onboarding_migrated_objects VALUES (?,?,?,?,?,?,?,?,?,?)", (
                owner, *_origin(item), item.origin_content_hash, item.payload_sha256,
                _json_bytes(target).decode(), archive_id,
                _json_bytes(item.payload).decode() if item.kind == "report" else None,
                now_text(),
            ))
        return {"status": "imported", "target": target}

    def commit(self, principal, raw, expected):
        self._owner(principal, write=True)
        actual, entries = self._verified_bundle(raw, expected)
        results = []
        with self._write_lock():
            for entry in entries:
                outcome = self._inspect(principal, entry)
                if outcome["status"] in {"importable", "ready_to_link"}:
                    try:
                        outcome = self._import_one(principal, entry, actual)
                    except PlatformError as error:
                        outcome = {"status": "rejected", "reason_code": error.code}
                    except StrategyError as error:
                        outcome = {"status": "rejected", "reason_code": PlatformError(str(error), 422).code}
                results.append(self._project(entry, outcome))
        accepted = sum(row["status"] in {"imported", "reused"} for row in results)
        status = "completed" if accepted == len(results) else "partial" if accepted else "rejected"
        if any(row["status"] == "pending" for row in results):
            status = "pending"
        return {"bundle_sha256": actual, "status": status, "items": results}

    def retry_yahoo(self, principal, raw, expected, previous_job_id, idempotency_key):
        owner = self._owner(principal, write=True)
        try:
            identifier(previous_job_id, "previous job id")
            identifier(idempotency_key, "idempotency key")
        except ValueError:
            raise PlatformError("INVALID_INPUT", 422) from None
        actual, entries = self._verified_bundle(raw, expected)
        binding = yahoo.RetryBinding(previous_job_id=previous_job_id, key_sha256=fingerprint(idempotency_key))
        with self._write_lock():
            previous = yahoo.previous_job(self.runtime, owner, previous_job_id)
            if previous is None:
                raise PlatformError("NOT_FOUND", 404)
            request = json.loads(previous["parameters_json"])
            origin = ("yahoo_dataset", request["origin_id"], request["origin_version"])
            item = next((entry for entry in entries if _origin(entry) == origin), None)
            if item is None or not _matches_yahoo(item, request):
                raise PlatformError("MIGRATION_VERSION_CONFLICT", 409)
            self._validate(item)
            if request.get("bundle_sha256") not in {None, actual}:
                raise PlatformError("MIGRATION_PREVIEW_CHANGED", 409)
            job = yahoo.retry_successor(self.runtime, owner, previous_job_id)
            if job is not None:
                saved = json.loads(job["parameters_json"])
                if saved["retry"] != binding.model_dump() or saved["bundle_sha256"] != actual:
                    raise PlatformError("MIGRATION_YAHOO_RETRY_SUPERSEDED", 409)
            else:
                if previous["status"] not in {"FAILED", "CANCELLED"}:
                    raise PlatformError("MIGRATION_YAHOO_RETRY_NOT_TERMINAL", 409)
                job = yahoo.submit(self.runtime, principal, item, origin, actual, retry=binding)
        return {"bundle_sha256": actual, "previous_job_id": previous_job_id,
                "job_id": job["job_id"], "job_status": job["status"]}

    def approve_strategy(self, principal, session_token, strategy_id, version, package_hash):
        owner = self._owner(principal, write=True)
        if principal.role != "admin" or self.runtime.access_mode != "accounts":
            raise PlatformError("FORBIDDEN", 403)
        if self.runtime.auth.require_recent(session_token, admin=True) != owner:
            raise PlatformError("FORBIDDEN", 403)
        service = StrategyService(self.runtime)
        with self.database.transaction() as con:
            if self.runtime.auth.require_recent(session_token, admin=True, connection=con) != owner:
                raise PlatformError("FORBIDDEN", 403)
            row = con.execute("""SELECT v.package_hash,v.manifest_json,v.trusted,m.origin_content_hash
                  FROM strategy_runtime_versions v JOIN onboarding_migrated_objects m
                    ON m.owner_id=v.owner_id AND m.kind='strategy' AND m.origin_id=v.strategy_id AND m.origin_version=v.version
                 WHERE v.owner_id=? AND v.strategy_id=? AND v.version=?""", (owner, strategy_id, version)).fetchone()
            if row is None:
                raise PlatformError("NOT_FOUND", 404)
            if row["package_hash"] != package_hash or row["origin_content_hash"] != package_hash:
                raise PlatformError("MIGRATION_STRATEGY_HASH_MISMATCH", 409)
            try:
                verified = service.packages.verify(package_hash)
                if verified.raw != json.loads(row["manifest_json"]):
                    raise StrategyError("FROZEN_PACKAGE_CHANGED")
            except (StrategyError, ValueError, OSError):
                raise PlatformError("MIGRATION_STRATEGY_PACKAGE_CHANGED", 409) from None
            if not row["trusted"]:
                con.execute("INSERT INTO strategy_migrated_approvals VALUES(?,?,?,?,?)",
                            (owner, strategy_id, version, package_hash, now_text()))
                self.database.audit(owner, "strategy.approve_migrated", strategy_id,
                                    {"version": version, "package_hash": package_hash}, connection=con)
        return {"owner_id": owner, "strategy_id": strategy_id, "version": version,
                "package_hash": package_hash, "trusted": True}

    def read_report(self, principal, archive_id):
        owner = self._owner(principal)
        with closing(self.database.connect()) as con:
            if not active_result(con, owner, "migration:" + archive_id):
                raise PlatformError("NOT_FOUND", 404)
            row = con.execute("""SELECT origin_id,payload_sha256,archive_json
                                 FROM onboarding_migrated_objects
                                 WHERE owner_id=? AND archive_id=? AND kind='report'""",
                              (owner, archive_id)).fetchone()
        if row is None:
            raise PlatformError("NOT_FOUND", 404)
        report = json.loads(row["archive_json"])
        if fingerprint(report) != row["payload_sha256"]:
            raise PlatformError("MIGRATION_PACKAGE_INVALID", 422)
        return {"origin_result_id": row["origin_id"], "report_sha256": row["payload_sha256"], "report": report}

    def read_archive(self, principal, archive_id):
        return self.read_report(principal, archive_id)["report"]

    def status(self, principal):
        owner = self._owner(principal)
        with closing(self.database.connect()) as con:
            rows = con.execute("""SELECT kind,origin_id,origin_version,origin_content_hash,target_json
                                 FROM onboarding_migrated_objects WHERE owner_id=?
                                 ORDER BY kind,origin_id,origin_version LIMIT 100""", (owner,)).fetchall()
        return {"format": FORMAT, "maximum_bytes": MAX_BYTES, "maximum_items": MAX_ITEMS,
                "actions": {"approve_strategy": self.runtime.access_mode == "accounts" and principal.role == "admin",
                            "retry_yahoo": principal.role in {"admin", "researcher"}},
                "object_types": ["dataset", "yahoo_dataset", "strategy", "report"], "imported": [
                    {**{key: row[key] for key in ("kind", "origin_id", "origin_version", "origin_content_hash")},
                     "target": json.loads(row["target_json"])} for row in rows]}


async def _bounded_body(request):
    if request.headers.get("content-type", "").partition(";")[0].strip().casefold() != "application/json":
        raise _invalid()
    data = bytearray()
    async for block in request.stream():
        if len(data) + len(block) > MAX_BYTES:
            raise PlatformError("MIGRATION_PACKAGE_TOO_LARGE", 413)
        data.extend(block)
    return bytes(data)


def create_router(runtime):
    service = Migration(runtime)
    runtime.onboarding_migration = service
    router = APIRouter(prefix="/onboarding/migration", tags=["onboarding"])

    @router.get("")
    def status(request: Request):
        return service.status(runtime.require_principal(request))

    @router.post("/export")
    def export(selection: MigrationSelection, request: Request):
        raw = service.export(runtime.require_principal(request, write=True), selection)
        return Response(raw, media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="FinanceAnalyze-transfer.json"'})

    @router.post("/preview")
    async def preview(request: Request):
        principal = runtime.require_principal(request, write=True)
        service._owner(principal, write=True)
        return await run_in_threadpool(service.preview, principal, await _bounded_body(request))

    @router.post("/import")
    async def commit(request: Request):
        principal = runtime.require_principal(request, write=True)
        service._owner(principal, write=True)
        expected = request.headers.get("x-migration-sha256")
        return await run_in_threadpool(service.commit, principal, await _bounded_body(request), expected)

    @router.post("/yahoo/{previous_job_id}/retry")
    async def retry_yahoo(previous_job_id: ObjectID, request: Request):
        principal = runtime.require_principal(request, write=True)
        service._owner(principal, write=True)
        return await run_in_threadpool(service.retry_yahoo, principal, await _bounded_body(request),
                                       request.headers.get("x-migration-sha256"), previous_job_id,
                                       request.headers.get("x-migration-retry-key"))

    @router.post("/strategies/{strategy_id}/{version}/approve")
    def approve_strategy(strategy_id: ObjectID, version: ObjectID, approval: StrategyApproval, request: Request):
        principal = runtime.require_principal(request, write=True)
        require_direct_local_scope(request.scope, runtime.allowed_origins)
        return service.approve_strategy(principal, request.cookies.get(COOKIE), strategy_id, version,
                                        approval.package_hash)

    @router.get("/reports/{archive_id}")
    def report(archive_id: str, request: Request):
        return service.read_report(runtime.require_principal(request), archive_id)

    return router
