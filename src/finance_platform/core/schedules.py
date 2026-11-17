"""UTC one-shot/interval plans; due occurrence and queue insertion are atomic."""
from contextlib import closing
from datetime import datetime, timezone
import json
import time
import uuid
import hashlib

from .auth import Principal, require_owner, require_writer
from .contracts import RunMode, identifier, parse_instant
from .errors import PlatformError
from .security import now_text, safe_json
from .database import install_immutable_guards


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z") if epoch is not None else None


class SchedulerService:
    def __init__(self, database, jobs, *, clock=time.time):
        self.database, self.jobs, self.clock = database, jobs, clock
        database.apply_module_migrations('scheduler_commands',("""
CREATE TABLE platform_schedule_commands(owner_id TEXT NOT NULL REFERENCES users(user_id),idempotency_key TEXT NOT NULL,request_hash TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(owner_id,idempotency_key));
""",))
        with database.transaction()as con:install_immutable_guards(con,('platform_schedule_commands',))

    def _cached(self,con,principal,key,request):
        if key is None:return None
        if type(key)is not str or not 1<=len(key)<=128:raise PlatformError('IDEMPOTENCY_KEY_REQUIRED',422)
        digest=hashlib.sha256(safe_json(request).encode()).hexdigest()
        row=con.execute('SELECT * FROM platform_schedule_commands WHERE owner_id=? AND idempotency_key=?',(principal.user_id,key)).fetchone()
        if row:
            if row['request_hash']!=digest:raise PlatformError('CONFLICT',409)
            return json.loads(row['response_json'])

    def _remember(self,con,principal,key,request,result):
        if key is not None:
            con.execute('INSERT INTO platform_schedule_commands VALUES(?,?,?,?,?)',(principal.user_id,key,hashlib.sha256(safe_json(request).encode()).hexdigest(),safe_json(result),now_text()))
        return result

    def _definition(self, principal, value):
        if type(value) is not dict or set(value) - {"name", "handler", "parameters", "mode", "rule", "interval_seconds", "start_at"}:
            raise PlatformError("INVALID_INPUT", 422)
        if type(value.get("name")) is not str or not 1 <= len(value["name"]) <= 100:
            raise PlatformError("INVALID_INPUT", 422)
        rule = value.get("rule")
        interval = value.get("interval_seconds")
        if rule not in {"ONCE", "INTERVAL"}:
            raise PlatformError("UNSUPPORTED_CAPABILITY", 422)
        if rule == "INTERVAL" and (type(interval) is not int or not 60 <= interval <= 366 * 86400):
            raise PlatformError("INVALID_INPUT", 422)
        if rule == "ONCE" and interval is not None:
            raise PlatformError("INVALID_INPUT", 422)
        try:
            start = parse_instant(value["start_at"]).timestamp()
        except (KeyError, ValueError, TypeError):
            raise PlatformError("INVALID_INPUT", 422) from None
        mode, definition, encoded = self.jobs._validate(principal, value.get("handler"), value.get("parameters", {}), value.get("mode", "BACKTEST"), None)
        return rule, interval, start, mode, definition, encoded

    def _event(self, con, schedule_id, kind, details=None):
        con.execute("INSERT INTO schedule_events(schedule_id,kind,recorded_at,details_json) VALUES (?,?,?,?)", (schedule_id, kind, now_text(), safe_json(details or {})))

    def _record(self, row):
        return {"schedule_id": row["schedule_id"], "owner_id": row["owner_id"], "name": row["name"], "handler": row["handler"],
                "parameters": json.loads(row["parameters_json"]), "mode": row["mode"], "rule": row["rule"], "interval_seconds": row["interval_seconds"],
                "next_due_at": _utc(row["next_due_at"]), "paused": bool(row["paused"]), "archived": bool(row["archived"]),
                "revision": row["revision"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def _owned(self, con, principal, schedule_id, *, write=False):
        row = con.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
        if not row:
            raise PlatformError("NOT_FOUND", 404)
        require_owner(principal, row["owner_id"], write=write)
        return row

    def create(self, principal, value,*,idempotency_key=None):
        require_writer(principal)
        rule, interval, start, mode, handler, encoded = self._definition(principal, value)
        schedule_id, stamp = str(uuid.uuid4()), now_text()
        with self.database.transaction() as con:
            request={'action':'create','definition':value}
            previous=self._cached(con,principal,idempotency_key,request)
            if previous is not None:return previous
            con.execute("INSERT INTO schedules(schedule_id,owner_id,name,handler,parameters_json,mode,rule,interval_seconds,next_due_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (schedule_id, principal.user_id, value["name"], handler.name, encoded, mode.value, rule, interval, start, stamp, stamp))
            self._event(con, schedule_id, "CREATED", {"rule": rule, "start_at": _utc(start)})
            self.database.audit(principal.user_id, "schedule.created", schedule_id, connection=con)
            return self._remember(con,principal,idempotency_key,request,self._record(self._owned(con,principal,schedule_id)))

    def get(self, principal, schedule_id):
        with closing(self.database.connect()) as con:
            return self._record(self._owned(con, principal, schedule_id))

    def list(self, principal, *, limit=50, offset=0):
        self.jobs._paging(limit, offset)
        predicate, args = ("1=1", ()) if principal.role == "admin" else ("owner_id=?", (principal.user_id,))
        with closing(self.database.connect()) as con:
            count = con.execute(f"SELECT count(*) FROM schedules WHERE {predicate}", args).fetchone()[0]
            rows = con.execute(f"SELECT * FROM schedules WHERE {predicate} ORDER BY created_at DESC,schedule_id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"items": [self._record(r) for r in rows], "total": count, "limit": limit, "offset": offset}

    def update(self, principal, schedule_id, value,*,idempotency_key=None):
        require_writer(principal)
        rule, interval, start, mode, handler, encoded = self._definition(principal, value)
        with self.database.transaction() as con:
            request={'action':'update','schedule_id':schedule_id,'definition':value}
            previous=self._cached(con,principal,idempotency_key,request)
            if previous is not None:return previous
            row = self._owned(con, principal, schedule_id, write=True)
            if row["archived"]:
                raise PlatformError("CONFLICT", 409)
            con.execute("UPDATE schedules SET name=?,handler=?,parameters_json=?,mode=?,rule=?,interval_seconds=?,next_due_at=?,revision=revision+1,updated_at=? WHERE schedule_id=?",
                        (value["name"], handler.name, encoded, mode.value, rule, interval, start, now_text(), schedule_id))
            self._event(con, schedule_id, "UPDATED", {"previous_revision": row["revision"], "definition": value})
            self.database.audit(principal.user_id, "schedule.updated", schedule_id, connection=con)
            return self._remember(con,principal,idempotency_key,request,self._record(self._owned(con,principal,schedule_id)))

    def set_paused(self, principal, schedule_id, paused):
        if type(paused) is not bool:
            raise PlatformError("INVALID_INPUT", 422)
        with self.database.transaction() as con:
            row = self._owned(con, principal, schedule_id, write=True)
            if row["archived"]:
                raise PlatformError("CONFLICT", 409)
            if not paused:
                self.jobs.registry.require_enabled(row["handler"])
            con.execute("UPDATE schedules SET paused=?,updated_at=? WHERE schedule_id=?", (int(paused), now_text(), schedule_id))
            self._event(con, schedule_id, "PAUSED" if paused else "RESUMED")
            self.database.audit(principal.user_id, "schedule.paused" if paused else "schedule.resumed", schedule_id, connection=con)
        return self.get(principal, schedule_id)

    def archive(self, principal, schedule_id):
        with self.database.transaction() as con:
            self._owned(con, principal, schedule_id, write=True)
            con.execute("UPDATE schedules SET archived=1,paused=1,updated_at=? WHERE schedule_id=?", (now_text(), schedule_id))
            self._event(con, schedule_id, "ARCHIVED")
            self.database.audit(principal.user_id, "schedule.archived", schedule_id, connection=con)
        return self.get(principal, schedule_id)

    def _trigger(self, con, row, *, key, due_at):
        old = con.execute("SELECT * FROM schedule_occurrences WHERE occurrence_key=?", (key,)).fetchone()
        if old:
            return {"schedule_id": row["schedule_id"], "job_id": old["job_id"], "status": old["status"], "error_code": old["error_code"], "occurrence_key": key}
        user = con.execute("SELECT * FROM users WHERE user_id=?", (row["owner_id"],)).fetchone()
        try:
            if not user or not user["enabled"]:
                raise PlatformError("FORBIDDEN", 403)
            principal = Principal(user["user_id"], user["username"], user["role"])
            mode, handler, encoded = self.jobs._validate(principal, row["handler"], json.loads(row["parameters_json"]), row["mode"], None)
            job = self.jobs._insert(con, row["owner_id"], handler, encoded, mode)
            status, job_id, code = "QUEUED", job["job_id"], None
        except PlatformError as error:
            status, job_id, code = "FAILED", None, error.code
        con.execute("INSERT INTO schedule_occurrences(schedule_id,occurrence_key,due_at,job_id,status,error_code,recorded_at) VALUES (?,?,?,?,?,?,?)",
                    (row["schedule_id"], key, due_at, job_id, status, code, now_text()))
        self._event(con, row["schedule_id"], "TRIGGERED" if job_id else "TRIGGER_FAILED", {"occurrence_key": key, "job_id": job_id, "error_code": code})
        return {"schedule_id": row["schedule_id"], "job_id": job_id, "status": status, "error_code": code, "occurrence_key": key}

    def trigger(self, principal, schedule_id, *, idempotency_key):
        identifier(idempotency_key)
        with self.database.transaction() as con:
            row = self._owned(con, principal, schedule_id, write=True)
            if row["archived"]:
                raise PlatformError("CONFLICT", 409)
            self.jobs.registry.require_enabled(row["handler"])
            result = self._trigger(con, row, key="manual:" + schedule_id + ":" + idempotency_key, due_at=self.clock())
            self.database.audit(principal.user_id, "schedule.triggered", schedule_id, {"occurrence_key": result["occurrence_key"]}, connection=con)
        if result["status"] == "FAILED":
            raise PlatformError(result["error_code"], 422)
        return result

    def tick(self, *, limit=100):
        self.jobs._paging(limit, 0)
        results = []
        with self.database.transaction() as con:
            now = self.clock()
            rows = con.execute("SELECT * FROM schedules WHERE paused=0 AND archived=0 AND next_due_at<=? ORDER BY next_due_at,schedule_id LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                due = row["next_due_at"]
                key = "due:" + row["schedule_id"] + ":" + str(row["revision"]) + ":" + format(due, ".6f")
                results.append(self._trigger(con, row, key=key, due_at=due))
                if row["rule"] == "ONCE":
                    next_due = None
                else:
                    # Coalesce downtime into one occurrence and advance to the
                    # first future interval. No unbounded missed-job storm.
                    intervals = int((now - due) // row["interval_seconds"]) + 1
                    next_due = due + intervals * row["interval_seconds"]
                    if intervals > 1:
                        self._event(con, row["schedule_id"], "MISSED_INTERVALS_COALESCED", {"skipped_intervals": intervals - 1})
                con.execute("UPDATE schedules SET next_due_at=?,updated_at=? WHERE schedule_id=?", (next_due, now_text(), row["schedule_id"]))
        return results

    def history(self, principal, schedule_id, *, limit=50, offset=0):
        self.jobs._paging(limit, offset)
        with closing(self.database.connect()) as con:
            self._owned(con, principal, schedule_id)
            total = con.execute("SELECT count(*) FROM schedule_occurrences WHERE schedule_id=?", (schedule_id,)).fetchone()[0]
            rows = con.execute("SELECT o.*,j.status AS job_status FROM schedule_occurrences o LEFT JOIN jobs j USING(job_id) WHERE schedule_id=? ORDER BY event_id DESC LIMIT ? OFFSET ?", (schedule_id, limit, offset)).fetchall()
        return {"items": [{"occurrence_key": r["occurrence_key"], "due_at": _utc(r["due_at"]), "job_id": r["job_id"],
                           "status": r["job_status"] or r["status"], "error_code": r["error_code"], "recorded_at": r["recorded_at"]} for r in rows],
                "total": total, "limit": limit, "offset": offset}
