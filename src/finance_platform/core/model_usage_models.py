"""Strict numeric-only telemetry; none of these fields is a billing estimate."""
from typing import Literal
from pydantic import Field
from .team_profiles import Strict

SPEED = 'CONFIRMED_OUTPUT_TOKENS_OVER_FULL_REQUEST_ELAPSED_INCLUDING_WAIT'
Coverage = Literal['UNKNOWN', 'INCOMPLETE', 'COMPLETE_FOR_RECORDED_SOURCES']


class TokenValues(Strict):
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    cached_input_tokens: int | None = Field(ge=0)
    reasoning_output_tokens: int | None = Field(ge=0)


class TokenCounts(Strict):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    reasoning_output_tokens: int = Field(ge=0)


class TokenComplete(Strict):
    input_tokens: bool
    output_tokens: bool
    total_tokens: bool
    cached_input_tokens: bool
    reasoning_output_tokens: bool


class CallMetrics(TokenValues):
    usage_source: Literal['SDK_NORMALIZED_USAGE_METADATA'] | None
    elapsed_ms: float | None = Field(ge=0, allow_inf_nan=False)
    output_tokens_per_second: float | None = Field(ge=0, allow_inf_nan=False)
    speed_basis: Literal['CONFIRMED_OUTPUT_TOKENS_OVER_FULL_REQUEST_ELAPSED_INCLUDING_WAIT']
    configured_input_limit: int | None = Field(ge=0)
    configured_output_limit: int | None = Field(ge=0)
    configured_history_messages: int | None = Field(ge=0)
    context_policy: Literal['reject', 'truncate_oldest'] | None
    known_model_context_window: int | None = Field(gt=0)
    known_model_context_window_source: str | None = Field(max_length=1024)


class Observation(Strict):
    schema_: Literal['ai-model-call-observation-1'] = Field(alias='schema')
    call_id: str = Field(min_length=1, max_length=128)
    provider: str | None = Field(max_length=256)
    model: str | None = Field(max_length=256)
    role: str | None = Field(max_length=256)
    profile_id: str | None = Field(max_length=256)
    revision: int | None = Field(ge=1)
    started_at: str | None = Field(max_length=64)
    ended_at: str | None = Field(max_length=64)
    status: str = Field(min_length=1, max_length=64)
    metrics: CallMetrics | None


class ScopedObservation(Observation):
    job_id: str
    run_id: str


class Group(Strict):
    provider: str | None
    model: str | None
    recorded_call_count: int = Field(ge=0)
    unknown_usage_call_count: int = Field(ge=0)
    known_subtotals: TokenValues
    known_field_call_counts: TokenCounts
    complete_for_recorded_calls: TokenComplete
    output_tokens_per_second: float | None = Field(ge=0, allow_inf_nan=False)
    speed_pair_count: int = Field(ge=0)
    latest_call: ScopedObservation | None


class UsageSummary(Strict):
    schema_: Literal['model-usage-summary-1'] = Field(alias='schema')
    scope_kind: Literal['session', 'job']
    scope_id: str
    availability: Literal['NOT_RECORDED', 'PARTIAL', 'RECORDED']
    coverage: Coverage
    coverage_basis: Literal['RECORDED_UNIQUE_CALL_IDENTITIES_NOT_PROVIDER_BILLING']
    recorded_call_count: int = Field(ge=0)
    unknown_usage_call_count: int = Field(ge=0)
    conflicting_call_count: int = Field(ge=0)
    known_subtotals: TokenValues
    known_field_call_counts: TokenCounts
    complete_for_recorded_calls: TokenComplete
    output_tokens_per_second: float | None = Field(ge=0, allow_inf_nan=False)
    speed_pair_count: int = Field(ge=0)
    speed_basis: Literal['CONFIRMED_OUTPUT_TOKENS_OVER_FULL_REQUEST_ELAPSED_INCLUDING_WAIT']
    latest_call: ScopedObservation | None
    models: list[Group]
    models_total: int = Field(ge=0)
    limit: int
    offset: int
    next_offset: int | None
    billed_cost: None
    billing_currency: None


class TraceRow(Strict):
    sequence: int = Field(ge=1)
    job_id: str
    run_id: str
    role: str | None = Field(max_length=256)
    provider: str | None = Field(max_length=256)
    model: str | None = Field(max_length=256)
    status: str = Field(max_length=64)
    model_observation: ScopedObservation | None


class TracePage(Strict):
    schema_: Literal['ai-model-trace-page-1'] = Field(alias='schema')
    job_id: str
    coverage: Coverage
    total: int = Field(ge=0)
    limit: int
    offset: int
    next_offset: int | None
    items: list[TraceRow]
    not_a_success_report: Literal[True]
