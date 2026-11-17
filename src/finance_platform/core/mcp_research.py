"""Delegated research views and controls over persisted product services."""
from contextlib import closing
from hashlib import sha256
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field, field_validator

from .autonomous_models import Control, CreateSession, PrepareSession
from .errors import PlatformError
from .goal_research import COUNTERS, ControlGoal, CreateGoal, SaveGoalDefaults
from .mcp_data_policy import ReportDataPolicy, local_only, require_dataset
from .mcp_grants import permits
from .mcp_domain_models import NativeDebate
from .team_profiles import Strict

BudgetCounter = Annotated[int, Field(ge=0, le=10**12)]


class GoalExecution(CreateGoal):
    approved_defaults_revision: int = Field(ge=0)
    approved_limits: dict[str, BudgetCounter] = Field(min_length=1, max_length=len(COUNTERS))


class GoalResume(ControlGoal):
    approved_defaults_revision: int | None = Field(default=None, ge=0)
    approved_limits: dict[str, BudgetCounter] | None = Field(default=None, min_length=1, max_length=len(COUNTERS))


class ApprovedTaProfile(Strict):
    profile_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovedTaLimits(Strict):
    max_model_calls: int = Field(ge=1, le=32)
    max_input_tokens: int = Field(ge=1, le=2097152)
    max_output_tokens: int = Field(ge=1, le=262144)
    max_wall_seconds: int = Field(ge=1, le=120)


class ExternalNativeDebate(NativeDebate):
    agent_team_snapshot_id: None = None
    approved_profile: ApprovedTaProfile
    approved_limits: ApprovedTaLimits
    approve_source_transfer: Literal[True]

    @field_validator("approve_source_transfer", mode="before")
    @classmethod
    def explicit_transfer_consent(cls, value):
        if value is not True:
            raise ValueError("source transfer requires explicit true")
        return value

def _only(value, names):
    return {name: value[name] for name in names.split() if name in value}


def _managed(principal):
    return permits(principal.mcp_scope, "manage")


def _goal_view(principal, value):
    allowed = {"pause", "cancel"} if _managed(principal) else set()
    if principal.mcp_scope == "external":
        allowed.add("resume")
    return _only(value, "task_id status stage revision reason_code job_id deadline created_at updated_at spent limits child "
                        "remaining billing_cost billing_status defaults_revision") | {
        "completed_stages": sorted(set(value.get("progress", {})) & {"PLAN", "DATA", "FREEZE", "START", "REVIEW"}),
        "allowed_actions": [action for action in value.get("allowed_actions", ())
                            if action in allowed],
    }


def _session_view(principal, value):
    return _only(value, "schema session_id spec_hash status revision generation deadline created_at updated_at spent "
                        "active_attempt final_attempt candidate_hash allowed_actions evaluation_scope budget_usage") | {
        "allowed_actions": [action for action in value.get("allowed_actions", ())
                            if _managed(principal) and action in {"pause", "cancel"}],
    }


def _page(page, project):
    return {**page, "items": [project(item) for item in page["items"]]}


def require_session_sources(runtime, principal, spec):
    try:
        policy = ReportDataPolicy(runtime.dataset_library, principal.user_id)
        refs = [*spec["development"]]
        if spec.get("final_test") is not None:
            refs.append(spec["final_test"])
        if not refs:
            raise ValueError
        for ref in refs:
            policy.require_reference({
                "dataset_id": ref["resource_id"], "version": ref["version"], "content_hash": ref["content_hash"],
            })
    except (AttributeError, KeyError, TypeError, ValueError):
        raise local_only() from None


def _prepared_data(runtime, principal, ids):
    for key in ids:
        try:
            require_dataset(runtime.dataset_library.get(principal.user_id, key, selectable=True))
        except (AttributeError, KeyError, TypeError, ValueError):
            raise local_only() from None


def _executor(runtime):
    status = getattr(runtime, "autonomous_deployment_status", {})
    service = getattr(runtime, "autonomous_research", None)
    if status.get("ready") is not True or service is None:
        raise PlatformError(status.get("code") or "AUTONOMOUS_DEPLOYMENT_UNAVAILABLE", 503)
    return service


def _model_scope(principal, budget):
    if (budget["model_requests"] or budget["tokens"] or budget["cost_microunits"]) and principal.mcp_scope != "external":
        raise PlatformError("MCP_SCOPE_REQUIRED", 403)


def require_synthetic_ta(bundle):
    if bundle["scope"] != "SYNTHETIC" or any(ref["scope"] != "SYNTHETIC" for ref in bundle["evidence_results"]):
        raise local_only()


