"""Official LangGraph plan -> real bounded tool -> assistant/team reporting.

Single assistant is explicitly one saved model; selected team mode uses all
selected snapshot roles. This is distinct from the preserved native TA debate.
"""
import json
from .model_call_observation import ModelCallObservation, prepare_openai_responses_invoke
from .portfolio import validate_dataset,request,digest,fail
from .task_plans import summary,factor_validation,FACTORS
from .portfolio import execute as portfolio_execute

TOOLS={"dataset_summary","factor_validation","factor_exploration","strategy_validation","document_analysis","needs_data"}


def evidence_summary(result):
    if result["family"]=="dataset-portfolio-result-1":
        return {k:result[k]for k in("family","reconciliation","security_pnl","coverage","return_basis")}
    if result["family"]=="factor-validation-result-1":
        return {k:result[k]for k in("family","factor","mean_rank_ic","excluded_rows","status","label","PIT","market_validity")}|{"sample_rows":result.get("sample_rows",len(result["rows"])),"cross_sections":len(result["rank_ic"])}
    return result


def _shared_route(compiled):
    """Only validated tool parameters enter a shared graph."""
    tool = compiled["tool"]
    route = {"tool": tool}
    for name, supported_tool in (("factor", "factor_validation"), ("strategy", "strategy_validation")):
        if tool == supported_tool:
            route[name] = compiled[name]
    return route


