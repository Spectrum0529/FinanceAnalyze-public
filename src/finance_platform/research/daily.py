"""Explicit daily-bar assumptions, never the independent-print event backend."""
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TypedDict
import re

from finance_platform.core.contracts import fingerprint, parse_instant
from finance_platform.data.provider_sdk.contracts import number
from finance_platform.trading.accounting import money, decimal, ZERO

DATA_FAMILY = "daily-history-series-2"
RESULT_FAMILY = "daily-simulation-result-3"
ENGINE_VERSION = "financeanalyze-daily-1"
STRATEGY_VERSION = "daily-long-cash-3"
CALCULATION_VERSION = "financeanalyze-daily-calculation-1"
ENGINE = {"name": "financeanalyze.daily", "version": "1.0.0", "adapter_version": CALCULATION_VERSION,
          "strategy_version": STRATEGY_VERSION, "license": "MIT"}
MAX_CALENDAR_DAYS = 11346
MAX_ROWS = 9000
DEFAULTS = {"provider": "yfinance", "initial_cash": "10000.00", "fee_per_share": "0.005", "minimum_fee": "1.00", "spread_bps": "0"}
STRATEGIES = (
    {"id": "buy_hold", "name": "买入持有", "warmup_bars": 1, "description": "从第2条行情收盘决定买入，下一条行情开盘成交；结束时不自动卖出。"},
    {"id": "sma20", "name": "20日均线", "warmup_bars": 20, "description": "先读满20条行情，从下一条收盘开始判断，随后开盘成交。"},
    {"id": "sma_cross_5_20", "name": "5/20日均线", "warmup_bars": 20, "description": "比较最近5条与20条平均价格；先读满20条行情，再开始判断。"},
)
ASSUMPTIONS = {
    "execution": "OBSERVED_CLOSE_SIGNAL_NEXT_OBSERVED_OPEN",
    "availability": "HISTORICAL_CLOSE_KNOWN_AT_CLOSE_ASSUMED_NOT_CERTIFIED",
    "price_basis": "PROVIDER_OBSERVED_NOT_RAW_OR_PIT_CERTIFIED",
    "corporate_actions": "SOURCE_ADJUSTED_PATH_NO_EXTRA_SPLIT_OR_DIVIDEND_NOT_TOTAL_RETURN",
    "portfolio": "SINGLE_INSTRUMENT_LONG_OR_CASH_MARGIN_1_NO_SHORT",
    "costs": "PER_UNIT_COMMISSION_WITH_MINIMUM_AND_CONSTANT_ENTRY_SPREAD",
    "spread_basis": "LONG_ENTRY_PRICE_MARKUP_RAW_OPEN_EXIT",
    "numeric_basis": "IEEE_FLOAT64_NO_CENT_TICK_QUANTIZATION",
    "settlement": "CASH_ACCOUNT_NO_CUSTOM_T_PLUS_1_SETTLEMENT",
    "allocation": "CASH_FRACTION_0.9999_INTEGER_UNITS_CONSERVATIVE_COMMISSION_RESERVE",
    "end": "FINALIZE_TRADES_FALSE_OPEN_POSITIONS_MARKED_CLOSED_TRADE_STATS_ONLY",
    "warmup": "DECLARED_BUILTIN_PANDAS_ROLLING_NO_HIDDEN_HISTORY_FETCH",
    "coverage": "OBSERVED_DATES_ONLY_MISSING_SESSIONS_NOT_INFERRED_OR_FILLED",
    "quantity_basis": "ADJUSTED_SIMULATION_UNITS",
    "fee_basis": "PER_ADJUSTED_SIMULATION_UNIT_WITH_MINIMUM_CALLABLE_NO_CENT_ROUNDING",
    "fee_report_basis": "ALL_EXECUTED_ENTRIES_AND_EXITS_OPEN_ENTRY_FEES_INCLUDED_ONCE",
}


