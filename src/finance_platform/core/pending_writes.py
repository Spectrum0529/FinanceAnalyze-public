"""Low-sensitivity recovery over the durable receipts of supported writes."""

from contextlib import closing
from dataclasses import dataclass
import re
from urllib.parse import unquote, urlsplit

from .auth import AccountDelegation
from .contracts import identifier
from .database import immutable
from .errors import PlatformError
from .security import now_text

KEY_CLOSED = "PENDING_WRITE_KEY_CLOSED"
UNKNOWN = {"status": "UNKNOWN", "resource": None}
CLOSED = {"status": "CLOSED", "resource": None}
ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"

# Only these trusted receipt tables participate in closing an operation.
TABLES = {
    "jobs": ("job_keys", "owner_id", "idempotency_key"),
    "library": ("data_library_keys", "owner", "key"),
    "models": ("model_profile_commands", "owner_id", "idempotency_key"),
    "history": ("data_history_events", "owner_id", "idempotency_key"),
}


@dataclass(frozen=True)
class Operation:
    group: str
    action: str
    resource_id: str | None = None
    revision: int | None = None


def _operation(method, path):
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment or not path.startswith("/") or path.startswith("//"):
        raise ValueError
    path = unquote(parts.path)
    if method == "DELETE":
        profile = re.fullmatch(fr"/model-profiles/({ID})", path)
        revision = re.fullmatch(r"expected_revision=([1-9][0-9]{0,17})", parts.query)
        if profile is None or revision is None:
            raise ValueError
        return Operation("models", "delete", profile[1], int(revision[1]))
    if parts.query:
        raise ValueError
    if method == "PUT" and (profile := re.fullmatch(fr"/model-profiles/({ID})", path)):
        return Operation("models", "update", profile[1])
    if method != "POST":
        raise ValueError
    jobs = {
        "/data/library/import": "data.library-import",
        "/data/library/download": "data.library-download",
        "/research/portfolio/backtests": "research.dataset_task",
        "/research/tasks/start": "research.dataset_task",
    }
    if path in jobs:
        return Operation("jobs", jobs[path])
    if control := re.fullmatch(fr"/jobs/({ID})/(retry|resume)", path):
        return Operation("jobs", control[2], control[1])
    if path == "/data/library/connections":
        return Operation("library", "connection")
    if path == "/data/library/import-preview":
        return Operation("library", "upload")
    if managed := re.fullmatch(fr"/data/library/managed/(library|history)/({ID})/lifecycle", path):
        return Operation(managed[1], "lifecycle", managed[2])
    if connection := re.fullmatch(fr"/data/library/connections/({ID})/manage", path):
        return Operation("library", "manage_connection", connection[1])
    if dataset := re.fullmatch(fr"/data/library/datasets/({ID})/(trash|restore)", path):
        return Operation("library", dataset[2], dataset[1])
    if path == "/model-profiles":
        return Operation("models", "create")
    if profile := re.fullmatch(fr"/model-profiles/({ID})/copy", path):
        return Operation("models", "copy", profile[1])
    raise ValueError


def _present(connection, table):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def install_guards(database):
    database.apply_module_migrations("pending_write_control", ("""
        CREATE TABLE pending_write_fences(
            owner_id TEXT NOT NULL REFERENCES users(user_id),
            receipt_group TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            closed_at TEXT NOT NULL,
            PRIMARY KEY(owner_id, receipt_group, idempotency_key)
        );
    """,))
    with database.transaction() as connection:
        immutable(connection, "pending_write_fences", ("owner_id", "receipt_group", "idempotency_key"))
        enabled = {group for group, (table, _, _) in TABLES.items() if _present(connection, table)}
    for group in sorted(enabled):
        table, owner, key = TABLES[group]
        database.apply_module_migrations(f"pending_write_{group}", (f"""
            CREATE TRIGGER pending_write_closed_{group} BEFORE INSERT ON {table}
            WHEN EXISTS (
                SELECT 1 FROM pending_write_fences f
                WHERE f.owner_id=NEW.{owner} AND f.receipt_group='{group}' AND f.idempotency_key=NEW.{key}
            )
            BEGIN
                SELECT RAISE(ABORT, '{KEY_CLOSED}');
            END;
        """,))


def _safe_id(kind, value):
    if isinstance(value, str) and re.fullmatch(ID, value):
        return {"kind": kind, "id": value}
    return None


def _fenced(connection, fence):
    return connection.execute("""
        SELECT 1 FROM pending_write_fences
        WHERE owner_id=? AND receipt_group=? AND idempotency_key=?
    """, fence).fetchone() is not None


def require_open_key(connection, owner_id, group, key):
    """Check before preparing a write. The receipt trigger still fences the commit."""
    if key is not None and _present(connection, "pending_write_fences") and _fenced(connection, (owner_id, group, key)):
        raise PlatformError(KEY_CLOSED, 409)


