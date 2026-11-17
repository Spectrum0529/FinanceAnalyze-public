"""Validate a derived working config against the public research template.

Readiness describes configuration prerequisites only. It does not certify data,
model performance, paper execution or permission to place an order.
"""

from copy import deepcopy
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re

from predictor.access import AccessPolicy
from predictor.secrets import reject_credentials


SOURCE_PATH = "resources/prediction_protocol.json"
SOURCE_HASH = "f77b5b49f6368d42999ba052ebf5bf15a29ba4c9fd12118b54217350e5c82b09"
WORKING_VERSION = "public-research-config-1"

# Only unresolved operational inputs can change in this working revision.
# Units are explicit; an unknown parameter must stay null, never silently zero.
PARAMETERS = {
    "clock.calendar_provider": ("text", "calendar_configuration_review", "provider_id"),
    "clock.calendar_hash": ("hash", "calendar_configuration_review", "sha256"),
    "models.model_revision_hashes": ("hash_map", "model_revision_review", "model_id_to_sha256"),
    "time_splits.prospective.start_session": ("date", "prospective_release_review", "exchange_session_date"),
    "execution.broker": ("text", "broker_fee_and_deadline_review", "broker_id"),
    "execution.broker_api_version": ("text", "broker_api_version_review", "version"),
    **{f"account.fee_schedule.{key}": ("fee", "broker_fee_and_deadline_review", "explicit_fee_unit")
       for key in ("commission_per_share", "commission_min", "commission_cap",
                   "platform_per_share", "platform_min", "clearing_per_share",
                   "sell_regulatory_rate", "sell_regulatory_min", "activity_fee", "borrow_fee")},
    **{f"risk_limits.{key}": ("rate", "risk_limits_review" if key != "model_error_noninferiority_margin" else "independent_evaluation_review", "decimal")
       for key in ("max_drawdown", "daily_loss_limit", "single_name_limit", "sector_limit",
                   "model_error_noninferiority_margin")},
    "auction.broker_deadline_local": ("time", "broker_fee_and_deadline_review", "America/New_York_local_time"),
    "auction.venue_deadline_local": ("time", "broker_fee_and_deadline_review", "America/New_York_local_time"),
    "auction.verified_latency_buffer_seconds": ("seconds", "broker_fee_and_deadline_review", "seconds"),
    "cost_model.version": ("text", "cost_model_review", "version"),
}
FEES = tuple(p for p in PARAMETERS if p.startswith("account.fee_schedule."))
RISKS = tuple(p for p in PARAMETERS if p.startswith("risk_limits."))
DIRECTION = ("clock.calendar_provider", "clock.calendar_hash", "models.model_revision_hashes")
ECONOMIC = (*FEES, "cost_model.version")
PAPER = (*DIRECTION, *ECONOMIC, *RISKS, "execution.broker", "execution.broker_api_version",
         "auction.broker_deadline_local", "auction.venue_deadline_local",
         "auction.verified_latency_buffer_seconds")
LEVELS = {
    "synthetic_engineering": (), "direction_prediction": DIRECTION,
    "economic_evaluation": (*DIRECTION, *ECONOMIC), "paper_trading": PAPER,
    "live_orders": PAPER,
}
REMAINING_TASKS = {
    "synthetic_engineering": (),
    "direction_prediction": ("data_rights_and_coverage_review", "data_contract_review", "feature_causality_review", "model_revision_review", "execution_boundary_review"),
    "economic_evaluation": ("broker_fee_and_deadline_review", "cost_model_review", "risk_limits_review"),
    "paper_trading": ("risk_limits_review", "paper_execution_review"),
    "live_orders": ("prospective_validation_review", "explicit_live_authorization"),
}


class ProtocolError(ValueError):
    """Malformed configuration or a blocked action."""


def get_path(value, path):
    for part in path.split("."):
        value = value[part]
    return value


def make_working_config(source: dict) -> dict:
    """Derive, never modify, the source protocol."""
    return {
        "schema_version": 1,
        "working_version": WORKING_VERSION,
        "source": {"path": SOURCE_PATH, "sha256": SOURCE_HASH, "version": source["version"]},
        "protocol": deepcopy(source),
        "risk_limits": {p.split(".")[-1]: None for p in RISKS},
        "auction": {"broker_deadline_local": None, "venue_deadline_local": None,
                    "verified_latency_buffer_seconds": None},
        "cost_model": {"version": None},
        "parameter_registry": {p: {"owner_task": owner, "unit": unit}
                               for p, (_, owner, unit) in PARAMETERS.items()},
    }


def _reject_constant(_):
    raise ProtocolError("Non-finite JSON numbers are forbidden")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate JSON keys are forbidden")
        result[key] = value
    return result


def parse_json(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                      object_pairs_hook=_unique_object)