def require_offline_ta_checkpoint(runtime, principal, parameters):
    if not parameters.get("agent_team_snapshot_id"):
        raise PlatformError("MCP_TA_EXTERNAL_REQUIRES_VERIFIED_METER", 403)
    try:
        debate = runtime.mcp_debate_reader
        _, bundle, _ = debate._bindings(principal, parameters)
        require_synthetic_ta(bundle)
    except PlatformError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise PlatformError(getattr(error, "code", "VERIFIED_CHECKPOINT_REQUIRED"), 409) from None


def require_external_ta_checkpoint(runtime, principal, parameters, declared_digest):
    if principal.mcp_scope != "external":
        raise PlatformError("MCP_SCOPE_REQUIRED", 403)
    try:
        approval = parameters["provider_approval"]
        proof = approval["execution_sha256"]
        if declared_digest is not None and declared_digest != proof:
            raise PlatformError("MCP_EXTERNAL_BUDGET_CHANGED", 409)
        debate = getattr(runtime, "mcp_debate_reader", None)
        if not callable(getattr(debate, "external_capabilities", None)):
            raise PlatformError("MCP_TA_EXTERNAL_POLICY_NOT_READY", 503)
        _, bundle, _ = debate._bindings(principal, parameters)
        require_synthetic_ta(bundle)
    except PlatformError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise PlatformError(getattr(error, "code", "VERIFIED_CHECKPOINT_REQUIRED"), 409) from None


def _ta_execution_digest(database, principal, job_id):
    if principal.mcp_scope != "external":
        return None
    with closing(database.connect()) as con:
        row = con.execute("""SELECT parameters_json,parameters_hash FROM jobs
                             WHERE owner_id=? AND job_id=? AND handler='research.debate'""",
                          (principal.user_id, job_id)).fetchone()
    if row is None or sha256(row["parameters_json"].encode()).hexdigest() != row["parameters_hash"]:
        return None
    try:
        approval = json.loads(row["parameters_json"]).get("provider_approval")
        proof = None if not approval else approval["execution_sha256"]
        return proof if _valid_sha256(proof) else None
    except (KeyError, TypeError, ValueError):
        return None


def _valid_sha256(value):
    return type(value) is str and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _ta_external_approval(body):
    reference = body.approved_profile.model_dump()
    limits = body.approved_limits.model_dump()

    def approve_execution(profile, bundle, _normalized_options):
        if _only(profile, "profile_id profile_sha256") != reference:
            raise PlatformError("MCP_EXTERNAL_PROFILE_CHANGED", 409)
        require_synthetic_ta(bundle)
        return {
            "approved_profile": reference,
            "approved_limits": limits,
            "approve_source_transfer": True,
        }
    return approve_execution


