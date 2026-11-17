"""Concrete Windows library/model adapters. No generated-code execution."""
from contextlib import closing
import hashlib
import json
import math
import time
import uuid
from typing import Annotated
from pydantic import Field, model_validator
from .auth import Principal
from .contracts import ContractError, identifier
from .autonomous_models import Strict, PrepareSession, SessionSpec, Reservation
from .autonomous_sessions import digest
from .errors import PlatformError
from .security import safe_json, now_text


def factor_diagnostics(capabilities):
    values=capabilities.get('development_diagnostics',{})
    if not isinstance(values,dict):
        values={}
    quantiles='GROSS_FORWARD_LABEL_MIN5_MEMBERS_MIN20_DATES'
    redundancy='EXACT_TRAIN_OUTPUT_HASH_INFORMATION_ONLY'
    return {'quantile_returns':quantiles if values.get('quantile_returns')==quantiles else 'UNSUPPORTED',
            'redundancy_analysis':redundancy if values.get('redundancy')==redundancy else 'UNSUPPORTED',
            'correlation_redundancy':'UNSUPPORTED'}


class BrokerMessage(Strict):
    role: str = Field(pattern=r'^(system|user|assistant)$')
    content: str = Field(max_length=65536)
    # Native continuation is retained in the journal. Only messages returned by
    # this broker may be replayed; no arbitrary provider option dictionaries.
    previous_call_id: str | None = Field(default=None, max_length=128)


class BrokerRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    attempt_id: str = Field(min_length=1, max_length=128)
    role: str = Field(pattern=r'^(hypothesis|coder|critic|reporter)$')
    messages: list[BrokerMessage] = Field(min_length=1, max_length=100)
    json_mode: bool = False
    history_groups: list[Annotated[str, Field(min_length=1, max_length=4096)]] | None = Field(
        default=None, max_length=20, exclude_if=lambda groups: groups is None)

    @model_validator(mode='after')
    def validate_history(self):
        if 'history_groups' in self.model_fields_set:
            if (self.history_groups is None or self.role != 'hypothesis'
                    or self.messages[-1].role != 'user'
                    or any(message.role not in {'system', 'user'} or message.previous_call_id is not None
                           for message in self.messages)):
                raise ValueError('invalid hypothesis history groups')
        return self


