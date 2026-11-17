"""Discoverable bounded inputs for named data/research/settings operations."""
from typing import Literal
from pydantic import Field,SecretStr,field_validator,model_validator
from .team_profiles import Strict

class ModelRef(Strict):
    profile_id:str=Field(min_length=1,max_length=128)
    revision:int=Field(ge=1)
class VersionReference(Strict):
    resource_id:str=Field(min_length=1,max_length=200)
    version:str=Field(min_length=1,max_length=128)
    content_hash:str=Field(pattern=r'^[0-9a-f]{64}$')
class Universe(Strict):
    groups:list[str]=Field(default_factory=list,max_length=100)
    manual:str=Field(default='',max_length=20000)
    exclude:str=Field(default='',max_length=20000)
class Connection(Strict):
    name:str=Field(min_length=1,max_length=80)
    provider:Literal['csv','massive','yfinance','akshare','openbb_cboe','akshare_cn','tushare','openbb_nasdaq','wrds']
    credential_ref:str|None=Field(default=None,max_length=128)
class ConnectionSave(Strict):
    connection:Connection
    idempotency_key:str=Field(min_length=1,max_length=128)
class CSVPreview(Strict):
    content_base64:str=Field(max_length=1400000,description='用户提供的CSV内容的base64；不是文件路径。')
    options:dict=Field(default_factory=dict,max_length=8)
    idempotency_key:str|None=Field(default=None,min_length=1,max_length=128)
class DatasetImport(Strict):
    dataset:dict=Field(description='固定数据集：connection_id/name/universe/start/end/metadata/rows/source/received_at/scope/kind/benchmark；按数据服务契约校验。')
    idempotency_key:str=Field(min_length=1,max_length=128)
class DatasetDownload(Strict):
    request:dict=Field(description='数据服务 DownloadRequest 或 MembershipRequest；只接受连接ID、日期、范围等，无URL或密钥。')
    idempotency_key:str=Field(min_length=1,max_length=128)


class PublicUniverse(Strict):
    groups: list[dict] = Field(max_length=100)
    manual: list[str] = Field(max_length=32)
    excluded: list[str] = Field(max_length=100)
    tickers: list[str] = Field(min_length=1, max_length=32)
    selection_mode: Literal["STATIC_SELECTED_MEMBERS"] = "STATIC_SELECTED_MEMBERS"
    historical_constituents: Literal[False] = False

    @field_validator("historical_constituents", mode="before")
    @classmethod
    def require_false_boolean(cls, value):
        if value is not False:
            raise ValueError("historical_constituents must be the boolean false")
        return value


class YahooPriceMetadata(Strict):
    currency: Literal["USD"]
    timezone: Literal["America/New_York"]
    price_unit: Literal["per_adjusted_simulation_unit"]
    volume_unit: Literal["provider_reported_unverified"]
    adjustment: Literal["split_adjusted"]


class PriceBenchmark(Strict):
    instrument_label: str = Field(min_length=1, max_length=120)
    return_basis: Literal["price"]
    dividend_policy: str = Field(min_length=1, max_length=120)


class PublicDatasetRequest(Strict):
    connection_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    universe: PublicUniverse
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    metadata: YahooPriceMetadata | None = None
    kind: Literal["dataset", "benchmark"] = "dataset"
    benchmark: PriceBenchmark | None = None


class PublicDatasetDownload(Strict):
    request: PublicDatasetRequest
    idempotency_key: str = Field(min_length=1, max_length=128)
class WrdsLookupRequest(Strict):
    connection_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=32,
                       pattern=r"^\s*(?:[A-Za-z][A-Za-z0-9.-]{0,15}|[1-9][0-9]{0,8})\s*$")
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    limit: int = Field(default=20, ge=1, le=50)


class WrdsLookup(Strict):
    request: WrdsLookupRequest
    idempotency_key: str = Field(min_length=1, max_length=128)


