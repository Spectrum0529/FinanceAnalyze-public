"""Durable, owner-scoped goal research over product Jobs and trusted adapters."""
from contextlib import closing
from datetime import date
from hashlib import sha256
import json
import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .auth import Principal, require_writer
from .autonomous_models import Strict
from .contracts import RunMode
from .database import install_immutable_guards
from .autonomous_models import ResearchRole, TeamMemberRole
from .errors import PlatformError
from .model_usage import TOKEN_FIELDS, observation
from .pagination import validate_pagination
from .security import now_text, safe_json

HANDLER = "research.goal.advance"
STAGES = ("PLAN", "DATA", "FREEZE", "START", "RESEARCH", "REVIEW")
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
COUNTERS = {"model_requests", "tokens", "download_jobs", "download_rows", "research_attempts",
            "compute_seconds", "cost_microunits", "artifact_bytes", "prefixes",
            "download_requests", "download_bytes"}
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Reason = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
MIGRATIONS = ("""
CREATE TABLE goal_tasks(
 task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(user_id), goal TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, deadline REAL, spent_json TEXT NOT NULL,
 outputs_json TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
 revision INTEGER NOT NULL, reason_code TEXT, job_id TEXT, child_json TEXT,
 effect_id TEXT, control_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX goal_tasks_owner ON goal_tasks(owner_id, created_at, task_id);
CREATE INDEX goal_tasks_status ON goal_tasks(status, updated_at);
CREATE TABLE goal_defaults(
 owner_id TEXT PRIMARY KEY REFERENCES users(user_id), revision INTEGER NOT NULL, configuration_json TEXT NOT NULL);
CREATE TABLE goal_commands(
 owner_id TEXT NOT NULL REFERENCES users(user_id), command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 response_json TEXT NOT NULL, PRIMARY KEY(owner_id, command_key));
CREATE TABLE goal_effects(
 effect_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES goal_tasks(task_id), stage TEXT NOT NULL,
 job_id TEXT NOT NULL, grant_json TEXT NOT NULL, started_at TEXT NOT NULL);
CREATE TABLE goal_children(
 kind TEXT NOT NULL, child_id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES goal_tasks(task_id),
 effect_id TEXT NOT NULL REFERENCES goal_effects(effect_id), PRIMARY KEY(kind,child_id));
CREATE UNIQUE INDEX goal_children_effect ON goal_children(effect_id);
CREATE TABLE goal_effect_receipts(
 effect_id TEXT PRIMARY KEY REFERENCES goal_effects(effect_id), result_json TEXT NOT NULL, recorded_at TEXT NOT NULL);
""",)


class Child(Strict):
    kind: Literal["job", "session"]
    id: Identifier


class StepResult(Strict):
    kind: Literal["DONE", "PENDING", "WAITING", "FAILED", "MORE_DATA"]
    output: dict | None = None
    child: Child | None = None
    reason_code: Reason | None = None
    model_observation: dict | None = None

    @field_validator("model_observation", mode="before")
    @classmethod
    def confirmed_observation(cls, value):
        if value is not None:
            try:
                return observation(value)
            except (TypeError, ValueError, PlatformError):
                return None  # Optional evidence never changes a stage outcome.
        return None

    @model_validator(mode="after")
    def consistent(self):
        if ((self.kind == "PENDING") != (self.child is not None)
                or (self.kind in {"DONE", "MORE_DATA"}) != (self.output is not None)
                or (self.kind in {"WAITING", "FAILED"}) != (self.reason_code is not None)):
            raise ValueError("invalid goal stage result")
        return self


class ProfileDefault(Strict):
    profile_id: Identifier
    revision: int = Field(ge=1)


class GoalDefaults(Strict):
    planner: ProfileDefault | None = None
    model_profile: ProfileDefault | None = None
    team_snapshot_id: Identifier | None = None
    team_role_map: dict[ResearchRole, TeamMemberRole] = Field(default_factory=dict, max_length=4)
    connection_ids: list[Identifier] = Field(default_factory=list,max_length=8)
    budget_preset_id: Identifier | None = None

    @model_validator(mode="after")
    def consistent(self):
        if ((self.model_profile and self.team_snapshot_id)
                or (self.team_role_map and not self.team_snapshot_id)
                or len(self.connection_ids) != len(set(self.connection_ids))):
            raise ValueError("ambiguous goal defaults")
        return self


class SaveGoalDefaults(Strict):
    defaults: GoalDefaults
    expected_revision: int = Field(ge=0)
    idempotency_key: Identifier


class CreateGoal(Strict):
    goal: Annotated[str, Field(min_length=1, max_length=2000)]
    idempotency_key: Identifier


class ControlGoal(Strict):
    action: Literal["pause", "cancel", "resume"]
    expected_revision: int = Field(ge=1)
    idempotency_key: Identifier


def _json(value, limit=65536):
    text = safe_json(value)
    if len(text.encode()) > limit:
        raise PlatformError("GOAL_PAYLOAD_TOO_LARGE", 422)
    return text


