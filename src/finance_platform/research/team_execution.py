"""Per-role Runnable binding over the unmodified official TradingAgents graph.

No provider SDK, Graph, checkpoint, queue or HTTP implementation is duplicated.
Only immutable Core snapshots enter this layer. Mandatory upstream messages and
current input/tool exchanges are retained; private additions never enter state.
"""
from copy import deepcopy
import hashlib
import json
import threading
from .model_call_observation import ModelCallObservation, prepare_openai_responses_invoke

from langchain_core.messages import SystemMessage, convert_to_messages
from langchain_core.runnables import Runnable
from langchain_core.runnables.config import ensure_config

NODES={
    'Market Analyst':'market_analyst','Sentiment Analyst':'social_analyst',
    'News Analyst':'news_analyst','Fundamentals Analyst':'fundamentals_analyst',
    'Bull Researcher':'bull_researcher','Bear Researcher':'bear_researcher',
    'Research Manager':'research_manager','Trader':'trader',
    'Aggressive Analyst':'risky_analyst','Conservative Analyst':'safe_analyst',
    'Neutral Analyst':'neutral_analyst','Portfolio Manager':'risk_manager'}
ROLES=tuple(NODES.values())

class TeamExecutionError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)

def fail(code):raise TeamExecutionError(code)

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def role_from_config(config):
    """Public LangGraph node/namespace metadata, never prompt-text guessing."""
    metadata=config.get('metadata',{})
    node=metadata.get('langgraph_node')
    if node in NODES:return NODES[node]
    # Analyst subgraphs use node='agent'/'wrap_up'. Their namespace retains
    # the exact containing analyst node. Ignore UUIDs and reject ambiguity.
    namespace=metadata.get('langgraph_checkpoint_ns',config.get('configurable',{}).get('checkpoint_ns',''))
    found={NODES[part.split(':',1)[0]] for part in namespace.split('|') if part.split(':',1)[0] in NODES}
    if node in {'agent','wrap_up'} and len(found)==1:return found.pop()
    fail('TEAM_ROLE_METADATA_REQUIRED')

def messages_for(input):
    if hasattr(input,'to_messages'):return input.to_messages()
    if isinstance(input,str):return convert_to_messages([('human',input)])
    return convert_to_messages(input)

def canonical_messages(messages):
    # Includes tools, IDs and provider reasoning fields in budget accounting.
    return [message.model_dump(mode='json') for message in messages]

def history_groups(messages):
    """Keep an AI tool request together with all its tool responses."""
    groups=[]
    for item in messages:
        if item.type=='tool':
            if not groups or not getattr(groups[-1][0],'tool_calls',None):fail('TEAM_ORPHAN_TOOL_MESSAGE')
            if item.tool_call_id not in {c['id'] for c in groups[-1][0].tool_calls}:fail('TEAM_TOOL_PAIR_MISMATCH')
            groups[-1].append(item)
        else:groups.append([item])
    for group in groups:
        calls=getattr(group[0],'tool_calls',None)
        if calls and {c['id'] for c in calls}!={m.tool_call_id for m in group[1:]}:
            fail('TEAM_INCOMPLETE_TOOL_EXCHANGE')
    return groups

def fit_history(history, before, after, budgets, measure, *, min_groups=0):
    """Fit complete history groups between two sequences of mandatory messages."""
    groups = history_groups(history)
    dropped = 0
    while True:
        tail = [message for group in groups for message in group]
        selected = [*before, *tail, *after]
        chars, tokens = measure(selected)
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            fail('TEAM_TOKENIZER_INVALID')
        if (len(tail) <= budgets['history_messages'] and chars <= budgets['input_chars']
                and (tokens is None or tokens <= budgets['input_tokens'])):
            return selected, {'input_tokens': tokens, 'input_chars': chars, 'history_dropped': dropped}
        if budgets['context_policy'] != 'truncate_oldest' or len(groups) <= min_groups:
            fail('TEAM_INPUT_BUDGET_EXCEEDED')
        dropped += len(groups.pop(0))


def prepare_messages(messages,role_config,budgets,count_tokens,tools=None):
    """Count actual supplied tokenizer; drop only complete old history groups."""
    original=list(messages)
    if not original:fail('TEAM_CURRENT_INPUT_REQUIRED')
    prompt=role_config['prompt']
    additions='\n\n'.join(f'[{key}]\n{prompt[key]}' for key in ('system','task','output') if prompt[key])
    context='\n\n'.join(text for text in (role_config.get('shared_context',''),role_config.get('private_context','')) if text)
    if context:additions+='\n\n[Additional context; evidence remains bounded by run as-of]\n'+context
    extra=[SystemMessage(content=additions)] if additions.strip() else []
    # System/developer messages are mandatory. The first human message often
    # contains the entire upstream task/evidence prompt, so it too is pinned.
    first_human=next((i for i,m in enumerate(original) if m.type=='human'),None)
    pinned=[m for i,m in enumerate(original) if m.type in {'system','developer'} or i==first_human]
    history=[m for i,m in enumerate(original) if m.type not in {'system','developer'} and i!=first_human]
    def measure(selected):
        chars = sum(len(json.dumps(m, ensure_ascii=False, allow_nan=False)) for m in canonical_messages(selected))
        return chars, None if count_tokens is None else count_tokens(selected, tools)

    # Keep the latest exchange in the TradingAgents conversation.
    return fit_history(history, [*pinned, *extra], [], budgets, measure, min_groups=1)