def _job_receipt(connection, owner, key, operation):
    from .jobs import retry_command_hash

    row = connection.execute("""
        SELECT j.job_id,j.run_id,j.owner_id,j.handler,k.request_hash,r.parent_run_id
        FROM job_keys k JOIN jobs j USING(job_id) JOIN runs r USING(run_id)
        WHERE k.owner_id=? AND k.idempotency_key=? AND j.owner_id=?
    """, (owner, key, owner)).fetchone()
    if row is None:
        return None
    if operation.action in {"retry", "resume"}:
        confirmation = retry_command_hash(operation.resource_id, key, operation.action)
        old = connection.execute("SELECT run_id,handler FROM jobs WHERE job_id=? AND owner_id=?",
                                 (operation.resource_id, owner)).fetchone()
        if (old is None or row["request_hash"] != confirmation or row["parent_run_id"] != old["run_id"]
                or row["handler"] != old["handler"]):
            return None
    else:
        handlers = {"data.library-download", "data.wrds-history", "data.goal-prepare"} if operation.action == "data.library-download" else {operation.action}
        if row["handler"] not in handlers or row["parent_run_id"] is not None:
            return None
    return {"kind": "job", "id": row["job_id"], "job_id": row["job_id"],
            "run_id": row["run_id"], "owner_id": row["owner_id"]}


def _library_receipt(connection, owner, key, operation):
    row = connection.execute("""
        SELECT json_extract(response,'$.connection_id') AS connection_id,
               json_extract(response,'$.upload_id') AS upload_id,
               json_extract(response,'$.dataset_id') AS dataset_id,
               json_extract(response,'$.id') AS managed_id,
               json_extract(response,'$.source') AS source,
               json_extract(response,'$.revision') AS revision,
               json_type(response,'$.trashed') AS trashed,
               json_type(response,'$.physical_delete') AS physical_delete
        FROM data_library_keys WHERE owner=? AND key=?
    """, (owner, key)).fetchone()
    if row is None:
        return None
    if operation.action == "connection":
        return _safe_id("connection", row["connection_id"]) if row["revision"] is None else None
    if operation.action == "manage_connection":
        if (row["connection_id"] == operation.resource_id and row["physical_delete"] == "false"
                and type(row["revision"]) is int and row["revision"] > 0):
            return _safe_id("connection", row["connection_id"])
        return None
    if operation.action == "upload":
        return _safe_id("upload", row["upload_id"])
    if operation.action == "lifecycle":
        if (row["managed_id"] == operation.resource_id and row["source"] == "library"
                and row["physical_delete"] == "false" and type(row["revision"]) is int):
            return _safe_id("dataset", row["managed_id"])
        return None
    if (row["dataset_id"] == operation.resource_id and row["physical_delete"] == "false"
            and row["trashed"] == ("true" if operation.action == "trash" else "false")):
        return _safe_id("dataset", row["dataset_id"])
    return None


def _history_receipt(connection, owner, key, operation):
    row = connection.execute("""
        SELECT history_id FROM data_history_events
        WHERE owner_id=? AND idempotency_key=? AND history_id=?
    """, (owner, key, operation.resource_id)).fetchone()
    return _safe_id("dataset", row["history_id"]) if row is not None else None


def _model_receipt(connection, owner, key, operation):
    row = connection.execute("""
        SELECT json_extract(response_json,'$.profile_id') AS id,
               json_extract(response_json,'$.revision') AS revision,
               json_type(response_json,'$.deleted') AS deleted
        FROM model_profile_commands WHERE owner_id=? AND idempotency_key=?
    """, (owner, key)).fetchone()
    if row is None:
        return None
    ident, revision = row["id"], row["revision"]
    if operation.action == "delete":
        if ident == operation.resource_id and revision == operation.revision and row["deleted"] == "true":
            return _safe_id("model_profile", ident)
        return None
    if row["deleted"] == "true" or type(revision) is not int:
        return None
    if operation.action == "update":
        if ident != operation.resource_id or revision < 2:
            return None
    elif revision != 1:
        return None
    if operation.action == "copy":
        audit = connection.execute("""
            SELECT 1 FROM audit_events WHERE actor_id=? AND resource_id=? AND action='model_profile.copied'
            AND json_extract(details_json,'$.source_profile_id')=?
        """, (owner, ident, operation.resource_id)).fetchone()
    else:
        audit = connection.execute("""
            SELECT 1 FROM audit_events WHERE actor_id=? AND resource_id=? AND action='model_profile.saved'
            AND json_extract(details_json,'$.revision')=?
        """, (owner, ident, revision)).fetchone()
    return _safe_id("model_profile", ident) if audit is not None else None


def recover(database, principal, method, path, key, *, close=False):
    if isinstance(principal, AccountDelegation):
        raise PlatformError("MCP_SCOPE_REQUIRED", 403)
    try:
        identifier(key, "idempotency key")
        operation = _operation(method, path)
    except ValueError:
        raise PlatformError("INVALID_INPUT", 422) from None
    readers = {"jobs": _job_receipt, "library": _library_receipt, "models": _model_receipt,
               "history": _history_receipt}
    with database.transaction() if close else closing(database.connect()) as connection:
        table, owner_column, key_column = TABLES[operation.group]
        owner = principal.user_id
        fence = (owner, operation.group, key)
        if _present(connection, table):
            result = readers[operation.group](connection, owner, key, operation)
            if result is not None:
                return {"status": "COMMITTED", "resource": result}
            if close and connection.execute(
                f"SELECT 1 FROM {table} WHERE {owner_column}=? AND {key_column}=?", (owner, key)
            ).fetchone() is not None:
                raise PlatformError("CONFLICT", 409)
        if _fenced(connection, fence):
            return CLOSED
        if close:
            connection.execute("INSERT INTO pending_write_fences VALUES(?,?,?,?)", (*fence, now_text()))
            return CLOSED
    return UNKNOWN