class WindowsInputs:
    def __init__(self, runtime, evaluator):
        self.runtime, self.evaluator = runtime, evaluator

    def __call__(self, principal, spec, *, goal_round=None):
        # Final data requires the coherent sealed evaluator protocol AND exact
        # installed readiness. Old/stale runtime cannot resolve, reserve or
        # expose a new final window merely because new source files exist.
        if spec.final_test is not None and not self.final_supported():raise PlatformError('AUTONOMOUS_FINAL_UNAVAILABLE',422)
        if spec.cost_policy!='COUNTERS_ONLY':raise PlatformError('AUTONOMOUS_PRICE_REQUIRED',422)
        # Evaluator is a trusted installed implementation, never API callables.
        capabilities = self.evaluator.capabilities()
        metrics = capabilities['metrics']
        if any(m.name not in metrics for m in spec.scoring):raise PlatformError('UNSUPPORTED_CAPABILITY',422)
        profiles = {}
        for role, ref in spec.models.items():
            value = self.runtime.model_profiles.prepare_execution_profile(principal, ref.resource_id, int(ref.version))
            if value['sha256'] != ref.content_hash:
                raise PlatformError('MODEL_PROFILE_HASH_MISMATCH', 409)
            if value['configuration']['model']['provider'] not in self.supported_providers():
                raise PlatformError('UNSUPPORTED_CAPABILITY', 422)
            profiles[role] = value
        if goal_round and (spec.reuse_policy!='context' or spec.final_test is not None or len(spec.development)!=1):
            raise PlatformError('GOAL_ROUND_INPUT_UNSUPPORTED',422)
        minimum=self.minimum(capabilities,spec.target,profiles,reuse_policy=spec.reuse_policy,goal_round=goal_round)
        if any(v<getattr(minimum,k) for k,v in spec.experiment.model_dump().items()):raise PlatformError('CONFLICT',409)
        if goal_round:
            round_limits=self.goal_round_limits(capabilities,goal_round)
            if (spec.experiment.compute_seconds != minimum.compute_seconds or
                    spec.experiment.prefixes != minimum.prefixes):
                raise PlatformError('GOAL_ROUND_RESERVATION_MISMATCH',422)
        elif 'goal_round_limits' in capabilities and spec.experiment.compute_seconds > minimum.compute_seconds:
            raise PlatformError('GOAL_ROUND_RESERVATION_MISMATCH',422)
        if spec.team_snapshot:
            team = self.runtime.agent_teams.get_snapshot(principal, spec.team_snapshot.resource_id)
            if spec.team_snapshot.version != '1' or team['sha256'] != spec.team_snapshot.content_hash:
                raise PlatformError('CONFLICT', 409)
            mapped=self.team_member_profiles(principal,team,spec.team_role_map)
            for role,profile in mapped.items():
                ref=spec.models[role]
                if (ref.resource_id!=profile['profile_id'] or ref.version!=str(profile['revision'])
                        or ref.content_hash!=profile['sha256']):
                    raise PlatformError('AUTONOMOUS_TEAM_MEMBER_PROFILE_CHANGED',409)
            # The explicit research roles remain authoritative; a team is a
            # frozen provenance selection, never a mutable global model.
        selected=[]
        cn_development = []
        def windows(refs,*,saved=False,development=False):
            from finance_platform.data.research_inputs import cn_research_market
            result=[]
            for ref in refs:
                data = self.runtime.dataset_library.get(principal.user_id, ref.resource_id, selectable=True)
                if str(data['version']) != ref.version or data['content_hash'] != ref.content_hash:
                    raise PlatformError('CONFLICT', 409)
                if goal_round:
                    self._check_goal_round(spec.budget.model_dump(),spec.experiment,spec.deadline_seconds,
                                           data['quality']['rows'],round_limits['max_prefix_workload'])
                if data.get("scope") == "licensed_local":
                    raise PlatformError("WRDS_NATIVE_INPUT_UNAVAILABLE", 422)
                market = cn_research_market(data)
                if market is not None:
                    from finance_platform.data.autonomous_receipts import CN_MARKET
                    if not development or spec.final_test or saved:
                        raise PlatformError('CN_RESEARCH_DEVELOPMENT_ONLY', 422)
                    if len(refs) != 1:
                        raise PlatformError('CN_RESEARCH_SINGLE_DATASET_REQUIRED', 422)
                    if CN_MARKET not in capabilities.get('market_semantics', []):
                        raise PlatformError('CN_RESEARCH_EXECUTOR_UNAVAILABLE', 503)
                    cn_development.append(market)
                # Existing public snapshots do not establish stable identity
                # across ticker/provider aliases. Coalesce all instruments
                # conservatively; do not claim ticker text is canonical ID.
                start=(spec.start or data['quality']['first']) if saved else data['quality']['first']
                end=(spec.end or data['quality']['last']) if saved else data['quality']['last']
                if not data['start']<=start<=end<=data['end']:
                    raise PlatformError('BACKTEST_WINDOW_OUTSIDE_DATASET',422)
                if saved:selected.append({'reference':ref.model_dump(),'start':start,'end':end})
                result.append({'source_id':'daily-price-observations-v1', 'security_ids':['*'],
                               'start':start,'end':end})
            return result
        seeds=self.runtime.research_candidates.context(principal,spec.saved_candidate_ids) if spec.saved_candidate_ids else []
        if spec.reuse_policy=='evaluate_saved' and (len(seeds)!=1 or spec.target!=seeds[0]['reusable_candidate']['kind']):
            raise PlatformError('REUSABLE_CANDIDATE_KIND_MISMATCH',422)
        development_windows=windows(spec.development,saved=spec.reuse_policy=='evaluate_saved',development=True)
        final_windows=windows([spec.final_test]) if spec.final_test else []
        limits={**factor_diagnostics(capabilities),
                'final_test':'SEALED_NONADAPTIVE' if self.final_supported() else 'UNSUPPORTED'}
        if cn_development:
            from finance_platform.data.autonomous_receipts import CN_MARKET
            limits.update(market_semantics=CN_MARKET, currency='CNY', final_test='UNSUPPORTED')
        bindings={'profiles':profiles,'evaluator':capabilities,'spec':spec.model_dump(),
                  'development_selection':selected,'development_windows':development_windows,'capability_limits':limits}
        return {'development_windows':development_windows,'development_selection':selected,'final_windows':final_windows,
                'scoring_metrics':metrics,'evaluator_version':capabilities['version'],'binding_hash':digest(bindings), 'model_profiles':profiles,'candidate_context':seeds,
                'capability_limits':limits}

    def minimum(self,capabilities,target,profiles,*,reuse_policy='context',goal_round=None):
        if reuse_policy=='evaluate_saved':
            value=capabilities.get('saved_backtest_reservation')
            if capabilities.get('saved_backtest_requires_model')is not False or type(value)is not dict:
                raise PlatformError('SAVED_BACKTEST_UNAVAILABLE',422)
            reservation=Reservation.model_validate(value)
            if any(getattr(reservation,key)!=0 for key in ('model_requests','tokens','cost_microunits')):
                raise PlatformError('SAVED_BACKTEST_UNAVAILABLE',422)
            return reservation
        value=Reservation.model_validate(capabilities['reservations'][target]).model_dump()
        if goal_round:
            limits=self.goal_round_limits(capabilities,goal_round)
            value['compute_seconds']=limits['compute_seconds']
        value['model_requests']=max(4,value['model_requests'])
        value['tokens']=max(value['tokens'],value['model_requests']*max(p['configuration']['budgets']['input_tokens']+p['configuration']['budgets']['output_tokens'] for p in profiles.values()))
        return Reservation.model_validate(value)

    @staticmethod
    def goal_round_limits(capabilities,goal_round):
        available=capabilities.get('goal_round_limits')
        limits=available.get(goal_round) if type(available) is dict and type(goal_round) is str else None
        if (type(limits) is not dict or set(limits)!={'compute_seconds','max_prefix_workload'}
                or type(limits['compute_seconds']) is not int or not 600 <= limits['compute_seconds'] <= 3600
                or type(limits['max_prefix_workload']) is not int or not 1 <= limits['max_prefix_workload'] <= 2000):
            raise PlatformError('GOAL_ROUND_CAPABILITY_UNAVAILABLE',503)
        return limits

    @staticmethod
    def _check_goal_round(budget,experiment,deadline_seconds,rows,max_workload):
        if type(rows) is not int or not 1 <= rows <= min(experiment.prefixes,max_workload):
            raise PlatformError('GOAL_ROUND_PREFIX_LIMIT_EXCEEDED',422)
        attempts=budget['attempts']
        if (deadline_seconds <= attempts*experiment.compute_seconds
                or any(budget[key] < attempts*value for key,value in experiment.model_dump().items()
                       if key in budget)):
            raise PlatformError('GOAL_ROUND_BUDGET_INSUFFICIENT',422)

    def team_member_profiles(self,principal,snapshot,mapping):
        # Never guess equivalence between trading-team and research roles.
        # Existing immutable saved-profile refs are the execution authority.
        refs=snapshot['configuration'].get('profile_refs',{})
        result={}
        for research_role,member in mapping.items():
            resolved=snapshot['resolved_roles'].get(member)
            ref=refs.get(member) or refs.get('default')
            if resolved is None or ref is None:
                raise PlatformError('AUTONOMOUS_TEAM_MEMBER_PROFILE_REQUIRED',422)
            profile=self.runtime.model_profiles.prepare_execution_profile(principal,ref['profile_id'],ref['revision'])
            if (profile['sha256']!=ref['sha256']
                    or profile['configuration']['model']!=resolved['model']):
                raise PlatformError('AUTONOMOUS_TEAM_MEMBER_PROFILE_CHANGED',409)
            result[research_role]=profile
        return result

    def final_supported(self):
        from .autonomous_final import receipt_contract
        try:
            receipt_contract()
        except PlatformError:
            return False
        caps=self.evaluator.capabilities()
        return (self.evaluator.readiness().get('ready')is True
                and caps.get('sealed_final_protocol')=='fa-sealed-final-request-1'
                and caps.get('sealed_final_evaluator_source')is True)

    def supported_providers(self):
        # Same capability registry used by saved-profile preparation and the
        # existing official SDK adapter. Offline is not a broker SDK client.
        registry=getattr(self.runtime,'model_capabilities',None)
        if registry is None:raise PlatformError('MODEL_TRUSTED_TARGET_REGISTRY_REQUIRED',409)
        return [p['id'] for p in registry.describe()['providers'] if p['id']!='offline'
                and not str(p.get('adapter_status','')).startswith('UNAVAILABLE')]

    def capabilities(self,principal):
        self.runtime.require_owner(principal,principal.user_id)
        evaluation=self.evaluator.capabilities()
        diagnostics=factor_diagnostics(evaluation)
        evaluation={**evaluation,**diagnostics}
        return {'schema':'autonomous-capabilities-1','evaluation':evaluation,
                'metrics_by_target':{target:[m for m in evaluation['metrics'] if target=='factor' or m not in {'cs_ic','ts_ic'}] for target in ('factor','strategy','joint')},
                'executor':self.evaluator.readiness(),'providers':self.supported_providers(),
                'model_selection':{'default_model':True,'role_overrides':True,'team_saved_default':True,'team_inline_model_only':False,'team_role_prompts_applied':False,'explicit_team_role_map':True},
                'saved_backtest':{'requires_model':False,'maximum_attempts':1,'model_requests':0,
                    'source_protocol_available':evaluation.get('saved_backtest_requires_model')is False,
                    'executor_ready':self.evaluator.readiness().get('ready')is True},
                'session_loop':'DURABLE_SCHEDULER_UNTIL_BUDGET_DEADLINE_OR_CONTROL',
                'roles':['hypothesis','coder','critic','reporter'],'weight_scale':'relative_positive_integer_1_to_10000',
                'currency':None,'price_basis':'UNKNOWN','supports_development_only':True,
                'supports_final_test':self.final_supported(),'final_test':'SEALED_NONADAPTIVE' if self.final_supported() else 'UNAVAILABLE',
                'final_test_reason':None if self.final_supported() else 'AUTONOMOUS_FINAL_UNAVAILABLE',
                'factor_diagnostics':diagnostics,
                'model_budget_modes':['COUNTERS_ONLY'],'cost_limit_guaranteed':False,
                'credential_status':'CHECKED_ONLY_AT_EXPLICIT_RUN','missing_credential_code':'MODEL_SECRET_NOT_CONFIGURED',
                'model_pricing_status':'NOT_REQUIRED_FOR_COUNTERS_ONLY',
                'cost_help':'仅限制请求次数、token和执行时间；金额未知，不保证费用上限。'}

    @staticmethod
    def engine_payload(begun):
        from .autonomous_models import EngineOptions
        options=EngineOptions.model_validate(begun['spec']['engine_options'])
        # User-facing rates remain exact decimal strings in the frozen spec;
        # Backtesting.py's numeric boundary explicitly consumes these values.
        return {'cash':options.cash,'fee_rate':float(options.fee_rate),'spread_rate':float(options.spread_rate),'seed':options.seed}

    @staticmethod
    def _check_saved_backtest(value, experiment):
        resources = {
            'prefixes': ('逐步评估次数', '次'),
            'compute_seconds': ('运行时间', '秒'),
            'artifact_bytes': ('结果保存空间', '字节'),
        }
        missing = []
        for field, (label, unit) in resources.items():
            minimum = getattr(experiment, field)
            if getattr(value.budget, field) < minimum:
                missing.append(f'{label}至少需要 {minimum} {unit}')
        if missing:
            error = PlatformError('SAVED_BACKTEST_BUDGET_TOO_LOW', 422)
            error.message = '回测预算不足。' + '，'.join(missing) + '。请提高对应额度后重试。'
            raise error

        names = [metric.name for metric in value.scoring]
        if len(set(names)) != len(names) or value.target != 'factor' and {'cs_ic', 'ts_ic'}.intersection(names):
            raise PlatformError('SAVED_BACKTEST_SCORING_INVALID', 422)

        try:
            for dataset_id in value.development_ids:
                identifier(dataset_id, 'dataset identifier')
        except ContractError:
            raise PlatformError('SAVED_BACKTEST_DATASET_ID_INVALID', 422) from None
        if len(set(value.development_ids)) != len(value.development_ids):
            raise PlatformError('SAVED_BACKTEST_DATASET_ID_INVALID', 422)

    def prepare(self,principal,request,*,goal_round=None):
        value=PrepareSession.model_validate(request)
        if goal_round and (value.reuse_policy!='context' or value.final_test_id is not None or len(value.development_ids)!=1):
            raise PlatformError('GOAL_ROUND_INPUT_UNSUPPORTED',422)
        if value.final_test_id is not None and not self.final_supported():raise PlatformError('AUTONOMOUS_FINAL_UNAVAILABLE',422)
        def data_ref(dataset_id):
            data=self.runtime.dataset_library.get(principal.user_id,dataset_id,selectable=True)
            return {'resource_id':data['dataset_id'],'version':str(data['version']),'content_hash':data['content_hash']}
        models={};profiles={}
        selections=dict(value.models)
        default=value.default_model
        team=None;team_default=None;member_profiles={}
        if value.team_role_map and not value.team_snapshot_id:
            raise PlatformError('AUTONOMOUS_TEAM_SNAPSHOT_REQUIRED',422)
        if set(value.models)&set(value.team_role_map):
            raise PlatformError('AUTONOMOUS_TEAM_MAPPING_AMBIGUOUS',422)
        if value.team_snapshot_id:
            snapshot=self.runtime.agent_teams.get_snapshot(principal,value.team_snapshot_id)
            team={'resource_id':snapshot['snapshot_id'],'version':'1','content_hash':snapshot['sha256']}
            team_default=snapshot['configuration'].get('profile_refs',{}).get('default')
            member_profiles=self.team_member_profiles(principal,snapshot,value.team_role_map)
            from .autonomous_models import ProfileSelection
            for role,profile in member_profiles.items():
                selections[role]=ProfileSelection(profile_id=profile['profile_id'],revision=profile['revision'])
            if default is None and team_default:
                from .autonomous_models import ProfileSelection
                default=ProfileSelection(profile_id=team_default['profile_id'],revision=team_default['revision'])
        for role in ('hypothesis','coder','critic','reporter'):
            if role not in selections and default is not None:selections[role]=default
        if value.reuse_policy!='evaluate_saved' and not {'hypothesis','coder','critic'}<=set(selections):
            raise PlatformError('AUTONOMOUS_RESEARCH_MODELS_REQUIRED',422)
        for role,selection in selections.items():
            revision=selection.revision or self.runtime.model_profiles.get(principal,selection.profile_id)['revision']
            profile=self.runtime.model_profiles.prepare_execution_profile(principal,selection.profile_id,revision)
            if (team_default and value.default_model is None and role not in value.models and role not in value.team_role_map
                    and profile['sha256']!=team_default['sha256']):
                raise PlatformError('MODEL_PROFILE_HASH_MISMATCH',409)
            profiles[role]=profile
            models[role]={'resource_id':selection.profile_id,'version':str(revision),'content_hash':profile['sha256']}
        capabilities=self.evaluator.capabilities()
        experiment=self.minimum(capabilities,value.target,profiles,reuse_policy=value.reuse_policy,goal_round=goal_round)
        if value.reuse_policy=='evaluate_saved':
            self._check_saved_backtest(value,experiment)
        body={k:v for k,v in value.model_dump().items() if k not in {'development_ids','final_test_id','models','default_model','team_snapshot_id'}}
        body.update(development=[data_ref(i) for i in value.development_ids],final_test=data_ref(value.final_test_id) if value.final_test_id else None,
                    models=models,team_snapshot=team,experiment=experiment.model_dump())
        spec=SessionSpec.model_validate(body)
        resolved=self(principal,spec,goal_round=goal_round)
        return {'development_selection':resolved['development_selection'],'schema':'autonomous-prepared-1','spec':spec.model_dump(),'spec_hash':digest(spec.model_dump()),'evaluation_scope':'FINAL_SEPARATE' if spec.final_test else 'DEVELOPMENT_ONLY',
                'model_selection':{'model_required':value.reuse_policy!='evaluate_saved','model_calls':0 if value.reuse_policy=='evaluate_saved' else None,
                                   'default_model_used':default is not None,'role_overrides':sorted(value.models),
                                   'team_snapshot_usage':('EXPLICIT_MEMBER_PROFILE_MAPPING' if value.team_role_map else 'PROVENANCE_AND_SAVED_DEFAULT_PROFILE_ONLY') if team else None,
                                   'role_preview':[{'role':role,'team_member':value.team_role_map.get(role),
                                        'profile_id':profile['profile_id'],'revision':profile['revision'],
                                        'sha256':profile['sha256'],'provider':profile['configuration']['model']['provider'],
                                        'model':profile['configuration']['model']['model']}for role,profile in profiles.items()],
                                   'prompt_policy':'DETERMINISTIC_SAVED_SOURCE_NO_MODEL_PROMPTS' if value.reuse_policy=='evaluate_saved' else 'RESEARCH_RUNNER_ROLE_PROMPTS; TEAM_TWELVE_ROLE_PROMPTS_NOT_APPLIED'}}

    def export(self, context, begun):
        from finance_platform.data.research_inputs import export_research_input
        if begun['phase']=='final_test':
            if not self.final_supported():raise PlatformError('AUTONOMOUS_FINAL_UNAVAILABLE',503)
            from .autonomous_final import final_scope
            final_scope(self.runtime.autonomous_research,context,begun)
        elif begun['phase']!='development':raise PlatformError('INVALID_INPUT',422)
        refs = begun['spec']['development'] if begun['phase']=='development' else [begun['spec']['final_test']]
        saved=begun['phase']=='development' and begun['spec'].get('reuse_policy')=='evaluate_saved'
        selection=begun['resolved'].get('development_selection',[]) if saved else []
        windows=begun['resolved']['development_windows'] if saved else []
        if saved and (len(selection)!=len(refs) or len(windows)!=len(refs)):
            raise PlatformError('SAVED_BACKTEST_WINDOW_BINDING_MISMATCH',409)
        cache = getattr(self.runtime, 'computation_cache', None)
        parse_options = {}
        if begun['phase'] == 'development' and cache is not None:
            def parse_cache(*, identity, compute, validate):
                return cache.run_research(context, self.runtime.autonomous_research, kind='dataset_parse',
                                          identity=identity, compute=compute, validate=validate)
            parse_options['parse_cache'] = parse_cache
        packages=[]
        for index,ref in enumerate(refs):
            reference={'dataset_id':ref['resource_id'],'version':int(ref['version']),'content_hash':ref['content_hash']}
            library=self.runtime.dataset_library
            options={}
            if saved:
                frozen=selection[index];window=windows[index]
                if (frozen['reference']!=ref or frozen['start']!=window['start'] or frozen['end']!=window['end']
                        or begun['spec'].get('start')is not None and frozen['start']!=begun['spec']['start']
                        or begun['spec'].get('end')is not None and frozen['end']!=begun['spec']['end']):
                    raise PlatformError('SAVED_BACKTEST_WINDOW_BINDING_MISMATCH',409)
                options={'start':frozen['start'],'end':frozen['end']}
            library.accept_for_run(context.run.owner_id,reference,context.run.run_id,context=context)
            package=export_research_input(library,context.run.owner_id,reference,context.run.run_id,
                                          context=context,**options,**parse_options)
            if package['payload'].get('research_market') is not None:
                from finance_platform.data.autonomous_receipts import CN_MARKET
                if (begun['phase'] != 'development' or len(refs) != 1 or saved
                        or begun['resolved'].get('capability_limits', {}).get('market_semantics') != CN_MARKET):
                    raise PlatformError('CN_RESEARCH_DEVELOPMENT_ONLY', 422)
            if saved and (package['payload']['window']!=options or package['manifest']['dataset_reference']!=reference):
                raise PlatformError('SAVED_BACKTEST_WINDOW_BINDING_MISMATCH',409)
            packages.append(package)
        return packages


