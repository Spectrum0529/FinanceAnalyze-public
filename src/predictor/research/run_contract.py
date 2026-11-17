"""Minimum ledger validity rules; declarations do not certify market evidence."""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from predictor.secrets import reject_credentials

RUN_REQUIRED = {
    "experiment_id", "hypothesis", "data_hash", "code_hash", "configuration", "seed",
    "search_parent", "information_cutoff", "train_label_maturity_max", "all_metrics",
    "compute_time", "failure", "decision_before_test", "prompt_hash_if_any",
    "model_hash", "kind", "uses_market_data",
}
TERMINAL = {"succeeded", "failed", "interrupted"}

def instant(value, field):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value):
        raise ValueError(f"{field} requires an explicit UTC ISO timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"Invalid {field} timestamp") from None

def digest(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")

def configuration_hash(configuration):
    return hashlib.sha256(json.dumps(configuration, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

def metadata_valid(metadata, *, recorded_at=None, stored=False):
    reject_credentials(metadata)
    if not isinstance(metadata, dict) or RUN_REQUIRED - set(metadata):
        raise ValueError("Run metadata is missing required fields")
    for field in ("experiment_id", "hypothesis", "decision_before_test"):
        if not isinstance(metadata[field], str) or not metadata[field].strip():
            raise ValueError(f"{field} must be a non-empty string")
    kind = metadata["kind"]
    if kind not in {"engineering", "development", "historical", "prospective"}:
        raise ValueError("Invalid run kind")
    if type(metadata["uses_market_data"]) is not bool or (kind == "engineering" and metadata["uses_market_data"]):
        raise ValueError("Invalid engineering/market-data scope")
    digest(metadata["code_hash"], "code_hash")
    if metadata["data_hash"] is None:
        if kind != "engineering" or metadata["uses_market_data"] or not isinstance(metadata.get("data_hash_reason"), str) or not metadata["data_hash_reason"].strip():
            raise ValueError("Absent data requires an engineering-only data_hash_reason")
    else:
        digest(metadata["data_hash"], "data_hash")
    if metadata["model_hash"] is None:
        if not isinstance(metadata.get("model_hash_reason"), str) or not metadata["model_hash_reason"].strip():
            raise ValueError("Absent model requires model_hash_reason")
    else:
        digest(metadata["model_hash"], "model_hash")
    if metadata["prompt_hash_if_any"] is not None:
        digest(metadata["prompt_hash_if_any"], "prompt_hash_if_any")
    if not isinstance(metadata["configuration"], dict) or metadata["failure"] is not None:
        raise ValueError("New runs require a configuration object and failure=null")
    if not isinstance(metadata["all_metrics"], dict) or metadata["compute_time"] is not None:
        raise ValueError("Starting metrics require an object and compute_time=null")
    expected = configuration_hash(metadata["configuration"])
    if (stored or "configuration_hash" in metadata) and metadata.get("configuration_hash") != expected:
        raise ValueError("configuration_hash does not match configuration")
    if metadata["seed"] is not None and type(metadata["seed"]) is not int:
        raise ValueError("seed must be an integer or null (deterministic/non-stochastic run)")
    if metadata["search_parent"] is not None and (not isinstance(metadata["search_parent"], str) or not metadata["search_parent"].strip()):
        raise ValueError("search_parent must be a run ID or null")
    formal = kind in {"historical", "prospective"}
    cutoff = metadata["information_cutoff"]
    cutoff = instant(cutoff, "information_cutoff") if cutoff is not None else None
    if formal and cutoff is None:
        raise ValueError("Formal research requires information_cutoff")
    mature = metadata["train_label_maturity_max"]
    mature = instant(mature, "train_label_maturity_max") if mature is not None else None
    if mature is not None and (cutoff is None or mature > cutoff):
        raise ValueError("Training label maturity must not exceed information_cutoff")
    start = instant(recorded_at, "recorded_at_utc") if recorded_at is not None else datetime.now(timezone.utc)
    if formal and cutoff > start:
        raise ValueError("Formal information_cutoff cannot be later than run registration")
    # The original protocol specifies text, not a required decision-time field.
    # Validate a supplied timestamp; never invent a preregistration timestamp.
    if "decision_recorded_at_utc" in metadata:
        if instant(metadata["decision_recorded_at_utc"], "decision_recorded_at_utc") > start:
            raise ValueError("Decision cannot be recorded after run registration")
    if formal and isinstance(metadata.get("recording_mode"), str) and "retrospect" in metadata["recording_mode"].lower():
        raise ValueError("Retrospective bookkeeping cannot be certified as a formal preregistered run")

def result_valid(status, result):
    reject_credentials(result)
    if status not in TERMINAL:
        raise ValueError("Runs finish as succeeded, failed or interrupted")
    if not isinstance(result, dict) or {"all_metrics", "compute_time", "failure"} - set(result):
        raise ValueError("Results require explicit all_metrics, compute_time and failure fields")
    if not isinstance(result["all_metrics"], dict):
        raise ValueError("Results require an all_metrics object")
    duration = result["compute_time"]
    if type(duration) not in (float, int) or not math.isfinite(duration) or duration < 0:
        raise ValueError("Results require finite nonnegative compute_time seconds")
    if status == "succeeded":
        if result["failure"] is not None:
            raise ValueError("Success requires explicit failure=null")
    elif not isinstance(result["failure"], str) or not result["failure"].strip():
        raise ValueError("Failed/interrupted runs require an explicit failure reason")
