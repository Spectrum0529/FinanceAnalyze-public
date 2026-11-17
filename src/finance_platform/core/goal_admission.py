"""Freeze source choices and share bounded grants with existing goal ports."""
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
import re
from zoneinfo import ZoneInfo

from finance_platform.core.errors import PlatformError
from finance_platform.data.provider_sdk.history_contracts import HISTORY_EARLIEST


DATA_FIELDS = ("market", "asset", "kind", "frequency", "adjustment", "currency")
MAX_GENERATIONS = 2
MAX_SYMBOLS = 8
MAX_DAYS = 366
CN_SEMANTICS = "CN_EQUITY_RAW_CNY_DAILY_SIMULATION_1"
DATA_GRANT = {"download_jobs": 1, "download_requests": 64, "download_rows": 10000,
              "download_bytes": 64 * 1024**2}
BLOCKERS = {
    "GOAL_MODEL_REQUIRED": "model",
    "GOAL_CREDENTIAL_REQUIRED": "model",
    "GOAL_SOURCE_REQUIRED": "data",
    "GOAL_BUDGET_REQUIRED": "budget",
    "GOAL_DEFAULTS_UNAVAILABLE": "executor",
    "AUTONOMOUS_DEPLOYMENT_UNAVAILABLE": "executor",
}


@dataclass(frozen=True)
class ResearchPreset:
    deadline_seconds: int
    attempts: int


PRESETS = {
    "balanced": ResearchPreset(1800, 1),
    "deep": ResearchPreset(3600, 1),
    "extended": ResearchPreset(86400, 3),
}


def goal_market_as_of(markets, now):
    """Use the exclusive cutoff expected by the current planner contract."""
    from exchange_calendars import get_calendar

    zones = {"US": ("America/New_York", "XNYS"), "CN": ("Asia/Shanghai", "XSHG")}
    cutoffs = []
    for market in markets:
        zone, name = zones[market]
        local_date = now.astimezone(ZoneInfo(zone)).date()
        # US daily aggregates can include extended-session volume, and some
        # provider plans publish the completed bar after New York midnight.
        if market == "US":
            local_date -= timedelta(days=1)
        calendar = get_calendar(name)
        session = calendar.date_to_session(local_date, direction="previous")
        if calendar.session_close(session).to_pydatetime() > now:
            session = calendar.previous_session(session)
        cutoffs.append(session.date() + timedelta(days=1))
    return min(cutoffs).isoformat()


def development_bounds(principal, goal, snapshot):
    """Intersect the public daily-history policy with explicit user limits."""
    if not snapshot["sources"] or any(source["scope"] != "public" for source in snapshot["sources"]):
        return None
    as_of = date.fromisoformat(snapshot["planner_input"]["as_of"])
    latest = as_of - timedelta(days=1)
    pattern = re.compile(
        r"(?:开发(?:期|范围|数据范围)|development(?:-only)?\s+(?:window|range))"
        r"\s*(?:为|是|from|[:：])?\s*(\d{4}-\d{2}-\d{2})"
        r"\s*(?:至|到|through|to|–|—|~)\s*(\d{4}-\d{2}-\d{2})(?![0-9])",
        re.IGNORECASE,
    )
    explicit = pattern.findall(goal)
    # Dates outside an explicit development envelope may be a user restriction
    # or an evaluation split. In that case, preserve the first result.
    if re.search(
        r"(?<![0-9])(?:19|20)\d{2}(?![0-9])|近日|近\s*\d|近[一二三四五六七八九十半]|过去|最近|以来|"
        r"今年|去年|上[月季]|\b(?:last|past|previous|since|between|before|after|"
        r"today|yesterday|year|quarter|month|weeks?|days?)\b",
        pattern.sub("", goal), re.IGNORECASE,
    ):
        return None
    if len(explicit) > 1 or re.search(r"留出|封存|holdout|final\s+test", goal, re.IGNORECASE):
        return None
    start, end = date.fromisoformat(HISTORY_EARLIEST), latest
    if explicit:
        try:
            lower, upper = (date.fromisoformat(value) for value in explicit[0])
        except ValueError:
            return None
        start, end = max(start, lower), min(end, upper)
    return {"start": start.isoformat(), "end": end.isoformat()} if start < end else None