class WrdsSecurity(Strict):
    permno: int = Field(ge=1, le=999999999)
    ticker: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.-]*$")


class WrdsHistoryRequest(Strict):
    connection_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    securities: list[WrdsSecurity] = Field(min_length=1, max_length=8)
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class WrdsHistory(Strict):
    request: WrdsHistoryRequest
    idempotency_key: str = Field(min_length=1, max_length=128)


class Contribution(Strict):
    date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    amount:str=Field(max_length=40)
class Dividend(Strict):
    ticker:str=Field(min_length=1,max_length=18)
    ex_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    pay_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    amount_per_unit:str=Field(max_length=40)
    source:str=Field(min_length=1,max_length=512)
    unit_basis:Literal['adjusted_simulation_units']
    event_id:str|None=Field(default=None,max_length=128)
class Portfolio(Strict):
    dataset_id:str=Field(min_length=1,max_length=128)
    strategy:Literal['buy_hold','sma20','sma_cross_5_20']
    start_date:str|None=None
    end_date:str|None=None
    initial_cash:str='10000.00'
    fee_per_share:str='0.005'
    minimum_fee:str='1.00'
    spread_bps:str='0'
    benchmark_id:str|None=None
    benchmark_mode:Literal['buy_hold','dca','dca_reserved']=Field(default='buy_hold',description='基准买入方式：buy_hold为原一次买入；dca为原追加本金定投；dca_reserved为同初始总资金内预留现金、按计划分批买入，不增加本金。')
    contributions:list[Contribution]=Field(default_factory=list,max_length=1000,description='分批日期和金额。dca_reserved下是初始资金内的买入预算，不是外部入金；dca下保留原追加本金含义。金额、费用和总预算仍由同一回测服务校验。')
    dividends:list[Dividend]=Field(default_factory=list,max_length=10000)
    idempotency_key:str=Field(min_length=1,max_length=128)


class PortfolioBenchmark(Strict):
    id: Literal["sp500", "nasdaq100", "csi300"]
    dataset_id: str = Field(min_length=1, max_length=128,
        description="专用 Yahoo 价格对照数据集 ID。下载时选择 kind=benchmark，SPY、QQQ、ASHR 分别对应三种指数。")


class WorkbenchPortfolio(Portfolio):
    strategy: Literal['buy_hold','sma20','sma_cross_5_20'] | None = None
    instance_id: str | None = Field(default=None, min_length=1, max_length=128)
    benchmarks: list[PortfolioBenchmark] | None = Field(default=None, min_length=1, max_length=3)
    benchmark_mode: Literal['buy_hold','dca','dca_reserved'] | None = None
    contributions: list[Contribution] | None = Field(default=None, max_length=1000)
    dividends: list[Dividend] | None = Field(default=None, max_length=10000)
    cache_policy: Literal["use", "bypass"] | None = None

    @model_validator(mode="after")
    def selection(self):
        if (self.strategy is None) == (self.instance_id is None):
            raise ValueError("select one built-in strategy or a saved strategy instance")
        if (self.benchmarks or self.instance_id) and any(
            value is not None for value in (self.benchmark_id, self.benchmark_mode, self.contributions, self.dividends)
        ):
            raise ValueError("benchmark selection conflict")
        if self.benchmarks and len({item.id for item in self.benchmarks}) != len(self.benchmarks):
            raise ValueError("benchmark identities must be distinct")
        return self


class MarketReference(Strict):
    source: Literal["library", "history"]
    id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class MarketLifecycle(Strict):
    action: Literal["rename", "trash", "restore"]
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class MarketMergePreview(Strict):
    inputs: list[MarketReference] = Field(min_length=2, max_length=2)
    conflict_policy: Literal["reject", "prefer_first", "prefer_second", "use_verification"] | None = None
    verification_reference: MarketReference | None = None


