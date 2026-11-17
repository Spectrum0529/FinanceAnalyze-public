"""Connect the shared application runtime to the goal and research services."""
from hashlib import sha256
import time

from finance_platform.core.errors import MESSAGES, PlatformError
from finance_platform.core.goal_research import StepResult
from finance_platform.core.goal_research_composition import GoalScheduler, install_goal_research
from finance_platform.data.goal_data.cn_identity import reviewed_identity
from finance_platform.data.goal_data.goal_sdk import install_sources
from finance_platform.data.native_library_download import gateway_for_runtime
from finance_platform.data.openbb_source import capability as openbb_capability
from finance_platform.research.goal_planner import freeze_us_universes, plan_step, planning_tokens, review_step
from .goal_admission import GoalAdmission, development_bounds, source_for_need
from .goal_messages import MESSAGES_ZH
from .goal_provider_binding import installed_goal_pins, registered_runners
from .goal_sources import GoalSources


def _planner_step(adapter, runtime, principal, request, grant, active, *, transport=None):
    selected = request["snapshot"]["planner_profile"]
    profile = runtime.model_profiles.prepare_execution_profile(principal, selected["profile_id"], selected["revision"])
    if profile["sha256"] != selected["sha256"]:
        return StepResult(kind="FAILED", reason_code="MODEL_PROFILE_HASH_MISMATCH")
    result = StepResult.model_validate(adapter(runtime, principal, request, grant, active, transport=transport))
    bounds = request["snapshot"].get("development_bounds")
    if adapter is plan_step and result.kind == "DONE" and bounds:
        needs = result.output["data_needs"]
        if any(not bounds["start"] <= need["start"] <= need["end"] <= bounds["end"] for need in needs):
            return StepResult(kind="WAITING", reason_code="GOAL_DATA_OUTSIDE_DEVELOPMENT_WINDOW",
                              model_observation=result.model_observation)
    return result


def install(runtime, *, inputs, source_options, data_factory, planner_transport=None,
            review_bounds=development_bounds):
    """Install the same API and worker handlers. Fixture ports are constructor-only."""
    admission = GoalAdmission(runtime, inputs=inputs, source_options=source_options,
                              planning_tokens=planning_tokens, freeze_universes=freeze_us_universes, review=True)

    def plan(principal, request, grant, active):
        return _planner_step(plan_step, runtime, principal, request, grant, active, transport=planner_transport)

    def review(principal, request, grant, active):
        return _planner_step(review_step, runtime, principal, request, grant, active, transport=planner_transport)

    def install_data(fence):
        data_step, poll_data = data_factory(fence)

        def data(principal, request, grant, active, link):
            plan = request["outputs"]["PLAN"]
            need = request.get("data_need")
            if need is None and len(plan["data_needs"]) == 1:
                need = plan["data_needs"][0]
            if need is None:
                return StepResult(kind="WAITING", reason_code="GOAL_DATA_REQUIREMENT_UNSUPPORTED")
            try:
                source = source_for_need(request["snapshot"]["sources"], need)
            except PlatformError as error:
                return StepResult(kind="WAITING", reason_code=error.code)
            snapshot = request["snapshot"]
            binding_key = "goal:" + sha256(request["task_id"].encode()).hexdigest()
            storage = runtime.storage.bind(principal.user_id, binding_key)
            snapshot = {**snapshot, "data_source": source, "storage": storage}
            outputs = {**request["outputs"], "PLAN": {**plan, "data_needs": [need]}}
            return data_step(principal, {**request, "snapshot": snapshot, "outputs": outputs}, grant, active, link)

        return data, poll_data

    service = install_goal_research(
        runtime, inputs=inputs, sessions=runtime.autonomous_research, defaults=admission.freeze,
        validate_defaults=admission.validate, readiness=admission.readiness,
        plan=plan, install_data=install_data, clock=time.time, review=review,
        development_bounds=review_bounds,
    )
    runtime.scheduler = GoalScheduler(runtime.scheduler, service, runtime.jobs)
    return service


def install_workflow(runtime, credential_store):
    MESSAGES.update(MESSAGES_ZH)
    library = runtime.dataset_library
    inputs = runtime.autonomous_research.resolve_inputs
    if not hasattr(inputs, "prepare"):
        inputs = None
    us_history = gateway_for_runtime(runtime)
    existing = {"yfinance", "akshare"}
    if getattr(runtime, "goal_massive_client", None) is not None:
        existing.add("massive")
    if openbb_capability(enabled=runtime.openbb_cboe_enabled,
                        runtime_binding=runtime.openbb_runtime_binding)["available"]:
        existing.add("openbb_cboe")
    sources = GoalSources(library, credential_store, registered_runners(runtime, pins=installed_goal_pins()),
                          existing_sources=existing)
    runtime.goal_provider_catalog = sources.catalog

    def data_factory(fence):
        return install_sources(runtime, library, runners=sources.runners, credential_store=credential_store,
                               fence=fence, identities=reviewed_identity, us_history=us_history)

    return install(runtime, inputs=inputs, source_options=sources.options, data_factory=data_factory)