def native_report_projection(research, principal, run_id, report):
    """Recheck the original owner, job, model and evidence on every delegated page."""
    if not permits(getattr(principal, "mcp_scope", None), "browse"):
        raise local_only()
    ta = report["family"] == "tradingagents-native-report-1"
    handler = "research.debate" if ta else "research.qlib"
    try:
        with closing(research.database.connect()) as con:
            row = con.execute("""SELECT r.sample_hash,j.job_id,j.parameters_hash,j.parameters_json FROM research_results r
                JOIN jobs j ON j.job_id=r.job_id AND j.owner_id=r.owner_id AND j.run_id=r.run_id
                WHERE r.owner_id=? AND r.run_id=? AND j.handler=? AND j.status='SUCCEEDED'""",
                              (principal.user_id, run_id, handler)).fetchone()
        if (row is None or report["run_id"] != run_id or report["job_id"] != row["job_id"]
                or sha256(row["parameters_json"].encode()).hexdigest() != row["parameters_hash"]):
            raise ValueError
        parameters = json.loads(row["parameters_json"])
        if ta:
            request = parameters["request"]
            if (report["lineage"]["data"] != request["data_reference"]
                    or report["data_binding"]["data_reference"] != request["data_reference"]):
                raise ValueError
            debate = research.debate_service()
            approval = parameters.get("provider_approval")
            if approval is None:
                snapshot_id = parameters["agent_team_snapshot_id"]
                if (report["execution_scope"] != "TEST_OFFLINE" or report["agent_team"]["snapshot_id"] != snapshot_id
                        or report["agent_team"]["real_provider_calls"] != "NOT_RUN"):
                    raise ValueError
                _, snapshot = debate._team(principal, snapshot_id)
                if snapshot["sha256"] != report["agent_team"]["sha256"]:
                    raise ValueError
            elif (report["lineage"]["model"] != parameters["model_reference"]
                    or report.get("approved_execution_sha256") != approval["execution_sha256"]
                    or not _valid_sha256(approval["execution_sha256"])
                    or report["model_status"] != "EXTERNAL_APPROVED"):
                raise ValueError
            bundle = debate.inputs.authorize(principal, request)
            require_synthetic_ta(bundle)
            if approval is not None:
                from finance_platform.research.debate_service import approved_fields, provider_approval
                profile, snapshot = debate._team(principal, parameters["agent_team_snapshot_id"], external=True)
                bundle = {**bundle, "team_snapshot": snapshot}
                debate._authorize_provider_source(principal, request, bundle)
                recomputed = provider_approval(profile, request, bundle, debate._provider_options(request, snapshot),
                                               approved_fields(approval))
                if recomputed != approval:
                    raise ValueError
            if any(report["data_binding"][key] != bundle[key] for key in ("selected_view_hash", "snapshot_content_hash")):
                raise ValueError
            projection = _only(report, "family engine scope data_scope execution_scope evidence_scopes data_binding run_id job_id "
                                       "lineage model_status agent_team report budget publication_gate")
            if approval is not None:
                projection["approved_execution_sha256"] = approval["execution_sha256"]
            return projection
        reference = parameters["export_reference"]
        if (report["export_reference"] != reference or report["lineage"]["data"] != reference
                or report["scope"] != "SYNTHETIC"):
            raise ValueError
        grant = research.inputs.validate_qlib(principal, reference)
        data = grant["data"]
        if (grant["source_reference"] != report["source_reference"]
                or data["logical_data_hash"] != row["sample_hash"]
                or data["native_manifest_hash"] != report["native_manifest_hash"]):
            raise ValueError
        return _only(report, "family schema_version engine run_id job_id export_reference source_reference scope workflow_hash "
                             "native_manifest_hash recorder metrics predictions portfolio_report positions effective_sample_audit "
                             "model_semantic_hash undefined_metrics publication_gate lineage")
    except (AttributeError, KeyError, TypeError, ValueError):
        raise local_only() from None


def _goal_approval(principal, body):
    if principal.mcp_scope != "external":
        raise PlatformError("MCP_SCOPE_REQUIRED", 403)
    limits = body.approved_limits
    if limits is None or body.approved_defaults_revision is None or not set(limits) <= COUNTERS:
        raise PlatformError("MCP_EXTERNAL_BUDGET_REQUIRED", 403)
    reference = {"defaults_revision": body.approved_defaults_revision, "limits": limits}

    def approve(snapshot, reason):
        if reason:
            raise PlatformError(reason, 409)
        if any(snapshot.get(key) != value for key, value in reference.items()):
            raise PlatformError("MCP_EXTERNAL_BUDGET_CHANGED", 409)
        sources = snapshot.get("sources", [])
        if not sources or any(source.get("provider") != "yfinance" or source.get("scope") != "public"
                              or source.get("execution") != "BOUND" for source in sources):
            raise PlatformError("MCP_PROVIDER_NOT_ALLOWED", 403)

    return {"approve_snapshot": approve, "approval_ref": reference}