class MarketMerge(MarketMergePreview):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MarketReuse(Strict):
    reference: MarketReference = Field(description="source=library 时仅需原引用和 idempotency_key，返回同一固定版本。")
    connection_id: str | None = Field(default=None, min_length=1, max_length=128,
        description="仅在 source=history 时可填写，用于指定保存固定数据集的连接。")
    name: str | None = Field(default=None, min_length=1, max_length=120,
        description="仅在 source=history 时可填写，用于指定保存的固定数据集名称。")
    idempotency_key: str = Field(min_length=1, max_length=128)


class ConnectionLifecycle(Strict):
    action: Literal["edit", "archive", "restore"]
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    credential_ref: str | None = Field(default=None, min_length=1, max_length=128)
class Evidence(Strict):
    id:str=Field(min_length=1,max_length=128)
    source:str=Field(min_length=1,max_length=512)
    text:str=Field(min_length=1,max_length=16000)
    published_at:str|None=Field(default=None,max_length=64)
    available_at:str|None=Field(default=None,max_length=64)
class SearchLimits(Strict):
    max_trials:int=Field(default=6,ge=1,le=12)
    max_model_calls:int=Field(default=12,ge=3,le=32)
    wall_seconds:int=Field(default=90,ge=1,le=120)
    max_engine_runs:int=Field(default=120,ge=2,le=600,description='按证券实际执行的引擎次数上限，含训练候选和冻结胜者验证；不是候选数量。')
    max_result_bytes:int=Field(default=8388608,ge=65536,le=25165824,description='搜索保存结果字节预算；超出时由服务拒绝，MCP读取仍使用分区分页，不静默截断。')
class StrategyCandidate(Strict):
    template:Literal['buy_hold','sma','sma_cross']
    fast:int|None=Field(default=None,ge=2,le=15)
    slow:int|None=Field(default=None,ge=5,le=30)
    @model_validator(mode='after')
    def bounded_template(self):
        if self.template=='buy_hold'and(self.fast is not None or self.slow is not None):raise ValueError('SEARCH_TEMPLATE_PARAMETERS_INVALID')
        if self.template=='sma'and self.fast is not None:raise ValueError('SEARCH_TEMPLATE_PARAMETERS_INVALID')
        if self.template in {'sma','sma_cross'}and self.slow is None:raise ValueError('SEARCH_TEMPLATE_PARAMETERS_REQUIRED')
        if self.template=='sma_cross'and self.fast is None:raise ValueError('SEARCH_TEMPLATE_PARAMETERS_REQUIRED')
        if self.fast is not None and self.slow is not None and self.fast>=self.slow:raise ValueError('SEARCH_WINDOW_ORDER_INVALID')
        return self
class TaskPlan(Strict):
    prompt:str=Field(min_length=1,max_length=2000)
    dataset_id:str=Field(min_length=1,max_length=128)
    cache_policy:Literal['use','bypass']|None=None
    execution_mode:Literal['tools','single_assistant','model_panel','role_team']='tools'
    kind:str|None=None
    strategy:str|None=None
    factor:str|None=None
    model_profiles:list[ModelRef]|None=Field(default=None,max_length=12)
    team_snapshot_id:str|None=None
    parameters:dict|None=Field(default=None,max_length=20)
    evidence:list[Evidence]|None=Field(default=None,max_length=16)
    as_of:str|None=Field(default=None,max_length=64)
    workflow:Literal['strategy_search','analysis']|None=None
    search_limits:SearchLimits|None=None
    candidates:list[StrategyCandidate]|None=Field(default=None,min_length=1,max_length=12)
class StrategySearchPlan(Strict):
    prompt:str=Field(default='在训练时间段比较支持的策略候选，冻结训练选择后仅验证胜出候选。',min_length=1,max_length=2000)
    dataset_id:str=Field(min_length=1,max_length=128)
    execution_mode:Literal['tools']='tools'
    workflow:Literal['strategy_search']='strategy_search'
    candidates:list[StrategyCandidate]=Field(min_length=1,max_length=12)
    search_limits:SearchLimits=Field(default_factory=SearchLimits)
    parameters:dict|None=Field(default=None,max_length=20)
