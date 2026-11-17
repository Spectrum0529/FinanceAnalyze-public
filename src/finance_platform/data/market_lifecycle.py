"""Display-only lifecycle and ordinary provider connection management."""
from contextlib import closing
import json
import re

from finance_platform.core.contracts import canonical, identifier
from finance_platform.core.database import immutable
from finance_platform.core.errors import PlatformError
from .contracts import DataError, validate_paging

MIGRATIONS = ("""
CREATE TABLE data_library_display_names(
    id TEXT NOT NULL REFERENCES data_library_versions(id),
    revision INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY(id,revision)
);
CREATE TABLE data_library_connection_events(
    id TEXT NOT NULL REFERENCES data_library_connections(id),
    revision INTEGER NOT NULL,
    body TEXT NOT NULL,
    archived INTEGER NOT NULL,
    PRIMARY KEY(id,revision)
);
""",)
CONNECTION_JOB = """FROM jobs WHERE owner_id=? AND
    coalesce(json_extract(parameters_json,'$.connection_id'),
             json_extract(parameters_json,'$.request.connection_id'))=?"""


def initialize(database):
    database.apply_module_migrations("data_market_management", MIGRATIONS)
    with database.transaction() as con:
        immutable(con, "data_library_display_names", ("id", "revision"))
        immutable(con, "data_library_connection_events", ("id", "revision"))


def checked_name(value, maximum):
    if (type(value) is not str or not 1 <= len(value.strip()) <= maximum
            or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in value)):
        raise DataError("MARKET_NAME_INVALID")
    return value.strip()


def revision_check(expected, current):
    if type(expected) is not int or expected < 0:
        raise DataError("MARKET_REVISION_INVALID")
    if expected != current:
        raise PlatformError("MARKET_REVISION_CONFLICT", 409)


def require_credential(runtime, owner, provider, reference):
    factory = getattr(runtime, "provider_credential_store_factory", None)
    if not callable(factory):
        raise DataError("CREDENTIAL_RESOLVER_UNCONFIGURED")
    store = factory()
    target = store.freeze_target(owner, provider, None)
    store.validate_reference_target(owner, reference, target=target)


def read_connection(con, owner, connection_id):
    identifier(owner)
    identifier(connection_id)
    row = con.execute(
        """SELECT coalesce(e.body,c.body),coalesce(e.archived,0),coalesce(e.revision,0)
           FROM data_library_connections c LEFT JOIN data_library_connection_events e
               ON e.id=c.id AND e.revision=(SELECT max(revision) FROM data_library_connection_events WHERE id=c.id)
           WHERE c.owner=? AND c.id=?""", (owner, connection_id),
    ).fetchone()
    if row is None:
        raise DataError("RESOURCE_NOT_FOUND")
    return {**json.loads(row[0]), "connection_id": connection_id, "archived": bool(row[1]), "revision": row[2]}


def reference_counts(con, owner, library):
    # Old externalized manifests have no SQL connection field. Read only
    # owner-scoped, registered dataset envelopes; never decode price chunks.
    counts = {}
    for row in con.execute("SELECT body FROM data_library_versions WHERE owner=?", (owner,)):
        try:
            connected = library.market.unpack(owner, row[0]).get("connection_id")
        except PlatformError as error:
            if error.code not in {"STORAGE_UNAVAILABLE", "STORAGE_ROOT_CHANGED"}:
                raise
            continue
        counts[connected] = counts.get(connected, 0) + 1
    return counts


def _job_counts(con, owner, ident):
    tasks = con.execute("SELECT count(*) " + CONNECTION_JOB, (owner, ident)).fetchone()[0]
    return tasks


