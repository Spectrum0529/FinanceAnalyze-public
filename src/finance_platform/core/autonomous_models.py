"""Owner API values; generated code is data, never a host execution request."""
from typing import Literal, Annotated
from datetime import date
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class Reference(Strict):
    resource_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    version: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class Metric(Strict):
    name: str = Field(min_length=1, max_length=80, pattern=r'^[a-z][a-z0-9_]*$')
    direction: Literal['maximize', 'minimize']
    weight: int = Field(ge=1, le=10000)


class Budget(Strict):
    attempts: int = Field(default=100, ge=1, le=100000)
    model_requests: int = Field(default=500, ge=0, le=1000000)
    tokens: int = Field(default=1000000, ge=0, le=1000000000)
    cost_microunits: int = Field(default=0, ge=0, le=1000000000000)
    compute_seconds: int = Field(default=3600, ge=1, le=31536000)
    artifact_bytes: int = Field(default=16777216, ge=1, le=1073741824)
    prefixes: int = Field(default=200000, ge=1, le=1000000)


class Reservation(Strict):
    attempts: Literal[1] = 1
    model_requests: int = Field(ge=0, le=100)
    tokens: int = Field(ge=0, le=1000000)
    cost_microunits: int = Field(ge=0, le=1000000000)
    compute_seconds: int = Field(ge=1, le=3600)
    artifact_bytes: int = Field(default=1048576, ge=1, le=16777216)
    prefixes: int = Field(default=2000, ge=1, le=2000)


class EngineOptions(Strict):
    cash: int = Field(default=10000,ge=1,le=1000000000)
    fee_rate: str = Field(default='0.001',pattern=r'^0(?:\.\d{1,8})?$')
    spread_rate: str = Field(default='0.0002',pattern=r'^0(?:\.\d{1,8})?$')
    seed: int = Field(default=0,ge=0,le=4294967295)

    @model_validator(mode='after')
    def finite_rates(self):
        if Decimal(self.fee_rate)>Decimal('0.05') or Decimal(self.spread_rate)>Decimal('0.05'):raise ValueError('rate exceeds supported engine bounds')
        return self


CandidateID = Annotated[str,Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')]


ResearchRole = Literal['hypothesis','coder','critic','reporter']
TeamMemberRole = Literal['market_analyst','social_analyst','news_analyst','fundamentals_analyst',
    'bull_researcher','bear_researcher','research_manager','trader','risky_analyst',
    'safe_analyst','neutral_analyst','risk_manager']


class DateRange(Strict):
    start: str | None = Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: str | None = Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}$')

    @model_validator(mode='after')
    def valid_range(self):
        for value in (self.start,self.end):
            if value is not None and date.fromisoformat(value).isoformat()!=value:
                raise ValueError('date must be canonical YYYY-MM-DD')
        if self.start is not None and self.end is not None and self.start>self.end:
            raise ValueError('start exceeds end')
        return self


class SelectedDevelopment(DateRange):
    reference: Reference
    start: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')


class SessionSpec(DateRange):
    name: str = Field(min_length=1, max_length=80)
    target: Literal['strategy', 'factor', 'joint']
    objective: str = Field(min_length=1, max_length=4000)
    saved_candidate_ids: list[CandidateID] = Field(default_factory=list,max_length=4)
    team_role_map: dict[ResearchRole,TeamMemberRole] = Field(default_factory=dict,max_length=4)
    reuse_policy: Literal['context','evaluate_saved'] = 'context'
    scoring: list[Metric] = Field(min_length=1, max_length=16)
    development: list[Reference] = Field(min_length=1, max_length=16)
    final_test: Reference | None = None
    models: dict[Literal['hypothesis', 'coder', 'critic', 'reporter'], Reference] = Field(default_factory=dict, max_length=4)
    team_snapshot: Reference | None = None
    budget: Budget
    experiment: Reservation
    deadline_seconds: int = Field(default=86400, ge=60, le=31536000)
    cost_policy: Literal['COUNTERS_ONLY','PRICED_CAP'] = 'COUNTERS_ONLY'
    engine_options: EngineOptions = Field(default_factory=EngineOptions)

    @model_validator(mode='after')
    def split_and_budget(self):
        if self.reuse_policy!='evaluate_saved' and (self.start is not None or self.end is not None):
            raise ValueError('date selection is supported only for saved candidate evaluation')
        if self.reuse_policy=='evaluate_saved':
            if (len(self.saved_candidate_ids)!=1 or self.final_test is not None or self.models or self.team_snapshot is not None or self.team_role_map
                    or self.budget.attempts!=1 or any(getattr(quota,key)!=0 for quota in (self.budget,self.experiment)
                        for key in ('model_requests','tokens','cost_microunits'))):
                raise ValueError('evaluate_saved requires one candidate, one attempt, no model/team/final inputs and zero model quotas')
        if self.team_role_map and (self.team_snapshot is None or not set(self.team_role_map)<=set(self.models)):
            raise ValueError('team member mapping requires frozen snapshot and research models')
        if self.reuse_policy!='evaluate_saved' and not {'hypothesis','coder','critic'}<=set(self.models):raise ValueError('required research roles missing')
        if self.target!='factor' and any(m.name in {'cs_ic','ts_ic'} for m in self.scoring):raise ValueError('metric inapplicable to target')
        if len({m.name for m in self.scoring}) != len(self.scoring):
            raise ValueError('duplicate metric')
        if self.final_test and self.final_test.content_hash in {r.content_hash for r in self.development}:
            raise ValueError('final test overlaps development')
        if any(v > getattr(self.budget, k) for k, v in self.experiment.model_dump().items()):
            raise ValueError('experiment exceeds session budget')
        return self