class TaskStart(Strict):
    plan_reference:VersionReference
    idempotency_key:str=Field(min_length=1,max_length=128)
class CredentialWrite(Strict):
    provider:Literal['openai','deepseek','anthropic','gemini','qwen','kimi','glm','openai_compatible','ollama','massive','tushare']
    endpoint_id:str|None=Field(default=None,max_length=80,pattern=r'^[A-Za-z0-9_-]{1,80}$')
    value:SecretStr=Field(description='只写入本机保险库；从不作为结果返回，不要放在其他字段。')
class Schedule(Strict):
    name:str=Field(min_length=1,max_length=100)
    handler:str=Field(min_length=1,max_length=128)
    parameters:dict=Field(default_factory=dict)
    mode:Literal['BACKTEST','LOCAL_SIMULATION']='BACKTEST'
    rule:Literal['ONCE','INTERVAL']
    interval_seconds:int|None=None
    start_at:str

class NativeDebate(Strict):
    agent_team_snapshot_id:str|None=None
    data_reference:VersionReference
    ticker:str=Field(min_length=1,max_length=18)
    trade_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    as_of:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    evidence_results:list[dict]=Field(default_factory=list,max_length=4)
    options:dict|None=Field(default=None,max_length=3)
    idempotency_key:str=Field(min_length=1,max_length=128)
class NativeQlib(Strict):
    export_reference:VersionReference
    workflow:dict=Field(max_length=24)
    idempotency_key:str=Field(min_length=1,max_length=128)
class DailyBacktest(Strict):
    ticker:str=Field(min_length=1,max_length=18)
    start_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    strategy:Literal['buy_hold','sma20','sma_cross_5_20']
    provider:Literal['yfinance','akshare']='yfinance'
    initial_cash:str='10000.00'
    fee_per_share:str='0.005'
    minimum_fee:str='1.00'
    spread_bps:str='0'
    history_policy:Literal['reuse_or_download','refresh']='reuse_or_download'
    history_id:str|None=None
    cache_policy:Literal['use','bypass']|None=None
    history_version:int|None=Field(default=None,ge=1)
    idempotency_key:str=Field(min_length=1,max_length=128)
class DailyLifecycle(Strict):
    action:Literal['rename','delete','restore']
    expected_revision:int=Field(ge=0)
    idempotency_key:str=Field(min_length=1,max_length=128)
    name:str|None=Field(default=None,min_length=1,max_length=120)
class StrategyTemplate(Strict):
    strategy_id:str=Field(min_length=1,max_length=64)
    style:Literal['event','batch','target','model','external']
    model_id:str|None=None
class StrategyConfig(Strict):
    strategy_id:str=Field(min_length=1,max_length=64)
    version:str=Field(pattern=r'^[0-9]+\.[0-9]+\.[0-9]+$')
    parameters:dict=Field(max_length=64)
    mode:Literal['BACKTEST','LOCAL_SIMULATION']='BACKTEST'
class HistoryDownload(Strict):
    ticker:str=Field(min_length=1,max_length=18)
    start_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    provider:Literal['yfinance','akshare']='yfinance'
    history_policy:Literal['reuse_or_download','refresh']='reuse_or_download'
    history_id:str|None=None
    history_version:int|None=Field(default=None,ge=1)
    idempotency_key:str=Field(min_length=1,max_length=128)
class SimulationStart(Strict):
    account_id:str=Field(min_length=1,max_length=128)
    sample_reference:VersionReference
    model_id:str|None=None
    idempotency_key:str=Field(min_length=1,max_length=128)


class StorageLocationCheck(Strict):
    category:Literal['market_data','reports','artifacts']
    directory:str|None=Field(max_length=4096)

class StorageLocationSave(StorageLocationCheck):
    expected_revision:int=Field(ge=0)
    idempotency_key:str=Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