def connections(library, owner, *, q="", state="active", limit=50, offset=0, with_references=True,
                delegated=False, publication_guard=None):
    identifier(owner)
    validate_paging(limit, offset)
    if state not in {"active", "archived", "all"} or type(q) is not str or len(q) > 120:
        raise DataError("INVALID_INPUT")
    needle = q.casefold().strip()
    with closing(library.db.connect()) as con:
        con.execute("BEGIN")
        if publication_guard is not None:
            publication_guard(con)
        query = """SELECT c.id,coalesce(e.body,c.body),coalesce(e.archived,0),coalesce(e.revision,0)
                   FROM data_library_connections c LEFT JOIN data_library_connection_events e
                       ON e.id=c.id AND e.revision=(SELECT max(revision) FROM data_library_connection_events WHERE id=c.id)
                   WHERE c.owner=? ORDER BY c.id"""
        matches = []
        total = 0
        for ident, payload, archived, revision in con.execute(query, (owner,)):
            if state != "all" and bool(archived) != (state == "archived"):
                continue
            body = json.loads(payload)
            if delegated and body["provider"] == "wrds":
                continue
            if needle and needle not in (body["name"] + " " + body["provider"]).casefold():
                continue
            if offset <= total < offset + limit:
                matches.append({**body, "connection_id": ident, "archived": bool(archived), "revision": revision})
            total += 1
        if matches and with_references:
            counts = reference_counts(con, owner, library)
            for item in matches:
                ident = item["connection_id"]
                item["reference_count"] = counts.get(ident, 0)
                item["job_reference_count"] = _job_counts(con, owner, ident)
    return {"items": matches, "total": total, "limit": limit, "offset": offset}


def change_connection(library, owner, ident, request, *, publication_guard=None, check_reference=None,
                      delegated=False):
    if type(request) is not dict or not {"action", "expected_revision", "idempotency_key"} <= request.keys():
        raise DataError("INVALID_INPUT")
    action = request["action"]
    if action not in {"edit", "archive", "restore"}:
        raise DataError("INVALID_INPUT")
    changes = set(request) - {"action", "expected_revision", "idempotency_key"}
    if action == "edit":
        if not changes or not changes <= {"name", "credential_ref"}:
            raise DataError("INVALID_INPUT")
    elif changes:
        raise DataError("INVALID_INPUT")

    def change(con):
        state = read_connection(con, owner, ident)
        if delegated and state["provider"] == "wrds":
            raise DataError("RESOURCE_NOT_FOUND")
        revision_check(request["expected_revision"], state["revision"])
        body = {key: state[key] for key in ("name", "provider", "credential_ref")}
        if "name" in changes:
            body["name"] = checked_name(request["name"], 100)
        if "credential_ref" in changes:
            ref = request["credential_ref"]
            if body["provider"] not in {"massive", "tushare"}:
                raise DataError("MARKET_CONNECTION_CREDENTIAL_UNSUPPORTED")
            if ref is not None:
                if type(ref) is not str or not re.fullmatch(r"credential:FA_[0-9a-f]{64}", ref):
                    raise DataError("CREDENTIAL_REFERENCE_REQUIRED")
                if not callable(check_reference):
                    raise DataError("CREDENTIAL_RESOLVER_UNCONFIGURED")
                check_reference(owner, body["provider"], ref)
            if con.execute("SELECT 1 " + CONNECTION_JOB +
                           " AND status IN ('QUEUED','RUNNING') LIMIT 1", (owner, ident)).fetchone():
                raise PlatformError("MARKET_CONNECTION_IN_USE", 409)
            body["credential_ref"] = ref
        archived = action == "archive" or action == "edit" and state["archived"]
        revision = state["revision"] + 1
        references = reference_counts(con, owner, library)
        con.execute("INSERT INTO data_library_connection_events VALUES(?,?,?,?)",
                    (ident, revision, canonical(body), int(archived)))
        return {**body, "connection_id": ident, "archived": archived, "revision": revision,
                "reference_count": references.get(ident, 0), "job_reference_count": _job_counts(con, owner, ident),
                "physical_delete": False}

    return library._write(owner, request["idempotency_key"], {"operation": "connection", "id": ident,
                          **{key: value for key, value in request.items() if key != "idempotency_key"}},
                          change, publication_guard=publication_guard)