def execute_agent(dataset,plan,*,secret_resolver,target_resolver,transports=None,active=lambda:None,progress=lambda n:None,observe=lambda v:None):
    from langgraph.graph import StateGraph,START,END
    from langchain_core.messages import SystemMessage,HumanMessage
    from langsmith import tracing_context
    from .provider_runtime import create_client
    from .tradingagents_workflow import Budget,budget_callback
    from typing import TypedDict
    profiles=plan["model_profiles"]
    licensed_local = dataset.get("scope") == "licensed_local"
    if not 1<=len(profiles)<=12:fail("AGENT_MODEL_SELECTION_INVALID")
    if plan["execution_mode"]=="single_assistant"and len(profiles)!=1:fail("AGENT_SINGLE_MODEL_REQUIRED")
    if licensed_local and plan["execution_mode"] != "single_assistant":
        fail("WRDS_LOCAL_MODEL_TOOL_REQUIRED")
    budget=Budget({},active=active);callback=budget_callback(budget);calls=[];observations=[]
    transport_map=transports or {}

    def invoke(selected,system,content):
        active();configuration=selected["configuration"];choice=configuration["model"];limits=configuration["budgets"]
        text=json.dumps(content,ensure_ascii=False,allow_nan=False)
        p=configuration["prompt"]
        instruction="\n".join([system,p["system"],p["task"],p["output"],configuration.get("context","")])
        if len(instruction)+len(text)>limits["input_chars"]:fail("AGENT_INPUT_CHAR_BUDGET_EXCEEDED")
        # Visible conservative size gate for models without a local exact
        # tokenizer. Never advertise it as provider-exact input token usage.
        bound=len((instruction+text).encode("utf-8"))+1024
        if bound>limits["input_tokens"]:fail("AGENT_CONSERVATIVE_INPUT_BOUND_EXCEEDED")
        target=target_resolver(choice,expected_target=selected.get("execution_target"))
        secret=None if choice["provider"]=="ollama"and not choice.get("secret_ref")else secret_resolver(choice,target=target)
        observation=ModelCallObservation(choice,limits,role=selected.get('role','assistant'),selected=selected)
        observation_status='FAILED'
        try:
            with tracing_context(enabled=False):
                client=create_client(choice,output_tokens=limits["output_tokens"],timeout_seconds=120,retry=0,secret=secret,
                    target=target,transport=transport_map.get(choice["provider"]))
                invoke_options,provenance=prepare_openai_responses_invoke(client)
                result=observation.invoke(lambda:client.invoke([SystemMessage(content=instruction),HumanMessage(content=text)],config={"callbacks":[callback],"metadata":{"task_kind":"bounded_tool_research"}},**invoke_options),provenance=provenance)
            active();output=result.text
            usage=getattr(result,"usage_metadata",None)or{}
            if usage.get("input_tokens",0)>limits["input_tokens"]or usage.get("output_tokens",0)>limits["output_tokens"]:fail("AGENT_REPORTED_TOKEN_BUDGET_EXCEEDED")
            if not isinstance(output,str):fail("AGENT_TEXT_INVALID")
            calls.append({"profile_id":selected.get("profile_id"),"revision":selected.get("revision"),"role":selected.get("role","assistant"),
                "provider":choice["provider"],"model":choice["model"],"input_bound":bound,"input_basis":"UTF8_BYTES_PLUS1024_CONSERVATIVE_NOT_EXACT","output_characters":len(output)})
            observation_status='SUCCEEDED'
            return str.__str__(output)

        finally:
            observations.append(observation.finish(observation_status,observe))

    class State(TypedDict,total=False):
        compiled:dict
        tool_result:dict
        reports:list

    def compile_plan(state):
        rows=validate_dataset(dataset)
        manifest = {}
        if not licensed_local:
            manifest.update(securities=list(rows), first=min(b[0].session_date for b in rows.values()),
                            last=max(b[-1].session_date for b in rows.values()))
        manifest.update(available_data=["OHLCV"], PIT="NOT_CERTIFIED",
                        financial_statements="NOT_PROVIDED", universe="FIXED_IMPORT")
        documents=list(plan.get("evidence",[]))
        manifest["planner_private_context_available"]=bool(profiles[0]["configuration"].get("context"))
        if documents:manifest.update(financial_statements="USER_SUPPLIED_MATERIAL_NOT_INDEPENDENTLY_VERIFIED",documents=documents)
        instruction='只输出JSON对象。tool只能是dataset_summary、factor_validation、factor_exploration、strategy_validation、document_analysis、needs_data。可选factor只能是return_1、momentum_20、sma20_distance；strategy只能buy_hold、sma20、sma_cross_5_20。reason和missing_evidence用中文。财报和用户提供文档/模型上下文有关的任务可用document_analysis并引用原材料，不得一律降级；无相关材料时用needs_data。不能将财报前瞻请求降成OHLCV摘要；前瞻仅作明确条件下情景分析，不得编未来事实。任意自由公式无法计算时用needs_data。探索模式实际验证三个有限因子，不能宣称发现有效预测。不得输出代码或其他工具。'
        raw=invoke(profiles[0],instruction,{"user_task":plan["prompt"],"dataset":manifest})
        try:compiled=json.loads(raw)
        except (ValueError,TypeError):fail("AGENT_PLAN_JSON_INVALID")
        if type(compiled)is not dict or set(compiled)-{"tool","factor","strategy","reason","missing_evidence"}or compiled.get("tool")not in TOOLS:fail("AGENT_PLAN_SCHEMA_INVALID")
        if compiled["tool"]=="factor_validation"and compiled.get("factor")not in FACTORS:fail("AGENT_FACTOR_UNSUPPORTED")
        if compiled["tool"]=="strategy_validation"and compiled.get("strategy")not in {"buy_hold","sma20","sma_cross_5_20"}:fail("AGENT_STRATEGY_UNSUPPORTED")
        if len(profiles) > 1:
            compiled = _shared_route(compiled)
        progress(20);return {"compiled":compiled}

    def run_tool(state):
        active();compiled=state["compiled"];tool=compiled["tool"]
        if tool=="needs_data":result={"family":"research-evidence-gap-1","status":"NEEDS_DATA","reason":compiled.get("reason","缺少所需资料"),"missing_evidence":compiled.get("missing_evidence",[]),"market_claim":"NONE"}
        elif tool=="document_analysis":
            documents=list(plan.get("evidence",[]));as_of=plan.get("as_of")
            private=[p.get("profile_id")for p in profiles if p["configuration"].get("context")]
            excluded=[];eligible=[]
            for doc in documents:
                from finance_platform.core.contracts import parse_instant
                if as_of and(not doc.get("available_at")or parse_instant(doc["available_at"])>parse_instant(as_of)):excluded.append({"id":doc["id"],"reason":"UNKNOWN_OR_LATER_AVAILABILITY"})
                else:eligible.append(doc)
            result={"family":"document-evidence-analysis-1","status":"PROVIDED_EVIDENCE_READY"if eligible or private and not as_of else"NEEDS_DATA","documents":eligible,"excluded":excluded,
                "private_context_policy":"Each model receives only its own context in its system message; context text never enters shared graph state or another model input.",
                "provenance":"USER_SUPPLIED_NOT_INDEPENDENTLY_VERIFIED","PIT":"ELIGIBILITY_CHECKED_USING_DECLARED_AVAILABLE_AT_ONLY"if as_of else"NOT_CERTIFIED",
                "future_claims":"SCENARIOS_ONLY_NOT_FUTURE_FACTS"}
        elif tool=="dataset_summary":result=summary(dataset,active=active)
        elif tool=="factor_validation":result=factor_validation(dataset,compiled["factor"],active=active)
        elif tool=="factor_exploration":result={"family":"factor-exploration-result-1","results":[evidence_summary(factor_validation(dataset,f,active=active))for f in FACTORS],"tests":3,"selection_bias":"EXPLORATORY_MULTIPLE_TESTING_NOT_OUT_OF_SAMPLE","market_validity":"NOT_ESTABLISHED"}
        elif tool=="strategy_validation":result=portfolio_execute(dataset,{**plan.get("parameters",{}),"strategy":compiled["strategy"]},active=active,progress=lambda n:None)
        progress(75);return {"tool_result":result}

    def report(state):
        reports=[]
        for selected in profiles:
            active();role=selected.get("role","assistant")
            output=invoke(selected,'根据真实工具结果和用户任务写研究说明；明确数据缺口、样本范围、收益和风险。禁止编造财报、未来数据、成交、市场验证或已完成缺失任务。你是'+role,
                {"user_task":plan["prompt"],"executed_plan":state["compiled"],"tool_evidence":evidence_summary(state["tool_result"])})
            reports.append({"role":role,"profile_id":selected.get("profile_id"),"revision":selected.get("revision"),"text":output})
            if state["tool_result"]["family"]=="document-evidence-analysis-1":
                reports[-1]["evidence_sources"]=[{k:d.get(k)for k in("id","source","published_at","available_at")}for d in state["tool_result"]["documents"]]
                if selected["configuration"].get("context")and not plan.get("as_of"):reports[-1]["evidence_sources"].append({"id":"own-model-context","source":"USER_MODEL_CONTEXT","published_at":None,"available_at":None})
        progress(90);return {"reports":reports}

    workflow=StateGraph(State);workflow.add_node("plan",compile_plan);workflow.add_node("tool",run_tool);workflow.add_node("report",report)
    workflow.add_edge(START,"plan");workflow.add_edge("plan","tool")
    if licensed_local:
        workflow.add_edge("tool",END)
    else:
        workflow.add_edge("tool","report")
        workflow.add_edge("report",END)
    graph=workflow.compile()
    if graph.stream_eager is not False:fail("AGENT_EAGER_STREAM_UNSUPPORTED")
    state=graph.invoke({},config={"max_concurrency":1,"recursion_limit":8})
    active()
    if state["tool_result"].get("status") == "NEEDS_DATA":
        status = "EVIDENCE_GAP_REPORTED"
    elif licensed_local:
        status = "MODEL_PLAN_AND_LOCAL_LICENSED_TOOLS_COMPLETED"
    else:
        status = "REAL_TOOLS_AND_MODEL_REPORT_COMPLETED"
    return {"family":"model-tool-research-result-1","execution_mode":plan["execution_mode"],"plan":state["compiled"],"tool_result":state["tool_result"],
        "reports":state.get("reports", []),"model_calls":calls,"model_observations":observations,"budget":budget.snapshot(),"status":status,
        "engine":"OFFICIAL_LANGGRAPH_STATEGRAPH","market_validity":"NOT_ESTABLISHED","paid_calls_in_acceptance":"NOT_RUN"}