def create_router(runtime, *, research_service=None):
    router = APIRouter(prefix="/mcp-research", tags=["delegated-research"])
    if research_service is not None:
        runtime.mcp_debate_reader = research_service.debate_service()

    def actor(request, *, write=False):
        principal = runtime.require_principal(request, write=write)
        if not getattr(principal, "mcp_scope", None):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        return principal

    def goal_service():
        service = getattr(runtime, "goal_research", None)
        if service is None:
            raise PlatformError("GOAL_DEFAULTS_UNAVAILABLE", 503)
        return service

    @router.post("/debates")
    def debate_start(body: NativeDebate, request: Request):
        principal = actor(request, write=True)
        if research_service is None:
            raise PlatformError("TA_CONTROLLED_NATIVE_HOST_REQUIRED", 503)
        if not body.agent_team_snapshot_id:
            raise PlatformError("TA_TEAM_SNAPSHOT_REQUIRED", 422)
        from finance_platform.research.debate_service import normalize
        debate = research_service.debate_service()
        payload = body.model_dump()
        try:
            debate._team(principal, body.agent_team_snapshot_id)
            declaration = normalize({
                **_only(payload, "data_reference ticker trade_date as_of evidence_results"),
                "options": body.options or {},
            })
            bundle = debate.inputs.authorize(principal, declaration)
            require_synthetic_ta(bundle)
            return debate.submit(principal, **payload)
        except PlatformError:
            raise
        except ValueError as error:
            raise PlatformError(getattr(error, "code", "INVALID_INPUT"), 422) from None

    @router.get("/debates/external-capabilities")
    def external_debate_capabilities(request: Request, limit: int = Query(20, ge=1, le=100),
                                     offset: int = Query(0, ge=0, le=10000),
                                     profile_id: str | None = Query(None, min_length=1, max_length=128)):
        principal = actor(request)
        if principal.mcp_scope != "external":
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        describe = getattr(getattr(runtime, "mcp_debate_reader", None), "external_capabilities", None)
        if not callable(describe):
            return {"available": False, "reason_code": "MCP_TA_EXTERNAL_POLICY_NOT_READY",
                    "items": [], "total": 0, "limit": limit, "offset": offset}
        where = "owner_id=?"
        values = [principal.user_id]
        if profile_id is not None:
            where += " AND 'team-'||snapshot_id=?"
            values.append(profile_id)
        with closing(runtime.database.connect()) as con:
            con.execute("BEGIN")
            total = con.execute("SELECT count(*) FROM agent_team_snapshots WHERE " + where, values).fetchone()[0]
            rows = con.execute("""SELECT snapshot_id,sha256,created_at,preset_id,preset_revision FROM agent_team_snapshots
                                  WHERE """ + where + " ORDER BY created_at DESC,snapshot_id LIMIT ? OFFSET ?",
                               (*values, limit, offset)).fetchall()
        items = []
        for row in rows:
            reference = {"profile_id": "team-" + row["snapshot_id"], "profile_sha256": row["sha256"]}
            try:
                value = describe(principal, reference["profile_id"])
                maximum = value.get("approved_limits")
                maximum = None if maximum is None else ApprovedTaLimits.model_validate(maximum).model_dump()
                available = value["can_submit"] is True
                if available and value.get("profile") != reference:
                    raise ValueError
                item = {"available": available, "reason_code": value.get("reason_code"),
                        "maximum_limits": maximum}
            except (KeyError, TypeError, ValueError) as error:
                item = {"available": False, "reason_code": getattr(error, "code", "MCP_PRODUCT_RESPONSE_INVALID"),
                        "maximum_limits": None}
            items.append(dict(row) | reference | item)
        available = any(item["available"] for item in items)
        return {"available": available, "reason_code": None if available else (
                    "TA_TEAM_SNAPSHOT_REQUIRED" if not total else "MCP_TA_EXTERNAL_PROFILE_UNAVAILABLE"
                ), "items": items, "total": total, "limit": limit, "offset": offset}

    @router.post("/debates/external")
    def external_debate_start(body: ExternalNativeDebate, request: Request):
        principal = actor(request, write=True)
        if principal.mcp_scope != "external":
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        debate = getattr(runtime, "mcp_debate_reader", None)
        if not callable(getattr(debate, "external_capabilities", None)):
            raise PlatformError("MCP_TA_EXTERNAL_POLICY_NOT_READY", 503)
        payload = body.model_dump(exclude={"approved_profile", "approved_limits", "approve_source_transfer"})
        try:
            receipt = debate.submit(principal, **payload, approved_profile=body.approved_profile.model_dump(),
                                    approve_execution=_ta_external_approval(body))
            return {**receipt, "approved_execution_sha256": _ta_execution_digest(runtime.database, principal, receipt["job_id"])}
        except PlatformError:
            raise
        except ValueError as error:
            raise PlatformError(getattr(error, "code", "INVALID_INPUT"), 422) from None

    @router.get("/debates/{job_id}")
    def debate_status(job_id: str, request: Request):
        principal = actor(request)
        debate = getattr(runtime, "mcp_debate_reader", None)
        if debate is None:
            raise PlatformError("TA_CONTROLLED_NATIVE_HOST_REQUIRED", 503)
        try:
            view = debate.status(principal, job_id)
            digest = _ta_execution_digest(runtime.database, principal, job_id)
            return {**view, "approved_execution_sha256": digest}
        except PlatformError:
            raise
        except ValueError as error:
            raise PlatformError(getattr(error, "code", "INVALID_INPUT"), 422) from None

    @router.get("/goals")
    def goals(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        principal = actor(request)
        return _page(goal_service().list(principal, limit=limit, offset=offset), lambda value: _goal_view(principal, value))

    @router.get("/goals/{task_id}")
    def goal(task_id: str, request: Request):
        principal = actor(request)
        return _goal_view(principal, goal_service().get(principal, task_id))

    @router.post("/goals", status_code=202)
    def goal_start(body: GoalExecution, request: Request):
        principal = actor(request, write=True)
        _executor(runtime)
        options = _goal_approval(principal, body)
        value = goal_service().create(principal, body.goal, body.idempotency_key, **options)
        return _goal_view(principal, value)

    @router.post("/goals/{task_id}/control")
    def goal_control(task_id: str, body: GoalResume, request: Request):
        principal = actor(request, write=True)
        if not _managed(principal):
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        options = {}
        if body.action == "resume":
            _executor(runtime)
            options = _goal_approval(principal, body)
        elif body.approved_defaults_revision is not None or body.approved_limits is not None:
            raise PlatformError("INVALID_INPUT", 422)
        value = goal_service().control(principal, task_id, body.action, body.expected_revision, body.idempotency_key,
                                       **options)
        return _goal_view(principal, value)

    @router.put("/goals/defaults")
    def goal_defaults(body: SaveGoalDefaults, request: Request):
        principal = actor(request, write=True)
        return goal_service().save_defaults(principal, body.model_dump())

    @router.get("/autonomous/sessions")
    def sessions(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        principal = actor(request)
        value = runtime.autonomous_research.list(principal, limit=limit, offset=offset)
        return _page(value, lambda item: _session_view(principal, item))

    @router.get("/autonomous/sessions/{session_id}")
    def session(session_id: str, request: Request):
        principal = actor(request)
        value = runtime.autonomous_research.get(principal, session_id)
        result = _session_view(principal, value)
        if permits(principal.mcp_scope, "workbench"):
            try:
                _executor(runtime)
                _model_scope(principal, value["spec"]["budget"])
                require_session_sources(runtime, principal, value["spec"])
            except PlatformError as error:
                result["execution_unavailable_code"] = error.code
            else:
                result["allowed_actions"] += [action for action in value["allowed_actions"]
                                              if action in {"run", "resume", "restart"}]
        return result

    @router.get("/autonomous/sessions/{session_id}/progress")
    def progress(session_id: str, request: Request, limit: int = Query(20, ge=1, le=100),
                 before_ordinal: int | None = Query(None, ge=1)):
        from .autonomous_status import session_progress
        principal = actor(request)
        service = runtime.autonomous_research
        value = session_progress(service, principal, session_id, limit=limit, before_ordinal=before_ordinal)
        lineage_unavailable = False
        try:
            require_session_sources(runtime, principal, service.get(principal, session_id)["spec"])
        except PlatformError:
            lineage_unavailable = True
            fields = ("attempt_id ordinal generation phase status job_id run_id created_at updated_at job_status "
                      "error_code outcome_status outcome_error_code")
            value["history"] = [_only(row, fields) for row in value["history"]]
            value["latest_execution_progress"] = None
            value["latest_checkpoint"] = None
        value["source_reason_code"] = "MCP_LOCAL_DATA_ONLY" if lineage_unavailable else None
        value["allowed_actions"] = [action for action in value["allowed_actions"]
                                    if _managed(principal) and action in {"pause", "cancel"}]
        return value

    @router.post("/autonomous/prepare")
    def prepare(body: PrepareSession, request: Request):
        principal = actor(request, write=True)
        service = _executor(runtime)
        _model_scope(principal, body.budget.model_dump())
        _prepared_data(runtime, principal, [*body.development_ids, *([body.final_test_id] if body.final_test_id else [])])
        return service.resolve_inputs.prepare(principal, body.model_dump())

    @router.post("/autonomous/sessions", status_code=201)
    def create(body: CreateSession, request: Request):
        principal = actor(request, write=True)
        service = _executor(runtime)
        spec = body.spec.model_dump()
        _model_scope(principal, spec["budget"])
        require_session_sources(runtime, principal, spec)
        return _session_view(principal, service.create(principal, spec, idempotency_key=body.idempotency_key))

    @router.post("/autonomous/sessions/{session_id}/control")
    def control(session_id: str, body: Control, request: Request):
        principal = actor(request, write=True)
        service = runtime.autonomous_research
        if body.action not in {"pause", "cancel"}:
            _executor(runtime)
            if not permits(principal.mcp_scope, "workbench"):
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            with closing(runtime.database.connect()) as con:
                row = service._row(con, principal, session_id)
                spec = json.loads(row["spec_json"])
                if body.expected_revision != row["revision"]:
                    raise PlatformError("CONFLICT", 409)
            _model_scope(principal, spec["budget"])
            require_session_sources(runtime, principal, spec)
            if body.candidate_hash or body.accept_unknown_charge:
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            approved = request.query_params.get("approved_spec_sha256")
            if approved != row["spec_hash"]:
                raise PlatformError("MCP_EXTERNAL_BUDGET_REQUIRED", 403)
        elif not _managed(principal) or request.query_params:
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        return _session_view(principal, service.control(principal, session_id, body.model_dump()))

    return router