class WindowsModelBroker:
    """Durable pre-call journal + existing official SDK/credential-target guards.

    Context is supplied only by the trusted host bridge for this leased attempt.
    It is never reconstructed from Linux-provided owner, paths or secret fields.
    A repeated REQUESTED/UNKNOWN call refuses IO. Reservations never refunded.
    """
    def __init__(self, runtime, sessions, *, transports=None, pricing=None):
        self.runtime,self.sessions,self.transports=runtime,sessions,transports or {}
        # Trusted quote keyed by immutable profile SHA: integer microunits per
        # token, conservatively rounded UP. COUNTERS_ONLY does not promise an
        # amount limit; PRICED_CAP refuses missing quotes before real IO.
        self.pricing=pricing or {}

    def _development_attempt(self, con, context, attempt_id):
        attempt, session = self.sessions._attempt(con, context)
        if attempt['phase'] != 'development':
            raise PlatformError('FINAL_ADAPTIVE_PORT_FORBIDDEN', 409)
        if json.loads(session['spec_json']).get('reuse_policy') == 'evaluate_saved':
            raise PlatformError('SAVED_BACKTEST_MODEL_FORBIDDEN', 409)
        if attempt['attempt_id'] != attempt_id or attempt['status'] != 'IN_FLIGHT':
            raise PlatformError('CONFLICT', 409)
        if session['status'] != 'RUNNING' or self.sessions.clock() >= session['deadline']:
            raise PlatformError('LEASE_LOST', 409)
        return attempt, session

    @staticmethod
    def _history_groups(begun, attempt, session):
        """Match Native's projection of the same frozen, Core-verified outcomes."""
        if (begun is None or begun['attempt_id'] != attempt['attempt_id']
                or begun['session_id'] != session['session_id'] or begun['phase'] != attempt['phase']
                or begun['spec_hash'] != session['spec_hash']):
            raise PlatformError('CONFLICT', 409)
        refs = begun['checkpoint_refs']
        latest = refs[0]['attempt_id'] if refs else None
        metric_names = ('return_fraction', 'max_drawdown_fraction', 'cs_ic', 'ts_ic', 'turnover')
        groups = []
        for entry in begun['development_history']:
            if 'outcome' not in entry or entry['attempt_id'] == latest:
                continue
            outcome = entry['outcome']
            available = outcome.get('metrics') or {}
            metrics = {name: available[name] for name in metric_names
                       if type(available.get(name)) in (int, float) and math.isfinite(available[name])}
            record = {'attempt_id': entry['attempt_id'], 'status': outcome['status'], 'metrics': metrics}
            if outcome['status'] != 'SUCCEEDED' and outcome.get('error_code'):
                record['error_code'] = outcome['error_code']
            groups.append(safe_json(record))
        return groups

    def call(self, context, request, *, begun=None):
        import httpx
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
        from finance_platform.research.provider_runtime import FencedTransport, create_client
        from finance_platform.research.search_agent import invoke_bounded
        from langsmith import tracing_context
        value=BrokerRequest.model_validate(request);request=value.model_dump()
        request_hash = digest(request)
        db=self.sessions.database
        with db.transaction() as con:
            attempt, session = self._development_attempt(con, context, value.attempt_id)
            prior=con.execute('SELECT * FROM autonomous_model_calls WHERE call_id=?',(value.call_id,)).fetchone()
            if prior:
                if prior['attempt_id']!=value.attempt_id or prior['request_hash']!=request_hash:
                    raise PlatformError('CONFLICT',409)
                if prior['status']!='DONE':raise PlatformError('CONFLICT',409)
                return json.loads(prior['response_json'])
            selected=json.loads(session['resolved_json'])['model_profiles'][value.role]
            config=selected['configuration'];bounds=config['budgets'];choice=config['model']
            messages=[]
            for msg in value.messages:
                if msg.previous_call_id:
                    old = con.execute("""
                        SELECT response_json,request_json FROM autonomous_model_calls
                        WHERE call_id=? AND attempt_id=? AND status='DONE'
                    """, (msg.previous_call_id, value.attempt_id)).fetchone()
                    if not old or msg.role!='assistant' or json.loads(old['request_json'])['role']!=value.role:
                        raise PlatformError('CONFLICT',409)
                    messages.append(AIMessage.model_validate(json.loads(old['response_json'])['native_message']))
                else:
                    messages.append({'system':SystemMessage,'user':HumanMessage,'assistant':AIMessage}[msg.role](content=msg.content))
            prefix='\n'.join([*config['prompt'].values(),config.get('context','')])
            messages.insert(0,SystemMessage(content=prefix))
            def measure(selected_messages):
                text = safe_json([message.model_dump(mode='json') for message in selected_messages])
                return len(text), len(text.encode()) + 1024

            history_dropped = 0
            if value.history_groups is None:
                chars, input_tokens = measure(messages)
                if chars > bounds['input_chars'] or input_tokens > bounds['input_tokens']:
                    raise PlatformError('INPUT_BUDGET_EXCEEDED', 422)
            else:
                from finance_platform.research.team_execution import TeamExecutionError, fit_history

                verified_history = self._history_groups(begun, attempt, session)
                if verified_history != value.history_groups:
                    raise PlatformError('CONFLICT', 409)
                history = [HumanMessage(content='[Prior development outcome]\n' + group)
                           for group in verified_history]
                try:
                    messages, counts = fit_history(history, messages[:-1], messages[-1:], bounds, measure)
                except TeamExecutionError as error:
                    if error.code != 'TEAM_INPUT_BUDGET_EXCEEDED':
                        raise
                    raise PlatformError('INPUT_BUDGET_EXCEEDED', 422) from None
                input_tokens = counts['input_tokens']
                history_dropped = counts['history_dropped']
            tokens = input_tokens + bounds['output_tokens']
            reservation=json.loads(attempt['reservation_json'])
            quote=self.pricing.get(selected['sha256'])
            policy=json.loads(session['spec_json'])['cost_policy']
            if quote is None and policy=='PRICED_CAP':raise PlatformError('AUTONOMOUS_PRICE_REQUIRED',422)
            quote=quote or {'input':0,'output':0}
            if set(quote)!={'input','output'} or any(type(v)is not int or v<0 for v in quote.values()):raise PlatformError('INVALID_INPUT',422)
            cost=input_tokens*quote['input']+bounds['output_tokens']*quote['output']
            used=con.execute('SELECT COUNT(*),COALESCE(SUM(tokens_reserved),0),COALESCE(SUM(cost_reserved),0) FROM autonomous_model_calls WHERE attempt_id=?',(value.attempt_id,)).fetchone()
            if used[0]+1>reservation['model_requests'] or used[1]+tokens>reservation['tokens'] or used[2]+cost>reservation['cost_microunits']:
                raise PlatformError('CONFLICT',409)
            # No prices are guessed. Pre-reserved whole experiment cost remains
            # consumed; a single request may use all remaining reservation.
            stamp=now_text()
            con.execute('INSERT INTO autonomous_model_calls VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (value.call_id,value.attempt_id,request_hash,'REQUESTED',tokens,cost,safe_json(request),None,stamp,stamp))
            if history_dropped:
                self.sessions._event(con, session['session_id'], 'MODEL_HISTORY_TRUNCATED',
                                     {'attempt_id': value.attempt_id, 'history_dropped': history_dropped})
        started=time.monotonic()
        timeout=min(120,reservation['compute_seconds'],max(0,int(session['deadline']-self.sessions.clock())))

        def active():
            self.sessions.require_effect_active(context, value.attempt_id, call_id=value.call_id)
            if time.monotonic() - started >= timeout:
                raise PlatformError('CONFLICT',409)

        owned=None
        guarded=None
        observation=None
        observation_status='UNKNOWN'
        invoke_options={}
        usage_provenance=None
        try:
            service=self.runtime.research_task_service
            principal=service.principal(context.run.owner_id)
            active()
            target=service._target(principal,choice,expected_target=selected['execution_target'])
            active()
            secret=None if choice['provider']=='ollama' and choice.get('secret_ref')is None else service._secret(principal,choice,target=target)
            active()
            provider = choice['provider']
            guarded=FencedTransport(active, self.transports.get(provider))
            if provider not in {'gemini', 'ollama'}:
                owned=httpx.Client(transport=guarded,trust_env=False,follow_redirects=False)
            close_transport=owned.close if owned is not None else guarded.close
            client=create_client(choice,output_tokens=bounds['output_tokens'],timeout_seconds=max(1,timeout),retry=0,secret=secret,target=target,transport=owned if owned is not None else guarded)
            if value.json_mode and provider in {'openai','deepseek','qwen','kimi','glm','openai_compatible'}:
                client=client.bind(response_format={'type':'json_object'})
            try:
                from finance_platform.research.model_call_observation import ModelCallObservation
                observation=ModelCallObservation(choice,bounds,role=value.role,selected=selected)
                observation.row['call_id']=value.call_id
            except Exception:
                observation=None
            try:
                from finance_platform.research.model_call_observation import prepare_openai_responses_invoke
                invoke_options,usage_provenance=prepare_openai_responses_invoke(client)
            except Exception:
                pass  # Missing optional proof retains raw-only/unknown metrics.
            def invoke():
                active()
                with tracing_context(enabled=False):
                    call=lambda:client.invoke(messages,**invoke_options)
                    return call() if observation is None else observation.invoke(call,provenance=usage_provenance)
            response=invoke_bounded(invoke,active,close=close_transport)
            usage=response.usage_metadata or {}
            if usage.get('input_tokens',0)>bounds['input_tokens'] or usage.get('output_tokens',0)>bounds['output_tokens']:
                raise PlatformError('CONFLICT',409)
            content=response.content
            if not isinstance(content,str):
                from langchain_core.messages import BaseMessage
                if not isinstance(response,BaseMessage) or getattr(response,'tool_calls',None):raise PlatformError('INVALID_INPUT',422)
                text=response.text
                content=str(text() if callable(text) else text)
                if not content:raise PlatformError('INVALID_INPUT',422)
            result={'content':content,'native_message':response.model_dump(mode='json'),'usage':response.usage_metadata or {}}
            encoded=safe_json(result)
            if not isinstance(content,str) or len(encoded.encode())>65536:raise PlatformError('INVALID_INPUT',422)
            with db.transaction() as con:
                self._development_attempt(con, context, value.attempt_id)
                if time.monotonic() - started >= timeout:
                    raise PlatformError('CONFLICT', 409)
                updated = con.execute("""
                    UPDATE autonomous_model_calls SET status='DONE',response_json=?,updated_at=?
                    WHERE call_id=? AND attempt_id=? AND request_hash=? AND status='REQUESTED'
                """, (encoded, now_text(), value.call_id, value.attempt_id, request_hash))
                if updated.rowcount != 1:
                    raise PlatformError('CONFLICT', 409)
            observation_status='DONE'
            return result
        except BaseException:
            if guarded is not None:
                guarded.close()
            observation_status='UNKNOWN' if guarded is not None and guarded.admitted else 'NOT_SENT'
            with db.transaction() as con:
                con.execute("""
                    UPDATE autonomous_model_calls SET status=?,updated_at=?
                    WHERE call_id=? AND attempt_id=? AND request_hash=? AND status='REQUESTED'
                """, (observation_status, now_text(), value.call_id, value.attempt_id, request_hash))
            raise
        finally:
            try:
                if observation is not None and observation.row.get('started_at') is not None:
                    from .model_usage import retain_observation
                    observation.finish(observation_status,sink=lambda row:retain_observation(context,row))
            except Exception:
                pass
            if owned is not None:
                owned.close()
            elif guarded is not None:
                guarded.close()
