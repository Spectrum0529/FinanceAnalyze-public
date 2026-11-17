"""Official TradingAgents clients; explicit offline model, no credential discovery.

Transport injection exercises real SDK serialization offline. Real-provider
execution remains disabled unless an explicit host opt-in and secret resolver
are supplied; secrets are never part of snapshots, traces or model selection.
"""
import json
import re
from .team_execution import ROLES,fail

def schema_primitive(schema):
    if schema is None:return None
    if isinstance(schema,type) and hasattr(schema,'model_json_schema'):return schema.model_json_schema()
    return schema

def offline_tokens(messages,tools=None):
    """Exact regex-v1 tokenization defined for deterministic-v1 ONLY."""
    text=json.dumps({'messages':[m.model_dump(mode='json') for m in messages],
        'tools':schema_primitive(tools)},sort_keys=True,ensure_ascii=False,default=str)
    return len(re.findall(r'\w+|[^\w\s]',text))

def offline_model(role,*,as_of,view_hash):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage,ToolMessage
    from langchain_core.outputs import ChatResult,ChatGeneration
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from .team_execution import digest
    class DeterministicChatModel(BaseChatModel):
        @property
        def _llm_type(self):return 'TEST_OFFLINE_deterministic-v1'
        def bind_tools(self,tools,**kwargs):return self.bind(bound_tools=[convert_to_openai_tool(t) for t in tools])
        def _generate(self,messages,stop=None,run_manager=None,**kwargs):
            names={t['function']['name'] for t in kwargs.get('bound_tools',[])}
            note='TEST_OFFLINE 合成验证；证据引用='+view_hash+'；证据不足，保持复核。'
            data={
                'SentimentReport':{'overall_band':'Mixed','overall_score':5.0,'confidence':'low','narrative':note},
                'ResearchPlan':{'recommendation':'Hold','rationale':note,'strategic_actions':'等待更多证据'},
                'TraderProposal':{'action':'Hold','reasoning':note,'entry_price':None,'stop_loss':None,'position_sizing':'零新增仓位'},
                'PortfolioDecision':{'rating':'Hold','executive_summary':note,'investment_thesis':note,'price_target':None,'time_horizon':'TEST_OFFLINE'}}
            tool=next((name for name in data if name in names),None)
            args=data.get(tool)
            if tool is None and role in {'market_analyst','news_analyst','fundamentals_analyst'} and not any(isinstance(m,ToolMessage) for m in messages):
                from datetime import date,timedelta
                chosen={'market_analyst':'get_stock_data','news_analyst':'get_news','fundamentals_analyst':'get_fundamentals'}[role]
                if chosen in names:
                    tool=chosen;args={'curr_date':as_of} if chosen=='get_fundamentals' else {'start_date':(date.fromisoformat(as_of)-timedelta(days=1)).isoformat(),'end_date':as_of}
            calls=[] if tool is None else [{'name':tool,'args':args,'id':'fixed_'+digest({'role':role,'tool':tool,'args':args})[:24],'type':'tool_call'}]
            content='' if calls else ('**Rating**: Hold\n**Executive Summary**: '+note if role=='risk_manager' else note)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content,tool_calls=calls))])
    return DeterministicChatModel(cache=False)


def broker_model(role, exchange, budget):
    """A native chat model that can only use the existing parent channel."""
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatResult, ChatGeneration
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from . import tradingagents_workflow as native

    class BrokerChatModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "TA_PARENT_METERED_RESPONSES"

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            # BaseChatModel supplies this display/tracing label for schemas.
            # Tools are emitted via the exact OpenAI function-schema adapter.
            if set(kwargs)-{"ls_structured_output_format","strict"}:
                native.fail("TA_PROVIDER_REQUEST_INVALID")
            frozen = [convert_to_openai_tool(tool, strict=kwargs.get("strict")) for tool in tools]
            return self.bind(bound_tools=frozen, tool_choice=tool_choice)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if stop is not None or set(kwargs)-{"bound_tools","tool_choice"}:
                native.fail("TA_PROVIDER_REQUEST_INVALID")
            result = exchange({"role":role,"messages":[row.model_dump(mode="json") for row in messages],
                "tools":kwargs.get("bound_tools",[]),"tool_choice":kwargs.get("tool_choice")})
            if type(result)is not dict or set(result)!={"message","spent","error_code"}:
                native.fail("TA_PROVIDER_BUDGET_STATE_INVALID")
            budget.provider_spent(result["spent"])
            if result["error_code"] is not None:
                native.fail(result["error_code"])
            return ChatResult(generations=[ChatGeneration(message=AIMessage.model_validate(result["message"]))])

    return BrokerChatModel(cache=False)

