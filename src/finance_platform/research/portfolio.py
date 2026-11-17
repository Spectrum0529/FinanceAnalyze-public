"""Dataset portfolio: mature engine executions plus reconciled account analytics.

Fixed sleeves partition one initial account. This module never matches orders.
All strategy fills come from the FinanceAnalyze daily execution engine.
"""
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math

from .daily import DailyBar, DailySeries, DailyRequest, STRATEGIES, ENGINE
from .daily_backtesting import _ExecutionVersions, _simulate as simulate

FAMILY="dataset-portfolio-result-1"
RESERVED_BENCHMARK_CALCULATION="benchmark-reserved-cash-2"


class PortfolioError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def fail(code):raise PortfolioError(code)


def number(value,*,minimum=None):
    if type(value)not in(str,int,float,Decimal)or isinstance(value,bool):fail("PORTFOLIO_NUMBER_INVALID")
    try:value=Decimal(str(value))
    except (InvalidOperation,ValueError):fail("PORTFOLIO_NUMBER_INVALID")
    if not value.is_finite()or (minimum is not None and value<Decimal(str(minimum))):fail("PORTFOLIO_NUMBER_INVALID")
    if len(value.as_tuple().digits)>128 or not -100<=value.as_tuple().exponent<=100:fail("PORTFOLIO_NUMBER_INVALID")
    return value


def text(value):return None if value is None else str(value)


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def day(value):
    if type(value)is not str:fail("PORTFOLIO_DATE_INVALID")
    try:parsed=date.fromisoformat(value)
    except ValueError:fail("PORTFOLIO_DATE_INVALID")
    if parsed.isoformat()!=value:fail("PORTFOLIO_DATE_INVALID")
    return value


def validate_dataset(dataset, *, verified_benchmark=False):
    if type(dataset)is not dict or type(dataset.get("rows"))is not list or not (1 if verified_benchmark else 2)<=len(dataset["rows"])<=1000000:fail("PORTFOLIO_DATASET_INVALID")
    meta=dataset.get("metadata",{})
    from .adjusted_portfolio import acquisition
    source=acquisition(dataset)
    if source is None and (meta.get("currency") != "USD"
            or meta.get("price_unit")!="per_share" or meta.get("adjustment") not in {"raw","split_adjusted"}):fail("PORTFOLIO_PRICE_BASIS_UNSUPPORTED")
    if dataset.get("splits"):fail("PORTFOLIO_RAW_SPLITS_UNSUPPORTED")
    symbols=defaultdict(list);seen=set()
    for row in dataset["rows"]:
        ticker=row.get("ticker")
        if type(ticker)is not str or not ticker or len(ticker)>18:fail("PORTFOLIO_SYMBOL_INVALID")
        at=day(row.get("session_date"));key=(ticker,at)
        if key in seen:fail("PORTFOLIO_DUPLICATE_BAR")
        seen.add(key)
        prices=[number(row.get(k),minimum="0.000000000001")for k in("open","high","low","close")]
        if prices[2]>min(prices[0],prices[3])or prices[1]<max(prices[0],prices[3]):fail("PORTFOLIO_OHLC_INVALID")
        volume=None if source and row.get('volume')is None else number(row.get("volume",0),minimum=0)
        if source is None and volume!=volume.to_integral_value():fail("PORTFOLIO_VOLUME_INVALID")
        symbols[ticker].append(DailyBar(at,*prices,volume))
    if not 1<=len(symbols)<=250:fail("PORTFOLIO_SYMBOL_LIMIT")
    for bars in symbols.values():bars.sort(key=lambda b:b.session_date)
    return dict(sorted(symbols.items()))