class TeamExecution:
    """One run, immutable resolved config; clients/counters supplied by host."""
    def __init__(self,snapshot,owner_id,*,models,token_counters,active=lambda:None,observation_sink=None):
        if snapshot.get('owner_id')!=owner_id:fail('TEAM_SNAPSHOT_OWNER_MISMATCH')
        roles=snapshot.get('resolved_roles')
        if type(roles)is not dict or set(roles)!=set(ROLES):fail('TEAM_RESOLVED_ROLES_REQUIRED')
        if type(snapshot.get('sha256'))is not str or len(snapshot['sha256'])!=64:fail('TEAM_SNAPSHOT_HASH_REQUIRED')
        if digest({key:snapshot[key] for key in ('configuration','resolved_roles')})!=snapshot['sha256']:fail('TEAM_SNAPSHOT_HASH_MISMATCH')
        self.snapshot=deepcopy(snapshot);self.owner_id=owner_id
        self.models=dict(models);self.counters=dict(token_counters);self.active=active;self.observation_sink=observation_sink
        if set(models)!=set(ROLES) or set(token_counters)!=set(ROLES):fail('TEAM_EXACT_ROLE_CLIENTS_REQUIRED')
        self.rows=[];self.lock=threading.Lock();self.stopped=None
    def runnable(self):return RoleRunnable(self)
    def trace_primitive(self):
        with self.lock:return deepcopy(self.rows)
    def invoke(self,role,input,config,*,tools=None,schema=None,kwargs=None):
        self.active()
        if self.stopped:fail(self.stopped)
        resolved=self.snapshot['resolved_roles'][role]
        budgets=resolved.get('budgets') or self.snapshot['configuration']['budgets']
        messages,counts=prepare_messages(messages_for(input),resolved,budgets,self.counters[role],tools or schema)
        model=self.models[role]
        if tools is not None:model=model.bind_tools(tools,**(kwargs or {}))
        if schema is not None:model=model.with_structured_output(schema,include_raw=True,**(kwargs or {}))
        with self.lock:
            if len(self.rows)>=4096:fail('TEAM_TRACE_LIMIT')
            self.rows.append({'role':role,'snapshot_sha256':self.snapshot['sha256'],
                'model':deepcopy(resolved['model']),'prompt_sha256':digest(canonical_messages(messages)),
                'schema_sha256':digest(str(schema)) if schema is not None else None,**counts,'status':'STARTED'})
            index=len(self.rows)-1
        invoke_options,provenance=prepare_openai_responses_invoke(model)
        observation=ModelCallObservation(resolved['model'],budgets,role=role)
        try:
            result=observation.invoke(lambda:model.invoke(messages,config=config,**invoke_options),provenance=provenance)
            self.active()
            # Structured output has no transport usage envelope; serialize it
            # and count with the same actual tokenizer, including tool args.
            raw=result.get('raw') if schema is not None and type(result)is dict else result
            if hasattr(raw,'type'):output=[raw]
            else:output=convert_to_messages([('ai',json.dumps(result.model_dump(mode='json') if hasattr(result,'model_dump') else result,ensure_ascii=False,allow_nan=False))])
            usage=getattr(raw,'usage_metadata',None) or {}
            counter=self.counters[role]
            if counter is None:
                # The admitted parent counts the serialized provider payload.
                if any(type(usage.get(key))is not int or usage[key]<0 for key in ('input_tokens','output_tokens')):
                    fail('TEAM_TRANSPORT_USAGE_REQUIRED')
                counts['input_tokens']=usage['input_tokens']
                spent=usage['output_tokens']
            else:
                spent=counter(output,None)
                if type(spent)is not int or spent<0:fail('TEAM_TOKENIZER_INVALID')
            if usage.get('input_tokens',0)>budgets['input_tokens']:fail('TEAM_TRANSPORT_INPUT_BUDGET_EXCEEDED')
            spent=max(spent,usage.get('output_tokens',0))
            if spent>budgets['output_tokens']:fail('TEAM_OUTPUT_BUDGET_EXCEEDED')
            with self.lock:self.rows[index].update(status='SUCCEEDED',input_tokens=counts['input_tokens'],output_tokens=spent)
            if schema is not None and type(result)is dict:
                if result.get('parsing_error')is not None:raise result['parsing_error']
                return result.get('parsed')
            return result
        except BaseException as error:
            if isinstance(error,TeamExecutionError):self.stopped=error.code
            with self.lock:self.rows[index].update(status='FAILED',reason_code=error.code if isinstance(error,TeamExecutionError) else 'TEAM_MODEL_FAILED')
            raise
        finally:
            observed=observation.finish(self.rows[index]['status'],sink=self.observation_sink or (lambda row:None))
            with self.lock:self.rows[index]['model_observation']=observed

class RoleRunnable(Runnable):
    def __init__(self,execution,*,tools=None,schema=None,kwargs=None):
        self.execution=execution;self.tools=tools;self.schema=schema;self.kwargs=kwargs or {}
    def bind_tools(self,tools,**kwargs):return RoleRunnable(self.execution,tools=tools,kwargs=kwargs)
    def with_structured_output(self,schema,**kwargs):return RoleRunnable(self.execution,schema=schema,kwargs=kwargs)
    def invoke(self,input,config=None,**kwargs):
        if kwargs:fail('TEAM_UNBOUND_INVOKE_ARGUMENTS')
        config=ensure_config(config);role=role_from_config(config)
        return self.execution.invoke(role,input,config,tools=self.tools,schema=self.schema,kwargs=self.kwargs)

def official_workflow(execution,*,rounds):
    """Public setup/model injection; zero graph topology or agent copies."""
    from tradingagents.graph.setup import GraphSetup
    from tradingagents.graph.conditional_logic import ConditionalLogic
    router=execution.runnable()
    return GraphSetup(router,router,ConditionalLogic(max_debate_rounds=rounds['debate'],max_risk_discuss_rounds=rounds['risk']),rounds['tool']).setup_graph()
