"""Authenticated external messages with persistent deduplication/sequence enforcement."""

from datetime import timedelta
import hashlib
import hmac
import json
import os
import re

from finance_platform.core.contracts import StrategyOutput, OutputKind, RunMode, parse_instant, parse_decimal
from .contracts import StrategyError, canonical, digest


class ExternalSignalGate:
    def __init__(self, strategies):
        self.strategies = strategies

    def accept(self, principal, instance_id, message, signature, *, expected_source, secret_env, now, acceptance_hook=None):
        instance = self.strategies.instance(principal, instance_id, write=True)
        if instance["status"] != "RUNNING":
            raise StrategyError("EXTERNAL_STRATEGY_NOT_RUNNING")
        if not re.fullmatch(r"FINANCE_PLATFORM_[A-Z0-9_]+_SECRET", secret_env) or not isinstance(signature, str):
            raise StrategyError("EXTERNAL_AUTH_CONFIGURATION_INVALID")
        secret = os.environ.get(secret_env)
        if not secret or len(secret) < 32:
            raise StrategyError("EXTERNAL_AUTH_NOT_CONFIGURED")
        encoded = canonical(message)
        if len(encoded) > 64_000:
            raise StrategyError("EXTERNAL_MESSAGE_TOO_LARGE")
        expected = hmac.new(secret.encode(), encoded, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise StrategyError("EXTERNAL_AUTH_FAILED")
        if not isinstance(message, dict) or set(message) != {"source_id", "sequence", "message_id", "produced_at", "effective_at", "expires_at", "mode", "outputs"}:
            raise StrategyError("EXTERNAL_MESSAGE_FIELDS_INVALID")
        if message["source_id"] != expected_source or type(message["sequence"]) is not int or not 1 <= message["sequence"] < 2**63:
            raise StrategyError("EXTERNAL_SOURCE_OR_SEQUENCE_INVALID")
        produced, effective, expires = (parse_instant(message[k]) for k in ("produced_at", "effective_at", "expires_at"))
        if not produced <= effective <= now < expires or now-produced > timedelta(minutes=5) or expires-produced > timedelta(minutes=5):
            raise StrategyError("EXTERNAL_MESSAGE_EXPIRED_OR_FUTURE")
        config = self.strategies.config(principal, instance["config_id"])
        if message["mode"] != config["mode"] or message["mode"] != "LOCAL_SIMULATION":
            raise StrategyError("EXTERNAL_MODE_MISMATCH")
        if not isinstance(message["outputs"], list) or not 1 <= len(message["outputs"]) <= 100:
            raise StrategyError("EXTERNAL_OUTPUT_COUNT_INVALID")
        outputs = []
        for i, item in enumerate(message["outputs"]):
            if not isinstance(item, dict) or set(item) != {"instrument_id", "kind", "value"} or item["kind"] not in {"target_quantity", "target_weight", "signal", "score"}:
                raise StrategyError("EXTERNAL_OUTPUT_INVALID")
            units = {"target_quantity": "shares", "target_weight": "decimal_weight", "signal": "direction", "score": "dimensionless"}
            outputs.append(StrategyOutput("ext-"+digest({"message": message, "i": i})[:32], item["instrument_id"], OutputKind(item["kind"]),
                           parse_decimal(item["value"]), units[item["kind"]], produced, effective, expires, RunMode.LOCAL_SIMULATION))
        sha = digest(message)
        with self.strategies.database.transaction() as con:
            current = con.execute("SELECT status FROM strategy_instances WHERE instance_id=?", (instance_id,)).fetchone()
            if current is None or current["status"] != "RUNNING":
                raise StrategyError("EXTERNAL_STRATEGY_NOT_RUNNING")
            rows = con.execute("SELECT sequence,message_hash FROM strategy_external_messages WHERE instance_id=? AND source_id=? ORDER BY sequence DESC LIMIT 1", (instance_id, expected_source)).fetchone()
            exact = con.execute("SELECT message_hash FROM strategy_external_messages WHERE instance_id=? AND source_id=? AND sequence=?", (instance_id, expected_source, message["sequence"])).fetchone()
            if exact:
                if exact["message_hash"] != sha:
                    raise StrategyError("EXTERNAL_DUPLICATE_CONFLICT")
                return {"duplicate": True, "outputs": (), "message_hash": sha}
            if rows and message["sequence"] != rows["sequence"]+1 or not rows and message["sequence"] != 1:
                raise StrategyError("EXTERNAL_SEQUENCE_GAP_OR_REORDER")
            con.execute("INSERT INTO strategy_external_messages VALUES(?,?,?,?,?,?)", (instance_id, expected_source, message["sequence"], sha, encoded.decode(), now.isoformat()))
            if acceptance_hook:
                acceptance_hook(con, sha, message)
        # Stored message becomes a durable outbox input. Sending is performed by a
        # common worker/trading handler; accepting a message never sends an order.
        self.strategies.runtime.audit(principal, "strategy.external.accept", instance_id, {"message_hash": sha, "sequence": message["sequence"]})
        return {"duplicate": False, "outputs": tuple(outputs), "message_hash": sha}