class DataWindow(Strict):
    # Trusted resolver supplies canonical underlying lineage/security IDs, not
    # caller-chosen session aliases or export filenames.
    source_id: str = Field(min_length=1, max_length=128)
    security_ids: list[str] = Field(min_length=1, max_length=1000)
    start: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')

    @model_validator(mode='after')
    def valid_dates(self):
        if date.fromisoformat(self.start) > date.fromisoformat(self.end) or len(set(self.security_ids)) != len(self.security_ids):
            raise ValueError('invalid coverage')
        return self


class ResolvedInputs(Strict):
    development_windows: list[DataWindow] = Field(min_length=1, max_length=16)
    development_selection: list[SelectedDevelopment] = Field(default_factory=list,max_length=16)
    final_windows: list[DataWindow] = Field(max_length=16)
    scoring_metrics: list[str] = Field(min_length=1, max_length=64)
    evaluator_version: str = Field(min_length=1, max_length=128)
    binding_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    model_profiles: dict = Field(default_factory=dict, max_length=4)
    candidate_context: list[dict] = Field(default_factory=list,max_length=4)
    capability_limits: dict[str,str] = Field(default_factory=dict,max_length=16)


class CreateSession(Strict):
    spec: SessionSpec
    idempotency_key: str = Field(min_length=1, max_length=128)


class ProfileSelection(Strict):
    profile_id: str = Field(min_length=1,max_length=128)
    revision: int | None = Field(default=None,ge=1)


class PrepareSession(DateRange):
    name: str = Field(min_length=1,max_length=80)
    target: Literal['strategy','factor','joint']
    objective: str = Field(min_length=1,max_length=4000)
    saved_candidate_ids: list[CandidateID] = Field(default_factory=list,max_length=4)
    team_role_map: dict[ResearchRole,TeamMemberRole] = Field(default_factory=dict,max_length=4)
    reuse_policy: Literal['context','evaluate_saved'] = 'context'
    scoring: list[Metric] = Field(min_length=1,max_length=16)
    development_ids: list[str] = Field(min_length=1,max_length=16)
    final_test_id: str | None = None
    models: dict[Literal['hypothesis','coder','critic','reporter'],ProfileSelection] = Field(default_factory=dict,max_length=4)
    default_model: ProfileSelection | None = None
    team_snapshot_id: str | None = None
    budget: Budget
    deadline_seconds: int = Field(default=86400,ge=60,le=31536000)
    cost_policy: Literal['COUNTERS_ONLY'] = 'COUNTERS_ONLY'
    engine_options: EngineOptions = Field(default_factory=EngineOptions)

    @model_validator(mode='after')
    def saved_mode(self):
        if self.reuse_policy!='evaluate_saved' and (self.start is not None or self.end is not None):
            raise ValueError('date selection is supported only for saved candidate evaluation')
        if self.reuse_policy=='evaluate_saved' and (len(self.saved_candidate_ids)!=1 or self.models or self.default_model is not None
                or self.team_snapshot_id is not None or self.team_role_map or self.final_test_id is not None
                or self.budget.attempts!=1 or any(getattr(self.budget,key)!=0 for key in ('model_requests','tokens','cost_microunits'))):
            raise ValueError('saved evaluation requires one candidate/attempt, no model/team/final inputs and zero model quotas')
        return self


