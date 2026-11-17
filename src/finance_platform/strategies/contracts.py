"""Strict JSON contracts for the trusted strategy subprocess protocol."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

from predictor.secrets import reject_credentials
from finance_platform.core.contracts import parse_decimal, ContractError


class StrategyError(ValueError):
    """Safe stable error code; plugin exception text is never returned."""


KINDS = frozenset({"score", "signal", "target_weight", "target_quantity", "order_intent"})
MODES = frozenset({"BACKTEST", "LOCAL_SIMULATION", "BROKER_PAPER"})
STYLE = frozenset({"event", "batch", "target", "model", "external"})
ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
ENTRY = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*):([a-zA-Z_][a-zA-Z0-9_]*)\Z")


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise StrategyError("NON_JSON_OR_NONFINITE_VALUE") from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def utc(value):
    if not isinstance(value, str):
        raise StrategyError("TIMESTAMP_REQUIRED")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise StrategyError("INVALID_TIMESTAMP") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise StrategyError("TIMEZONE_REQUIRED")
    return result.astimezone(timezone.utc)


def number(value):
    try:
        return parse_decimal(value)
    except ContractError:
        raise StrategyError("DECIMAL_STRING_REQUIRED") from None


def schema_check(schema, value=None, *, validate_value=False):
    """Deliberately small documented JSON Schema subset; unknown keywords fail closed."""
    if not isinstance(schema, dict) or set(schema) - {
            "type", "properties", "required", "additionalProperties", "enum",
            "minimum", "maximum", "default", "description"}:
        raise StrategyError("UNSUPPORTED_PARAMETER_SCHEMA")
    kind = schema.get("type")
    if kind not in {"object", "string", "integer", "number", "boolean"}:
        raise StrategyError("UNSUPPORTED_PARAMETER_TYPE")
    if kind == "object":
        props, required = schema.get("properties", {}), schema.get("required", [])
        if not isinstance(props, dict) or not isinstance(required, list) or any(
                not isinstance(k, str) or k not in props for k in required):
            raise StrategyError("INVALID_PARAMETER_PROPERTIES")
        if schema.get("additionalProperties") is not False:
            raise StrategyError("PARAMETERS_MUST_BE_CLOSED")
        for subschema in props.values():
            schema_check(subschema)
        if validate_value:
            if not isinstance(value, dict) or set(value) - set(props) or set(required) - set(value):
                raise StrategyError("PARAMETER_KEYS_INVALID")
            for key, item in value.items():
                schema_check(props[key], item, validate_value=True)
    elif validate_value:
        valid = {"string": isinstance(value, str), "boolean": isinstance(value, bool),
                 "integer": type(value) is int,
                 "number": type(value) in {int, float} and math.isfinite(value)}[kind]
        if not valid:
            raise StrategyError("PARAMETER_TYPE_INVALID")
        if "enum" in schema and value not in schema["enum"]:
            raise StrategyError("PARAMETER_ENUM_INVALID")
        if kind in {"integer", "number"}:
            if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
                raise StrategyError("PARAMETER_RANGE_INVALID")
    canonical(schema)


@dataclass(frozen=True)
class Manifest:
    raw: dict

    @classmethod
    def parse(cls, raw):
        fields = {"sdk_version", "id", "version", "entrypoint", "style", "parameters",
                  "data", "assets", "outputs", "modes", "dependency_lock", "model", "state"}
        if not isinstance(raw, dict) or set(raw) != fields:
            raise StrategyError("MANIFEST_FIELDS_INVALID")
        reject_credentials(raw)
        if raw["sdk_version"] != "1.0" or not ID.fullmatch(str(raw["id"])) or not VERSION.fullmatch(str(raw["version"])):
            raise StrategyError("MANIFEST_VERSION_OR_ID_INVALID")
        if not ENTRY.fullmatch(str(raw["entrypoint"])) or raw["style"] not in STYLE:
            raise StrategyError("MANIFEST_ENTRY_OR_STYLE_INVALID")
        schema_check(raw["parameters"])
        if not isinstance(raw["data"], dict) or set(raw["data"]) != {"fields", "frequencies"}:
            raise StrategyError("MANIFEST_DATA_INVALID")
        fields_needed = raw["data"]["fields"]
        if not isinstance(fields_needed, list) or not fields_needed or any(not isinstance(x, str) for x in fields_needed):
            raise StrategyError("MANIFEST_DATA_FIELDS_INVALID")
        for name, allowed in (("assets", {"US_EQUITY", "US_ETF"}), ("outputs", KINDS), ("modes", MODES)):
            values = raw[name]
            if not isinstance(values, list) or not values or len(set(values)) != len(values) or not set(values) <= allowed:
                raise StrategyError("MANIFEST_CAPABILITY_INVALID")
        frequencies = raw["data"]["frequencies"]
        if not isinstance(frequencies, list) or not frequencies or not set(frequencies) <= {"1d", "1m"}:
            raise StrategyError("MANIFEST_FREQUENCY_INVALID")
        lock = raw["dependency_lock"]
        if not isinstance(lock, dict) or set(lock) != {"file", "sha256", "python"} or lock["file"] != "dependencies.lock.json" or lock["python"] != "3.12.14" or not re.fullmatch(r"[a-f0-9]{64}", str(lock["sha256"])):
            raise StrategyError("MANIFEST_DEPENDENCIES_INVALID")
        state = raw["state"]
        if not isinstance(state, dict) or set(state) != {"version", "restore"} or state["version"] != 1 or state["restore"] != "json_checkpoint":
            raise StrategyError("MANIFEST_STATE_INVALID")
        model = raw["model"]
        if model is not None:
            if not isinstance(model, dict) or set(model) != {"artifact_id", "sha256", "train_cutoff", "version"} or not re.fullmatch(r"[a-f0-9]{64}", str(model["sha256"])):
                raise StrategyError("MANIFEST_MODEL_INVALID")
            utc(model["train_cutoff"])
        if raw["style"] == "model" and model is None:
            raise StrategyError("MODEL_REFERENCE_REQUIRED")
        canonical(raw)
        return cls(json.loads(canonical(raw)))

    def parameters(self, values):
        reject_credentials(values)
        schema_check(self.raw["parameters"], values, validate_value=True)
        return json.loads(canonical(values))


def validate_output(raw, manifest, clock):
    expected_fields = {"outputs", "state", "message"} if manifest.raw["style"] == "external" else {"outputs", "state"}
    if not isinstance(raw, dict) or set(raw) != expected_fields or not isinstance(raw["state"], dict):
        raise StrategyError("PLUGIN_RESPONSE_INVALID")
    reject_credentials(raw)
    if len(canonical(raw)) > 256_000:
        raise StrategyError("PLUGIN_OUTPUT_TOO_LARGE")
    outputs = raw["outputs"]
    if not isinstance(outputs, list) or len(outputs) > 1000:
        raise StrategyError("PLUGIN_OUTPUT_COUNT_INVALID")
    for output in outputs:
        base = {"kind", "instrument_id", "value", "decision_at"}
        if not isinstance(output, dict) or not base <= set(output) or set(output) - base - {"side", "order_type", "limit_price"}:
            raise StrategyError("STRATEGY_OUTPUT_FIELDS_INVALID")
        if output["kind"] not in manifest.raw["outputs"] or not isinstance(output["instrument_id"], str) or not output["instrument_id"] or len(output["instrument_id"]) > 128:
            raise StrategyError("STRATEGY_OUTPUT_CAPABILITY_INVALID")
        if utc(output["decision_at"]) != utc(clock):
            raise StrategyError("STRATEGY_DECISION_TIME_INVALID")
        value = number(output["value"])
        if output["kind"] == "target_weight" and not 0 <= value <= 1:
            raise StrategyError("TARGET_WEIGHT_INVALID")
        if output["kind"] in {"target_quantity", "order_intent"} and value < 0:
            raise StrategyError("LONG_ONLY_OUTPUT_REQUIRED")
    return raw