def _conflict():
    raise PlatformError("CONFLICT", 409)


class GoalResearch:
    def __init__(self, database, *, enqueue, defaults, step, poll, forward,
                 readiness=None, validate_defaults=None, clock=time.time, development_bounds=None):
        self.database = database
        self.enqueue = enqueue
        self.defaults = defaults
        self.step = step
        self.poll = poll
        self.forward = forward
        self.readiness_port = readiness
        self.validate_defaults = validate_defaults
        self.clock = clock
        self.development_bounds = development_bounds

    def migrate(self):
        self.database.apply_module_migrations("goal_research", MIGRATIONS)
        with self.database.transaction() as con:
            install_immutable_guards(con, ("goal_commands", "goal_effects", "goal_effect_receipts", "goal_children"))

    def _row(self, con, principal, task_id):
        row = con.execute("SELECT * FROM goal_tasks WHERE task_id=? AND owner_id=?",
                          (task_id, principal.user_id)).fetchone()
        if row is None:
            raise PlatformError("NOT_FOUND", 404)
        return row

    def get_defaults(self, principal):
        with closing(self.database.connect()) as con:
            row = con.execute("SELECT * FROM goal_defaults WHERE owner_id=?",(principal.user_id,)).fetchone()
        return {"revision":row["revision"],"defaults":GoalDefaults.model_validate(
            json.loads(row["configuration_json"])).model_dump()} if row else {
            "revision":0,"defaults":GoalDefaults().model_dump(),
        }

    def save_defaults(self, principal, body):
        require_writer(principal)
        request = SaveGoalDefaults.model_validate(body)
        key = request.idempotency_key
        command = {"defaults":request.model_dump()}
        if not request.defaults.team_role_map:
            command["defaults"]["defaults"].pop("team_role_map")
        with closing(self.database.connect()) as con:
            _, cached = self._command(con,principal.user_id,key,command)
            if cached is not None:
                return cached
        if self.validate_defaults is None:
            raise PlatformError("GOAL_DEFAULTS_UNAVAILABLE",503)
        self.validate_defaults(principal,request.defaults)
        with self.database.transaction() as con:
            digest,cached = self._command(con,principal.user_id,key,command)
            if cached is not None:
                return cached
            row = con.execute("SELECT revision FROM goal_defaults WHERE owner_id=?",(principal.user_id,)).fetchone()
            if (row["revision"] if row else 0) != request.expected_revision:
                _conflict()
            revision = request.expected_revision+1
            value = request.defaults.model_dump()
            con.execute("""INSERT INTO goal_defaults VALUES (?,?,?)
                ON CONFLICT(owner_id) DO UPDATE SET revision=excluded.revision,
                configuration_json=excluded.configuration_json""",(principal.user_id,revision,_json(value)))
            return self._remember(con,principal,key,digest,{"revision":revision,"defaults":value})

    def readiness(self, principal):
        settings = self.get_defaults(principal)
        value = (self.readiness_port(principal,settings["defaults"]) if self.readiness_port else
                 {"ready":False,"blockers":[{"code":"GOAL_DEFAULTS_UNAVAILABLE","setting":"executor"}]})
        response = {**value,**settings,"can_create":principal.role in {"admin","researcher"}}
        _json(response)
        return response

    def _snapshot(self, principal, settings, goal):
        try:
            snapshot = self.defaults(principal,settings["defaults"])
        except PlatformError as error:
            if error.code not in {"GOAL_DEFAULTS_UNAVAILABLE","GOAL_MODEL_REQUIRED","GOAL_SOURCE_REQUIRED",
                                  "GOAL_BUDGET_REQUIRED","GOAL_CREDENTIAL_REQUIRED","AUTONOMOUS_DEPLOYMENT_UNAVAILABLE"}:
                raise
            return {},error.code
        delay = snapshot["deadline_seconds"]
        limits, grants = snapshot["limits"], snapshot["grants"]
        if type(delay) is not int or not 60 <= delay <= 86400 or not limits or not set(grants) <= set(STAGES):
            raise PlatformError("GOAL_DEFAULTS_INVALID", 422)
        for budget in (limits, *grants.values()):
            if not set(budget) <= COUNTERS or any(type(v) is not int or v < 0 for v in budget.values()):
                raise PlatformError("GOAL_DEFAULTS_INVALID", 422)
        if any(amount > limits.get(name, 0) for grant in grants.values() for name, amount in grant.items()):
            raise PlatformError("GOAL_DEFAULTS_INVALID", 422)
        if self.development_bounds is not None:
            bounds = self.development_bounds(principal, goal, snapshot)
            snapshot = {**snapshot, "development_bounds":bounds}
        return {**snapshot,"defaults_revision":settings["revision"]},None

    def _view(self, row):
        child = json.loads(row["child_json"]) if row["child_json"] else None
        outputs = json.loads(row["outputs_json"])
        snapshot = json.loads(row["snapshot_json"])
        limits = snapshot.get("limits", {})
        spent = json.loads(row["spent_json"])
        actions = []
        if row["status"] in {"QUEUED","EXECUTING","CHILD_RUNNING"}:
            actions = ["pause","cancel"]
        elif row["status"] == "RESUME_REQUESTED":
            actions = ["cancel"]
        elif row["status"] in {"PAUSED","WAITING"}:
            uncertain = row["reason_code"] == "GOAL_EFFECT_UNCERTAIN"
            if not uncertain or child and child["kind"] == "session":
                actions = ["cancel"]
            if not uncertain and (row["deadline"] is None or self.clock() < row["deadline"]):
                actions.insert(0,"resume")
        if "FREEZE" in outputs:
            outputs["FREEZE"] = {"spec_hash": outputs["FREEZE"]["spec_hash"]}
        for entry in outputs.get("additional_data", []):
            if entry.get("prior_freeze") is not None:
                entry["prior_freeze"] = {"spec_hash": entry["prior_freeze"]["spec_hash"]}
        return {key: row[key] for key in ("task_id", "goal", "status", "stage", "revision",
                                          "reason_code", "job_id", "deadline", "created_at", "updated_at")} | {
            "child": child, "spent": spent, "defaults_revision": snapshot.get("defaults_revision"),
            "progress": outputs, "limits":limits,
            "remaining":{k: v-spent[k] for k,v in limits.items()}, "allowed_actions":actions,
            "billing_cost":None, "billing_status":"UNKNOWN",
        }

    def get(self, principal, task_id):
        with closing(self.database.connect()) as con:
            return self._view(self._row(con, principal, task_id))

    def model_usage(self, principal, task_id, *, limit=50, offset=0):
        validate_pagination(limit, offset)
        if offset > 10000:
            raise PlatformError("INVALID_INPUT", 422)
        with closing(self.database.connect()) as con:
            con.execute("BEGIN")
            self._row(con, principal, task_id)
            effects = con.execute("""SELECT e.effect_id,e.stage,
                       CASE WHEN j.job_id IS NOT NULL THEN r.result_json END AS receipt
                FROM goal_effects e LEFT JOIN goal_effect_receipts r USING(effect_id)
                LEFT JOIN jobs j ON j.job_id=e.job_id AND j.owner_id=? AND j.handler=?
                    AND json_extract(j.parameters_json,'$.task_id')=e.task_id
                WHERE e.task_id=? AND e.stage IN ('PLAN','REVIEW')
                ORDER BY e.started_at,e.effect_id LIMIT ? OFFSET ?""",
                (principal.user_id, HANDLER, task_id, limit + 1, offset)).fetchall()
            study_sessions = con.execute("""SELECT c.child_id FROM goal_children c
                JOIN goal_effects e ON e.effect_id=c.effect_id AND e.task_id=c.task_id AND e.stage='START'
                WHERE c.task_id=? AND c.kind='session' ORDER BY e.started_at,c.child_id LIMIT 2""", (task_id,)).fetchall()
        items = []
        for row in effects[:limit]:
            metrics = None
            if row["receipt"]:
                receipt = json.loads(row["receipt"])
                value = StepResult.confirmed_observation(receipt.get("model_observation"))
                if value is not None:
                    metrics = value["metrics"]
                    if metrics is not None:
                        if not metrics.pop("known_model_context_window_source", None):
                            metrics["known_model_context_window"] = None
                        if metrics["usage_source"] is None:
                            for key in TOKEN_FIELDS:
                                metrics[key] = None
                        if metrics["output_tokens"] is None or not metrics["elapsed_ms"]:
                            metrics["output_tokens_per_second"] = None
            available = metrics is not None and any(metrics[key] is not None for key in TOKEN_FIELDS)
            items.append({"effect_id":row["effect_id"], "stage":row["stage"],
                          "availability":"RECORDED" if available else "UNKNOWN", "metrics":metrics})
        return {"schema":"goal-model-usage-1", "task_id":task_id, "model_observations":items,
                "research_sessions":[row["child_id"] for row in study_sessions], "limit":limit, "offset":offset,
                "next_offset":offset + limit if len(effects) > limit and offset + limit <= 10000 else None}

    def list(self, principal, *, limit=50, offset=0):
        validate_pagination(limit, offset)
        with closing(self.database.connect()) as con:
            rows = con.execute("SELECT * FROM goal_tasks WHERE owner_id=? ORDER BY created_at DESC,task_id LIMIT ? OFFSET ?",
                               (principal.user_id, limit, offset)).fetchall()
        return {"items": [self._view(row) for row in rows], "limit": limit, "offset": offset}

    @staticmethod
    def _command(con, owner, key, request):
        digest = sha256(_json(request).encode()).hexdigest()
        row = con.execute("SELECT request_hash,response_json FROM goal_commands WHERE owner_id=? AND command_key=?",
                          (owner, key)).fetchone()
        if row and row["request_hash"] != digest:
            _conflict()
        return digest, json.loads(row["response_json"]) if row else None

    @staticmethod
    def _remember(con, principal, key, digest, value):
        con.execute("INSERT INTO goal_commands VALUES (?,?,?,?)",
                    (principal.user_id, key, digest, _json(value, 131072)))
        return value

    def create(self, principal, goal, key, *, approve_snapshot=None, approval_ref=None):
        require_writer(principal)
        body = CreateGoal(goal=goal.strip(), idempotency_key=key)
        request = {"create": body.goal}
        if approve_snapshot is not None:
            request["delegated_approval"] = approval_ref
        with (self.database.transaction() if approve_snapshot is not None else closing(self.database.connect())) as con:
            _, cached = self._command(con, principal.user_id, key, request)
            if cached is not None:
                return cached
        snapshot, reason = self._snapshot(principal,self.get_defaults(principal),body.goal)
        task_id = str(uuid4())
        with self.database.transaction() as con:
            digest, cached = self._command(con, principal.user_id, key, request)
            if cached is not None:
                return cached
            if approve_snapshot is not None:
                approve_snapshot(snapshot, reason)
            stamp = now_text()
            deadline = self.clock()+snapshot["deadline_seconds"] if snapshot else None
            status = "QUEUED" if snapshot else "WAITING"
            con.execute("""INSERT INTO goal_tasks
                (task_id,owner_id,goal,snapshot_json,deadline,spent_json,outputs_json,status,stage,
                 revision,reason_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'PLAN',1,?,?,?)""",
                        (task_id,principal.user_id,body.goal,_json(snapshot),deadline,
                         _json({name:0 for name in snapshot.get("limits",{})}),"{}",status,reason,stamp,stamp))
            if snapshot:
                job = self.enqueue(con, principal, task_id)
                con.execute("UPDATE goal_tasks SET job_id=? WHERE task_id=?", (job,task_id))
            return self._remember(con,principal,key,digest,self._view(self._row(con,principal,task_id)))

    def _set(self, con, row, *, status, stage=None, reason=None, job=None, child=None,
             effect=None, control=None, outputs=None):
        con.execute("""UPDATE goal_tasks SET status=?,stage=?,reason_code=?,job_id=?,child_json=?,
                    effect_id=?,control_id=?,outputs_json=?,revision=revision+1,updated_at=? WHERE task_id=?""",
                    (status,stage or row["stage"],reason,job,_json(child) if child else None,effect,control,
                     _json(outputs) if outputs is not None else row["outputs_json"],now_text(),row["task_id"]))

    def control(self, principal, task_id, action, expected_revision, key, *, approve_snapshot=None, approval_ref=None):
        require_writer(principal)
        command = ControlGoal(action=action,expected_revision=expected_revision,idempotency_key=key)
        request = {"task_id":task_id, **command.model_dump()}
        if approve_snapshot is not None:
            request["delegated_approval"] = approval_ref
        replacement = None
        if action == "resume":
            with (self.database.transaction() if approve_snapshot is not None else closing(self.database.connect())) as con:
                previous = self._row(con,principal,task_id)
                _, cached = self._command(con,principal.user_id,key,request)
                if cached is not None:
                    return cached
            if previous["deadline"] is None and previous["revision"] == expected_revision:
                replacement = self._snapshot(principal,self.get_defaults(principal),previous["goal"])
        with self.database.transaction() as con:
            row = self._row(con,principal,task_id)
            digest,cached = self._command(con,principal.user_id,key,request)
            if cached is not None:
                return cached
            if row["revision"] != expected_revision or action not in self._view(row)["allowed_actions"]:
                _conflict()
            child = json.loads(row["child_json"]) if row["child_json"] else None
            if action == "resume":
                if row["status"] not in {"PAUSED","WAITING"} or row["reason_code"] == "GOAL_EFFECT_UNCERTAIN":
                    _conflict()
                if row["deadline"] is None:
                    snapshot,reason = replacement
                    if approve_snapshot is not None:
                        approve_snapshot(snapshot, reason)
                    if reason:
                        self._set(con,row,status="WAITING",reason=reason)
                        return self._remember(con,principal,key,digest,self._view(self._row(con,principal,task_id)))
                    con.execute("UPDATE goal_tasks SET snapshot_json=?,deadline=?,spent_json=? WHERE task_id=?",
                                (_json(snapshot),self.clock()+snapshot["deadline_seconds"],
                                 _json({name:0 for name in snapshot["limits"]}),task_id))
                else:
                    if self.clock() >= row["deadline"]:
                        _conflict()
                    if approve_snapshot is not None:
                        approve_snapshot(json.loads(row["snapshot_json"]), None)
                if child:
                    self._set(con,row,status="RESUME_REQUESTED",child=child,effect=row["effect_id"],control=str(uuid4()))
                else:
                    job = self.enqueue(con,principal,task_id)
                    self._set(con,row,status="QUEUED",job=job)
            else:
                intent = "PAUSE_REQUESTED" if action == "pause" else "CANCEL_REQUESTED"
                settled = "PAUSED" if action == "pause" else "CANCELLED"
                if child or row["status"] == "EXECUTING":
                    self._set(con,row,status=intent,job=row["job_id"],child=child,
                              effect=row["effect_id"],control=str(uuid4()))
                else:
                    self._set(con,row,status=settled)
            return self._remember(con,principal,key,digest,self._view(self._row(con,principal,task_id)))

    def _request(self, row):
        outputs = json.loads(row["outputs_json"])
        request = {"task_id":row["task_id"],"goal":row["goal"],"stage":row["stage"],"deadline":row["deadline"],
                   "operation_key":row["effect_id"],"snapshot":json.loads(row["snapshot_json"]),"outputs":outputs}
        if row["stage"] == "DATA":
            additional = outputs.get("additional_data", [])
            needs = outputs.get("PLAN", {}).get("data_needs", [])
            if additional:
                request["data_need"] = additional[-1]["result"]["need"]
                request["previous_data"] = additional[-1]["prior_data"]["reference"]
            elif len(needs) == 1:
                request["data_need"] = needs[0]
        return request

    @staticmethod
    def _within_budget(snapshot, spent, *stages):
        requested = {}
        for stage in stages:
            for name, amount in snapshot["grants"].get(stage, {}).items():
                requested[name] = requested.get(name, 0) + amount
        return all(spent.get(name, 0) + amount <= snapshot["limits"].get(name, 0)
                   for name, amount in requested.items())

    @staticmethod
    def _review_need_valid(snapshot, outputs, value):
        keys = {"market", "asset", "kind", "frequency", "adjustment", "currency", "symbols", "start", "end"}
        need = value.get("need")
        prior = outputs["PLAN"]["data_needs"][0]
        if (set(value) != {"need", "reason_code"} or not isinstance(need, dict) or set(need) != keys
                or value["reason_code"] not in ("HISTORY_SENSITIVITY", "LONGER_DEVELOPMENT_SAMPLE")
                or any(need[field] != prior[field] for field in keys - {"start", "end", "symbols"})
                or not isinstance(need["symbols"], list) or any(type(symbol) is not str for symbol in need["symbols"])
                or sorted(need["symbols"]) != sorted(prior["symbols"])):
            return False
        bounds = snapshot.get("development_bounds") or {}
        try:
            start, end = (date.fromisoformat(need[key]) for key in ("start", "end"))
            lower, upper = (date.fromisoformat(bounds[key]) for key in ("start", "end"))
            old_start, old_end = (date.fromisoformat(prior[key]) for key in ("start", "end"))
        except (KeyError, TypeError, ValueError):
            return False
        planner = snapshot.get("planner_input", {})
        max_days = planner.get("defaults", {}).get("max_days", 366)
        as_of = planner.get("as_of", "")
        return (need["start"] == start.isoformat() and need["end"] == end.isoformat()
                and lower <= start < old_start <= old_end == end <= upper and bounds["end"] < as_of
                and (end - start).days + 1 <= max_days)

    def handle(self, context, parameters):
        task_id = parameters["task_id"]
        with self.database.transaction() as con:
            user = con.execute("SELECT * FROM users WHERE user_id=? AND enabled=1",(context.run.owner_id,)).fetchone()
            if user is None:
                raise PlatformError("FORBIDDEN",403)
            principal = Principal(user["user_id"],user["username"],user["role"])
            require_writer(principal)
            row = self._row(con,principal,task_id)
            if row["job_id"] != context.job_id or row["status"] != "QUEUED":
                return {"task_id":task_id}
            context.assert_fenced(con)
            if self.clock() >= row["deadline"]:
                self._set(con,row,status="FAILED",reason="GOAL_DEADLINE_EXCEEDED")
                return {"task_id":task_id}
            snapshot = json.loads(row["snapshot_json"])
            spent = json.loads(row["spent_json"])
            grant = snapshot["grants"].get(row["stage"], {})
            if not self._within_budget(snapshot, spent, row["stage"]):
                self._set(con,row,status="FAILED",reason="GOAL_BUDGET_EXCEEDED")
                return {"task_id":task_id}
            for name, amount in grant.items():
                spent[name] = spent.get(name,0)+amount
            effect = str(uuid4())
            con.execute("UPDATE goal_tasks SET spent_json=? WHERE task_id=?",(_json(spent),task_id))
            con.execute("INSERT INTO goal_effects VALUES (?,?,?,?,?,?)",
                        (effect,task_id,row["stage"],context.job_id,_json(grant),now_text()))
            self._set(con,row,status="EXECUTING",job=context.job_id,effect=effect)
            request = self._request(self._row(con,principal,task_id))
        context.require_active()
        def active(connection=None):
            if connection is None:
                with closing(self.database.connect()) as reader:
                    return active(reader)
            context.assert_fenced(connection)
            current = self._row(connection,principal,task_id)
            if current["status"] != "EXECUTING" or current["effect_id"] != effect:
                raise PlatformError("GOAL_CONTROL_REQUESTED",409)
            if self.clock() >= current["deadline"]:
                raise PlatformError("GOAL_DEADLINE_EXCEEDED",409)

        def link(connection, child):
            active(connection)
            self.bind_child(connection,principal,task_id,effect,Child.model_validate(child))

        result = StepResult.model_validate(self.step(principal,request,grant,active,link))
        with self.database.transaction() as con:
            context.assert_fenced(con)
            row = self._row(con,principal,task_id)
            if row["effect_id"] != effect or row["job_id"] != context.job_id:
                _conflict()
            receipt = result.model_dump(exclude={"model_observation"})
            if row["stage"] in {"PLAN", "REVIEW"} and result.model_observation is not None:
                receipt["model_observation"] = result.model_observation
            con.execute("INSERT INTO goal_effect_receipts VALUES (?,?,?)",
                        (effect,_json(receipt),now_text()))
            self._accept(con,principal,row,result)
        return {"task_id":task_id}

    def _accept(self, con, principal, row, result):
        status = row["status"]
        stopping = status in {"PAUSE_REQUESTED","CANCEL_REQUESTED"}
        if result.kind == "PENDING":
            if (row["stage"] == "START" and result.child.kind != "session"
                    or row["stage"] == "DATA" and result.child.kind != "job"
                    or row["stage"] not in {"START","DATA"}):
                _conflict()
            binding = con.execute("SELECT task_id,effect_id FROM goal_children WHERE kind=? AND child_id=?",
                                  (result.child.kind,result.child.id)).fetchone()
            if binding is None or tuple(binding) != (row["task_id"],row["effect_id"]):
                raise PlatformError("GOAL_CHILD_BINDING_REQUIRED",409)
            stage = "RESEARCH" if row["stage"] == "START" else row["stage"]
            self._set(con,row,status=status if stopping else ("RESUME_REQUESTED" if stage=="RESEARCH" else "CHILD_RUNNING"),
                      stage=stage, child=result.child.model_dump(),effect=row["effect_id"],
                      control=row["control_id"] if stopping else (str(uuid4()) if stage=="RESEARCH" else None))
            return
        if result.kind in {"FAILED","WAITING"}:
            if stopping and not row["child_json"] and result.reason_code != "GOAL_EFFECT_UNCERTAIN":
                self._set(con,row,status="PAUSED" if status=="PAUSE_REQUESTED" else "CANCELLED",
                          reason=result.reason_code)
            else:
                retain_effect = result.kind == "WAITING" and (row["child_json"] or result.reason_code == "GOAL_EFFECT_UNCERTAIN")
                self._set(con,row,status=result.kind,reason=result.reason_code,
                          child=json.loads(row["child_json"]) if retain_effect and row["child_json"] else None,
                          effect=row["effect_id"] if retain_effect else None)
            return
        outputs = json.loads(row["outputs_json"])
        stage = row["stage"]
        snapshot = json.loads(row["snapshot_json"])
        spent = json.loads(row["spent_json"])
        additional = outputs.get("additional_data", [])
        if stage == "FREEZE":
            # Only the existing research prepare receipt's immutable spec and
            # digest may cross the start boundary.
            if set(result.output) != {"spec","spec_hash"}:
                _conflict()
        if stage == "DATA" and additional:
            reference = result.output["reference"]
            previous = additional[-1]["prior_data"]["reference"]
            if reference["content_hash"] == previous["content_hash"] or reference["dataset_id"] == previous["dataset_id"]:
                final = "CANCELLED" if status == "CANCEL_REQUESTED" else "SUCCEEDED"
                self._set(con,row,status=final,reason="GOAL_REVIEW_NO_NEW_DATA",outputs=outputs)
                return
            need = additional[-1]["result"]["need"]
            if result.output.get("window") != {key:need[key] for key in ("start", "end")}:
                self._set(con,row,status="FAILED",reason="GOAL_REVIEW_DATA_WINDOW_CHANGED",outputs=outputs)
                return
        outputs[stage] = result.output
        if result.kind == "MORE_DATA":
            if stage != "REVIEW":
                _conflict()
            if (additional or not self._review_need_valid(snapshot, outputs, result.output)
                    or not self._within_budget(snapshot, spent, "DATA", "START")):
                outputs.pop("REVIEW")
                self._set(con,row,status="FAILED",reason="GOAL_REVIEW_NEED_INVALID",outputs=outputs)
                return
            prior_result = outputs["RESEARCH"]
            outputs["additional_data"] = [{"result":result.output,
                "prior_child":{"kind":"session","id":prior_result["session_id"]},
                "prior_result":prior_result, "prior_data":outputs["DATA"],
                "prior_freeze":{"spec_hash":outputs["FREEZE"]["spec_hash"]}}]
            stage = "DATA"
        elif stage == "START":
            _conflict()  # START must return a durable child session to observe.
        elif stage == "RESEARCH":
            if self.clock() >= row["deadline"]:
                self._set(con,row,status="FAILED",reason="GOAL_DEADLINE_EXCEEDED",outputs=outputs)
                return
            review_grant = snapshot["grants"].get("REVIEW", {})
            if (additional or not snapshot.get("development_bounds")
                    or review_grant.get("model_requests") != 1 or review_grant.get("tokens", 0) < 1
                    or not self._within_budget(snapshot, spent, "REVIEW", "DATA", "START")):
                self._set(con,row,status="SUCCEEDED",outputs=outputs)
                return
            stage = "REVIEW"
        elif stage == "REVIEW":
            if set(result.output) != {"decision"} or result.output["decision"] not in (
                "NO_MORE_DATA", "NO_EXPANSION_RANGE", "NO_VALID_RECOMMENDATION",
            ):
                outputs.pop("REVIEW")
                self._set(con,row,status="FAILED",reason="GOAL_REVIEW_NEED_INVALID",outputs=outputs)
                return
            final = "CANCELLED" if status == "CANCEL_REQUESTED" else "SUCCEEDED"
            self._set(con,row,status=final,outputs=outputs)
            return
        else:
            stage = STAGES[STAGES.index(stage)+1]
        if stopping:
            self._set(con,row,status="PAUSED" if status=="PAUSE_REQUESTED" else "CANCELLED",
                      stage=stage,outputs=outputs)
        elif self.clock() >= row["deadline"]:
            self._set(con,row,status="FAILED",stage=stage,reason="GOAL_DEADLINE_EXCEEDED",outputs=outputs)
        else:
            self._set(con,row,status="QUEUED",stage=stage,job=self.enqueue(con,principal,row["task_id"]),outputs=outputs)

    def bind_child(self, con, principal, task_id, effect_id, child):
        row = self._row(con,principal,task_id)
        if row["status"] != "EXECUTING" or row["effect_id"] != effect_id or self.clock() >= row["deadline"]:
            raise PlatformError("GOAL_CONTROL_REQUESTED",409)
        con.execute("INSERT INTO goal_children VALUES (?,?,?,?)",
                    (child.kind,child.id,task_id,effect_id))
        con.execute("UPDATE goal_tasks SET child_json=? WHERE task_id=?",
                    (_json(child.model_dump()),task_id))

    def guard_child(self, con, owner_id, kind, child_id, *, dispatch=False):
        """Check the parent inside the existing child transaction."""
        row = con.execute("""SELECT t.*, c.effect_id AS bound_effect, e.stage AS effect_stage
            FROM goal_children c JOIN goal_tasks t USING(task_id)
            JOIN goal_effects e ON e.effect_id=c.effect_id AND e.task_id=c.task_id
            WHERE c.kind=? AND c.child_id=?""",(kind,child_id)).fetchone()
        if row is None:
            return True  # Existing manually submitted child.
        current = json.loads(row["child_json"]) if row["child_json"] else None
        allowed = (row["owner_id"] == owner_id and row["effect_id"] == row["bound_effect"]
                   and row["effect_stage"] == ("DATA" if kind == "job" else "START")
                   and (row["status"] in {"CHILD_RUNNING","RESUME_REQUESTED"} or kind=="job" and row["status"]=="EXECUTING")
                   and current == {"kind":kind,"id":child_id} and self.clock() < row["deadline"])
        if not allowed and not dispatch:
            raise PlatformError("GOAL_CONTROL_REQUESTED",409)
        return allowed

    def data_fence(self, context, parameters, connection=None):
        if connection is None:
            with closing(self.database.connect()) as reader:
                return self.data_fence(context,parameters,reader)
        if context.internal_origin(connection) != "goal":
            raise PlatformError("GOAL_CHILD_BINDING_REQUIRED",409)
        binding = connection.execute("""SELECT task_id,effect_id FROM goal_children
            WHERE kind='job' AND child_id=?""",(context.job_id,)).fetchone()
        if binding is None or tuple(binding) != (parameters["task_id"],parameters["effect_id"]):
            raise PlatformError("GOAL_CHILD_BINDING_REQUIRED",409)
        return self.guard_child(connection,context.run.owner_id,"job",context.job_id)

    def reconcile(self, principal, task_id, job_status=None, *, job_id=None):
        require_writer(principal)
        with self.database.transaction() as con:
            row = self._row(con,principal,task_id)
            if row["job_id"] and row["job_id"] == job_id and job_status in {"FAILED","CANCELLED","SUCCEEDED"}:
                if row["effect_id"]:
                    bound = con.execute("SELECT kind,child_id FROM goal_children WHERE task_id=? AND effect_id=?",
                                        (task_id,row["effect_id"])).fetchone()
                    if bound is not None:
                        # The create transaction is the durable receipt for the child.
                        child = Child(kind=bound["kind"], id=bound["child_id"])
                        self._accept(con,principal,row,StepResult(kind="PENDING",child=child))
                    else:
                        self._set(con,row,status="WAITING",reason="GOAL_EFFECT_UNCERTAIN",effect=row["effect_id"])
                else:
                    self._set(con,row,status="FAILED",reason="GOAL_JOB_TERMINATED")
                return self._view(self._row(con,principal,task_id))
            if row["job_id"] or not row["child_json"] or row["status"] in TERMINAL:
                return self._view(row)
            child = Child.model_validate_json(row["child_json"])
            if row["status"] == "WAITING" and child.kind == "job" and row["reason_code"] == "GOAL_EFFECT_UNCERTAIN":
                return self._view(row)
            if row["status"] in {"PAUSED","WAITING"} and self.clock() < row["deadline"]:
                return self._view(row)
            if self.clock() >= row["deadline"] and row["status"] != "CANCEL_REQUESTED":
                self._set(con,row,status="CANCEL_REQUESTED",reason="GOAL_DEADLINE_EXCEEDED",
                          child=json.loads(row["child_json"]),effect=row["effect_id"],control=str(uuid4()))
                row = self._row(con,principal,task_id)
            request = self._request(row)
        control = {"PAUSE_REQUESTED":"pause","CANCEL_REQUESTED":"cancel","RESUME_REQUESTED":"resume"}.get(row["status"])
        if control:
            result = self.forward(principal,request,child,control,row["control_id"])
        else:
            result = StepResult.model_validate(self.poll(principal,request,child))
        with self.database.transaction() as con:
            current = self._row(con,principal,task_id)
            if current["revision"] != row["revision"]:
                return self._view(current)
            if control:
                confirmation = {"pause":"PAUSED","cancel":"CANCELLED","resume":"CHILD_RUNNING"}[control]
                if result == confirmation:
                    expired = row["reason_code"] == "GOAL_DEADLINE_EXCEEDED"
                    self._set(con,row,status="FAILED" if expired else confirmation,reason=row["reason_code"],
                              child=child.model_dump() if control!="cancel" else None,effect=row["effect_id"])
                elif control == "pause" and result == "PAUSED_RESTART" and child.kind == "job":
                    self._set(con,row,status="PAUSED")
                elif result == "UNCERTAIN":
                    self._set(con,row,status="WAITING",reason="GOAL_EFFECT_UNCERTAIN",
                              child=child.model_dump(),effect=row["effect_id"])
            elif result.kind != "PENDING":
                self._accept(con,principal,row,result)
            elif result.child != child:
                _conflict()
            return self._view(self._row(con,principal,task_id))