def official_client(choice,*,output_tokens,timeout_seconds,retry,secret,transport=None,endpoint_resolver=None,target=None):
    """Use upstream's normalization/tool/DeepSeek reasoning roundtrip classes.

    Its generic factory drops reasoning_effort for non-GPT names, including
    DeepSeek. Official class constructor fields preserve it across later
    bind_tools/with_structured_output calls (a pre-bound Runnable would not).
    Exact effort support is validated by Core's catalogue before this call.
    """
    from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI,DeepSeekChatOpenAI
    provider=choice['provider'];effort=choice['effort']
    if target is not None or provider not in {'openai','deepseek'} or choice.get('native_parameters') or choice.get('endpoint_profile'):
        from .provider_runtime import create_client
        return create_client(choice,output_tokens=output_tokens,timeout_seconds=timeout_seconds,retry=retry,secret=secret,transport=transport,endpoint_resolver=endpoint_resolver,target=target)
    if provider not in {'openai','deepseek'}:fail('TEAM_PROVIDER_UNSUPPORTED')
    args={'model':choice['model'],'api_key':secret,'max_tokens':output_tokens,
        'timeout':timeout_seconds,'max_retries':retry,'cache':False}
    if transport is not None:args['http_client']=transport
    if effort is not None:args['reasoning_effort']=effort
    if provider=='openai':
        args.update(base_url='https://api.openai.com/v1',use_responses_api=True)
        return NormalizedChatOpenAI(**args)
    # Native DeepSeek efforts are not silently mapped to OpenAI's scale.
    # Current ChatOpenAI renames max_tokens to max_completion_tokens for
    # OpenAI. DeepSeek's native Chat Completions field remains max_tokens.
    args.pop('max_tokens')
    args['extra_body']={'max_tokens':output_tokens}
    if effort is not None:args['extra_body']['thinking']={'type':'enabled'}
    return DeepSeekChatOpenAI(base_url='https://api.deepseek.com',**args)

def clients_for_snapshot(snapshot,*,as_of,view_hash,real_enabled=False,secret_resolver=None,tokenizers=None,
                         transport=None,endpoint_resolver=None,target_resolver=None,broker=None,budget=None):
    models={};counters={};config=snapshot['configuration']
    for role in ROLES:
        choice=snapshot['resolved_roles'][role]['model']
        budgets=snapshot['resolved_roles'][role].get('budgets') or config['budgets']
        if choice['provider']=='offline':
            if choice['model']!='deterministic-v1' or choice['effort']is not None or choice['secret_ref']is not None:fail('TEAM_OFFLINE_CHOICE_INVALID')
            models[role]=offline_model(role,as_of=as_of,view_hash=view_hash);counters[role]=offline_tokens
        elif broker is not None:
            models[role]=broker_model(role,broker,budget);counters[role]=None
        else:
            if not real_enabled or secret_resolver is None:fail('TEAM_REAL_PROVIDER_DISABLED')
            if tokenizers is None or (choice['provider'],choice['model']) not in tokenizers:fail('TEAM_MODEL_TOKENIZER_UNAVAILABLE')
            if not callable(target_resolver):fail('TEAM_FROZEN_TARGET_REQUIRED')
            target=target_resolver(choice)
            secret=secret_resolver(choice['secret_ref'],target=target)
            if type(secret)is not str or not secret:fail('TEAM_SECRET_REFERENCE_UNAVAILABLE')
            models[role]=official_client(choice,output_tokens=budgets['output_tokens'],
                timeout_seconds=config['execution']['timeout_seconds'],retry=config['execution']['retry'],secret=secret,transport=transport,endpoint_resolver=endpoint_resolver,target=target)
            counters[role]=tokenizers[(choice['provider'],choice['model'])]
    return models,counters
