"""Owner-scoped saved report state with atomic bulk changes."""
import json
import re
from typing import Literal

from pydantic import Field

from .auth import AccountDelegation, require_writer
from .contracts import fingerprint, identifier
from .errors import PlatformError
from .security import now_text
from .team_profiles import Strict

SOURCES = ("research", "daily", "tasks", "jobs", "autonomous", "migration")
MIGRATIONS = ("""
CREATE TABLE result_lifecycle(
 owner_id TEXT NOT NULL REFERENCES users(user_id), result_id TEXT NOT NULL,
 name TEXT NOT NULL, deleted INTEGER NOT NULL CHECK(deleted IN(0,1)),
 revision INTEGER NOT NULL CHECK(revision>=1), updated_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,result_id));
CREATE TABLE result_lifecycle_commands(
 owner_id TEXT NOT NULL REFERENCES users(user_id), idempotency_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,idempotency_key));
""",)
RESULT_ID = re.compile(r"(research|daily|tasks|jobs|autonomous|migration):[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class LifecycleTarget(Strict):
    result_id: str = Field(pattern=rf"^{RESULT_ID.pattern}$")
    expected_revision: int = Field(ge=0, le=2147483647)


class LifecycleCommand(Strict):
    action: Literal["rename", "delete", "restore"]
    expected_revision: int = Field(ge=0, le=2147483647)
    idempotency_key: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=120)


class BatchLifecycleCommand(Strict):
    action: Literal["delete", "restore"]
    targets: list[LifecycleTarget] = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=128)


def table_exists(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def visible_clause(source, run_expression, owner_expression):
    """SQL predicate for a fixed source and native owner/run columns."""
    if source not in SOURCES:
        raise ValueError("unknown result source")
    if source == "daily":
        return ("NOT EXISTS(SELECT 1 FROM research_daily_result_management state "
                f"WHERE state.owner_id={owner_expression} AND state.run_id={run_expression} AND state.deleted=1)")
    return ("NOT EXISTS(SELECT 1 FROM result_lifecycle state "
            f"WHERE state.owner_id={owner_expression} AND state.result_id='{source}:'||{run_expression} AND state.deleted=1)")


def state_for(con, owner_id, source, run_id):
    if source not in SOURCES:
        raise ValueError("unknown result source")
    table = "research_daily_result_management" if source == "daily" else "result_lifecycle"
    if table_exists(con, table):
        key, value = ("run_id", run_id) if source == "daily" else ("result_id", f"{source}:{run_id}")
        row = con.execute(f"SELECT name,deleted,revision FROM {table} WHERE owner_id=? AND {key}=?",
                          (owner_id, value)).fetchone()
        if row is not None:
            return {"name": row["name"], "deleted": bool(row["deleted"]), "revision": row["revision"]}
    return {"name": None, "deleted": False, "revision": 0}


def active_result(con, owner_id, result_id):
    if type(result_id) is not str or not RESULT_ID.fullmatch(result_id):
        return False
    source, run_id = result_id.split(":", 1)
    return not state_for(con, owner_id, source, run_id)["deleted"]


class ResultLifecycle:
    def __init__(self, index):
        self.index = index
        self.database = index.database
        self.database.apply_module_migrations("result_lifecycle", MIGRATIONS)

    def change(self, principal, *, action, targets, idempotency_key, name=None, batch=False):
        require_writer(principal)
        try:
            identifier(idempotency_key)
        except ValueError:
            raise PlatformError("INVALID_INPUT", 422) from None
        if (action not in {"rename", "delete", "restore"} or not 1 <= len(targets) <= 100
                or len({target.result_id for target in targets}) != len(targets)):
            raise PlatformError("INVALID_INPUT", 422)
        if action == "rename":
            if batch or type(name) is not str or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
                raise PlatformError("INVALID_INPUT", 422)
            name = name.strip()
        elif name is not None:
            raise PlatformError("INVALID_INPUT", 422)
        request_hash = fingerprint({
            "batch": batch, "action": action, "name": name,
            "targets": [target.model_dump() for target in targets],
        })
        owner = principal.user_id
        with self.database.transaction() as con:
            if isinstance(principal, AccountDelegation):
                principal.check_approval(con)
            rows = [self.index._report_row(con, owner, target.result_id) for target in targets]
            previous = con.execute("""SELECT request_hash,response_json FROM result_lifecycle_commands
                                      WHERE owner_id=? AND idempotency_key=?""", (owner, idempotency_key)).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise PlatformError("CONFLICT", 409)
                response = json.loads(previous["response_json"])
            else:
                if any(row["revision"] != target.expected_revision for row, target in zip(rows, targets)):
                    raise PlatformError("CONFLICT", 409)
                now = now_text()
                items = [self._write(con, row, action, name, now) for row in rows]
                response = {"items": items, "physical_delete": False} if batch else items[0]
                con.execute("INSERT INTO result_lifecycle_commands VALUES(?,?,?,?,?)",
                            (owner, idempotency_key, request_hash, json.dumps(response, ensure_ascii=False), now))
        return self._receipt(principal, rows, response, batch)

    def _receipt(self, principal, rows, response, batch):
        if not isinstance(principal, AccountDelegation):
            return response
        items = response["items"] if batch else [response]
        projected = [
            item if self.index._name_authorized(principal, row, inspect_deleted=True)
            else item | {"name": self.index._item(principal, row)["name"]}
            for row, item in zip(rows, items, strict=True)
        ]
        return response | {"items": projected} if batch else projected[0]

    def _write(self, con, row, action, name, now):
        source, run, owner = row["source"], row["run_id"], row["owner_id"]
        result_id = f"{source}:{run}"
        current_name = name if action == "rename" else row["name"]
        deleted = bool(row["deleted"]) if action == "rename" else action == "delete"
        revision = row["revision"] + 1
        if source == "daily":
            con.execute("""INSERT INTO research_daily_result_management VALUES(?,?,?,?,?,?)
                           ON CONFLICT(run_id) DO UPDATE SET name=excluded.name,deleted=excluded.deleted,
                           revision=excluded.revision,updated_at=excluded.updated_at
                           WHERE research_daily_result_management.owner_id=excluded.owner_id""",
                        (run, owner, current_name, int(deleted), revision, now))
        else:
            con.execute("""INSERT INTO result_lifecycle VALUES(?,?,?,?,?,?)
                           ON CONFLICT(owner_id,result_id) DO UPDATE SET name=excluded.name,deleted=excluded.deleted,
                           revision=excluded.revision,updated_at=excluded.updated_at""",
                        (owner, result_id, current_name, int(deleted), revision, now))
        self.database.audit(owner, f"result.{action}", result_id, {"revision": revision}, connection=con)
        return {"result_id": result_id, "name": current_name, "deleted": deleted, "revision": revision,
                "physical_delete": False}
