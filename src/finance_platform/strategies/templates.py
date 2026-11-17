"""Five executable engineering examples, generated without importing strategy code."""

import hashlib
import json
from pathlib import Path

from .contracts import ID, STYLE, StrategyError, canonical, digest


SOURCES = {
    "event": '''from decimal import Decimal
def run(context):
    state = dict(context["state"])
    state["events"] = state.get("events", 0) + 1
    state["timers"] = state.get("timers", 0) + (context["event_type"] == "timer")
    outputs = []
    for row in context["rows"]:
        signal = "1" if Decimal(row["close"]) >= Decimal(context["parameters"]["threshold"]) else "0"
        outputs.append({"kind": "signal", "instrument_id": row["instrument_id"], "value": signal, "decision_at": context["clock"]})
    return {"outputs": outputs, "state": state}
''',
    "batch": '''from decimal import Decimal
def run(context):
    rows = context["rows"]
    values = [Decimal(row["close"]) for row in rows]
    mean = sum(values) / len(values) if values else Decimal(0)
    outputs = [{"kind": "score", "instrument_id": row["instrument_id"], "value": format(value - mean, ".8f"), "decision_at": context["clock"]} for row, value in zip(rows, values)]
    return {"outputs": outputs, "state": {"batches": context["state"].get("batches", 0) + 1}}
''',
    "target": '''def run(context):
    outputs = [{"kind": context["parameters"]["kind"], "instrument_id": row["instrument_id"], "value": context["parameters"]["target"], "decision_at": context["clock"]} for row in context["rows"]]
    return {"outputs": outputs, "state": {"decisions": context["state"].get("decisions", 0) + 1}}
''',
    "model": '''from decimal import Decimal
def run(context):
    model = context["model"]
    mean, scale, coefficient, intercept = (Decimal(model[x][0]) if isinstance(model[x], list) else Decimal(model[x]) for x in ("mean", "scale", "coefficients", "intercept"))
    outputs = []
    for row in context["rows"]:
        prediction = (Decimal(row["close"]) - mean) / scale * coefficient + intercept
        outputs.append({"kind": "score", "instrument_id": row["instrument_id"], "value": format(prediction, ".8f"), "decision_at": context["clock"]})
    return {"outputs": outputs, "state": {"model_version": model["version"]}}
''',
    "external": '''from datetime import datetime, timedelta
def run(context):
    # This is a process-out structured producer. Ingestion independently checks
    # source authentication, sequence, expiry and then the common trade boundary.
    sequence = context["state"].get("sequence", 0) + 1
    outputs = [{"kind": "target_quantity", "instrument_id": row["instrument_id"], "value": context["parameters"]["quantity"], "decision_at": context["clock"]} for row in context["rows"]]
    expires = (datetime.fromisoformat(context["clock"].replace("Z", "+00:00")) + timedelta(seconds=60)).isoformat()
    message = {"source_id": context["source_id"], "sequence": sequence, "message_id": context["source_id"] + "-" + str(sequence), "produced_at": context["clock"], "effective_at": context["clock"], "expires_at": expires, "mode": context["mode"], "outputs": outputs}
    return {"outputs": outputs, "state": {"sequence": sequence}, "message": message}
''',
}


def template(destination, strategy_id, style="event", *, model=None):
    if not isinstance(strategy_id, str) or not ID.fullmatch(strategy_id) or style not in STYLE:
        raise StrategyError("TEMPLATE_ID_OR_STYLE_INVALID")
    destination = Path(destination)
    if destination.exists():
        raise StrategyError("TEMPLATE_DESTINATION_EXISTS")
    lock = canonical({"python": "3.12.14", "packages": []})
    parameter_sets = {
        "event": ({"threshold": {"type": "string"}}, ["threshold"]),
        "batch": ({}, []),
        "target": ({"kind": {"type": "string", "enum": ["target_weight", "target_quantity"]}, "target": {"type": "string"}}, ["kind", "target"]),
        "model": ({}, []), "external": ({"quantity": {"type": "string"}}, ["quantity"]),
    }
    props, required = parameter_sets[style]
    if style == "model" and model is None:
        raise StrategyError("MODEL_REFERENCE_REQUIRED")
    manifest = {"sdk_version": "1.0", "id": strategy_id, "version": "1.0.0", "entrypoint": "strategy:run", "style": style,
                "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
                "data": {"fields": ["close"], "frequencies": ["1d", "1m"]}, "assets": ["US_EQUITY", "US_ETF"],
                "outputs": {"event": ["signal"], "batch": ["score"], "target": ["target_weight", "target_quantity"],
                            "model": ["score"], "external": ["target_quantity"]}[style],
                "modes": ["BACKTEST", "LOCAL_SIMULATION"],
                "dependency_lock": {"file": "dependencies.lock.json", "sha256": hashlib.sha256(lock).hexdigest(), "python": "3.12.14"},
                "model": model, "state": {"version": 1, "restore": "json_checkpoint"}}
    destination.mkdir(parents=True)
    (destination / "manifest.json").write_bytes(canonical(manifest))
    (destination / "dependencies.lock.json").write_bytes(lock)
    (destination / "strategy.py").write_text(SOURCES[style], encoding="utf-8")
    (destination / "README.txt").write_text("Synthetic engineering example only; no Alpha or investment claim.\n", encoding="utf-8")
    return manifest
