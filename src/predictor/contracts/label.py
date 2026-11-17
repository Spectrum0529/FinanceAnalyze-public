"""Ex-post label/evaluation links; realized values never enter Forecast."""

from dataclasses import dataclass
import hashlib
import json

from predictor.contracts.forecast import BINDING, Forecast, LinkageError, binding, canonical, digest, number, text, timestamp
from predictor.time.calendar import utc_text


LABEL_FIELDS = {*BINDING, "label_id", "forecast_hash", "previous_label_id", "recorded_at", "status", "return_decimal",
                "binary_up", "direction", "label_mature_at", "exit_auction_verified_at", "corporate_action_status",
                "corporate_action_resolution_at", "cash_entitlement_status", "cash_entitlement_confirmation_at", "evidence_ids"}


def classify_return(value):
    number(value, "return_decimal", minimum=-1)
    return (value > 0, "up" if value > 0 else "down" if value < 0 else "flat")


@dataclass(frozen=True)
class Label:
    payload: str

    def record(self):
        return json.loads(self.payload)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload.encode()).hexdigest()

    @classmethod
    def from_mapping(cls, value, forecast: Forecast, calendar):
        if not isinstance(value, dict) or set(value) != LABEL_FIELDS:
            raise LinkageError("Label has missing or unknown fields")
        value = json.loads(canonical(value))
        binding(value, calendar)
        original = forecast.record()
        if any(value[k] != original[k] for k in BINDING) or value["forecast_hash"] != forecast.fingerprint:
            raise LinkageError("Label does not bind the frozen forecast")
        text(value["label_id"], "label_id")
        if value["previous_label_id"] is not None:
            text(value["previous_label_id"], "previous_label_id")
        value["recorded_at"] = utc_text(timestamp(value["recorded_at"]))
        if timestamp(value["recorded_at"]) < timestamp(original["published_at"]):
            raise LinkageError("Label cannot precede forecast publication")
        status = value["status"]
        if status not in ("MATURE", "UNRESOLVED", "CENSORED", "REFERENCE_UNAVAILABLE"):
            raise LinkageError("Unknown label status")
        events = []
        if value["exit_auction_verified_at"] is not None:
            exit_verified = timestamp(value["exit_auction_verified_at"])
            if exit_verified < calendar.clock(value["exit_session"]).open_utc:
                raise LinkageError("Exit verification precedes the target auction")
            value["exit_auction_verified_at"] = utc_text(exit_verified)
            events.append(exit_verified)
        for field, time_field in (("corporate_action_status", "corporate_action_resolution_at"),
                                 ("cash_entitlement_status", "cash_entitlement_confirmation_at")):
            if value[field] not in ("resolved", "not_applicable", "unresolved"):
                raise LinkageError("Unknown label resolution status")
            if value[field] == "resolved":
                resolved = timestamp(value[time_field])
                value[time_field] = utc_text(resolved)
                events.append(resolved)
            elif value[time_field] is not None:
                raise LinkageError("Unresolved/not-applicable condition cannot claim a resolution time")
        if status == "MATURE":
            if value["exit_auction_verified_at"] is None or "unresolved" in (value["corporate_action_status"], value["cash_entitlement_status"]):
                raise LinkageError("Mature label needs verified exit and all required resolutions")
            mature = max(events)
            if timestamp(value["label_mature_at"]) != mature or timestamp(value["recorded_at"]) < mature:
                raise LinkageError("Label maturity must equal the latest resolution and precede recording")
            value["label_mature_at"] = utc_text(mature)
            expected_up, expected_direction = classify_return(value["return_decimal"])
            if type(value["binary_up"]) is not bool or value["binary_up"] != expected_up or value["direction"] != expected_direction:
                raise LinkageError("Realized classification mismatch: zero is non-up and flat, never down")
        elif any(value[k] is not None for k in ("return_decimal", "binary_up", "direction", "label_mature_at")):
            raise LinkageError("Unresolved/censored/unavailable labels retain null outcomes; never impute zero")
        if not isinstance(value["evidence_ids"], list) or any(not isinstance(v, str) or not v.strip() for v in value["evidence_ids"]):
            raise LinkageError("Label evidence identifiers malformed")
        return cls(canonical(value))

    def require_mature(self, at_time):
        value = self.record()
        if value["status"] != "MATURE" or timestamp(at_time) < max(timestamp(value["label_mature_at"]), timestamp(value["recorded_at"])):
            raise LinkageError("Unresolved or immature label cannot enter evaluation/training")


@dataclass(frozen=True)
class Evaluation:
    payload: str

    def record(self):
        return json.loads(self.payload)

    @classmethod
    def from_mapping(cls, value, forecast: Forecast, label: Label):
        if not isinstance(value, dict) or set(value) != {"evaluation_id", "forecast_id", "forecast_hash", "label_id", "label_hash",
                "evaluated_at", "rule_id", "correct_binary_up"}:
            raise LinkageError("Evaluation has missing or unknown fields")
        value = json.loads(canonical(value))
        text(value["evaluation_id"], "evaluation_id")
        f, l = forecast.record(), label.record()
        if any(l[key] != f[key] for key in BINDING) or l["forecast_hash"] != forecast.fingerprint:
            raise LinkageError("Evaluation label belongs to a different forecast")
        if value["forecast_id"] != f["forecast_id"] or value["forecast_hash"] != forecast.fingerprint or value["label_id"] != l["label_id"] or value["label_hash"] != label.fingerprint:
            raise LinkageError("Evaluation references differ from frozen forecast/label")
        evaluated = timestamp(value["evaluated_at"])
        label.require_mature(evaluated)
        if evaluated < timestamp(l["recorded_at"]):
            raise LinkageError("Evaluation precedes label recording")
        decision = f["binary_decision"]
        if decision is None or value["rule_id"] != decision["rule_id"]:
            raise LinkageError("Evaluation requires the forecast's frozen ex-ante decision rule")
        if type(value["correct_binary_up"]) is not bool or value["correct_binary_up"] != (decision["predicted_up"] == l["binary_up"]):
            raise LinkageError("Evaluation correctness contradicts the linked records")
        value["evaluated_at"] = utc_text(evaluated)
        return cls(canonical(value))
