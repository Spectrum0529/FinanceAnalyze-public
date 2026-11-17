"""An explicit delegated surface over the existing product routes."""
import hashlib
import json
import re
from functools import lru_cache
from contextlib import closing
from urllib.parse import parse_qsl, quote

from pydantic import ValidationError

from .errors import PlatformError
from .mcp_data_policy import local_only, require_dataset
from .mcp_grants import permits
from .mcp_operations import account_extensions, account_variant, operations
from .mcp_proof import EXECUTE, PROOF_HEADER, SESSION, listener_origin

READ_OPERATIONS = {
    "application_capabilities": "/capabilities",
    "data_datasets_list": "/data/library/datasets",
    "data_dataset_read": "/data/library/datasets/{dataset_id}",
    "backtest_catalog": "/research/portfolio/catalog",
    "jobs_list": "/jobs",
    "job_status": "/jobs/{job_id}",
    "job_events": "/jobs/{job_id}/events",
    "results_list": "/results",
    "result_read": "/results/{result_id}/summary",
    "result_section": "/results/{result_id}/series",
    "result_export": "/results/{result_id}/export-manifest",
}
RESEARCH_OPERATIONS = {"backtest_start": "/research/portfolio/backtests", "job_cancel": "/jobs/{job_id}/cancel"}
BROWSE_OPERATIONS = frozenset("""
    application_overview model_capabilities model_profiles_list model_profile_read model_profile_revision
    team_capabilities team_presets_list team_preset_read team_snapshot_read computation_cache_get
    data_provider_catalog data_connections_list data_universes_list benchmarks_list
    daily_backtest_catalog strategies_list strategy_configs_list strategy_instances_list strategy_config_read
    strategy_instance_read ai_task_status native_ta_capabilities native_qlib_capabilities native_qlib_templates
    trained_models_list trained_model_read histories_list history_read history_rows
    results_search result_management market_data_list market_data_read data_connections_manage_list jobs_capabilities
    strategy_search_catalog strategy_search_evidence strategy_search_evidence_section
    ai_task_model_usage_read ai_task_model_trace_read
    goal_research_readiness goal_research_defaults goal_research_list goal_research_read goal_research_model_usage_read
    autonomous_capabilities autonomous_sessions_list autonomous_session_read autonomous_session_progress autonomous_model_usage_read
    native_qlib_exports native_ta_status
    onboarding_first_example_status
""".split())
MANAGE_OPERATIONS = frozenset("""
    model_profile_create model_profile_update model_profile_copy model_profile_delete team_compose_from_profiles
    team_preview team_preset_create team_preset_update team_preset_copy team_preset_delete team_snapshot_create
    computation_cache_settings computation_cache_clear data_connection_create data_universe_resolve
    data_csv_preview
    strategy_config_create strategy_instance_create strategy_instance_validate
    market_data_lifecycle market_data_reuse market_merge_preview market_data_merge data_connection_lifecycle
    result_lifecycle results_lifecycle
    goal_research_save_defaults goal_research_control autonomous_session_control native_ta_cancel ai_task_cancel
""".split())
WORKBENCH_OPERATIONS = frozenset("""
    portfolio_backtest_start strategy_template_create data_dataset_import data_provider_download history_download history_search
    daily_backtest_start ai_task_plan account_ai_task_start job_retry job_verified_resume
    autonomous_prepare autonomous_session_create native_qlib_input_prepare native_qlib_input_register native_qlib_start native_ta_start
    research_synthetic_sample
    onboarding_first_example_start onboarding_first_example_action
""".split())
EXTERNAL_OPERATIONS = {"goal_research_create", "native_ta_external_capabilities", "native_ta_external_start"}
PATH_FIELD = re.compile(r"\{[a-z_]+\}")
FIELD_VALUE = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"
HEADER = PROOF_HEADER.lower().encode("ascii")


def operation_capability(op):
    if op.method == "GET" and READ_OPERATIONS.get(op.name) == op.path:
        return "read"
    if op.method == "POST" and RESEARCH_OPERATIONS.get(op.name) == op.path:
        return "research"
    if op.name in BROWSE_OPERATIONS and op.method == "GET" and not op.external:
        return "browse"
    if op.name in MANAGE_OPERATIONS and not op.external:
        return "manage"
    if op.name in WORKBENCH_OPERATIONS:
        return "workbench"
    if op.name in EXTERNAL_OPERATIONS:
        return "external"
    return None


