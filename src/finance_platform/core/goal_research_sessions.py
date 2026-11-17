"""Adapt goal task stages to the existing autonomous research service."""
from math import floor
import time

from .autonomous_models import Budget, PrepareSession, SessionSpec
from .autonomous_sessions import digest
from .autonomous_status import session_progress
from .errors import PlatformError
from .goal_research import Child, StepResult

TERMINAL_CHILD = {"COMPLETED","BUDGET_EXHAUSTED","CANCELLED"}


class GoalResearchSessions:
    def __init__(self, inputs, sessions, *, clock=time.time):
        self.inputs = inputs
        self.sessions = sessions
        self.clock = clock

    def freeze(self, principal, request, grant, active, link):
        plan = request["outputs"]["PLAN"]
        refs = [request["outputs"]["DATA"]["reference"]]
        selection = request["snapshot"]["references"]["research_models"]
        budget = request["snapshot"]["grants"]["START"]
        remaining = floor(request["deadline"]-self.clock())
        goal_round = request["snapshot"].get("goal_round")
        options = {"goal_round": goal_round} if goal_round is not None else {}
        if remaining < 60:
            return StepResult(kind="FAILED",reason_code="GOAL_DEADLINE_EXCEEDED")
        active()
        request_model = PrepareSession.model_validate({
            **selection, "name":plan["name"], "objective":plan["objective"], "target":plan["target"],
            "scoring":plan["scoring"], "development_ids":[r["dataset_id"] for r in refs],
            "budget":self._budget(budget), "deadline_seconds":remaining,
        })
        prepared = self.inputs.prepare(principal,request_model.model_dump(), **options)
        spec = SessionSpec.model_validate(prepared["spec"])
        expected = [(r["dataset_id"],str(r["version"]),r["content_hash"]) for r in refs]
        actual = [(r.resource_id,r.version,r.content_hash) for r in spec.development]
        if expected != actual or spec.final_test is not None:
            raise PlatformError("GOAL_DATA_BINDING_CHANGED",409)
        if options and (spec.budget != request_model.budget or spec.deadline_seconds != remaining
                        or prepared["spec_hash"] != digest(spec.model_dump())):
            raise PlatformError("GOAL_RESEARCH_SPEC_CHANGED",409)
        active()
        return StepResult(kind="DONE",output={"spec":spec.model_dump(),"spec_hash":prepared["spec_hash"]})

    @staticmethod
    def _budget(grant):
        return Budget.model_validate({"attempts":grant.get("research_attempts",0),
                                      **{k:grant.get(k,0) for k in Budget.model_fields if k!="attempts"}}).model_dump()

    def start(self, principal, request, grant, active, link):
        prepared = request["outputs"]["FREEZE"]
        spec = SessionSpec.model_validate(prepared["spec"])
        if spec.final_test is not None or spec.budget.model_dump() != self._budget(grant):
            raise PlatformError("GOAL_RESEARCH_BUDGET_MISMATCH",409)
        goal_round = request["snapshot"].get("goal_round")
        options = {"goal_round": goal_round} if goal_round is not None else {}
        if options and prepared["spec_hash"] != digest(spec.model_dump()):
            raise PlatformError("GOAL_RESEARCH_SPEC_CHANGED",409)
        active()
        # The child is created in DRAFT and linked by Core before run is forwarded.
        view = self.sessions.create(principal,spec.model_dump(),idempotency_key="goal:"+request["operation_key"],
                                    goal_link=lambda con,sid:link(con,Child(kind="session",id=sid)),
                                    goal_deadline=request["deadline"], **options)
        return StepResult(kind="PENDING",child=Child(kind="session",id=view["session_id"]))

    def poll(self, principal, request, child):
        progress = session_progress(self.sessions,principal,child.id,limit=20)
        state = progress["status"]
        if state == "BLOCKED":
            reason = "GOAL_EFFECT_UNCERTAIN" if progress.get("requires_unknown_charge_ack") else "GOAL_RESEARCH_BLOCKED"
            return StepResult(kind="WAITING",reason_code=reason)
        if state not in TERMINAL_CHILD:
            return StepResult(kind="PENDING",child=child)
        if state == "CANCELLED":
            return StepResult(kind="FAILED",reason_code="GOAL_RESEARCH_CANCELLED")
        result = next((row for row in progress["history"] if row["status"] == row["job_status"] == "SUCCEEDED"
                       and row["phase"] == "development"), None)
        if result is None:
            return StepResult(kind="FAILED",reason_code="GOAL_RESEARCH_NO_RESULT")
        return StepResult(kind="DONE",output={"session_id":child.id,"attempt_id":result["attempt_id"],
                                              "status":state,"metrics":result["metrics"]})

    def forward(self, principal, request, child, action, key):
        view = self.sessions.get(principal,child.id)
        state = view["status"]
        if action == "pause" and (state == "PAUSED" or state in TERMINAL_CHILD):
            return "PAUSED"
        if action == "cancel" and state in TERMINAL_CHILD:
            return "CANCELLED"
        if action == "resume" and (state in {"READY","RUNNING"} or state in TERMINAL_CHILD):
            return "CHILD_RUNNING"
        if action in {"pause", "resume"} and state in {"BLOCKED", "PAUSED"}:
            progress = session_progress(self.sessions, principal, child.id, limit=1)
            if progress["requires_unknown_charge_ack"]:
                return "UNCERTAIN"
        if state in {"CANCEL_REQUESTED","PAUSE_REQUESTED"}:
            return "PENDING"
        actual = "run" if action == "resume" and state == "DRAFT" else action
        value = self.sessions.control(principal,child.id,{"action":actual,"expected_revision":view["revision"],
                                                        "idempotency_key":"goal:"+key})
        return {"READY":"CHILD_RUNNING","PAUSED":"PAUSED","CANCELLED":"CANCELLED"}.get(value["status"],"PENDING")
