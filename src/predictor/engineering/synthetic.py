"""Guarded, temporary empty-model replay through production contract factories."""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile

from predictor.access import AccessPolicy
from predictor.contracts.forecast import BINDING, Forecast, REFERENCE, canonical, timestamp
from predictor.contracts.label import classify_return
from predictor.contracts.linkage import RecordJournal
from predictor.contracts.protocol import SOURCE_HASH, parse_json, require_action, load_protocol
from predictor.contracts.snapshot import DevelopmentRecords, SnapshotError
from predictor.contracts.training import training_labels
from predictor.research.windows import WindowRegister
from predictor.time.calendar import load_calendar, utc_text


def load_fixture(root, path):
    value = parse_json(AccessPolicy(root).read_bytes(path))
    if not isinstance(value, dict) or set(value) != {"scope", "uses_market_data", "entry_session", "horizon", "records", "outcomes"} or value["scope"] != "synthetic_only" or value["uses_market_data"] is not False:
        raise SnapshotError("Engineering replay accepts only explicit synthetic_only fixtures")
    if not isinstance(value["records"], list) or not isinstance(value["outcomes"], list):
        raise SnapshotError("Synthetic fixture requires record and outcome lists")
    return value


def empty_forecast(snapshot, calendar, *, security_id, entry_session, horizon):
    """No inferred probabilities: hand-authored record binding only."""
    clock = calendar.clock(entry_session)
    if snapshot.record()["cutoff"] != utc_text(clock.cutoff_utc):
        raise SnapshotError("Forecast must bind the same cutoff as its input snapshot")
    if not any(row["metadata"]["security_id"] == security_id for row in snapshot.record()["records"]):
        raise SnapshotError("Empty forecast requires admitted inputs for its security")
    return Forecast.from_mapping({"forecast_id": f"synthetic:{security_id}:{entry_session}:{horizon}", "security_id": security_id,
        "asof": utc_text(clock.cutoff_utc), "cutoff": utc_text(clock.cutoff_utc),
        "created_at": utc_text(clock.cutoff_utc + timedelta(minutes=5)), "published_at": utc_text(clock.publish_target_utc),
        "entry_session": entry_session, "exit_session": calendar.shift(entry_session, horizon).isoformat(), "horizon": horizon,
        "returns_units": "decimal", "reference_price_definition": REFERENCE, "protocol_hash": SOURCE_HASH, "calendar_hash": calendar.calendar_hash,
        "p_down": None, "p_flat": None, "p_up": None, "mean_return": None, "quantiles": {}, "threshold_probabilities": {},
        "price_anchor": None, "price_quantiles": {}, "data_snapshot_hash": snapshot.fingerprint, "feature_hash": None,
        "model_hash": None, "calibrator_hash": None, "model_version": "empty-model-synthetic-interface-v1",
        "model_hash_reason": "Empty model: no probability generation, training or market inference",
        "evidence_ids": ["synthetic_fixture_only"], "applicability_status": "RESEARCH_ONLY", "invalidation_events": [], "binary_decision": None}, calendar)


def synthetic_label(forecast, outcome):
    """An explicit fixture adapter for split/cash/halt scenarios, not a market label engine."""
    required = {"security_id", "status", "entry_usd", "exit_usd", "terminal_shares", "cash_usd", "exit_verified_at",
                "action_resolved_at", "cash_confirmed_at", "recorded_at"}
    if not isinstance(outcome, dict) or set(outcome) != required or outcome["security_id"] != forecast.record()["security_id"]:
        raise SnapshotError("Synthetic outcome does not match forecast security")
    mature = outcome["status"] == "MATURE"
    if not mature and outcome["status"] not in ("CENSORED", "REFERENCE_UNAVAILABLE", "UNRESOLVED"):
        raise SnapshotError("Unknown synthetic outcome status")
    resolutions = [timestamp(outcome[k]) for k in ("exit_verified_at", "action_resolved_at", "cash_confirmed_at") if outcome[k] is not None]
    value, up, direction = None, None, None
    if mature:
        from predictor.contracts.forecast import number
        for k in ("entry_usd", "exit_usd", "terminal_shares", "cash_usd"):
            number(outcome[k], k, minimum=0)
        if outcome["entry_usd"] == 0 or outcome["terminal_shares"] == 0:
            raise SnapshotError("Synthetic entry/share counts must be positive")
        value = (outcome["terminal_shares"] * outcome["exit_usd"] + outcome["cash_usd"] - outcome["entry_usd"]) / outcome["entry_usd"]
        up, direction = classify_return(value)
    else:
        if outcome["exit_usd"] is not None or outcome["terminal_shares"] is not None or outcome["cash_usd"] is not None:
            raise SnapshotError("Unresolved synthetic outcomes must not impute terminal values")
    return {**{k: forecast.record()[k] for k in BINDING}, "forecast_hash": forecast.fingerprint,
        "label_id": forecast.record()["forecast_id"] + ":label", "previous_label_id": None, "recorded_at": outcome["recorded_at"],
        "status": outcome["status"], "return_decimal": value, "binary_up": up, "direction": direction,
        "label_mature_at": utc_text(max(resolutions)) if mature else None, "exit_auction_verified_at": outcome["exit_verified_at"],
        "corporate_action_status": "resolved" if outcome["action_resolved_at"] else "unresolved",
        "corporate_action_resolution_at": outcome["action_resolved_at"],
        "cash_entitlement_status": "resolved" if outcome["cash_confirmed_at"] else "unresolved",
        "cash_entitlement_confirmation_at": outcome["cash_confirmed_at"], "evidence_ids": ["synthetic_split_cash_or_halt_fixture"]}


def replay(root, path):
    root = Path(root).resolve()
    require_action(load_protocol(root), "synthetic_engineering")
    WindowRegister(root).status()  # S01.05 view must match its authoritative decisions.
    fixture = load_fixture(root, path)
    calendar, _ = load_calendar(root)
    records = DevelopmentRecords(calendar)
    for record in fixture["records"]:
        records.append(record)
    entry = fixture["entry_session"]
    snapshot = records.snapshot(cutoff=calendar.clock(entry).cutoff_utc, event_reference=date.fromisoformat(entry))
    base = root / "tmp/synthetic-replay"
    base.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix="合成 空模型 ", dir=base) as name:
        temporary = Path(name).resolve()
        if not temporary.is_relative_to(base):
            raise SnapshotError("Temporary replay journal escapes workspace")
        journal = RecordJournal(temporary, calendar=calendar)
        for outcome in fixture["outcomes"]:
            forecast = empty_forecast(snapshot, calendar, security_id=outcome["security_id"], entry_session=entry, horizon=fixture["horizon"])
            journal.append_forecast(forecast.record())
            label = journal.append_label(synthetic_label(forecast, outcome))
            status = "blocked_unresolved"
            if label.record()["status"] == "MATURE":
                training_labels(journal, [forecast.record()["forecast_id"]], fit_started_at=timestamp(label.record()["recorded_at"]) + timedelta(microseconds=1))
                status = "mature_input_only_no_model_fitted"
            rows.append({"security_id": outcome["security_id"], "forecast_hash": forecast.fingerprint, "model_hash": None,
                         "probabilities": None, "label_status": label.record()["status"], "direction": label.record()["direction"],
                         "return_decimal": label.record()["return_decimal"], "training_input_status": status})
    return {"scope": "synthetic_only", "uses_market_data": False, "snapshot_hash": snapshot.fingerprint,
            "admitted_records": len(snapshot.record()["records"]), "empty_model": True, "model_evaluation": "NOT_RUN",
            "research_result": "NOT_RUN", "rows": rows}
