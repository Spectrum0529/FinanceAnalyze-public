"""Thin binding to the original research profile; no new approval authority."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from predictor.access import AccessPolicy
from predictor.contracts.forecast import Forecast
from predictor.contracts.label import Label, Evaluation
from predictor.contracts.protocol import SOURCE_HASH, SOURCE_PATH, parse_json, validate_protocol

from .contracts import ContractError


@dataclass(frozen=True, slots=True)
class ResearchProfile:
    profile_id: str
    source_hash: str
    working_hash: str
    calendar_config_hash: str
    primary_horizon_sessions: int
    cutoff_local: str
    publish_local: str
    exchange_timezone: str
    entry_reference: str
    exit_reference: str
    formal_research_status: str = "BLOCKED_DATA_UNVERIFIED"

    @classmethod
    def load(cls, root: Path, *, policy: AccessPolicy | None = None):
        gate = policy or AccessPolicy(root)
        values = []
        for path in (SOURCE_PATH, "configs/protocol.json", "configs/protocol.calendar.json"):
            authorized = gate.authorize(path, operation="config_read")
            values.append(authorized.read_bytes())
        source, working, calendar = values
        if hashlib.sha256(source).hexdigest() != SOURCE_HASH:
            raise ContractError("Original research source hash differs")
        # Use the original complete validator over the exact hashed bytes. It
        # permits only its declared unresolved operational extensions and checks
        # every other field, source/version binding, unit and nested key.
        original = parse_json(source)
        try:
            w = validate_protocol(parse_json(working), original)
            validate_protocol(parse_json(calendar), original)
        except ValueError:
            raise ContractError("Research configuration differs from preserved protocol") from None
        p = w["protocol"]
        return cls(p["protocol_id"], SOURCE_HASH, hashlib.sha256(working).hexdigest(), hashlib.sha256(calendar).hexdigest(),
                   5, "08:45:00", "09:00:00", p["clock"]["exchange_timezone"],
                   p["targets"]["entry_reference"], p["targets"]["exit_reference"])

    def require_formal_research(self, root: Path):
        # The original approval gate is the only authority; a platform profile does
        # not certify market coverage, licensing, identity or model performance.
        from predictor.data.approval import ApprovalError, require_use, verified_contract
        try:
            contract = verified_contract(root)
        except FileNotFoundError:
            raise ApprovalError("DATA_PACKAGE_NOT_APPROVED: evidence-unavailable") from None
        return require_use(contract, "nyse", "formal_historical_construction")


__all__ = ["ResearchProfile", "Forecast", "Label", "Evaluation"]