class DailyError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DailyRequest:
    ticker: str
    start_date: str
    end_date: str
    strategy: str
    provider: str = "yfinance"
    initial_cash: str = "10000.00"
    fee_per_share: str = "0.005"
    minimum_fee: str = "1.00"
    spread_bps: str = "0"
    history_policy: str = "reuse_or_download"
    history_id: str | None = None
    history_version: int | None = None

    @classmethod
    def parse(cls, value, *, today=None):
        if type(value) is dict and "slippage_bps" in value:
            raise DailyError("DAILY_LEGACY_SLIPPAGE_UNSUPPORTED")
        required = {"ticker", "start_date", "end_date", "strategy"}
        history_keys = {"history_policy", "history_id", "history_version"}
        if type(value) is not dict or not required <= set(value) or set(value) - (required | set(DEFAULTS) | history_keys):
            raise DailyError("DAILY_REQUEST_INVALID")
        v = {**DEFAULTS, **value}
        if any(type(x) is not str for k,x in v.items() if k not in {"history_id","history_version"}):
            raise DailyError("DAILY_REQUEST_INVALID")
        v["ticker"]=v["ticker"].strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9]{0,13}(?:[.\-][A-Z0-9]{1,3})?", v["ticker"]) is None:
            raise DailyError("DAILY_SYMBOL_UNSUPPORTED")
        if v.get("history_policy", "reuse_or_download") not in {"reuse_or_download", "refresh"}:
            raise DailyError("DAILY_HISTORY_POLICY_INVALID")
        for key in ("history_id",):
            if v.get(key) is not None and (type(v[key]) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}",v[key]) is None):
                raise DailyError("DAILY_HISTORY_REFERENCE_INVALID")
        if v.get("history_version") is not None and (type(v["history_version"])is not int or not 1<=v["history_version"]<=2147483647):
            raise DailyError("DAILY_HISTORY_REFERENCE_INVALID")
        if (v.get("history_version") is not None and v.get("history_id") is None) or (v.get("history_id") is not None and v.get("history_policy") == "refresh"):
            raise DailyError("DAILY_HISTORY_REFERENCE_INVALID")
        if v["provider"] not in {"akshare", "yfinance"} or v["strategy"] not in {s["id"] for s in STRATEGIES}:
            raise DailyError("DAILY_CHOICE_UNSUPPORTED")
        try:
            start, end = date.fromisoformat(v["start_date"]), date.fromisoformat(v["end_date"])
            if start.isoformat() != v["start_date"] or end.isoformat() != v["end_date"] or not 0 <= (end-start).days < MAX_CALENDAR_DAYS or end >= (today or datetime.now(timezone.utc).date()):
                raise ValueError
        except (ValueError, TypeError):
            raise DailyError("DAILY_DATE_RANGE_INVALID") from None
        try:
            cash, per, minimum, slip = (decimal(v[k]) for k in ("initial_cash", "fee_per_share", "minimum_fee", "spread_bps"))
            if not Decimal("1") <= cash <= Decimal("1000000000") or cash != money(cash) or not ZERO <= per <= Decimal("1") or not ZERO <= minimum <= Decimal("1000") or minimum != money(minimum) or not ZERO <= slip <= Decimal("1000"):
                raise ValueError
        except (ValueError, ArithmeticError):
            raise DailyError("DAILY_COST_CONFIGURATION_INVALID") from None
        v.update(initial_cash=str(money(cash)), fee_per_share=format(per.normalize(), "f"), minimum_fee=str(money(minimum)), spread_bps=format(slip.normalize(), "f"))
        return cls(**v)

    def payload(self):
        return asdict(self)


@dataclass(frozen=True)
class DailyBar:
    session_date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


