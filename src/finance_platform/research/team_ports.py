"""Existing native model-port protocol over public Runnable role injection."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from .team_execution import TeamExecution,fail,digest
from .team_models import clients_for_snapshot

class TeamModelPorts:
    def __init__(self,profile):
        self.profile=deepcopy(profile);self.profile_sha256=profile['profile_sha256']
        self.snapshot=None;self.execution=None;self.bound=None;self.native_callbacks=[];self.observation_sink=None
        self.provider_approval=None;self.provider_exchange=None
    def configure_provider(self,approval,exchange=None):
        if self.provider_approval is not None or type(approval)is not dict or not callable(exchange):
            fail('TA_PROVIDER_PORT_BINDING_INVALID')
        approved={key:value for key,value in approval.items() if key!='execution_sha256'}
        if (digest(approved)!=approval.get('execution_sha256')
                or approval.get('approved_profile')!={key:self.profile[key] for key in ('profile_id','profile_sha256')}):
            fail('TA_PROVIDER_PORT_BINDING_INVALID')
        self.provider_approval=deepcopy(approval);self.provider_exchange=exchange
    def configure_team_snapshot(self,snapshot):
        if snapshot['sha256']!=self.profile_sha256:fail('TEAM_PROFILE_HASH_MISMATCH')
        if digest({k:snapshot[k] for k in ('configuration','resolved_roles')})!=snapshot['sha256']:fail('TEAM_SNAPSHOT_HASH_MISMATCH')
        if self.profile['profile_id']!='team-'+snapshot['snapshot_id']:fail('TEAM_SNAPSHOT_ID_MISMATCH')
        if self.snapshot is not None and self.snapshot!=snapshot:fail('TEAM_SNAPSHOT_CHANGED')
        self.snapshot=deepcopy(snapshot)
    def authorize_binding(self,value):
        if value.get('model_profile')!=self.profile or self.snapshot is None or value['owner_id']!=self.snapshot['owner_id']:fail('TEAM_PORT_BINDING_MISMATCH')
        if self.bound is not None and self.bound!=value:fail('TEAM_PORT_BINDING_CHANGED')
        rounds=self.snapshot['configuration']['rounds']
        expected={'debate_rounds':rounds['debate'],'risk_rounds':rounds['risk'],'tool_rounds':rounds['tool'],
            'wall_seconds':self.snapshot['configuration']['execution']['timeout_seconds']}
        if self.provider_approval is not None:
            expected={**expected,**self.provider_approval['options']}
        elif 'provider_input_tokens' in value['options']:
            fail('TA_PROVIDER_PORT_BINDING_INVALID')
        if any(value['options'].get(k)!=v for k,v in expected.items()):fail('TEAM_SNAPSHOT_OPTIONS_MISMATCH')
        self.bound=deepcopy(value)
    def trace_primitive(self):return [] if self.execution is None else self.execution.trace_primitive()
    @contextmanager
    def scope(self,native_module,config,value,budget):
        from unittest.mock import patch
        self.authorize_binding(value)
        if self.execution is not None:fail('TEAM_PORT_REENTRY_DENIED')
        models,counters=clients_for_snapshot(self.snapshot,as_of=value['as_of'],view_hash=value['selected_view_hash'],
            broker=self.provider_exchange,budget=budget)
        self.execution=TeamExecution(self.snapshot,value['owner_id'],models=models,token_counters=counters,active=budget.active,observation_sink=self.observation_sink)
        router=self.execution.runnable()
        # Snapshot hash enters upstream's settings/checkpoint signature.
        config.update(llm_provider='offline',quick_think_provider='offline',deep_think_provider='offline',
            quick_think_llm='deterministic-v1',deep_think_llm='deterministic-v1',team_snapshot_sha256=self.snapshot['sha256'])
        if self.provider_approval is not None:
            # No actual client is constructed by upstream. Settings records
            # the fixed graph adapter and the approved binding.
            config.update(llm_provider='openai',quick_think_provider='openai',deep_think_provider='openai',
                quick_think_llm='metered-team',deep_think_llm='metered-team',
                approved_execution_sha256=self.provider_approval['execution_sha256'])
        def factory(configuration,tier,**extra):
            if tier not in {'quick','deep'}:fail('TEAM_NATIVE_TIER_INVALID')
            return SimpleNamespace(get_llm=lambda:router)
        with patch.object(native_module,'create_tier_client',factory):yield

    def configure_observation_sink(self,sink):
        if not callable(sink) or self.execution is not None or self.observation_sink is not None:fail('TEAM_OBSERVATION_SINK_INVALID')
        self.observation_sink=sink

def create_server_model_ports(profile):
    if type(profile)is not dict or set(profile)!={'mode','profile_id','profile_sha256'} or profile['mode']!='SERVER_PROFILE' or not profile['profile_id'].startswith('team-'):
        fail('TEAM_EXACT_PROFILE_REQUIRED')
    return TeamModelPorts(profile)
