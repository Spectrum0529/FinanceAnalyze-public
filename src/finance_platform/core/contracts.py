"""Strict, immutable v1 values. Financial values never accept binary floats."""

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from predictor.secrets import reject_credentials

CONTRACT_VERSION = "platform-1"


class ContractError(ValueError):
    """Fixed, input-free error messages safe for service translation."""


class AssetClass(StrEnum):
    US_EQUITY = "US_EQUITY"
    US_ETF = "US_ETF"


class RunMode(StrEnum):
    BACKTEST = "BACKTEST"
    LOCAL_SIMULATION = "LOCAL_SIMULATION"
    BROKER_PAPER = "BROKER_PAPER"
    LIVE = "LIVE"


class OutputKind(StrEnum):
    SCORE = "score"
    SIGNAL = "signal"
    TARGET_WEIGHT = "target_weight"
    TARGET_QUANTITY = "target_quantity"
    ORDER_INTENT = "order_intent"


class DataScope(StrEnum):
    SYNTHETIC = "SYNTHETIC"
    DEVELOPMENT = "DEVELOPMENT"


def identifier(value: str, name: str = "identifier") -> str:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ContractError(f"Invalid {name}")
    return value


def digest(value: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError("Expected lowercase SHA-256")
    return value


def instant(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError("Explicit timezone required")
    return value.astimezone(timezone.utc)


def parse_instant(value: str) -> datetime:
    if type(value) is not str or len(value) > 40 or "T" not in value:
        raise ContractError("ISO timestamp required")
    try:
        return instant(datetime.fromisoformat(value))
    except ValueError:
        raise ContractError("ISO timestamp with timezone required") from None


def decimal_value(value: Decimal, *, minimum: Decimal | None = None,
                  maximum: Decimal | None = None) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ContractError("Finite Decimal required")
    if len(value.as_tuple().digits) > 36 or not -12 <= value.as_tuple().exponent <= 24 or abs(value) >= Decimal("1e24"):
        raise ContractError("Decimal precision or magnitude unsupported")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ContractError("Decimal outside permitted range")
    return value


def parse_decimal(value: str) -> Decimal:
    if type(value) is not str or not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,12})?", value):
        raise ContractError("Decimal must be a bounded base-10 string")
    return decimal_value(Decimal(value))


def _enum(value, enum_type):
    if type(value) is not enum_type:
        raise ContractError("Typed enum required")


def require_runnable_mode(mode: RunMode):
    _enum(mode, RunMode)
    if mode is RunMode.LIVE:
        raise ContractError("LIVE is disabled")


def freeze(value: Any) -> Any:
    """Copy JSON/contract values recursively; no credential-bearing objects."""
    if isinstance(value, Mapping):
        if any(type(k) is not str for k in value):
            raise ContractError("JSON keys must be strings")
        reject_credentials(dict(value))
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if type(value) in (list, tuple):
        return tuple(freeze(v) for v in value)
    if value is None or type(value) in (str, int, bool):
        reject_credentials(value)
        return value
    if type(value) is Decimal:
        return decimal_value(value)
    if type(value) is datetime:
        return instant(value)
    if is_dataclass(value) and type(value).__module__ == __name__:
        return value
    raise ContractError("Unsupported view value; float and service objects forbidden")