@lru_cache(maxsize=1)
def account_operations():
    variants = (account_variant(op) for op in (*account_extensions(), *operations()))
    return tuple(op for op in variants
                 if op.response_format == "json" and operation_capability(op) is not None)


def permitted_operations(grant_scope):
    return frozenset(op.name for op in account_operations() if permits(grant_scope, operation_capability(op)))


def _path_pattern(operation):
    parts = PATH_FIELD.split(operation.path)
    pattern = re.escape(parts[0])
    for field, suffix in zip(operation.path_fields, parts[1:], strict=True):
        pattern += f"(?P<{field}>{FIELD_VALUE})" + re.escape(suffix)
    return re.compile(pattern)


def _query_parameters(raw):
    try:
        pairs = parse_qsl(raw.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                          errors="strict", max_num_fields=20)
    except (UnicodeError, ValueError):
        raise PlatformError("INVALID_INPUT", 422) from None
    if len(dict(pairs)) != len(pairs):
        raise PlatformError("INVALID_INPUT", 422)
    return dict(pairs)


def signed_path(scope):
    path = scope["path"]
    return path == SESSION or path == EXECUTE or path.startswith(EXECUTE + "/")


class McpAccess:
    def __init__(self, grants, origins, *, runtime=None):
        self.grants = grants
        self.origins = origins
        self.runtime = runtime
        self.routes = tuple(sorted(((op, _path_pattern(op)) for op in account_operations()),
                                   key=lambda entry: len(entry[0].path_fields)))

    def authorize(self, scope, body):
        headers = scope["headers"]
        proofs = [value for name, value in headers if name.lower() == HEADER]
        if len(proofs) != 1 or any(name.lower() in {b"cookie", b"authorization", b"x-csrf-token"} for name, _ in headers):
            raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 401)
        origin = listener_origin(self.origins, scope, bridge=True)
        path, method = scope["path"], scope["method"]
        try:
            raw_path = scope["raw_path"]
            if quote(path, safe="/").encode("ascii") != raw_path:
                raise ValueError
            proof = proofs[0].decode("ascii")
        except (KeyError, UnicodeError, ValueError):
            raise PlatformError("MCP_AUTHORIZATION_REQUIRED", 401) from None
        query = scope["query_string"]
        target = raw_path + (b"?" + query if query else b"")
        native_path, matches = path, []
        if path != SESSION or method != "GET":
            route = path.removeprefix(EXECUTE)
            for op, pattern in self.routes:
                matched = pattern.fullmatch(route) if op.method == method else None
                if matched is not None:
                    matches.append((op, matched.groupdict()))
            if not matches:
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            best = len(matches[0][1])
            matches = [(op, params) for op, params in matches if len(params) == best]
            native_path = "/api/v1" + route
        capabilities = {operation_capability(op) for op, _ in matches}
        required = next((cap for cap in ("read", "research", "browse", "manage", "workbench", "external")
                         if cap in capabilities), "read")
        query_params = _query_parameters(query)
        observe_tools = path == SESSION and method == "GET" and query_params == {"observe": "tools"} and not body
        principal, grant_scope = self.grants.authorize(proof, origin=origin, method=method, target=target,
                                                       body=body, required=required, observe_tools=observe_tools)
        state = {**scope["state"], "mcp_principal": principal, "mcp_grant_scope": grant_scope}
        if matches:
            operation, params = next((match for match in matches if permits(grant_scope, operation_capability(match[0]))),
                                     (None, {}))
            if operation is None:
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            if set(query_params) - set(operation.query_fields):
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            payload = self._validate_body(operation, params, body)
            self._authorize_operation(principal, operation.name, payload, query_params, params)
            if method != "GET" and permits(grant_scope, "manage"):
                state["mcp_transaction_approval"] = (self.grants.database.path, principal.check_approval)
            if operation.name in {"job_cancel", "job_retry", "job_verified_resume", "jobs_capabilities"}:
                state["mcp_authorize_job"] = self._job_approval(principal, operation.name, query_params)
        elif body or query_params and not observe_tools:
            raise PlatformError("INVALID_INPUT", 422)
        return {**scope, "state": state, "path": native_path, "raw_path": quote(native_path, safe="/").encode("ascii")}

    @staticmethod
    def _validate_body(operation, params, body):
        if not operation.body_rest and not operation.body_field:
            if body:
                raise PlatformError("INVALID_INPUT", 422)
            return {}
        try:
            document = json.loads(body)
            if type(document) is not dict:
                raise ValueError
            values = {operation.body_field: document} if operation.body_field else document
            if params.keys() & values.keys() or set(values) & set(operation.query_fields):
                raise ValueError
            operation.input_model.model_validate({**params, **values})
            return document
        except (ValidationError, ValueError):
            raise PlatformError("INVALID_INPUT", 422) from None

    def _authorize_operation(self, principal, name, payload, query, path):
        if name in {"data_connection_create", "data_connection_lifecycle"}:
            config = payload.get("connection", payload)
            if config.get("provider") == "wrds" or (config.get("credential_ref") or "").casefold().startswith("wrds"):
                raise local_only()
        if name == "data_dataset_import":
            require_dataset(payload["dataset"])
        if name in {"history_download", "daily_backtest_start"} and payload.get("provider", "yfinance") != "yfinance":
            raise PlatformError("MCP_PROVIDER_NOT_ALLOWED", 403)
        if name in {"history_read", "history_rows"}:
            self._require_history(principal, path["history_id"], query.get("version"), query.get("include_deleted"))
        if name == "data_provider_download":
            self._public_download(principal, payload["request"])
        if name == "ai_task_plan":
            if payload.get("execution_mode", "tools") != "tools" and principal.mcp_scope != "external":
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            self._require_dataset_id(principal, payload["dataset_id"])
            benchmark = (payload.get("parameters") or {}).get("benchmark_id")
            if benchmark is not None:
                self._require_dataset_id(principal, benchmark)
        if name == "account_ai_task_start":
            self._approve_plan(principal, self._plan(principal, payload["plan_reference"]), query)
        if name in {"strategy_search_evidence", "strategy_search_evidence_section"}:
            plan = self._search_plan(principal, path["job_id"])
            if plan.get("workflow") != "strategy_search" and plan.get("kind") != "strategy_search":
                raise PlatformError("NOT_FOUND", 404)
        if name == "ai_task_cancel":
            with closing(self.grants.database.connect()) as con:
                row = con.execute("SELECT 1 FROM jobs WHERE owner_id=? AND job_id=? AND handler='research.dataset_task'",
                                  (principal.user_id, path["job_id"])).fetchone()
            if row is None:
                raise PlatformError("NOT_FOUND", 404)

    def _search_plan(self, principal, job_id):
        with closing(self.grants.database.connect()) as con:
            row = con.execute("""SELECT parameters_json,parameters_hash FROM jobs WHERE owner_id=? AND job_id=?
                                 AND handler='research.dataset_task'""", (principal.user_id, job_id)).fetchone()
        if row is None:
            raise PlatformError("NOT_FOUND", 404)
        try:
            raw = row["parameters_json"]
            if hashlib.sha256(raw.encode()).hexdigest() != row["parameters_hash"]:
                raise ValueError
            return self._plan(principal, json.loads(raw)["plan_reference"])
        except (KeyError, TypeError, ValueError):
            raise local_only() from None

    def _require_dataset_id(self, principal, dataset_id):
        try:
            require_dataset(self.runtime.dataset_library.get(principal.user_id, dataset_id, selectable=True))
        except (AttributeError, KeyError, TypeError, ValueError):
            raise local_only() from None

    def _require_history(self, principal, history_id, version, include_deleted):
        try:
            from finance_platform.data.native_library_download import gateway_for_runtime
            if version is not None and not re.fullmatch(r"[1-9][0-9]{0,17}", version):
                raise ValueError
            if include_deleted not in {None, "false", "true"}:
                raise ValueError
            history = gateway_for_runtime(self.runtime).summary(
                principal.user_id, history_id, int(version) if version else None,
                include_deleted=include_deleted == "true",
            )
            if history["provider"] != "yfinance":
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise local_only() from None

    def _public_download(self, principal, request):
        try:
            if 'membership' in request or 'resume_id' in request:
                raise ValueError
            connection = self.runtime.dataset_library.connection(principal.user_id, request["connection_id"])
            if connection["provider"] != "yfinance" or connection.get("credential_ref"):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise PlatformError("MCP_PROVIDER_NOT_ALLOWED", 403) from None

    def _plan(self, principal, reference):
        try:
            service = self.runtime.research_task_service
            plan = service._plan(principal, reference)
            service._require_mcp_plan(principal, plan)
            return plan
        except (AttributeError, KeyError, TypeError, ValueError):
            raise local_only() from None

    @staticmethod
    def _approve_plan(principal, plan, query):
        if plan["kind"] != "model_research":
            if query:
                raise PlatformError("INVALID_INPUT", 422)
            return
        if principal.mcp_scope != "external":
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        try:
            keys = ("max_model_calls", "max_input_tokens", "max_output_tokens")
            caps = (32, 2097152, 262144)
            if set(query) != set(keys) or any(not re.fullmatch(r"[1-9][0-9]{0,6}", query[key]) for key in keys):
                raise ValueError
            approved = [int(query[key]) for key in keys]
            budgets = [profile["configuration"]["budgets"] for profile in plan["model_profiles"]]
            if plan.get("workflow") == "strategy_search":
                calls = plan["search_limits"]["max_model_calls"]
                maximum = [calls, *(calls * max(row[key] for row in budgets) for key in ("input_tokens", "output_tokens"))]
            else:
                maximum = [len(budgets) + 1, *(budgets[0][key] + sum(row[key] for row in budgets)
                                                for key in ("input_tokens", "output_tokens"))]
            if any(not 0 < need <= limit <= cap for need, limit, cap in zip(maximum, approved, caps)):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise PlatformError("MCP_EXTERNAL_BUDGET_REQUIRED", 403) from None

    def _job_approval(self, principal, action, query):
        def approve(con, row):
            principal.check_approval(con)
            if not permits(principal.mcp_scope, "workbench"):
                require_portfolio_job(con, row)
                if action in {"job_retry", "job_verified_resume"}:
                    raise PlatformError("MCP_SCOPE_REQUIRED", 403)
                return
            handler = row["handler"]
            raw = row["parameters_json"]
            if hashlib.sha256(raw.encode()).hexdigest() != row["parameters_hash"]:
                raise PlatformError("MCP_SCOPE_REQUIRED", 403)
            try:
                params = json.loads(raw)
                if handler == "research.debate":
                    if action == "job_cancel":
                        return
                    if action in {"job_verified_resume", "jobs_capabilities"}:
                        from .mcp_research import require_external_ta_checkpoint, require_offline_ta_checkpoint
                        if "provider_approval" in params:
                            expected = query.get("approved_execution_sha256")
                            if action == "job_verified_resume" and (expected is None or not re.fullmatch("[0-9a-f]{64}", expected)):
                                raise PlatformError("MCP_EXTERNAL_BUDGET_REQUIRED", 403)
                            require_external_ta_checkpoint(self.runtime, principal, params, expected)
                        else:
                            if query:
                                raise PlatformError("INVALID_INPUT", 422)
                            require_offline_ta_checkpoint(self.runtime, principal, params)
                        return
                if handler == "research.dataset_task":
                    if action != "job_cancel":
                        self._approve_plan(principal, self._plan(principal, params["plan_reference"]), query)
                    return
                if query:
                    raise PlatformError("INVALID_INPUT", 422)
                if handler in {"research.daily_backtest", "data.history_download", "data.history_search"} and params.get("provider", "yfinance") == "yfinance":
                    return
                if handler == "data.library-download":
                    self._public_download(principal, params)
                    return
                if handler in {"data.qlib-prepare", "data.qlib-register", "research.qlib"}:
                    return
            except PlatformError:
                raise
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
            raise PlatformError("MCP_SCOPE_REQUIRED", 403)
        return approve


def require_portfolio_job(connection, row):
    """Read the persisted, owner-bound plan inside the cancellation transaction."""
    if row["handler"] != "research.dataset_task":
        raise PlatformError("MCP_SCOPE_REQUIRED", 403)
    try:
        reference = json.loads(row["parameters_json"])["plan_reference"]
        plan = connection.execute("""
            SELECT payload_json,content_hash FROM resource_versions
            WHERE resource_id=? AND version=? AND owner_id=? AND kind='config'""",
                                  (reference["resource_id"], reference["version"], row["owner_id"])).fetchone()
        if (plan is not None and plan["content_hash"] == reference["content_hash"]
                and hashlib.sha256(plan["payload_json"].encode()).hexdigest() == plan["content_hash"]
                and json.loads(plan["payload_json"])["kind"] == "strategy_validation"):
            return
    except (KeyError, TypeError, ValueError):
        pass
    raise PlatformError("MCP_SCOPE_REQUIRED", 403)
