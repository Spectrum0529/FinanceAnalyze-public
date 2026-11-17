"""Actual official SDK tool-call roundtrips inside bounded LangGraph loop."""
import json
from .model_call_observation import ModelCallObservation, prepare_openai_responses_invoke
from .portfolio import fail
from .strategy_search import Search,TOOLS


def invoke_bounded(invoke,check,*,poll_seconds=.05,close=lambda:None):
    """Coordinator stops at aggregate deadline/cancel, not a per-read timeout.

    The owned daemon performs ONLY the already-reserved SDK request. It never
    executes tools, writes results or schedules calls. A late response is
    discarded. Native retry0/remaining-time timeout additionally bounds its IO;
    an initiated remote request may still complete/bill after cancellation.
    """
    import threading
    done=threading.Event();box={}
    def io():
        try:box['result']=invoke()
        except BaseException as error:box['error']=error
        finally:done.set()
    thread=threading.Thread(target=io,name='bounded-search-sdk-io',daemon=True)
    try:
        check();thread.start()
        while not done.wait(poll_seconds):check()
        check()
        if 'error'in box:raise box['error']
        return box['result']
    finally:
        close() # public owned HTTP client/transport closure interrupts local IO
        if thread.ident is not None:thread.join(timeout=1)
        if thread.is_alive():fail('SEARCH_SDK_CLEANUP_UNPROVEN')


