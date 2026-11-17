"""Bounded supported-language planning and actual observed-data tools.

This is an explicit deterministic grammar, not a claim of general LLM reasoning.
Unrecognized/ambiguous requests ask for supported input instead of fabricating
an analysis or executing untrusted code. Existing TA/Qlib services stay intact.
"""
from collections import defaultdict
from decimal import Decimal
from .portfolio import validate_dataset,request,execute,number,digest,fail

FACTORS={"return_1":1,"momentum_20":20,"sma20_distance":20}


def plan_task(value,dataset):
    if type(value)is not dict or set(value)-{"prompt","dataset_id","kind","strategy","factor","parameters","team_snapshot_id"}:fail("TASK_REQUEST_INVALID")
    prompt=value.get("prompt","")
    if type(prompt)is not str or not 1<=len(prompt.strip())<=2000:fail("TASK_PROMPT_INVALID")
    kind=value.get("kind")
    if kind is None:
        matches=[]
        if any(s in prompt.lower()for s in("因子","factor")):matches.append("factor_validation")
        if any(s in prompt.lower()for s in("回测","策略","backtest","strategy")):matches.append("strategy_validation")
        if not matches and any(s in prompt.lower()for s in("研究","分析","概况","research","summary")):matches.append("dataset_summary")
        if len(matches)!=1:return {"status":"NEEDS_INPUT","reason":"请选择数据研究、因子验证或策略验证；复杂请求请拆成可执行任务。","supported_kinds":["dataset_summary","factor_validation","strategy_validation"]}
        kind=matches[0]
    if kind not in {"dataset_summary","factor_validation","strategy_validation"}:fail("TASK_KIND_UNSUPPORTED")
    params=value.get("parameters",{})
    if type(params)is not dict:fail("TASK_PARAMETERS_INVALID")
    if kind=="strategy_validation":
        strategy=value.get("strategy")
        if strategy is None:
            if "5/20"in prompt or "交叉"in prompt:strategy="sma_cross_5_20"
            elif "20日"in prompt or "20日均线"in prompt:strategy="sma20"
            elif "持有"in prompt or "buy"in prompt.lower():strategy="buy_hold"
        if strategy is None:return {"status":"NEEDS_INPUT","reason":"请选择买入持有、20日均线或5/20日均线策略。"}
        params,_=request({**params,"strategy":strategy,"dataset_id":dataset["dataset_id"]},dataset)
    elif kind=="factor_validation":
        factor=value.get("factor")
        if factor is None:
            if "动量"in prompt:factor="momentum_20"
            elif "均线"in prompt:factor="sma20_distance"
            elif "日收益"in prompt or "return_1"in prompt:factor="return_1"
        if factor not in FACTORS:return {"status":"NEEDS_INPUT","reason":"请选择20期动量、20期均线距离或单期收益因子。"}
        if params:fail("TASK_FACTOR_UNKNOWN_PARAMETER")
        params={"factor":factor}
    elif params:fail("TASK_SUMMARY_UNKNOWN_PARAMETER")
    if value.get("team_snapshot_id")is not None:
        return {"status":"NEEDS_INPUT","reason":"这些任务直接运行数据工具。需要多Agent报告时请使用已有团队研究入口，不能把数值工具报告冒称模型回答。"}
    plan={"family":"supported-research-plan-1","status":"READY","kind":kind,"prompt":prompt,"parameters":params,
        "dataset_reference":{k:dataset[k]for k in("dataset_id","version","content_hash")},
        "tools":[kind],"planner":"SUPPORTED_DETERMINISTIC_GRAMMAR_1","requires_paid_model":False,"market_claim":"NONE"}
    plan["plan_hash"]=digest(plan)
    return plan


def summary(dataset,*,active=lambda:None):
    symbols=validate_dataset(dataset);items=[]
    for ticker,bars in symbols.items():
        active();returns=[float(b.close/a.close-1)for a,b in zip(bars,bars[1:])]
        items.append({"ticker":ticker,"rows":len(bars),"first":bars[0].session_date,"last":bars[-1].session_date,
            "observed_close_return":str(bars[-1].close/bars[0].close-1),"observed_returns":len(returns),
            "return_min":min(returns)if returns else None,"return_max":max(returns)if returns else None})
    return {"family":"dataset-research-result-1","tool":"dataset_summary","items":items,"status":"OBSERVED_DATA_ANALYSIS",
        "interpretation":"实际输入统计；未调用模型，不代表真实市场预测、PIT或研究有效性验证。"}