def enqueue_with_jobs(jobs):
    """Create the queue job in the same product transaction as its parent."""
    def enqueue(con, principal, task_id):
        mode, definition, encoded = jobs._validate(principal,HANDLER,{"task_id":task_id},
                                                   RunMode.LOCAL_SIMULATION,None,internal=True)
        return jobs._insert(con,principal.user_id,definition,encoded,mode,internal_origin="goal")["job_id"]
    return enqueue


def register_worker(runtime, service):
    def handle(context, parameters):
        return service.handle(context, parameters)

    def validate(parameters):
        if set(parameters) != {"task_id"} or not isinstance(parameters["task_id"],str):
            raise PlatformError("INVALID_INPUT",422)

    runtime.handlers.register(HANDLER,handle,validate=validate,recoverable=False,auto_recovery_limit=0,
                              allowed_modes=(RunMode.LOCAL_SIMULATION,),internal_only=True)


def reconcile_tick(service, jobs):
    """One bounded scheduler pass over persisted goal and child state."""
    with closing(service.database.connect()) as con:
        rows = con.execute(
            """SELECT t.task_id,t.job_id,u.user_id,u.username,u.role FROM goal_tasks t
            JOIN users u ON u.user_id=t.owner_id
            WHERE u.enabled=1 AND (
                t.status IN ('QUEUED','EXECUTING','CHILD_RUNNING','PAUSE_REQUESTED','CANCEL_REQUESTED','RESUME_REQUESTED')
                OR (t.status IN ('PAUSED','WAITING') AND t.child_json IS NOT NULL AND t.deadline <= ?
                    AND NOT (COALESCE(t.reason_code,'')='GOAL_EFFECT_UNCERTAIN' AND json_extract(t.child_json,'$.kind')='job')))
            ORDER BY t.updated_at,t.task_id LIMIT 100""",(service.clock(),)).fetchall()
    for row in rows:
        principal = Principal(row["user_id"],row["username"],row["role"])
        try:
            status = jobs.get(principal,row["job_id"])["status"] if row["job_id"] else None
            service.reconcile(principal,row["task_id"],status,job_id=row["job_id"])
        except PlatformError:
            # Keep pending state durable. A transient child read or stale CAS
            # cannot establish that a close completed.
            continue