def execute_search(dataset,plan,*,secret_resolver,target_resolver,transports=None,active=lambda:None,progress=lambda n:None,native_reader=None,retain=lambda v:None,observe=lambda v:None):
    from langgraph.graph import StateGraph,START,END
    from langchain_core.messages import SystemMessage,HumanMessage,ToolMessage
    from langsmith import tracing_context
    from .provider_runtime import create_client
    from .tradingagents_workflow import Budget,budget_callback
    from typing import TypedDict
    profiles=plan['model_profiles'];session=Search(dataset,plan.get('parameters'),search_limits=plan.get('search_limits'),active=active,progress=progress,native_reader=native_reader,retain=retain)
    if not session.sufficient:return {'family':'model-tool-research-result-1','tool_result':session.result(),'reports':[],'model_calls':[],'status':'INSUFFICIENT_DATA'}
    budget=Budget({},active=session.check);callback=budget_callback(budget);histories=[[]for p in profiles];calls=[];observations=[]
    class State(TypedDict,total=False):
        call:dict
        done:bool
    def model(state):
        session.check()
        if len(calls)>=session.bounds['max_model_calls']:return {'done':True}
        index=len(calls)%len(profiles);selected=profiles[index];config=selected['configuration'];choice=config['model'];bound=config['budgets']
        if len(histories[index])>bound['history_messages']:
            if bound['context_policy']=='reject':fail('AGENT_HISTORY_BUDGET_EXCEEDED')
            keep=bound['history_messages']//2*2
            histories[index]=histories[index][-keep:]if keep else[]
        system='使用每次一个正式工具调用，先提出不同受控参数候选并运行训练期回测，再比较，可有限迭代，最后strategy_finalize。严禁读取验证期后调参，不能宣称挖掘成功。预算耗尽直接结束。'+ '\n'.join([*config['prompt'].values(),config.get('context','')])
        messages=[SystemMessage(content=system),HumanMessage(content=json.dumps({'task':plan['prompt'],'dataset_reference':session.ref,'limits':session.bounds,'training_evidence':session.comparison()},ensure_ascii=False)),*histories[index]]
        encoded=json.dumps([m.model_dump(mode='json')for m in messages],ensure_ascii=False)
        if len(encoded)>bound['input_chars']or len(encoded.encode())+1024>bound['input_tokens']:fail('AGENT_INPUT_BUDGET_EXCEEDED')
        target=target_resolver(choice,expected_target=selected.get('execution_target'))
        secret=None if choice['provider']=='ollama'and not choice.get('secret_ref')else secret_resolver(choice,target=target)
        if choice['provider']!='ollama'and not secret:fail('MODEL_SECRET_NOT_CONFIGURED')
        # These two official adapters expose the public sync HTTP client API.
        # Other provider workflows remain available through the existing path.
        if choice['provider']not in{'openai','deepseek'}:fail('SEARCH_PROVIDER_TOOL_RUNTIME_UNSUPPORTED')
        retain({'event':'MODEL_REQUEST_STARTED','profile_id':selected['profile_id'],'revision':selected['revision'],'call_number':len(calls)+1,'limits':session.bounds})
        import httpx
        injected=(transports or{}).get(choice['provider'])
        owned=None if injected is not None else httpx.Client(trust_env=False,follow_redirects=False)
        transport=injected or owned
        observation=ModelCallObservation(choice,bound,role=selected.get('role','assistant'),selected=selected)
        observation_status='FAILED'
        try:
            with tracing_context(enabled=False):
                client=create_client(choice,output_tokens=bound['output_tokens'],timeout_seconds=max(1,min(120,int(session.bounds['wall_seconds']-(session.clock()-session.started)))),retry=0,secret=secret,target=target,transport=transport)
                # tracing_context is thread-local: scope it inside the IO thread.
                def sdk_call():
                    # Current DeepSeek thinking mode rejects required/named choice;
                    # its API reference does not advertise parallel_tool_calls.
                    # Preserve thinking/effort, use auto, enforce one typed call here.
                    control={'strict':False,'tool_choice':'required','parallel_tool_calls':False}if choice['provider']=='openai'else{'strict':False,'tool_choice':'auto'}
                    with tracing_context(enabled=False):
                        prepared=client.bind_tools(TOOLS,**control)
                        invoke_options,provenance=prepare_openai_responses_invoke(prepared)
                        return observation.invoke(lambda:prepared.invoke(messages,config={'callbacks':[callback]},**invoke_options),provenance=provenance)
                result=invoke_bounded(sdk_call,session.check,close=owned.close if owned is not None else lambda:None)
            session.check();usage=result.usage_metadata or{}
            if usage.get('input_tokens',0)>bound['input_tokens']or usage.get('output_tokens',0)>bound['output_tokens']:fail('AGENT_REPORTED_TOKEN_BUDGET_EXCEEDED')
            if getattr(result,'invalid_tool_calls',None)or len(result.tool_calls)!=1:fail('SEARCH_SINGLE_TYPED_TOOL_CALL_REQUIRED')
            call=result.tool_calls[0]
            if call['name']not in{t['function']['name']for t in TOOLS}:fail('SEARCH_TOOL_UNSUPPORTED')
            histories[index].append(result) # preserve DeepSeek reasoning_content and native tool ID
            calls.append({'profile_id':selected['profile_id'],'revision':selected['revision'],'provider':choice['provider'],'model':choice['model'],'tool_call_id':call['id'],'tool':call['name']})
            retain({'event':'MODEL_TOOL_CALL','call':calls[-1],'usage':budget.snapshot()})
            observation_status='SUCCEEDED'
            return {'call':{'index':index,'name':call['name'],'args':call['args'],'id':call['id']},'done':False}
        finally:
            observations.append(observation.finish(observation_status,observe))
    def tool(state):
        call=state['call']
        step=session.tool(call['name'],call['args'])
        histories[call['index']].append(ToolMessage(content=json.dumps(step,ensure_ascii=False),tool_call_id=call['id']))
        return {'done':session.finished is not None}
    graph=StateGraph(State);graph.add_node('model',model);graph.add_node('tool',tool);graph.add_edge(START,'model')
    graph.add_conditional_edges('model',lambda s:'end'if s.get('done')else'tool',{'end':END,'tool':'tool'})
    graph.add_conditional_edges('tool',lambda s:'end'if s['done']else'model',{'end':END,'model':'model'})
    compiled=graph.compile()
    if compiled.stream_eager is not False:fail('AGENT_EAGER_STREAM_UNSUPPORTED')
    compiled.invoke({},config={'max_concurrency':1,'recursion_limit':session.bounds['max_model_calls']*2+4})
    evidence=session.result()
    note=('已完成'+str(len(session.trials))+'个训练期候选的实际模拟，按训练结果固定'+json.dumps(session.finished['candidate'],ensure_ascii=False)+'，后段仅验证一次，后段净盈亏为'+session.finished['result']['reconciliation']['net_profit']+'美元。此为固定快照内的探索性比较，不代表找到可交易预测。'if session.finished else'预算已用尽；仅保存已执行的训练期证据，尚未进行后段验证，不能称策略搜索成功。')
    return {'family':'model-tool-research-result-1','execution_mode':plan['execution_mode'],'tool_result':evidence,'reports':[{'role':'evidence_summary','text':note,'basis':'DETERMINISTIC_ACTUAL_TOOL_FACTS_NOT_MODEL_NARRATIVE'}],
        'model_calls':calls,'model_observations':observations,'participants':[{'profile_id':p['profile_id'],'revision':p['revision'],'calls':sum(c['profile_id']==p['profile_id']for c in calls),'status':'PARTICIPATED'if any(c['profile_id']==p['profile_id']for c in calls)else'NOT_SCHEDULED_BEFORE_FINALIZATION_OR_BUDGET'}for p in profiles],
        'budget':budget.snapshot(),'status':session.result()['status'],'engine':'OFFICIAL_LANGGRAPH_NATIVE_TOOL_CALLS','market_validity':'NOT_ESTABLISHED'}