def payload(value: Any) -> Any:
    if is_dataclass(value):
        return {f.name: payload(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [payload(v) for v in value]
    if isinstance(value, StrEnum):
        return value.value
    if type(value) is datetime:
        return instant(value).isoformat().replace("+00:00", "Z")
    if type(value) is Decimal:
        return format(decimal_value(value), "f")
    if value is None or type(value) in (str, bool, int):
        return value
    raise ContractError("Non-canonical value")


def canonical(value: Any) -> str:
    result = payload(value)
    reject_credentials(result)
    return json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VersionRef:
    resource_id: str
    version: str
    content_hash: str

    def __post_init__(self):
        identifier(self.resource_id)
        identifier(self.version, "version")
        digest(self.content_hash)


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    ticker: str
    asset_class: AssetClass
    mic: str
    currency: str
    tick_size: Decimal
    lot_size: Decimal
    fractional: bool
    calendar_id: str
    settlement: str
    ticker_valid_from: datetime
    ticker_valid_to: datetime | None = None
    tradable: bool = True

    def __post_init__(self):
        identifier(self.instrument_id, "permanent instrument ID")
        identifier(self.ticker, "ticker")
        _enum(self.asset_class, AssetClass)
        if self.mic not in {"XNYS", "XNAS", "ARCX", "BATS", "IEXG", "XASE"} or self.currency != "USD":
            raise ContractError("Unsupported market or currency")
        if self.calendar_id not in {"XNYS", "XNAS"} or self.settlement != "T+1":
            raise ContractError("Unsupported calendar or settlement")
        for value in (self.tick_size, self.lot_size):
            decimal_value(value, minimum=Decimal("0.000000000001"))
        if type(self.fractional) is not bool or type(self.tradable) is not bool:
            raise ContractError("Boolean capability required")
        if not self.fractional and self.lot_size % 1:
            raise ContractError("Fractional lot requires fractional capability")
        object.__setattr__(self, "ticker_valid_from", instant(self.ticker_valid_from))
        if self.ticker_valid_to is not None:
            object.__setattr__(self, "ticker_valid_to", instant(self.ticker_valid_to))
            if self.ticker_valid_to <= self.ticker_valid_from:
                raise ContractError("Empty ticker validity interval")

    @classmethod
    def from_mapping(cls, value: Mapping):
        expected = {f.name for f in fields(cls)}
        if type(value) is not dict or set(value) != expected:
            raise ContractError("Instrument fields differ from v1")
        v = dict(value)
        try:
            v["asset_class"] = AssetClass(v["asset_class"])
            for k in ("tick_size", "lot_size"):
                v[k] = parse_decimal(v[k])
            for k in ("ticker_valid_from", "ticker_valid_to"):
                v[k] = None if v[k] is None else parse_instant(v[k])
            return cls(**v)
        except (ValueError, TypeError):
            raise ContractError("Invalid instrument") from None


@dataclass(frozen=True, slots=True)
class Provenance:
    record_id: str
    source_id: str
    license_id: str
    content_hash: str
    revision: int
    scope: DataScope
    effective_at: datetime
    available_at: datetime
    received_at: datetime
    valid_to: datetime | None = None

    def __post_init__(self):
        for v in (self.record_id, self.source_id, self.license_id):
            identifier(v)
        digest(self.content_hash)
        if type(self.revision) is not int or self.revision < 1:
            raise ContractError("Positive revision integer required")
        _enum(self.scope, DataScope)
        for name in ("effective_at", "available_at", "received_at"):
            object.__setattr__(self, name, instant(getattr(self, name)))
        if self.valid_to is not None:
            object.__setattr__(self, "valid_to", instant(self.valid_to))
            if self.valid_to <= self.effective_at:
                raise ContractError("Empty economic validity interval")
        # Historical proven availability may precede actual receipt; never infer it.

    def visible(self, *, as_of: datetime, effective_at: datetime) -> bool:
        return self.available_at <= instant(as_of) and self.effective_at <= instant(effective_at) and (
            self.valid_to is None or instant(effective_at) < self.valid_to)


@dataclass(frozen=True, slots=True)
class StrategyOutput:
    output_id: str
    instrument_id: str
    kind: OutputKind
    value: Decimal
    unit: str
    produced_at: datetime
    effective_at: datetime
    expires_at: datetime
    mode: RunMode
    side: str | None = None
    order_type: str | None = None
    limit_price: Decimal | None = None

    def __post_init__(self):
        identifier(self.output_id)
        identifier(self.instrument_id)
        _enum(self.kind, OutputKind)
        require_runnable_mode(self.mode)
        decimal_value(self.value)
        for name in ("produced_at", "effective_at", "expires_at"):
            object.__setattr__(self, name, instant(getattr(self, name)))
        if not self.produced_at <= self.effective_at < self.expires_at:
            raise ContractError("Output time order invalid")
        units = {OutputKind.SCORE: "dimensionless", OutputKind.SIGNAL: "direction",
                 OutputKind.TARGET_WEIGHT: "decimal_weight", OutputKind.TARGET_QUANTITY: "shares",
                 OutputKind.ORDER_INTENT: "shares"}
        if self.unit != units[self.kind]:
            raise ContractError("Output unit mismatch")
        if self.kind is OutputKind.TARGET_WEIGHT:
            decimal_value(self.value, minimum=Decimal(0), maximum=Decimal(1))
        if self.kind is OutputKind.SIGNAL and self.value not in {Decimal(-1), Decimal(0), Decimal(1)}:
            raise ContractError("Signal must be -1, 0 or 1")
        if self.kind in {OutputKind.TARGET_QUANTITY, OutputKind.ORDER_INTENT} and self.value < 0:
            raise ContractError("Short quantities unsupported")
        if self.kind is OutputKind.ORDER_INTENT:
            if self.value == 0 or self.side not in {"BUY", "SELL"} or self.order_type not in {"MARKET", "LIMIT"}:
                raise ContractError("Unsupported order intent")
            if self.order_type == "LIMIT":
                decimal_value(self.limit_price, minimum=Decimal("0.000000000001"))
            elif self.limit_price is not None:
                raise ContractError("Market intent has no limit price")
        elif any(v is not None for v in (self.side, self.order_type, self.limit_price)):
            raise ContractError("Order fields forbidden for non-order outputs")


@dataclass(frozen=True, slots=True)
class RunSpec:
    run_id: str
    owner_id: str
    handler: str
    mode: RunMode
    strategy: VersionRef
    config: VersionRef
    data: VersionRef
    code_hash: str
    dependency_hash: str
    created_at: datetime
    seed: int
    model: VersionRef | None = None
    model_reason: str | None = "No model used"
    parent_run_id: str | None = None
    profile_id: str | None = None

    def __post_init__(self):
        for v in (self.run_id, self.owner_id, self.handler):
            identifier(v)
        require_runnable_mode(self.mode)
        for v in (self.strategy, self.config, self.data):
            if type(v) is not VersionRef:
                raise ContractError("Frozen version reference required")
        for v in (self.code_hash, self.dependency_hash):
            digest(v)
        object.__setattr__(self, "created_at", instant(self.created_at))
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ContractError("Seed must be uint32")
        if self.model is None:
            if type(self.model_reason) is not str or not self.model_reason.strip() or len(self.model_reason) > 200:
                raise ContractError("Missing model requires a reason")
        elif type(self.model) is not VersionRef or self.model_reason is not None:
            raise ContractError("Model reference and null reason required")
        if self.parent_run_id is not None:
            identifier(self.parent_run_id)
            if self.parent_run_id == self.run_id:
                raise ContractError("Retry needs a new run ID")
        if self.profile_id is not None:
            identifier(self.profile_id)


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Value-only allowed view. No DB, filesystem, environment or credentials."""
    as_of: datetime
    mode: RunMode
    config: Mapping[str, Any]
    data_view: Mapping[str, Any]
    account_snapshot: Mapping[str, Any]
    state: Mapping[str, Any]
    allowed_instruments: tuple[str, ...]
    scope: DataScope = DataScope.SYNTHETIC

    def __post_init__(self):
        object.__setattr__(self, "as_of", instant(self.as_of))
        require_runnable_mode(self.mode)
        _enum(self.scope, DataScope)
        if type(self.allowed_instruments) is not tuple or len(set(self.allowed_instruments)) != len(self.allowed_instruments):
            raise ContractError("Unique instrument tuple required")
        for v in self.allowed_instruments:
            identifier(v)
        for name in ("config", "data_view", "account_snapshot", "state"):
            v = getattr(self, name)
            if not isinstance(v, Mapping):
                raise ContractError("Read-only mapping required")
            object.__setattr__(self, name, freeze(v))

    def validate_output(self, output: StrategyOutput):
        if type(output) is not StrategyOutput or output.instrument_id not in self.allowed_instruments:
            raise ContractError("Output instrument not allowed")
        if output.mode != self.mode or output.produced_at != self.as_of:
            raise ContractError("Output mode or decision clock mismatch")


@runtime_checkable
class StrategyPlugin(Protocol):
    contract_version: str

    def on_event(self, context: StrategyContext, event: Mapping[str, Any]) -> tuple[StrategyOutput, ...]: ...

    def snapshot_state(self) -> Mapping[str, Any]: ...


@runtime_checkable
class DataPlugin(Protocol):
    contract_version: str

    def snapshot(self, *, as_of: datetime, instrument_ids: tuple[str, ...]) -> Mapping[str, Any]: ...


@runtime_checkable
class EnginePlugin(Protocol):
    contract_version: str

    def execute(self, run: RunSpec, context: StrategyContext) -> Mapping[str, Any]: ...


@runtime_checkable
class JobContext(Protocol):
    run: RunSpec
    fencing_token: int

    def checkpoint(self, progress: int, metadata: Mapping[str, Any] | None = None) -> None: ...

    def require_active(self) -> None: ...

    def assert_fenced(self, connection: Any) -> None: ...


@runtime_checkable
class JobHandler(Protocol):
    def __call__(self, context: JobContext, parameters: Mapping[str, Any]) -> Mapping[str, Any]: ...