def request(value,dataset,*,sdk=False):
    allowed={"dataset_id","strategy","start_date","end_date","initial_cash","fee_per_share","minimum_fee","spread_bps","benchmark_id","benchmark_mode","contributions","dividends"}
    strategies={s["id"]:s["warmup_bars"] for s in STRATEGIES}
    if (type(value)is not dict or set(value)-allowed or type(value.get("strategy")) is not str
            or value["strategy"] not in ({"sdk"} if sdk else strategies)):
        fail("PORTFOLIO_REQUEST_INVALID")
    symbols=validate_dataset(dataset);dates=[b.session_date for bars in symbols.values()for b in bars]
    start=day(value.get("start_date")or min(dates));end=day(value.get("end_date")or max(dates))
    if start>end or start<min(dates)or end>max(dates)or end>=datetime.now(timezone.utc).date().isoformat():fail("PORTFOLIO_DATE_RANGE_INVALID")
    selected={ticker:[b for b in bars if start<=b.session_date<=end]for ticker,bars in symbols.items()}
    if any(len(bars)<(2 if sdk else strategies[value["strategy"]]+1)for bars in selected.values()):fail("PORTFOLIO_INSUFFICIENT_SYMBOL_SESSIONS")
    if sdk:
        from .sdk_daily import MAX_SESSIONS
        if len(selected)!=1 or len(next(iter(selected.values())))>MAX_SESSIONS:
            fail("SDK_DAILY_DATA_BUDGET_EXCEEDED")
        if dataset.get("scope") not in {"public","synthetic"} or dataset.get("dividends") or value.get("dividends"):
            fail("SDK_DAILY_DATA_SCOPE_UNSUPPORTED")
    cfg={**value,"start_date":start,"end_date":end,"initial_cash":str(number(value.get("initial_cash","10000"),minimum="1")),
         "fee_per_share":str(number(value.get("fee_per_share","0.005"),minimum=0)),"minimum_fee":str(number(value.get("minimum_fee","1"),minimum=0)),
         "spread_bps":str(number(value.get("spread_bps","0"),minimum=0))}
    if number(cfg["spread_bps"])>1000 or number(cfg["initial_cash"])>Decimal("1e12"):fail("PORTFOLIO_COST_LIMIT")
    # Current engine has no public external-capital method. DCA comparison is
    # separately labeled, never silently injected into a strategy account.
    if cfg.get("contributions") and not cfg.get("benchmark_id"):fail("PORTFOLIO_CONTRIBUTIONS_REQUIRE_BENCHMARK")
    return cfg,selected


def calendar(curve):
    output={"daily":[],"monthly":[],"yearly":[]}
    for point in curve:
        output["daily"].append({k:point[k]for k in("at","daily_pnl","daily_return","valuation_status")})
    for label,width in(("monthly",7),("yearly",4)):
        grouped=defaultdict(list)
        for point in curve:grouped[point["at"][:width]].append(point)
        for at,points in sorted(grouped.items()):
            complete=all(p["daily_pnl"]is not None and p["daily_return"]is not None for p in points)
            pnl=sum((number(p["daily_pnl"])for p in points),Decimal(0))if complete else None
            growth=Decimal(1)
            if complete:
                for p in points:growth*=1+number(p["daily_return"])
            output[label].append({"at":at,"net_pnl":text(pnl),"return_fraction":text(growth-1)if complete else None,"observed_sessions":len(points),"complete":complete})
    return output


def make_curve(points,initial,flows=None):
    flows=flows or {};capital=initial;previous=initial;result=[]
    for at,equity in points:
        flow=flows.get(at,Decimal(0));capital+=flow
        pnl=None if equity is None or previous is None else equity-previous-flow
        denominator=None if previous is None else previous+flow
        ret=None if pnl is None or denominator<=0 else pnl/denominator
        result.append({"at":at,"equity":text(equity),"contributed_capital":text(capital),"external_flow":text(flow),
            "net_profit":text(equity-capital)if equity is not None else None,"return_fraction":text((equity-capital)/capital)if equity is not None else None,
            "daily_pnl":text(pnl),"daily_return":text(ret),"valuation_status":"OBSERVED"if equity is not None else"MISSING_REQUIRED_PRICE"})
        previous=equity
    # Cumulative time-weighted return is only available across fully valued
    # periods. Net-profit / contributed capital is a different explicit field.
    growth=Decimal(1);valid=True
    for p in result:
        if p["daily_return"]is None:valid=False
        elif valid:growth*=1+number(p["daily_return"])
        p["time_weighted_return"]=text(growth-1)if valid else None
    return result


