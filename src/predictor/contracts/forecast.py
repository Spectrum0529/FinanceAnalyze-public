"""Validated immutable ex-ante forecast interface; no model or publisher."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re

from predictor.contracts.protocol import SOURCE_HASH
from predictor.secrets import reject_credentials
from predictor.time.calendar import ExchangeCalendar, utc_text


CONTRACT_VERSION = "forecast-label-linkage-1"
REFERENCE = {"entry": "official_primary_listing_opening_auction_session_t",
             "exit": "official_primary_listing_opening_auction_session_t_plus_h"}
QUANTILES = ("0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95")
THRESHOLDS = ("-0.1", "-0.05", "-0.02", "0", "0.02", "0.05", "0.1")
STATUSES = {"RESEARCH_ONLY", "VALID", "LOW_CONFIDENCE", "STALE", "OUT_OF_DOMAIN", "INVALID_DATA", "MODEL_ERROR"}
BINDING = ("forecast_id", "security_id", "asof", "cutoff", "entry_session", "exit_session", "horizon", "returns_units",
           "reference_price_definition", "protocol_hash", "calendar_hash")
FIELDS = {*BINDING, "created_at", "published_at", "p_down", "p_flat", "p_up", "mean_return", "quantiles",
          "threshold_probabilities", "price_anchor", "price_quantiles", "data_snapshot_hash", "feature_hash", "model_hash",
          "calibrator_hash", "model_version", "model_hash_reason", "evidence_ids", "applicability_status", "invalidation_events", "binary_decision"}


class LinkageError(ValueError):
    """Rejected interface/link; messages do not echo input contents."""


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise LinkageError(f"{field} must be a non-empty string")
    return value


def number(value, field, *, minimum=None, maximum=None):
    try:
        finite = type(value) in {float, int} and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise LinkageError(f"{field} has invalid numeric type, units or range")
    return value


def timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise LinkageError("Timestamp must be ISO with explicit timezone") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LinkageError("Timestamp must have an explicit timezone")
    return value.astimezone(timezone.utc)


def digest(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise LinkageError(f"{field} must be lowercase SHA-256")
    return value


def canonical(value):
    reject_credentials(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def binding(value, calendar):
    for field in ("forecast_id", "security_id"):
        text(value[field], field)
    if type(value["horizon"]) is not int or value["horizon"] not in {1, 5, 20}:
        raise LinkageError("horizon must be 1, 5 or 20 trading sessions, never boolean")
    clock = calendar.clock(value["entry_session"])
    if calendar.shift(value["entry_session"], value["horizon"]).isoformat() != value["exit_session"]:
        raise LinkageError("Exit session does not match the trading-session horizon")
    for field in ("asof", "cutoff"):
        if timestamp(value[field]) != clock.cutoff_utc:
            raise LinkageError("asof/cutoff must be the entry-session NY 08:45 cutoff")
        value[field] = utc_text(timestamp(value[field]))
    if value["returns_units"] != "decimal" or value["reference_price_definition"] != REFERENCE:
        raise LinkageError("Return units and official auction references are locked")
    if value["protocol_hash"] != SOURCE_HASH or value["calendar_hash"] != calendar.calendar_hash:
        raise LinkageError("Protocol/calendar binding differs")
    return clock


@dataclass(frozen=True)
class Forecast:
    payload: str

    def record(self):
        return json.loads(self.payload)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload.encode()).hexdigest()

    @classmethod
    def from_mapping(cls, value, calendar: ExchangeCalendar):
        reject_credentials(value)
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise LinkageError("Forecast has missing/unknown fields; future realized values are forbidden")
        value = json.loads(canonical(value))
        clock = binding(value, calendar)
        created, published = timestamp(value["created_at"]), timestamp(value["published_at"])
        if not clock.cutoff_utc <= created <= published < clock.open_utc:
            raise LinkageError("Forecast creation/publication must follow cutoff and precede the entry auction")
        value["created_at"], value["published_at"] = utc_text(created), utc_text(published)
        status = value["applicability_status"]
        if not isinstance(status, str) or status not in STATUSES or value["invalidation_events"] != []:
            raise LinkageError("Initial status invalid; invalidation events must be appended separately")
        if status == "VALID" and published > clock.publish_target_utc:
            raise LinkageError("Late publication cannot be marked VALID")
        probabilities = [value[field] for field in ("p_down", "p_flat", "p_up")]
        if any(p is not None for p in probabilities):
            for p in probabilities:
                number(p, "probability", minimum=0, maximum=1)
            if not math.isclose(sum(probabilities), 1, abs_tol=1e-12, rel_tol=0):
                raise LinkageError("Three-way probabilities must sum to one")
        if value["mean_return"] is not None:
            number(value["mean_return"], "mean_return", minimum=-1)
        for field, keys in (("quantiles", QUANTILES), ("price_quantiles", QUANTILES), ("threshold_probabilities", THRESHOLDS)):
            points = value[field]
            if not isinstance(points, dict) or (points and set(points) != set(keys)):
                raise LinkageError(f"{field} requires the locked keys or an explicit empty map")
            if points:
                numbers = [number(points[k], field, minimum=0 if field != "quantiles" else -1,
                                  maximum=1 if field == "threshold_probabilities" else None) for k in keys]
                ordered = sorted(numbers, reverse=field == "threshold_probabilities")
                if numbers != ordered:
                    raise LinkageError(f"{field} violates monotonicity")
                if field == "threshold_probabilities" and (value["p_up"] is None or not math.isclose(points["0"], value["p_up"], abs_tol=1e-12, rel_tol=0)):
                    raise LinkageError("P(R>0) must equal p_up")
        anchor = value["price_anchor"]
        if anchor is not None:
            if not isinstance(anchor, dict) or set(anchor) != {"definition", "value_usd", "asof"} or anchor["definition"] != "previous_session_close_USD":
                raise LinkageError("Price anchor must explicitly name the previous-session close")
            number(anchor["value_usd"], "price_anchor", minimum=0)
            if anchor["value_usd"] == 0 or timestamp(anchor["asof"]) > clock.cutoff_utc:
                raise LinkageError("Price anchor must be positive and known before cutoff")
            anchor["asof"] = utc_text(timestamp(anchor["asof"]))
        if value["price_quantiles"] and anchor is None:
            raise LinkageError("Price quantiles require their separate previous-close anchor, never an R-to-price shortcut")
        for field in ("data_snapshot_hash", "feature_hash", "model_hash", "calibrator_hash"):
            if value[field] is not None:
                digest(value[field], field)
        text(value["model_version"], "model_version")
        if value["model_hash"] is None:
            text(value["model_hash_reason"], "model_hash_reason")
        elif value["model_hash_reason"] is not None:
            raise LinkageError("Existing model hash must not carry an absent-model reason")
        if status == "VALID" and (value["model_hash"] is None or value["p_up"] is None):
            raise LinkageError("VALID forecast requires a model hash and probabilities")
        if not isinstance(value["evidence_ids"], list) or any(not isinstance(v, str) or not v.strip() for v in value["evidence_ids"]):
            raise LinkageError("evidence_ids must be a list of non-empty identifiers")
        decision = value["binary_decision"]
        if decision is not None:
            if not isinstance(decision, dict) or set(decision) != {"rule_id", "up_threshold", "tie_policy", "predicted_up"}:
                raise LinkageError("Binary decision requires a frozen rule and prediction")
            text(decision["rule_id"], "rule_id")
            threshold = number(decision["up_threshold"], "up_threshold", minimum=0, maximum=1)
            if decision["tie_policy"] not in ("up", "non_up") or type(decision["predicted_up"]) is not bool or value["p_up"] is None:
                raise LinkageError("Invalid frozen binary decision")
            predicted = value["p_up"] > threshold or (value["p_up"] == threshold and decision["tie_policy"] == "up")
            if decision["predicted_up"] != predicted:
                raise LinkageError("Binary decision contradicts its frozen rule")
        return cls(canonical(value))