def factor_validation(dataset,factor,*,active=lambda:None):
    if factor not in FACTORS:fail("TASK_FACTOR_UNSUPPORTED")
    import pandas as pd
    symbols=validate_dataset(dataset);rows=[];excluded=0;window=FACTORS[factor];missing_next=0
    calendar=sorted({b.session_date for bars in symbols.values()for b in bars})
    next_session=dict(zip(calendar,calendar[1:]))
    for ticker,bars in symbols.items():
        for i,bar in enumerate(bars):
            if i%64==0:active()
            if i<window or i+1>=len(bars):excluded+=1;continue
            if bars[i+1].session_date!=next_session.get(bar.session_date):missing_next+=1;continue
            if factor=="sma20_distance":value=bar.close/(sum((b.close for b in bars[i-19:i+1]),Decimal(0))/20)-1
            else:value=bar.close/bars[i-window].close-1
            rows.append({"ticker":ticker,"as_of":bar.session_date,"label_at":bars[i+1].session_date,
                "factor":float(value),"forward_return":float(bars[i+1].close/bar.close-1)})
    by_day=defaultdict(list)
    for row in rows:by_day[row["as_of"]].append(row)
    correlations=[];sections_excluded={"fewer_than_three":0,"constant_factor_or_label":0}
    for at,group in sorted(by_day.items()):
        active();frame=pd.DataFrame(group)
        if len(group)<3:sections_excluded["fewer_than_three"]+=1;continue
        if frame.factor.nunique()<2 or frame.forward_return.nunique()<2:sections_excluded["constant_factor_or_label"]+=1;continue
        # Spearman is Pearson correlation of actual average ranks; no SciPy
        # dependency or made-up constant score for insufficient samples.
        score=frame.factor.rank().corr(frame.forward_return.rank())
        if pd.notna(score):correlations.append({"at":at,"rank_ic":float(score),"securities":len(group)})
    return {"family":"factor-validation-result-1","tool":"factor_validation","factor":factor,"rows":rows[:2000],"sample_rows":len(rows),"rows_omitted":max(0,len(rows)-2000),"row_listing":"FIRST2000_DERIVED_ROWS_METRICS_USE_ALL_ELIGIBLE_ROWS","rank_ic":correlations,
        "mean_rank_ic":sum(r["rank_ic"]for r in correlations)/len(correlations)if correlations else None,
        "excluded_rows":excluded+missing_next,"exclusions":{"warmup_or_no_future_row":excluded,"missing_next_union_session_price":missing_next,"sections":sections_excluded},"minimum_cross_section":3,"status":"OBSERVED_SAMPLE_COMPUTED"if correlations else"INSUFFICIENT_EVIDENCE",
        "label":"NEXT_OBSERVED_UNION_SESSION_CLOSE_RETURN_NOT_TRADE_RETURN","label_availability":"ASSUMED_KNOWN_AFTER_LABEL_SESSION_CLOSE_NOT_PIT_CERTIFIED","PIT":"NOT_CERTIFIED","universe":"FIXED_IMPORTED_MEMBERS_NOT_HISTORICAL_CONSTITUENTS","market_validity":"NOT_ESTABLISHED"}


def execute_plan(dataset,plan,*,active=lambda:None,progress=lambda n:None):
    expected={k:dataset[k]for k in("dataset_id","version","content_hash")}
    if expected!=plan.get("dataset_reference")or digest({k:v for k,v in plan.items()if k!="plan_hash"})!=plan.get("plan_hash"):fail("TASK_PLAN_BINDING_MISMATCH")
    active()
    if plan["kind"]=="strategy_validation":return execute(dataset,plan["parameters"],active=active,progress=progress)
    if plan["kind"]=="dataset_summary":return summary(dataset,active=active)
    if plan["kind"]=="factor_validation":return factor_validation(dataset,plan["parameters"]["factor"],active=active)
    fail("TASK_KIND_UNSUPPORTED")
