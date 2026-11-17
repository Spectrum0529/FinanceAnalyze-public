"""Compose the goal task with the existing planner, data adapter and research service."""
from .errors import PlatformError
from .goal_research import GoalResearch, StepResult, enqueue_with_jobs, reconcile_tick, register_worker
from .goal_research_api import create_router
from .goal_research_sessions import GoalResearchSessions


def install_goal_research(runtime, *, inputs, sessions, defaults, validate_defaults, readiness,
                          plan, install_data, clock, review=None, development_bounds=None):
    research = GoalResearchSessions(inputs,sessions,clock=clock)
    stages = {"PLAN":lambda principal,request,grant,active,link:plan(principal,request,grant,active),
              "FREEZE":research.freeze,"START":research.start}

    def review_stage(principal, request, grant, active, link):
        if review is None:
            raise PlatformError("GOAL_REVIEW_UNAVAILABLE", 503)
        return review(principal, request, grant, active)

    stages["REVIEW"] = review_stage

    def step(principal, request, grant, active, link):
        try:
            return stages[request["stage"]](principal, request, grant, active, link)
        except PlatformError as error:
            if error.code in {"LEASE_LOST", "JOB_CANCELLED"}:
                raise
            return StepResult(kind="FAILED", reason_code=error.code)

    def poll(principal, request, child):
        if child.kind == "session":
            return research.poll(principal,request,child)
        return poll_data(principal,child.model_dump())

    def forward(principal, request, child, action, key):
        if child.kind == "session":
            return research.forward(principal,request,child,action,key)
        job = runtime.jobs.get(principal,child.id)
        if action == "resume":
            return "CHILD_RUNNING"
        if job["status"] in {"FAILED","SUCCEEDED","CANCELLED"}:
            if job["status"] != "SUCCEEDED":
                result = StepResult.model_validate(poll_data(principal,child.model_dump()))
                if result.kind == "WAITING" and result.reason_code == "GOAL_EFFECT_UNCERTAIN":
                    return "UNCERTAIN"
            if action == "cancel":
                return "CANCELLED"
            if job["status"] == "SUCCEEDED":
                return "PAUSED"
            events = runtime.jobs.events(principal, child.id, limit=3)["items"]
            # A queued cancellation is safe to try again under a fresh grant.
            if [event["kind"] for event in events] == ["QUEUED", "CANCELLED"]:
                return "PAUSED_RESTART"
            return "UNCERTAIN"
        if not job["cancel_requested"]:
            runtime.jobs.cancel(principal,child.id)
        return "PENDING"

    service = GoalResearch(runtime.database,enqueue=enqueue_with_jobs(runtime.jobs),defaults=defaults,
                           validate_defaults=validate_defaults,readiness=readiness,
                           step=step,
                           poll=poll,forward=forward,clock=clock,development_bounds=development_bounds)
    service.migrate()
    register_worker(runtime,service)
    data, poll_data = install_data(service.data_fence)
    stages["DATA"] = data
    sessions.goal_guard = service.guard_child
    runtime.routers.append(create_router(runtime,service))
    runtime.goal_research = service
    return service


class GoalScheduler:
    """Optional wrapper for the existing periodic scheduler."""
    def __init__(self, original, service, jobs):
        self.original = original
        self.service = service
        self.jobs = jobs

    def __getattr__(self, key):
        return getattr(self.original,key)

    def tick(self):
        result = self.original.tick()
        reconcile_tick(self.service,self.jobs)
        return result