def execute(dataset,value,*,active=lambda:None,progress=lambda n:None,strategy_parameters=None,reserve_engine=None,sdk_factory=None):
    cfg,symbols=request(value,dataset,sdk=sdk_factory is not None);active()
    total=number(cfg["initial_cash"])
    if total!=total.quantize(Decimal("0.01"))or total<len(symbols):fail("PORTFOLIO_CAPITAL_TOO_SMALL_OR_NOT_CENTS")
    allocation=(total/len(symbols)).quantize(Decimal("0.01"),rounding="ROUND_DOWN")
    # Decimal partition remainder goes once to the last sleeve.
    allocations={ticker:allocation for ticker in symbols};last=next(reversed(symbols));allocations[last]=total-allocation*(len(symbols)-1)
    days=sorted({b.session_date for bars in symbols.values()for b in bars})
    account={at:Decimal(0)for at in days};cash_curve={at:Decimal(0)for at in days};receivable_curve={at:Decimal(0)for at in days};invalid=set();security=[];fills=[];engine_runs=[]
    actions=[*dataset.get("dividends",[]),*cfg.get("dividends",[])]
    from .adjusted_portfolio import acquisition,series as adjusted_series
    source=acquisition(dataset)
    if source and actions:fail('PORTFOLIO_ADJUSTED_CASH_ACTIONS_UNSUPPORTED')
    if len(actions)>10000:fail("PORTFOLIO_DIVIDEND_LIMIT")
    identities=set()
    for action in actions:
        if type(action)is not dict or not {"ticker","ex_date","pay_date","amount_per_unit","source"}<=set(action)or type(action["source"])is not str or not action["source"]:fail("PORTFOLIO_DIVIDEND_SOURCE_REQUIRED")
        ident=action.get("event_id",(action["ticker"],action["ex_date"],action["pay_date"],action["amount_per_unit"]))
        if ident in identities:fail("PORTFOLIO_DUPLICATE_DIVIDEND")
        identities.add(ident)
        if action["ticker"]not in symbols:fail("PORTFOLIO_DIVIDEND_TICKER_INVALID")
    versions = _ExecutionVersions()
    for index,(ticker,bars)in enumerate(symbols.items()):
        active();cash0=allocations[ticker]
        req=DailyRequest.parse({"ticker":ticker,"start_date":cfg["start_date"],"end_date":cfg["end_date"],"strategy":"buy_hold" if sdk_factory else cfg["strategy"],
            "initial_cash":str(cash0),**({'provider':source['provider']}if source else{}),**{k:cfg[k]for k in("fee_per_share","minimum_fee","spread_bps")}})
        if source:
            series,policy=adjusted_series(dataset,ticker,req)
            if [(b.session_date,b.open,b.high,b.low,b.close,b.volume)for b in bars]!=[(b.session_date,b.open,b.high,b.low,b.close,b.volume)for b in series.bars]:fail('PORTFOLIO_NATIVE_ROW_PROJECTION_MISMATCH')
        else:series=DailySeries(tuple(bars),{},req)
        if reserve_engine is not None:reserve_engine()
        native=simulate(series,active=active,progress=lambda n:None,include_fills=True,
                        strategy_parameters=strategy_parameters,versions=versions,
                        daily_decisions=sdk_factory(ticker) if sdk_factory else None)
        ledger=native["full_accounting"];events=defaultdict(list)
        for fill in ledger["fills"]:events[fill["at"]].append(fill);fills.append({"ticker":ticker,**fill})
        marks={b.session_date:b.close for b in bars};cash=cash0;quantity=Decimal(0);dividends=Decimal(0);rights=[]
        div_events=defaultdict(list)
        for action in actions:
            if action.get("ticker")!=ticker:continue
            if dataset.get("metadata",{}).get("dividend_units")!="same_adjusted_units"and action.get("unit_basis")!="adjusted_simulation_units":fail("PORTFOLIO_DIVIDEND_UNITS_UNVERIFIED")
            ex=day(action["ex_date"]);pay=day(action["pay_date"])
            if pay<ex:fail("PORTFOLIO_DIVIDEND_DATE_INVALID")
            # Value the receivable at ex-date, hold it outside trading capital.
            if cfg["start_date"]<=ex<=cfg["end_date"]and ex not in marks:fail("PORTFOLIO_DIVIDEND_EX_DATE_UNOBSERVED")
            div_events[ex].append((number(action["amount_per_unit"],minimum=0),pay))
        native_equity={p["at"]:number(p["equity"])for p in native["equity_curve"]}
        for at in days:
            for dividend,pay in div_events.get(at,[]):
                amount=quantity*dividend;dividends+=amount;rights.append((pay,amount))
            for fill in events.get(at,[]):
                size=number(fill["quantity"]);price=number(fill["price"]);fee=number(fill["fee"])
                if fill["side"]=="buy":cash-=size*price+fee;quantity+=size
                else:cash+=size*price-fee;quantity-=size
            paid=sum((amount for pay,amount in rights if pay<=at),Decimal(0))
            cash_curve[at]+=cash+paid;receivable_curve[at]+=dividends-paid
            if quantity and at not in marks:invalid.add(at);continue
            equity=cash+quantity*marks.get(at,Decimal(0))
            if at in native_equity and not math.isclose(float(equity),float(native_equity[at]),rel_tol=1e-9,abs_tol=1e-6):fail("PORTFOLIO_ENGINE_LEDGER_MISMATCH")
            account[at]+=equity+dividends
        pnl=number(ledger["realized_pnl"])+number(ledger["unrealized_pnl"])+dividends
        if ledger["fill_count"]:
            security.append({"ticker":ticker,"allocation":str(cash0),"realized_pnl":ledger["realized_pnl"],"unrealized_pnl":ledger["unrealized_pnl"],
                "dividends":str(dividends),"dividend_cash_paid":str(paid),"dividend_receivable":str(dividends-paid),"dividend_coverage":"EXPLICIT_SOURCED_EVENTS"if actions else"NOT_PROVIDED_PRICE_ONLY", "fees":ledger["fees"],"net_pnl":str(pnl),"trade_count":ledger["fill_count"],
                "closed_round_trips":len(native["trades"]),"open_positions":native["open_trades"]})
        engine_runs.append({"ticker":ticker,"allocation":str(cash0),"engine":native["engine"],"warmup":native["report"]["warmup"],"pending_orders":native["pending_orders"]})
        if sdk_factory:
            engine_runs[-1].update(sdk_decisions=native["decisions"], orders=native["orders"],
                                   sdk_worker="WINDOWS_TRUSTED_SDK_SUBPROCESS", sdk_calls=len(native["decisions"]),
                                   decision_contract="OBSERVED_DAILY_PREFIX_TO_NEXT_OBSERVED_OPEN")
        if source:engine_runs[-1].update(price_policy=policy,history=series.history,quantity_basis='ADJUSTED_SIMULATION_UNITS',fee_basis='PER_ADJUSTED_SIMULATION_UNIT_WITH_MINIMUM')
        progress(10+(index+1)*70//len(symbols))
    curve=make_curve([(at,None if at in invalid else account[at])for at in days],total)
    for point in curve:point.update(cash=str(cash_curve[point["at"]]),dividend_receivable=str(receivable_curve[point["at"]]))
    ranked=sorted(security,key=lambda r:(-number(r["net_pnl"]),r["ticker"]))
    profit=number(curve[-1]["net_profit"])if curve[-1]["net_profit"]is not None else None
    security_sum=sum((number(r["net_pnl"])for r in security),Decimal(0));residual=None if profit is None else profit-security_sum
    if residual is not None and abs(residual)>Decimal("0.00001"):fail("PORTFOLIO_ACCOUNT_RECONCILIATION_FAILED")
    result={"family":FAMILY,"schema_version":FAMILY,"scope":dataset.get("scope","UNVERIFIED"),"status":"SIMULATION_NOT_MARKET_VALIDATION",
        "dataset_reference":{k:dataset[k]for k in("dataset_id","version","content_hash")},"request":cfg,"engine":{**ENGINE,"allocation":"ONE_INITIAL_ACCOUNT_EQUAL_FIXED_SLEEVES_NO_REBALANCE"},
        "allocations":{k:str(v)for k,v in allocations.items()},"equity_curve":curve,"calendar":calendar(curve),"security_pnl":{"all":ranked,"top5":ranked[:5],"worst":list(reversed(ranked))},
        "fills":sorted(fills,key=lambda r:(r["at"],r["ticker"])),"engine_runs":engine_runs,
        "reconciliation":{"initial_capital":str(total),"allocated_capital":str(sum(allocations.values())),"net_profit":text(profit),"security_net_pnl":str(security_sum),
            "cash_residual":text(residual),"fees":str(sum((number(r["fees"])for r in security),Decimal(0))),"tolerance":"0.00001","external_contributions":"0"},
        "return_basis":"PRICE_AND_PROVIDED_CASH_DIVIDENDS"if actions else"PRICE_ONLY_DIVIDENDS_NOT_PROVIDED","return_definition":"NET_PROFIT_OVER_INITIAL_CAPITAL; calendar returns compound daily returns",
        "dividend_policy":"RECEIVABLE_AT_EX_DATE_HELD_UNINVESTED_NOT_REINVESTED; payment is transfer from receivable, never a second profit",
        "tax_policy":"NO_WITHHOLDING_OR_OTHER_TAX_INPUT; not an after-tax return claim",
        "coverage":{"first":days[0],"last":days[-1],"symbols":len(symbols),"missing_valuation_days":sorted(invalid),"PIT":"NOT_CERTIFIED","calendar":"ONLY_OBSERVED_UNION_DATES_NO_PRICE_IMPUTATION"}}
    if source:result.update(display_name='按复权价格模拟',quantity_basis='ADJUSTED_SIMULATION_UNITS',fee_basis='PER_ADJUSTED_SIMULATION_UNIT_WITH_MINIMUM',return_basis='ADJUSTED_PRICE_PATH_NOT_SHAREHOLDER_TOTAL_RETURN',native_acquisition=source,
        native_caveat='数量为调整模拟单位，非历史实际股数；源复权锚/填充可能含后见信息，分段验证不证明PIT或无前视；无额外拆股/现金分红/成交量容量推断。')
    if strategy_parameters is not None:result['search_template']={'version':'controlled-long-cash-parameters-1','parameters':strategy_parameters}
    active();return result


def benchmark(dataset,spec,*,active=lambda:None,verified_benchmark=False):
    symbols=validate_dataset(dataset,verified_benchmark=verified_benchmark)
    if len(symbols)!=1:fail("BENCHMARK_SINGLE_SERIES_REQUIRED")
    metadata=dataset.get("benchmark",dataset.get("benchmark_metadata",{}))
    if not metadata:fail("BENCHMARK_IDENTITY_REQUIRED")
    mode=spec.get("benchmark_mode","buy_hold")
    if mode not in {"buy_hold","dca","dca_reserved"}:fail("BENCHMARK_MODE_INVALID")
    reserved=mode=="dca_reserved"
    start=day(spec["start_date"]);end=day(spec["end_date"])
    bars=[b for b in next(iter(symbols.values()))if start<=b.session_date<=end]
    if not bars:fail("BENCHMARK_NO_COVERAGE")
    initial=number(spec["initial_cash"],minimum=1);cash=initial;units=Decimal(0);invested=False
    schedules=defaultdict(Decimal)
    contributions=spec.get("contributions",[])
    if reserved and(type(contributions)is not list or len(contributions)>1000):fail("BENCHMARK_RESERVED_BUDGET_INVALID")
    for flow in contributions:
        if reserved and(type(flow)is not dict or set(flow)!={"date","amount"}):fail("BENCHMARK_RESERVED_BUDGET_INVALID")
        at=day(flow["date"])
        if not start<=at<=end:fail("BENCHMARK_CONTRIBUTION_DATE_INVALID")
        if mode not in {"dca","dca_reserved"}:fail("BENCHMARK_CONTRIBUTIONS_REQUIRE_DCA")
        schedules[at]+=number(flow["amount"],minimum="0.000001")
    scheduled=sum(schedules.values(),Decimal(0))
    if reserved and scheduled>initial:fail("BENCHMARK_RESERVED_BUDGET_EXCEEDS_INITIAL_CAPITAL")
    fee_per=number(spec.get("fee_per_share","0.005"),minimum=0);minimum=number(spec.get("minimum_fee","1"),minimum=0)
    spread=number(spec.get("spread_bps","0"),minimum=0)/10000
    points=[];flows={};fees=Decimal(0);pending=sorted(schedules);fills=[];balances=[];spent=Decimal(0);processed=Decimal(0)
    for bar in bars:
        active();inflow=Decimal(0)
        while pending and pending[0]<=bar.session_date:inflow+=schedules[pending.pop(0)]
        # Reserved tranches are purchase ceilings drawn from the one initial
        # cash balance. They never create another deposit, flow or profit.
        if reserved:
            available=min(cash,inflow);flows[bar.session_date]=Decimal(0);processed+=inflow
        else:
            cash+=inflow;flows[bar.session_date]=inflow;available=cash
        if(inflow>0 if reserved else not invested or inflow):
            price=bar.open*(1+spread)
            size=max(0,int(min(available/(price+fee_per),(available-minimum)/price)))
            fee=max(minimum,Decimal(size)*fee_per)if size else Decimal(0)
            if size:
                cost=size*price+fee
                cash-=cost;units+=size;fees+=fee;spent+=cost
                fills.append({"at":bar.session_date,"quantity":str(size),"price":str(price),"fee":str(fee)})
                if reserved:fills[-1].update(purchase_budget=str(inflow),cash_after=str(cash),cost=str(cost))
            invested=True
        points.append((bar.session_date,cash+units*bar.close))
        if reserved:balances.append({"cash":str(cash),"quantity":str(units),"purchase_budget":str(inflow)})
    curve=make_curve(points,initial,flows)
    if reserved:
        for point,balance in zip(curve,balances):point.update(balance)
        if cash!=initial-spent:fail("BENCHMARK_RESERVED_CAPITAL_RECONCILIATION_FAILED")
    matched=reserved or not schedules
    price_basis=metadata.get("return_basis")=="price"and not(reserved and spec.get("dividends"))
    result={"family":"benchmark-comparison-1","dataset_reference":{k:dataset[k]for k in("dataset_id","version","content_hash")},"metadata":metadata,"curve":curve,"fills":fills,"fees":str(fees),
        "cache_key":benchmark_key(dataset,spec),
        "coverage_complete":bars[0].session_date==start and bars[-1].session_date==end,
        "uninvested_contributions":[]if reserved else[{"date":at,"amount":str(schedules[at])}for at in pending],
        "comparable":matched and bars[0].session_date==start and bars[-1].session_date==end and price_basis,
        "comparability_reason":"STRATEGY_INITIAL_ONLY_NO_MATCHED_DCA_CASH_FLOWS"if not matched else"BENCHMARK_COVERAGE_INCOMPLETE"if bars[0].session_date!=start or bars[-1].session_date!=end else"RETURN_BASIS_MISMATCH"if not price_basis else"SAME_INITIAL_CAPITAL_RESERVED_PURCHASE_BUDGETS_NO_EXTERNAL_FLOWS"if reserved else"SAME_INITIAL_CAPITAL_DATE_RANGE_COSTS; benchmark buys first open, strategy signals next observed open",
        "return_definition":"return_fraction=net_profit/contributed_capital (not IRR); time_weighted_return compounds BOD-flow-adjusted daily returns",
        "return_basis":metadata.get("return_basis"),"dividend_policy":metadata.get("dividend_policy"),"calendar":calendar(curve)}
    if verified_benchmark:
        result.update(cash_remaining=str(cash), quantity=str(units), lot_size=1)
    if reserved:result.update(benchmark_mode=mode,calculation_version=RESERVED_BENCHMARK_CALCULATION,
        capital_policy="ONE_INITIAL_CAPITAL; contributions are per-date purchase ceilings, not deposits; fees included; unused tranche cash is retained, not added to later purchase ceilings",
        reconciliation={"initial_capital":str(initial),"external_contributions":"0","scheduled_purchase_budget":str(scheduled),"processed_purchase_budget":str(processed),
            "spent_including_fees":str(spent),"cash":str(cash),"quantity":str(units),"capital_residual":str(initial-spent-cash)},
        unexecuted_purchase_budgets=[{"date":at,"amount":str(schedules[at])}for at in pending])
    return result


def benchmark_key(dataset,spec):
    reserved=spec.get("benchmark_mode")=="dca_reserved"
    return digest({"dataset":{k:dataset[k]for k in("dataset_id","version","content_hash")},"metadata":dataset.get("benchmark",dataset.get("benchmark_metadata",{})),"spec":spec,"cash_policy":"ONE_INITIAL_CAPITAL_RESERVED_BUDGETS_INTEGER_SHARES"if reserved else"UNINVESTED_INTEGER_SHARES","execution":"OBSERVED_OPEN_WITH_SPREAD","calculation":RESERVED_BENCHMARK_CALCULATION if reserved else"benchmark-cash-flow-1"})
