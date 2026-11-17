"""Split roles and append-only exposure/release metadata; never a data reader.

Decisions in the research ledger are authoritative. The JSONL register is a
rebuildable view. History is global across window IDs and protocol revisions.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from predictor.access import AccessPolicy
from predictor.contracts.protocol import SOURCE_HASH, load_protocol, parse_json
from predictor.logging import utc_timestamp
from predictor.research.ledger import Ledger, encode, require_hash
from predictor.secrets import reject_credentials
from predictor.time.calendar import pinned_zone


VERSION = "split-access-public-1"
REGISTER = "docs/test_window_register.jsonl"
ROLE_SOURCE = {"T": "training_initial", "V": "hyperparameter_rolling_validation", "E": "ensemble_E",
               "C": "calibration_C", "D": "policy_and_integration_D", "H": "historical_holdout_H"}
PURPOSE = {"T": "base_model_training", "V": "rolling_hyperparameter_selection", "E": "ensemble_fitting_only",
           "C": "out_of_sample_calibration_only", "D": "policy_and_integration_selection", "H": "candidate_historical_check"}
GATES = ("S02.06", "S03.06", "S06.05", "S08.06", "S10.01", "S11.01")
HASHES = ("data_manifest", "code", "models", "features", "ensemble", "calibration", "policy", "license_evidence")


class WindowError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def period(value):
    if not isinstance(value, list) or len(value) != 2 or any(not isinstance(v, str) for v in value):
        raise WindowError("Period requires two inclusive YYYY-MM-DD bounds")
    try:
        parsed = [date.fromisoformat(v) for v in value]
    except ValueError:
        raise WindowError("Invalid period date") from None
    if [v.isoformat() for v in parsed] != value or parsed[0] > parsed[1]:
        raise WindowError("Period bounds must be canonical and ordered")
    return parsed


def overlaps(a, b):
    a, b = period(a), period(b)
    return a[0] <= b[1] and b[0] <= a[1]


def instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise WindowError("Timestamp must be timezone-aware") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WindowError("Timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def make_policy(source):
    splits = source["time_splits"]
    return {"schema_version": 1, "version": VERSION, "source_protocol_sha256": SOURCE_HASH,
        "actual_coverage": None, "actual_windows": {role: None for role in ROLE_SOURCE},
        "illustrative_only": True, "illustrative_windows": {r: splits[key] for r, key in ROLE_SOURCE.items()},
        "roles": {role: {"purpose": PURPOSE[role], "status": "candidate_not_certified_untouched" if role == "H" else "development",
                         "reader": "locked_evaluator_or_explorer" if role == "H" else "developer"} for role in ROLE_SOURCE},
        "split_unit": splits["split_unit"], "purge": splits["purge"], "embargo": splits["embargo"],
        "oof_generation": splits["OOF_generation"], "prospective": deepcopy(splits["prospective"]),
        "arms": deepcopy(source["updates"]), "primary_horizon": source["targets"]["primary_horizon_sessions"],
        "evaluation_baseline": deepcopy(source["evaluation"]),
        "required_release_gates": list(GATES), "final_comparison": None, "final_evaluation_design": None,
        "access": {"development_mounts": ["public_samples", "development"], "protected_reads_enabled": False,
                   "fixed_label_feedback": False, "roll_label_feedback": "predeclared_recipe_only_mature_before_fit_started_at",
                   "historical_after_view_and_adapt": "exploration_requires_new_independent_window",
                   "protocol_revision_resets_exposure": False},
        "owners": {"actual_coverage_and_splits": "data_rights_and_coverage_review", "final_comparison_and_evaluation": "independent_evaluation_review",
                   "prospective_release": "prospective_release_review", "H_exposure_history": "operator_record_and_independent_review"}}


def load_policy(root, path="configs/split_policy.json"):
    access = AccessPolicy(root)
    source = load_protocol(root, policy=access)["protocol"]
    result = parse_json(access.authorize(path, operation="config_read").read_bytes())
    reject_credentials(result)
    if encode(result) != encode(make_policy(source)):
        raise WindowError("Split policy differs from frozen rules; append a decision and new revision")
    return result


def exposure(events, bounds):
    """Observed market information contaminates overlapping dates regardless of aliases."""
    seen = [e for e in events if e["action"] == "seen" and e["scope"] == "market_data" and overlaps(e["period"], bounds)]
    ids = {e["id"] for e in seen}
    adapted = any(e["action"] == "adapt" and ids.intersection(e["seen_event_ids"]) for e in events)
    downgraded = any(e["action"] == "downgrade" and overlaps(e["period"], bounds) for e in events)
    return {"status": "exploration" if adapted or downgraded else "seen" if seen else "candidate_not_certified_untouched",
            "seen_event_ids": sorted(ids), "certified_untouched": False}


def prospective_bounds(calendar, locked_at):
    # A full session begins on a later NY date, even for a pre-cutoff release.
    next_day = locked_at.astimezone(pinned_zone("America/New_York")).date() + timedelta(days=1)
    start = calendar.resolve(next_day, direction="next")
    return [start.isoformat(), calendar.shift(start, 251).isoformat()]


def validate_lock(value, policy, calendar, events, locked_at):
    reject_credentials(value)
    required = {"release_scope", "coverage", "windows", "comparison", "evaluation_design", "version_hashes", "release_gates",
                "calendar_hash", "source_protocol_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise WindowError("Release requires concrete windows, comparison, evaluation rules, version hashes and all gates")
    if value["release_scope"] != "forecast_only":
        raise WindowError("This window contract only supports direction research; economic/paper release needs additional gates")
    if value["source_protocol_sha256"] != SOURCE_HASH or value["calendar_hash"] != calendar.calendar_hash:
        raise WindowError("Release protocol/calendar differs")
    coverage = period(value["coverage"])
    windows = value["windows"]
    if not isinstance(windows, dict) or set(windows) != {*ROLE_SOURCE, "F_fixed", "F_roll"}:
        raise WindowError("Release requires T/V/E/C/D/H and separate F_fixed/F_roll windows")
    last = None
    for role in ROLE_SOURCE:
        bounds = period(windows[role])
        for day in bounds:
            calendar.resolve(day)
        if bounds[0] < coverage[0] or bounds[1] > coverage[1] or (last is not None and bounds[0] <= last):
            raise WindowError("Historical roles must be chronological, disjoint and within acquired coverage")
        last = bounds[1]
    future = prospective_bounds(calendar, locked_at)
    if windows["F_fixed"] != future or windows["F_roll"] != future or period(future)[0] <= last:
        raise WindowError("Prospective arms must use the first full post-lock NY session and exactly 252 sessions")
    # H remains historical candidate evidence, never promoted to blind final evidence.
    if exposure(events, future)["status"] != "candidate_not_certified_untouched":
        raise WindowError("Prospective window already exposed; choose a new independent window")
    comparison = value["comparison"]
    if not isinstance(comparison, dict) or set(comparison) != {"primary_arm", "primary_comparator", "comparators", "horizon", "pairing", "binary_rule"}:
        raise WindowError("One concrete primary comparison and frozen binary rule required")
    if comparison["primary_arm"] != "F_fixed" or comparison["horizon"] != policy["primary_horizon"] or type(comparison["horizon"]) is not int:
        raise WindowError("Primary arm must be F_fixed with a protocol horizon")
    names = comparison["comparators"]
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names) or len(set(names)) != len(names) or comparison["primary_comparator"] not in names:
        raise WindowError("Comparison set and single primary comparator must be declared")
    baseline = policy["evaluation_baseline"]
    if comparison["pairing"] != baseline["pairing"]:
        raise WindowError("Comparisons must pair identical date/security/horizon/information/costs")
    rule = comparison["binary_rule"]
    if not isinstance(rule, dict) or set(rule) != {"rule_id", "up_threshold", "tie_policy"} or not isinstance(rule["rule_id"], str) or not rule["rule_id"].strip():
        raise WindowError("Frozen binary decision rule required")
    threshold = rule["up_threshold"]
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1 or rule["tie_policy"] not in ("up", "non_up"):
        raise WindowError("Invalid binary threshold/tie rule")
    design = value["evaluation_design"]
    keys = {"baseline_sha256", "block_length_sessions", "minimum_dates", "material_effect", "dependence_analysis_sha256", "extra_embargo_sessions", "maturity_policy"}
    if not isinstance(design, dict) or set(design) != keys or design["baseline_sha256"] != digest(baseline):
        raise WindowError("Frozen evaluation baseline and development dependence design required")
    for name in ("block_length_sessions", "minimum_dates", "extra_embargo_sessions"):
        if type(design[name]) is not int or design[name] < (0 if name == "extra_embargo_sessions" else 1):
            raise WindowError("Evaluation session counts must be explicit nonnegative/positive integers")
    effect = design["material_effect"]
    if type(effect) not in (int, float) or not math.isfinite(effect) or not 0 < effect <= 1 or design["maturity_policy"] != "wait_all_horizons_or_report_censoring":
        raise WindowError("Material effect and post-window label maturity rule required")
    require_hash(design["dependence_analysis_sha256"], "dependence_analysis")
    versions = value["version_hashes"]
    if not isinstance(versions, dict) or set(versions) != set(HASHES):
        raise WindowError("All frozen version/coverage/license hashes required")
    for key, val in versions.items():
        require_hash(val, key)
    gates = value["release_gates"]
    if not isinstance(gates, dict) or set(gates) != set(GATES):
        raise WindowError("All release gates must be declared")
    for gate in gates.values():
        if not isinstance(gate, dict) or set(gate) != {"status", "evidence", "passed_at"} or gate["status"] != "PASSED":
            raise WindowError("Release gate is pending or failed")
        if instant(gate["passed_at"]) > locked_at:
            raise WindowError("Gate completion cannot be later than lock")
        ref = gate["evidence"]
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
            raise WindowError("Gate evidence reference required")
        require_hash(ref["sha256"], "gate_evidence")
    return deepcopy(value)


class WindowRegister:
    def __init__(self, root, *, policy=None, calendar=None):
        self.root = Path(root).resolve()
        self.access = AccessPolicy(self.root)
        self.ledger = Ledger(self.root)
        self.policy = deepcopy(policy) if policy is not None else load_policy(self.root)
        self.calendar = calendar

    @staticmethod
    def _events(connection):
        result = []
        for row in connection.execute("SELECT decision_id,recorded_at_utc,payload FROM decisions ORDER BY event_id"):
            payload = json.loads(row["payload"])
            if payload.get("window_register_version") == VERSION:
                result.append({**payload["window_event"], "recorded_at_utc": row["recorded_at_utc"]})
        return result

    def events(self):
        self.access.authorize("planning/research_ledger.sqlite3", operation="config_read")
        with self.ledger.connection() as connection:
            return self._events(connection)

    def append(self, event):
        event = deepcopy(event)
        reject_credentials(event)
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", event["id"]):
            raise WindowError("Stable unique event ID required")
        action = event.get("action")
        fields = {"inventory": {"scope", "period", "prior_market_exposure", "statement"},
                  "seen": {"scope", "period", "reader", "purpose", "protocol_revision"},
                  "adapt": {"seen_event_ids", "new_protocol_revision", "change", "tasks_to_rerun"},
                  "downgrade": {"period", "reason"}, "lock": {"manifest"}}
        if action not in fields or set(event) != {"id", "action", *fields[action]}:
            raise WindowError("Unknown action or missing/unknown window event fields")
        if action == "inventory":
            if event["scope"] not in ("synthetic_only", "public_calendar_metadata") or event["prior_market_exposure"] != "unknown":
                raise WindowError("Inventory cannot certify untouched market history")
            if event["period"] is not None:
                period(event["period"])
            if not isinstance(event["statement"], str) or not event["statement"].strip():
                raise WindowError("Inventory statement required")
        if action == "seen":
            period(event["period"])
            if event["scope"] not in ("market_data", "synthetic_only", "public_calendar_metadata") or event["reader"] not in ("developer", "evaluator", "online_updater"):
                raise WindowError("Explicit exposure scope and reader required")
            for key in ("purpose", "protocol_revision"):
                if not isinstance(event[key], str) or not event[key].strip():
                    raise WindowError("Exposure purpose/revision required")
        if action == "adapt":
            ids = event["seen_event_ids"]
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                raise WindowError("Adaptation must link previously viewed market windows")
            if not all(isinstance(event[k], str) and event[k].strip() for k in ("new_protocol_revision", "change")) or not isinstance(event["tasks_to_rerun"], list) or not event["tasks_to_rerun"]:
                raise WindowError("Adaptation revision/change/rerun tasks required")
        if action == "downgrade":
            period(event["period"])
            if not isinstance(event["reason"], str) or not event["reason"].strip():
                raise WindowError("Downgrade reason required")
        self.access.authorize("planning/research_ledger.sqlite3", operation="config_read")

        def precondition(connection):
            history = self._events(connection)
            if action == "adapt":
                by_id = {e["id"]: e for e in history}
                if any(i not in by_id or by_id[i]["action"] != "seen" or by_id[i]["scope"] != "market_data" for i in event["seen_event_ids"]):
                    raise WindowError("Adaptation must reference existing market exposure")
            if action == "lock":
                if self.calendar is None:
                    raise WindowError("Locked calendar required")
                validate_lock(event["manifest"], self.policy, self.calendar, history, datetime.now(timezone.utc))
                for gate in event["manifest"]["release_gates"].values():
                    ref = gate["evidence"]
                    if Path(ref["path"]).is_absolute():
                        raise WindowError("Release evidence must be repository-relative")
                    data = self.access.authorize(ref["path"], operation="config_read").read_bytes()
                    if hashlib.sha256(data).hexdigest() != ref["sha256"]:
                        raise WindowError("Release gate evidence hash mismatch")
                # A successful artifact is insufficient: each gate task must actually be done.
                for task in GATES:
                    if self.ledger.task_state(connection, task)["status"] != "done":
                        raise WindowError("Release gate task is not done")

        self.ledger.append_decision("window:" + event["id"], {
            "motivation": "Append window exposure/release metadata", "impact": action,
            "data_seen": [event.get("scope", "linked_prior_events_only")],
            "tasks_to_rerun": event.get("tasks_to_rerun", []), "new_test_window_required": action in ("adapt", "downgrade"),
            "window_register_version": VERSION, "window_event": event}, precondition=precondition)

    def export(self):
        self.access.authorize(REGISTER, operation="config_read")
        # Serialize view exporters with event writers; a crash only leaves a stale view.
        with self.ledger.transaction() as connection:
            target = self.root / REGISTER
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
            temporary.write_text("".join(encode(e) + "\n" for e in self._events(connection)), encoding="utf-8", newline="\n")
            temporary.replace(target)

    def status(self):
        events = self.events()
        expected = "".join(encode(e) + "\n" for e in events)
        raw = self.access.authorize(REGISTER, operation="config_read").read_text(encoding="utf-8")
        if raw != expected:
            raise WindowError("Window view stale or edited; rebuild through WindowRegister.export")
        return {"version": VERSION, "actual_coverage": self.policy["actual_coverage"], "actual_windows": self.policy["actual_windows"],
                "H": exposure(events, self.policy["illustrative_windows"]["H"]), "H_bounds_are_illustrative": True,
                "events": len(events), "release_lock_ids": [e["id"] for e in events if e["action"] == "lock"],
                "final_action_ready": False, "protected_reads_enabled": False,
                "pending": ["acquired_data_coverage_and_actual_splits", "comparison_set", "evaluation_dependence_design", "release_gates"],
                "research_result": "NOT_RUN"}

    def require_final(self, lock_id, *, run_started_at, manifest_hash):
        events = self.events()
        matches = [e for e in events if e["id"] == lock_id and e["action"] == "lock"]
        if not matches:
            raise WindowError("Final evaluation requires an existing immutable release lock")
        locked = matches[0]
        if digest(locked["manifest"]) != manifest_hash or instant(run_started_at) <= instant(locked["recorded_at_utc"]):
            raise WindowError("Final run must start after lock with identical window/comparison/evaluation/version manifest")
        later = events[events.index(locked) + 1:]
        bounds = locked["manifest"]["windows"]["F_fixed"]
        if any(e["action"] == "adapt" or (e["action"] == "downgrade" and overlaps(e["period"], bounds)) for e in later):
            raise WindowError("Post-lock adaptation/downgrade invalidates final claim; new independent lock required")
        return deepcopy(locked["manifest"])  # Metadata validation, never grants protected read access.


def allow_label_feedback(policy, arm, *, label_mature_at, fit_started_at, human_adaptation=False):
    if arm != "F_roll" or human_adaptation is not False:
        raise WindowError("Fixed arm/human adaptation cannot train on final-window labels")
    if policy["arms"]["prequential_secondary"]["human_adaptation"] is not False:
        raise WindowError("Prequential recipe must forbid human adaptation")
    if instant(label_mature_at) >= instant(fit_started_at):
        raise WindowError("Prequential labels must mature strictly before fit starts")