def _number(value, path, *, minimum=0, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ProtocolError(f"{path} must be a finite number, never boolean or text")
    if value < minimum or (maximum is not None and value > maximum):
        raise ProtocolError(f"{path} is outside its declared unit/range")


def _parameter(value, path):
    if value is None:
        return
    kind, _, _ = PARAMETERS[path]
    if kind == "fee":
        if not isinstance(value, dict) or set(value) != {"value", "unit"}:
            raise ProtocolError(f"{path} requires an explicit value and unit, or null")
        key = path.split(".")[-1]
        allowed = {"USD", "USD_per_share", "decimal", "annual_decimal"}
        if key.endswith("per_share"):
            allowed = {"USD_per_share"}
        elif key.endswith("min"):
            allowed = {"USD"}
        elif key == "sell_regulatory_rate":
            allowed = {"decimal"}
        if not isinstance(value["unit"], str) or value["unit"] not in allowed:
            raise ProtocolError(f"{path} has an incompatible fee unit")
        _number(value["value"], path, maximum=1 if value["unit"] in {"decimal", "annual_decimal"} else None)
    elif kind in {"rate", "seconds"}:
        _number(value, path, maximum=1 if kind == "rate" else None)
    elif kind == "hash":
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ProtocolError(f"{path} must be a lowercase SHA-256")
    elif kind == "hash_map":
        if not isinstance(value, dict) or not value or any(
            not isinstance(key, str) or not key.strip() or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest) for key, digest in value.items()
        ):
            raise ProtocolError(f"{path} must map model IDs to lowercase SHA-256 digests")
    elif kind == "date":
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ProtocolError(f"{path} must be YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ProtocolError(f"{path} is not a valid date") from None
    elif kind == "time":
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", value):
            raise ProtocolError(f"{path} must be HH:MM:SS exchange-local time")
    elif not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{path} must be a non-empty string or null")


def _match(value, baseline, path=""):
    # Do type checks before equality: Python considers True==1 and False==0.
    if path in PARAMETERS:
        _parameter(value, path)
        return
    if isinstance(baseline, dict):
        if not isinstance(value, dict) or set(value) != set(baseline):
            raise ProtocolError(f"{path or 'protocol'} has missing or unknown fields")
        for key in baseline:
            _match(value[key], baseline[key], f"{path}.{key}" if path else key)
    elif isinstance(baseline, list):
        if not isinstance(value, list) or len(value) != len(baseline):
            raise ProtocolError(f"{path} must preserve the baseline list")
        for index, (item, original) in enumerate(zip(value, baseline)):
            _match(item, original, f"{path}[{index}]")
    else:
        if type(baseline) in (int, float):
            _number(value, path, minimum=-math.inf)
            if type(baseline) is int and type(value) is not int:
                raise ProtocolError(f"{path} must be an integer")
        elif type(value) is not type(baseline):
            raise ProtocolError(f"{path} has the wrong type")
        if value != baseline:
            raise ProtocolError(f"{path} changes the locked baseline; requires a new protocol revision")


def validate_protocol(values: dict, source: dict) -> dict:
    """Pure validation for callers supplying an already authenticated source."""
    reject_credentials(values)
    expected = make_working_config(source)
    if not isinstance(values, dict) or set(values) != set(expected):
        raise ProtocolError("Working config has missing or unknown top-level fields")
    for key in ("schema_version", "working_version", "source", "parameter_registry"):
        # Registry metadata is locked so changing units cannot legalize 5 as 5%.
        _match(values[key], expected[key], f"metadata.{key}")
    _match(values["protocol"], source)
    for key in ("risk_limits", "auction", "cost_model"):
        _match(values[key], expected[key], key)
    clock = values["protocol"]["clock"]
    if (clock["calendar_provider"] is None) != (clock["calendar_hash"] is None):
        raise ProtocolError("Calendar provider and version hash must be supplied together")
    fees = values["protocol"]["account"]["fee_schedule"]
    if fees["commission_min"] is not None and fees["commission_cap"] is not None:
        if fees["commission_cap"]["unit"] == "USD" and fees["commission_min"]["value"] > fees["commission_cap"]["value"]:
            raise ProtocolError("Commission minimum exceeds the USD cap")
    auction = values["auction"]
    deadlines = (auction["broker_deadline_local"], auction["venue_deadline_local"])
    if all(d is not None for d in deadlines) and auction["verified_latency_buffer_seconds"] is not None:
        seconds = lambda s: sum(int(part) * factor for part, factor in zip(s.split(":"), (3600, 60, 1)))
        effective = min(map(seconds, deadlines)) - auction["verified_latency_buffer_seconds"]
        if seconds(clock["order_submit_target_local"]) > effective:
            raise ProtocolError("Order submit target is later than the buffered auction deadline")
    return deepcopy(values)


def load_protocol(root: Path, path: str | Path = "configs/protocol.json",
                  *, policy: AccessPolicy | None = None) -> dict:
    policy = policy or AccessPolicy(root)
    source_raw = policy.authorize(SOURCE_PATH, operation="config_read").read_bytes()
    if hashlib.sha256(source_raw).hexdigest() != SOURCE_HASH:
        raise ProtocolError("Source protocol hash differs; preserved baseline required")
    raw = policy.authorize(path, operation="config_read").read_bytes()
    return validate_protocol(parse_json(raw), parse_json(source_raw))


def readiness(config: dict) -> dict:
    """Input must be the validated result of load_protocol/validate_protocol."""
    def parameter(path):
        container = config["protocol"] if path.split(".")[0] in {"clock", "account", "execution", "models", "time_splits"} else config
        return get_path(container, path)

    result = {}
    for level, required in LEVELS.items():
        missing = [{"parameter": p, **config["parameter_registry"][p]} for p in required if parameter(p) is None]
        if level == "live_orders":
            missing.append({"parameter": "explicit_scoped_live_authorization", "owner_task": "explicit_live_authorization", "unit": "authorization_record"})
        result[level] = {
            "configuration_ready": not missing,
            "missing_parameters": missing,
            "remaining_verification_tasks": list(REMAINING_TASKS[level]),
            "action_ready": level == "synthetic_engineering",
        }
    return result


def require_action(config: dict, level: str):
    if level not in LEVELS:
        raise ProtocolError("Unknown release level")
    if not readiness(config)[level]["action_ready"]:
        raise ProtocolError(f"{level} blocked: unresolved parameters and/or unimplemented verification gates")