def goal_budgets(*, planning_tokens, research_minimum, review=False, attempts_per_generation=1):
    research = {key: amount * attempts_per_generation for key, amount in research_minimum.items()}
    research["research_attempts"] = research.pop("attempts")
    grants = {
        "PLAN": {"model_requests": 1, "tokens": planning_tokens},
        "DATA": dict(DATA_GRANT),
        "START": research,
    }
    if review:
        grants["REVIEW"] = dict(grants["PLAN"])
    limits = {}
    for stage, grant in grants.items():
        generations = MAX_GENERATIONS if stage in {"DATA", "START"} else 1
        for name, amount in grant.items():
            limits[name] = limits.get(name, 0) + generations * amount
    return limits, grants


def source_for_need(sources, need):
    """Respect the saved source order among exact compatible capabilities."""
    for source in sources:
        if any(all(need.get(key) == row[key] for key in DATA_FIELDS)
               for row in source["capabilities"]):
            return source
    raise PlatformError("GOAL_SOURCE_REQUIRED", 409)


class GoalAdmission:
    def __init__(self, runtime, *, inputs, source_options, planning_tokens, freeze_universes, review=False):
        self.runtime = runtime
        self.inputs = inputs
        self.source_options = source_options
        self.planning_tokens = planning_tokens
        self.freeze_universes = freeze_universes
        self.review = review

    def _profile(self, principal, selection, providers):
        if selection is None:
            raise PlatformError("GOAL_MODEL_REQUIRED", 409)
        try:
            value = self.runtime.model_profiles.prepare_execution_profile(
                principal, selection["profile_id"], selection["revision"],
            )
        except PlatformError as error:
            if error.code.startswith("CREDENTIAL_"):
                raise PlatformError("GOAL_CREDENTIAL_REQUIRED", 409) from error
            if error.status in {404, 409, 422}:
                raise PlatformError("GOAL_MODEL_REQUIRED", 409) from error
            raise
        choice = value["configuration"]["model"]
        if choice["provider"] not in providers:
            raise PlatformError("GOAL_MODEL_REQUIRED", 409)
        if not choice.get("secret_ref") and choice["provider"] != "ollama":
            raise PlatformError("GOAL_CREDENTIAL_REQUIRED", 409)
        return value

    def _team(self, principal, team_id):
        try:
            return self.runtime.agent_teams.get_snapshot(principal, team_id)
        except PlatformError as error:
            if error.status in {404, 409, 422}:
                raise PlatformError("GOAL_MODEL_REQUIRED", 409) from error
            raise

    def freeze(self, principal, settings):
        if self.inputs is None or not self.runtime.autonomous_deployment_status.get("ready"):
            raise PlatformError("AUTONOMOUS_DEPLOYMENT_UNAVAILABLE", 503)
        preset_id = settings.get("budget_preset_id")
        preset = PRESETS.get(preset_id)
        if preset is None:
            raise PlatformError("GOAL_BUDGET_REQUIRED", 409)
        team_id = settings.get("team_snapshot_id")
        role_map = settings.get("team_role_map", {})
        team = self._team(principal, team_id) if team_id else None
        default = team["configuration"].get("profile_refs", {}).get("default") if team else None
        selection = default or settings.get("model_profile")
        providers = self.inputs.supported_providers()
        research = self._profile(principal, selection, providers)
        if default and default["sha256"] != research["sha256"]:
            raise PlatformError("GOAL_MODEL_REQUIRED", 409)
        profiles = {"default": research}
        if role_map:
            try:
                members = self.inputs.team_member_profiles(principal, team, role_map)
            except PlatformError as error:
                if error.status in {404, 409, 422}:
                    raise PlatformError("GOAL_MODEL_REQUIRED", 409) from error
                raise
            for role, profile in members.items():
                profiles[role] = self._profile(principal, profile, providers)
        planner = self._profile(principal, settings.get("planner") or selection, {"openai", "deepseek"})
        sources = self.source_options(principal, settings["connection_ids"])
        if not sources:
            raise PlatformError("GOAL_SOURCE_REQUIRED", 409)
        evaluation = self.inputs.evaluator.capabilities()
        if preset_id not in evaluation.get("goal_round_limits", {}):
            raise PlatformError("AUTONOMOUS_DEPLOYMENT_UNAVAILABLE", 503)
        round_limits = self.inputs.goal_round_limits(evaluation, preset_id)
        reservations = [self.inputs.minimum(evaluation, target, profiles, goal_round=preset_id).model_dump()
                        for target in ("strategy", "factor", "joint")]
        minimum = {key: max(reservation[key] for reservation in reservations) for key in reservations[0]}
        limits, grants = goal_budgets(planning_tokens=self.planning_tokens(planner), research_minimum=minimum,
                                     review=self.review, attempts_per_generation=preset.attempts)
        capabilities = {}
        for source in sources:
            for row in source["capabilities"]:
                if row["adjustment"] == "provider_observed_unverified":
                    continue
                if row["market"] == "CN" and CN_SEMANTICS not in evaluation.get("market_semantics", []):
                    continue
                capabilities[tuple(row[key] for key in DATA_FIELDS)] = row
        if not capabilities:
            raise PlatformError("GOAL_SOURCE_REQUIRED", 409)
        defaults = {}
        for field in DATA_FIELDS:
            options = {row[field] for row in capabilities.values()}
            if len(options) == 1:
                defaults[field] = options.pop()
        metrics = evaluation["metrics"]
        as_of = goal_market_as_of({row["market"] for row in capabilities.values()}, datetime.now(timezone.utc))
        universes = self.freeze_universes(
            self.runtime.dataset_library, principal.user_id, as_of=as_of, max_symbols=MAX_SYMBOLS,
        ) if any(row["market"] == "US" for row in capabilities.values()) else []
        return {
            "deadline_seconds": preset.deadline_seconds,
            "goal_round": preset_id,
            "limits": limits,
            "grants": grants,
            "references": {
                "research_models": {
                    "default_model": {"profile_id": research["profile_id"], "revision": research["revision"]},
                    "team_snapshot_id": team_id,
                    "team_role_map": role_map,
                },
                "research_model_sha256": research["sha256"],
            },
            "sources": sources,
            "planner_profile": planner,
            "planner_input": {
                "as_of": as_of,
                "defaults": {**defaults, "max_days": MAX_DAYS, "max_symbols": MAX_SYMBOLS,
                             "max_prefix_workload": round_limits["max_prefix_workload"]},
                "data_capabilities": list(capabilities.values()),
                "universes": [universe.model_dump(mode="json") for universe in universes],
                "scoring_metrics": {
                    target: [metric for metric in metrics if target == "factor" or metric not in {"cs_ic", "ts_ic"}]
                    for target in ("strategy", "factor", "joint")
                },
            },
        }

    def validate(self, principal, defaults):
        settings = defaults.model_dump()
        if settings["budget_preset_id"] is not None and settings["budget_preset_id"] not in PRESETS:
            raise PlatformError("GOAL_BUDGET_REQUIRED", 422)
        if settings["connection_ids"]:
            self.source_options(principal, settings["connection_ids"])
        if settings["team_snapshot_id"]:
            team = self._team(principal, settings["team_snapshot_id"])
            if settings.get("team_role_map") and self.inputs is not None:
                self.inputs.team_member_profiles(principal, team, settings["team_role_map"])
        for selection in (settings["planner"], settings["model_profile"]):
            if selection:
                self.runtime.model_profiles.revision(principal, selection["profile_id"], selection["revision"])

    def readiness(self, principal, settings):
        presets = [{"id": key, "deadline_seconds": preset.deadline_seconds,
                    "research_attempts_per_generation": preset.attempts, "cost_policy": "COUNTERS_ONLY"}
                   for key, preset in PRESETS.items()]
        if self.inputs is not None and self.runtime.autonomous_deployment_status.get("ready"):
            evaluator = self.inputs.evaluator.capabilities()
            for row in presets:
                if row["id"] in evaluator.get("goal_round_limits", {}):
                    limits = self.inputs.goal_round_limits(evaluator, row["id"])
                    row["compute_seconds_per_round"] = limits["compute_seconds"]
                    row["max_prefix_workload"] = limits["max_prefix_workload"]
        try:
            snapshot = self.freeze(principal, settings)
        except PlatformError as error:
            setting = BLOCKERS.get(error.code)
            if setting is None:
                raise
            return {"ready": False, "blockers": [{"code": error.code, "setting": setting}],
                    "budget_presets": presets, "limits": {}}
        return {"ready": True, "blockers": [], "budget_presets": presets,
                "deadline_seconds": snapshot["deadline_seconds"], "limits": snapshot["limits"]}