@dataclass(frozen=True)
class DailySeries:
    bars: tuple[DailyBar, ...]
    observation: dict
    request: DailyRequest
    history: dict | None = None

    @classmethod
    def parse(cls, observation, request, *, history=None):
        if type(observation) is not dict:
            raise DailyError("DAILY_OBSERVATION_INVALID")
        body = {k: v for k, v in observation.items() if k != "content_hash"}
        if observation.get("content_hash") != fingerprint(body):
            raise DailyError("DAILY_OBSERVATION_HASH_MISMATCH")
        if body.get("data_family") != "provider-observation-1" or body.get("schema_version") != "provider-observation-1" or body.get("provider") != request.provider or body.get("price_basis") != "PROVIDER_OBSERVED" or body.get("raw_compatible") is not False or body.get("availability_basis") != "OBSERVED_RECEIPT_ONLY" or body.get("historical_first_available_at") is not None:
            raise DailyError("DAILY_OBSERVATION_INVALID")
        call = body.get("request_parameters", {})
        from datetime import timedelta
        if call.get("ticker") != request.ticker or not (call.get("start", "9999") <= request.start_date and call.get("end", "0000") >= (date.fromisoformat(request.end_date)+timedelta(days=1)).isoformat()) or call.get("timeframe") != "1Day":
            raise DailyError("DAILY_OBSERVATION_BINDING_MISMATCH")
        try:
            parse_instant(body["observed_received_at"])
            rows = body["rows"]
            if type(rows) is not list or not 1 <= len(rows) <= MAX_ROWS or body.get("row_count") != len(rows):
                raise DailyError("DAILY_OBSERVATION_INVALID")
            bars = []
            last = ""
            for row in rows:
                day = row["session_date"]
                if type(day) is not str or date.fromisoformat(day).isoformat() != day or not call["start"] <= day < call["end"] or day <= last:
                    raise DailyError("DAILY_SESSION_ORDER_INVALID")
                if row.get("currency") != "USD" or row.get("price_basis") != "PROVIDER_OBSERVED" or row.get("raw_compatible") is not False or row.get("availability_basis") != "OBSERVED_RECEIPT_ONLY" or row.get("historical_first_available_at") is not None or row.get("existing_instrument_id") != call.get("existing_instrument_id") or row.get("provider_symbol") != call.get("provider_symbol"):
                    raise DailyError("DAILY_OBSERVATION_INVALID")
                prices = [Decimal(number(row[k], minimum=Decimal("0.000000000001"))) for k in ("open", "high", "low", "close")]
                if max(prices) > Decimal("1e12") or prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
                    raise DailyError("DAILY_OHLC_INVALID")
                volume = None if row.get("volume") is None else Decimal(number(row["volume"], minimum=ZERO))
                if volume is not None and volume != volume.to_integral_value():
                    raise DailyError("DAILY_VOLUME_INVALID")
                if request.start_date <= day <= request.end_date:
                    bars.append(DailyBar(day, *prices, volume))
                last = day
        except DailyError:
            raise
        except (ValueError, TypeError, KeyError, ArithmeticError):
            raise DailyError("DAILY_OBSERVATION_INVALID") from None
        warmup = next(s["warmup_bars"] for s in STRATEGIES if s["id"] == request.strategy)
        if len(bars) <= warmup:
            raise DailyError("DAILY_INSUFFICIENT_SESSIONS")
        return cls(tuple(bars), observation, request, history)

    def payload(self):
        # Source history owns large original rows. Only this compact, immutable
        # manifest enters core VersionService's unchanged2MiB metadata limit.
        return {"schema_version": DATA_FAMILY, "request": self.request.payload(),
                "history": self.history, "observation_reference": {k:self.observation.get(k) for k in ("result_id","content_hash","source_ref","dataset_id","observed_received_at","price_basis","availability_basis","raw_compatible","request_parameters")},
                "selection": {"start":self.request.start_date,"end":self.request.end_date,"rows":len(self.bars),"first":self.bars[0].session_date,"last":self.bars[-1].session_date}}

    @classmethod
    def from_history(cls,view,request):
        """Validated gateway window, rooted in an immutable source snapshot.

        Unlike an original observation body, content_hash here identifies a
        source-version/window/index view. Bind the actual selected row hash in
        our compact manifest as well; never pretend this is an observation hash.
        """
        from datetime import timedelta
        try:
            if type(view)is not dict or view["provider"]!=request.provider or view["ticker"]!=request.ticker or view["raw_compatible"]is not False or view["availability_basis"]!="OBSERVED_RECEIPT_ONLY":raise DailyError("DAILY_HISTORY_BINDING_MISMATCH")
            expected={"start":request.start_date,"end":(date.fromisoformat(request.end_date)+timedelta(days=1)).isoformat()}
            if view["requested_range"]!=expected or view["history_cache"].get("no_vintage_merge")is not True:raise DailyError("DAILY_HISTORY_BINDING_MISMATCH")
            indices=view["history_cache"]["selected_row_indices"]
            rows=view["rows"]
            if type(rows)is not list or not 1<=len(rows)<=MAX_ROWS or type(indices)is not list or len(indices)!=len(rows) or any(type(i)is not int or i<0 for i in indices) or any(b!=a+1 for a,b in zip(indices,indices[1:])):raise DailyError("DAILY_HISTORY_BINDING_MISMATCH")
            expected_hash=fingerprint({"history_hash":view["history_content_hash"],"window":expected,"slice":{"start":indices[0],"stop":indices[-1]+1}})
            if expected_hash!=view["content_hash"]:raise DailyError("DAILY_HISTORY_BINDING_MISMATCH")
            identity=view["identity"]
            if identity["ticker"]!=request.ticker or identity["currency"]!="USD" or identity["asset_class"] not in {"US_EQUITY","US_ETF"}:raise DailyError("DAILY_HISTORY_BINDING_MISMATCH")
            call=view["request_parameters"]
            if call["ticker"]!=request.ticker or call["existing_instrument_id"]!=identity["instrument_id"]:raise DailyError("DAILY_HISTORY_BINDING_MISMATCH")
            parse_instant(view["observed_received_at"])
            from .daily_price_policy import NATIVE_MODE,validate_price_policy
            policy=view.get("price_policy")
            native=(policy.get("mode") if type(policy)is dict else policy)==NATIVE_MODE
            native_policy=validate_price_policy(view,request) if native else None
            bars=[];last=""
            for row in rows:
                day=row["session_date"]
                if type(day)is not str or date.fromisoformat(day).isoformat()!=day or not expected["start"]<=day<expected["end"] or day<=last:raise DailyError("DAILY_SESSION_ORDER_INVALID")
                if row.get("currency")!="USD" or row.get("raw_compatible")is not False or row.get("historical_first_available_at")is not None or row.get("availability_basis")!="OBSERVED_RECEIPT_ONLY" or row.get("existing_instrument_id")!=identity["instrument_id"] or row.get("provider_symbol")!=call["provider_symbol"]:raise DailyError("DAILY_HISTORY_BINDING_MISMATCH")
                prices=[Decimal(number(row[k],minimum=Decimal("0.000000000001"))) for k in ("open","high","low","close")]
                if max(prices)>Decimal("1e12") or prices[2]>min(prices[0],prices[3]) or prices[1]<max(prices[0],prices[3]):raise DailyError("DAILY_OHLC_INVALID")
                volume=None if row.get("volume")is None else Decimal(number(row["volume"],minimum=ZERO))
                if not native and volume is not None and volume!=volume.to_integral_value():raise DailyError("DAILY_VOLUME_INVALID")
                bars.append(DailyBar(day,*prices,volume));last=day
            if len(bars)<=next(s["warmup_bars"] for s in STRATEGIES if s["id"]==request.strategy):raise DailyError("DAILY_INSUFFICIENT_SESSIONS")
            history={k:view[k] for k in ("history_id","version","history_content_hash","content_hash","observation_ref","price_policy")}
            history["corporate_action_basis"]=view.get("corporate_action_basis")
            history["price_transform"]=view.get("price_transform")
            history["quantity_basis"]=view.get("quantity_basis")
            if native:
                history.update(source_ref=dict(view["source_ref"]),volume_unit="UNVERIFIED",
                               native_price_semantics=native_policy["native_transform"])
            history["selected_rows_hash"]=fingerprint(rows)
            if "data_quality" in view:
                from finance_platform.data.provider_sdk.sina_quality import validate_selection
                history["data_quality"]=validate_selection(view["data_quality"],expected["start"],expected["end"],rows)
            return cls(tuple(bars),view,request,history)
        except DailyError:raise
        except (ValueError,TypeError,KeyError,ArithmeticError):raise DailyError("DAILY_HISTORY_BINDING_MISMATCH") from None


class DailyResult(TypedDict):
    schema_version: str
    family: str
    ticker: str
    provider: str
    strategy: str
    report: dict
    equity_curve: list[dict]
    trades: list[dict]


def simulate(series: DailySeries, *, active=lambda: None, progress=lambda value: None) -> DailyResult:
    """Execute the current daily engine; legacy result reads never call it."""
    from .daily_backtesting import simulate as daily_simulate
    return daily_simulate(series, active=active, progress=progress)