class SavedBacktestBudget(Budget):
    attempts: Literal[1] = 1
    model_requests: Literal[0] = 0
    tokens: Literal[0] = 0
    cost_microunits: Literal[0] = 0


class SavedBacktestPreparation(DateRange):
    name: str = Field(default='保存候选回测',min_length=1,max_length=80)
    objective: str = Field(default='按固定源代码、数据、费用和ABI评估一次；不调用模型。',min_length=1,max_length=4000)
    scoring: list[Metric] = Field(min_length=1,max_length=16)
    development_ids: list[str] = Field(min_length=1,max_length=16)
    budget: SavedBacktestBudget
    deadline_seconds: int = Field(default=86400,ge=60,le=31536000)
    engine_options: EngineOptions = Field(default_factory=EngineOptions)


class SavedCandidateBacktest(Strict):
    request: SavedBacktestPreparation
    idempotency_key: str = Field(min_length=1,max_length=128)


class Control(Strict):
    action: Literal['run', 'pause', 'resume', 'cancel', 'restart', 'seal_final']
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    accept_unknown_charge: bool = False
    candidate_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class Artifact(Strict):
    kind: Literal['proposal', 'strategy', 'factor', 'evaluation', 'feedback', 'checkpoint']
    media_type: Literal['application/json', 'text/plain', 'text/x-python']
    content: str = Field(max_length=65536)
    logical_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    part_index: int = Field(default=0, ge=0, le=255)
    part_count: int = Field(default=1, ge=1, le=256)
    full_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Outcome(Strict):
    status: Literal['SUCCEEDED', 'FAILED', 'UNKNOWN']
    error_code: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]{0,63}$')
    metrics: dict[str, float] = Field(default_factory=dict, max_length=32)
    artifacts: list[Artifact] = Field(default_factory=list, max_length=256)


class ExecutionProgressView(Strict):
    attempt_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    event_id: int = Field(ge=1)
    recorded_at: str = Field(min_length=1, max_length=64)
    stage: Literal['execute']
    completed_prefixes: int = Field(ge=0, le=2000)
    total_prefixes: int = Field(ge=1, le=2000)
    current_symbol: str | None = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    current_index: int | None = Field(ge=0, le=1999)

    @model_validator(mode='after')
    def completed_coordinates(self):
        if self.completed_prefixes > self.total_prefixes:
            raise ValueError('completed prefixes exceed total')
        if self.completed_prefixes == 0:
            if self.current_symbol is not None or self.current_index is not None:
                raise ValueError('zero completion has no completed coordinate')
        elif self.current_symbol is None or self.current_index is None:
            raise ValueError('completed prefix requires its coordinate')
        return self


class SessionView(Strict):
    schema_version: Literal['autonomous-session-1'] = Field(alias='schema')
    session_id: str
    owner_id: str
    spec_hash: str
    spec: SessionSpec
    resolved: dict
    status: Literal['DRAFT','READY','RUNNING','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED','CANCELLED','BLOCKED','BUDGET_EXHAUSTED','SEALED','COMPLETED']
    revision: int
    generation: int
    spent: dict[str,int]
    active_attempt: str | None
    candidate_hash: str | None
    final_attempt: str | None
    deadline: float
    created_at: str
    updated_at: str
    evaluation_scope: Literal['FINAL_SEPARATE','DEVELOPMENT_ONLY']
    allowed_actions: list[str]
    budget_usage: dict
    latest_execution_progress: ExecutionProgressView | None = None


class CursorPageView(Strict):
    schema_version: Literal['autonomous-page-2'] = Field(alias='schema')
    session_id:str
    kind:Literal['attempts','events','artifacts']
    attempt_id:str|None
    limit:int=Field(ge=1,le=100)
    items:list[dict]=Field(max_length=100)
    next_cursor:str=Field(min_length=1,max_length=1024)
    has_more:bool
    next_offset:int|None=Field(ge=0)


class SaveResearchCandidate(Strict):
    session_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    attempt_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    logical_id: str = Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    name: str = Field(min_length=1,max_length=80)
