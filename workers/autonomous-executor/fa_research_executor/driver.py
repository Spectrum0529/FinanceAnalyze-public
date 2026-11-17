"""Concrete Linux experiment driver; model/data ports are Windows broker RPC.

The only generated-code invocation is controller.execute, once per causal prefix.
No direct upstream shell/Conda/workspace execution route exists in this driver.
"""
import ast
import hashlib
import json
import math
import time
from .protocol import require, digest, encoded, identifier, sha, MAX_CODE, OHLCV_BINDING, validate_ohlcv_binding
from .broker import BrokerBackend, role_scope
from .causal import collect
from .inputs import merge_packages
from .autonomous_receipts import CN_MARKET
from .evaluation import NORMALIZATION, REPLAY_ENGINE, COST_MODEL, evaluate, account_state, sleeve_options
from .artifacts import pack, single, chunks
from .diagnostics import training_signature, redundancy, quantile_returns

NATIVE_FEEDBACK_METRICS = ('1day.excess_return_with_cost.annualized_return',
                           '1day.excess_return_with_cost.max_drawdown', 'IC')


def validate_development_history(history):
    require(type(history) is list, 'OHLCV_BINDING_MISMATCH')
    for entry in history:
        require(type(entry) is dict, 'OHLCV_BINDING_MISMATCH')
        if set(entry) == {'reusable_candidate','source_sha256','meaning'}:
            # A saved source is re-evaluated under the current guest. It does
            # not import the previous attempt's thresholds or metrics.
            seed = entry['reusable_candidate']
            require(type(seed) is dict and seed.get('schema') == 'research-reusable-candidate-1'
                    and seed.get('scope') == 'DEVELOPMENT_ONLY', 'OHLCV_BINDING_MISMATCH')
            continue
        outcome = entry.get('outcome')
        if (set(entry) == {'attempt_id','outcome'} and type(outcome) is dict
                and outcome.get('status') in {'FAILED','CANCELLED'} and outcome.get('metrics') == {}):
            continue
        validate_ohlcv_binding(entry)


def _scalar_metrics(values, keys):
    if type(values) is not dict:
        return {}
    return {key: values[key] for key in keys if type(values.get(key)) in (int, float)
            and math.isfinite(values[key])}


def _bounded_text(value, limit):
    return value.encode()[:limit].decode('utf-8', errors='ignore')


def _feedback_excerpt(feedback):
    if type(feedback) is not dict:
        return '', False
    # The two critic paths produce either thin text or named native feedback.
    keys = ('text', 'observations', 'hypothesis_evaluation', 'new_hypothesis', 'reason')
    fragments = [feedback[key] for key in keys if type(feedback.get(key)) is str and feedback[key]]
    text = '\n'.join(fragments)
    excerpt = _bounded_text(text, 384)
    return excerpt, excerpt != text


def hypothesis_context(history):
    """Project verified development inputs into pinned evidence and old outcome groups."""
    latest = None
    outcomes = []
    saved = []
    for entry in history:
        require(entry.get('phase', 'development') == 'development', 'FINAL_ADAPTIVE_PORT_FORBIDDEN')
        if 'reusable_candidate' in entry:
            seed = entry['reusable_candidate']
            require(seed['scope']=='DEVELOPMENT_ONLY', 'FINAL_ADAPTIVE_PORT_FORBIDDEN')
            require(seed['kind'] in ('factor', 'strategy') and seed['source_sha256']==entry['source_sha256'],
                    'REUSABLE_CANDIDATE_HASH_MISMATCH')
            sha(seed['source_sha256'])
            saved.append({'kind':seed['kind'], 'source_sha256':seed['source_sha256'],
                          'source_preview':_bounded_text(seed['source'], 256)})
            continue
        if 'outcome' in entry:
            outcome = entry['outcome']
            identifier(entry['attempt_id'])
            status = outcome['status']
            require(status in ('SUCCEEDED','FAILED','CANCELLED'), 'CHECKPOINT_BINDING_MISMATCH')
            summary = {'attempt_id':entry['attempt_id'], 'status':status,
                       'metrics':_scalar_metrics(outcome.get('metrics'), NORMALIZATION)}
            if status != 'SUCCEEDED' and outcome.get('error_code'):
                identifier(outcome['error_code'])
                summary['error_code'] = outcome['error_code']
            outcomes.append(summary)
            continue
        require(latest is None and entry.get('schema') == 'research-iteration-checkpoint-2'
                and entry.get('ohlcv_binding') == OHLCV_BINDING, 'CHECKPOINT_BINDING_MISMATCH')
        identifier(entry['attempt_id'])
        sha(entry['source_sha256'])
        require(entry['kind'] in ('factor','strategy') and entry['next_kind'] in ('factor','strategy'),
                'CHECKPOINT_BINDING_MISMATCH')
        feedback, truncated = _feedback_excerpt(entry.get('feedback'))
        latest = {key:entry[key] for key in ('attempt_id','kind','next_kind','source_sha256','ohlcv_binding')}
        latest.update(native_feedback_metrics=_scalar_metrics(entry.get('native_feedback_metrics'), NATIVE_FEEDBACK_METRICS),
                      feedback=feedback, feedback_truncated=truncated)
    pinned = {}
    if latest is not None:
        # The source service can retain this success ahead of the optional
        # window. Match both its checkpoint attempt and its successful result.
        current = next((row for row in outcomes if row['attempt_id']==latest['attempt_id']
                        and row['status']=='SUCCEEDED'), None)
        if current is not None:
            latest['metrics'] = current['metrics']
        pinned['latest'] = latest
    if saved:
        pinned['saved_development_candidates'] = saved
    groups = [encoded(row).decode() for row in outcomes if latest is None or row['attempt_id']!=latest['attempt_id']]
    return pinned, groups


class LinuxResearchDriver:
    def __init__(self, *, controller, broker, export_input, resolve_roles, committed_history, reserve_prefix, commit_stage_artifacts, progress):
        self.controller = controller
        self.broker = broker
        self.export_input = export_input
        self.resolve_roles = resolve_roles
        self.committed_history = committed_history
        self.reserve_prefix = reserve_prefix
        self.commit_stage_artifacts = commit_stage_artifacts
        self.progress = progress

    def run(self, context, begun):
        if begun['phase'] == 'development':
            return self.run_development(context, begun)
        require(begun['phase']=='final_test', 'FINAL_PHASE_REQUIRED')
        from .sealed_final import SealedFinalEvaluator, SealedPrefixCollector
        seal = begun.get('sealed_candidate')
        require(type(seal) is dict, 'FINAL_SEAL_REQUIRED')
        def reserve(context, envelope, rows, frozen):
            context.require_active()
            self.controller.reconcile_startup()
            return self.controller.reserve_experiment(context,envelope,rows)
        evaluator = SealedFinalEvaluator(
            export_final=lambda c,b,s:self.export_input(c,b),
            collect_final=SealedPrefixCollector(self.controller,self.reserve_prefix),
            reserve=reserve,current_seal=lambda c,b:seal)
        self.progress(context, 'final_evaluate')
        return evaluator(context,begun)

    def run_development(self, context, begun):
        require(begun['phase'] == 'development' and 'final_test' not in begun['spec'], 'FINAL_SCOPE_LEAK')
        spec = begun['spec']; reservation = begun['reservation']
        started = time.monotonic(); prefix_count = 0; calls = []
        def active():
            context.require_active()
            require(time.monotonic()-started < reservation['compute_seconds'], 'EXPERIMENT_WALL_BUDGET_EXCEEDED')
        from .capacity import CHINESE_REASONS
        try:
            self.controller.reconcile_startup()
        except Exception as error:
            code=str(error)
            if code in CHINESE_REASONS:
                self.commit_stage_artifacts(context,begun,chunks('evaluation','application/json',
                    {'stage':'executor_preflight','status':'BLOCKED','code':code,'message_zh':CHINESE_REASONS[code],'model_calls':0},'executor-preflight'))
            raise
        history = self.committed_history(context, begun['session_id'])
        validate_development_history(history)
        saved_backtest = spec.get('reuse_policy') == 'evaluate_saved'
        if saved_backtest:
            require(reservation['model_requests']==0 and reservation['tokens']==0
                    and reservation['cost_microunits']==0, 'SAVED_BACKTEST_ZERO_MODEL_RESERVATION_REQUIRED')
            roles = {}  # Do not resolve profiles, credentials, or a default model.
        else:
            roles = {k:v for k,v in self.resolve_roles(context, spec).items() if v is not None}
            require({'hypothesis', 'coder', 'critic'} <= set(roles) <= {'hypothesis', 'coder', 'critic', 'reporter'}, 'ROLE_PROFILE_MAPPING_REQUIRED')
        def broker(**kwargs):
            require(len(calls) < reservation['model_requests'], 'MODEL_EXPERIMENT_CALL_BUDGET_EXCEEDED')
            active()
            # Windows host verifies profile revision/target and role-specific
            # prompt/context/effort/token/cost before each physical SDK request.
            calls.append({'role': kwargs['role'], 'profile_sha256': digest(kwargs['profile']), 'status': 'REQUESTED'})
            response = self.broker(**kwargs)
            calls[-1].update(status='RETURNED', response_sha256=hashlib.sha256(response.encode()).hexdigest())
            return response
        packages = self.export_input(context, begun)
        rows_by_symbol, input_receipt = merge_packages(
            packages, allow_cn=begun['resolved'].get('capability_limits', {}).get('market_semantics') == CN_MARKET,
        )
        require(sum(len(rows) for rows in rows_by_symbol.values()) <= reservation['prefixes'], 'EXPERIMENT_PREFIX_RESERVATION_INSUFFICIENT')
        for values in rows_by_symbol.values():
            require(80 <= len(values)*7//10 <= len(values)-40, 'INSUFFICIENT_TRAIN_VALID_HISTORY')
        try:
            capacity_receipt=self.controller.reserve_experiment(context,begun,rows_by_symbol)
        except Exception as error:
            code=str(error)
            if code in CHINESE_REASONS:
                self.commit_stage_artifacts(context,begun,chunks('evaluation','application/json',
                    {'stage':'executor_preflight','status':'BLOCKED','code':code,'message_zh':CHINESE_REASONS[code],'model_calls':0},'executor-preflight'))
            raise
        self.commit_stage_artifacts(context,begun,chunks('evaluation','application/json',
            {'stage':'executor_preflight','status':'RESERVED_BEFORE_MODELS','capacity':capacity_receipt},'executor-preflight'))
        self.progress(context, 'hypothesis')
        kind = spec['target']
        if kind == 'joint':
            kind = next((h['next_kind'] for h in reversed(history) if h.get('next_kind') in {'factor','strategy'}), 'factor')
        if spec.get('reuse_policy') == 'evaluate_saved':
            selected=[h['reusable_candidate'] for h in history if type(h.get('reusable_candidate')) is dict]
            require(len(selected)==1 and selected[0].get('kind') in {'factor','strategy'}, 'EXACT_ONE_REUSABLE_CANDIDATE_REQUIRED')
            kind=selected[0]['kind']
            require(spec['target'] in {kind,'joint'}, 'REUSABLE_CANDIDATE_KIND_MISMATCH')
        prior_native = next((h['native_feedback_metrics'] for h in reversed(history) if h.get('native_feedback_metrics') is not None), None)
        self.commit_stage_artifacts(context,begun,[single('checkpoint','application/json',{
            'ohlcv_binding':OHLCV_BINDING,
            'planned_kind':kind,'next_kind':'strategy' if kind=='factor' else 'factor',
            'native_feedback_metrics':prior_native,'native_metrics_origin':'RETAINED_PRIOR_EXPERIMENT_NOT_CURRENT_RESULT'},'plan-checkpoint')])
        native = None
        reuse = spec.get('reuse_policy', 'context')
        require(reuse in {'context','evaluate_saved'}, 'REUSE_POLICY_INVALID')
        seeds = [h['reusable_candidate'] for h in history if type(h.get('reusable_candidate')) is dict]
        if reuse == 'context':
            pinned_history, history_groups = hypothesis_context(history)
        if reuse == 'evaluate_saved':
            require(len(seeds)==1, 'EXACT_ONE_REUSABLE_CANDIDATE_REQUIRED')
            saved = seeds[0]
            require(saved.get('kind') == kind and saved.get('scope') == 'DEVELOPMENT_ONLY', 'REUSABLE_CANDIDATE_KIND_MISMATCH')
            source = saved['source']
            require(hashlib.sha256(source.encode()).hexdigest()==saved['source_sha256'], 'REUSABLE_CANDIDATE_HASH_MISMATCH')
            proposal = {'hypothesis':'Reevaluate saved immutable candidate on newly authorized development inputs',
                        'source_origin':saved['origin'],'source_sha256':saved['source_sha256'],
                        'engine':'SAVED_CANDIDATE_NEW_CAUSAL_GUEST_EVALUATION'}
            self.progress(context, 'code')
        elif kind == 'factor':
            from .native_rd import NativeFactorStages
            native = NativeFactorStages()
            hypothesis = native.propose(spec['objective'], pinned_history, history_groups, broker, roles['hypothesis'], active)
            self.progress(context, 'code')
            upstream_code = native.code(hypothesis, broker, roles['coder'], active)
            source = native.abi_code(upstream_code, broker, roles['coder'], active)
            proposal = {'hypothesis': hypothesis.__dict__, 'upstream_code_sha256': hashlib.sha256(upstream_code.encode()).hexdigest(),
                        'engine': 'RD-Agent1.0.0-native-factor-proposal-and-coder'}
        else:
            with role_scope(broker, roles['hypothesis'], 'hypothesis', active=active, max_calls=1,
                            max_chars=65536, history_groups=history_groups):
                text = BrokerBackend().build_messages_and_create_chat_completion(
                    encoded({'objective': spec['objective'], 'committed_development_history': pinned_history}).decode(),
                    'Propose a genuinely new daily long/cash strategy. No template catalogue. Explain hypothesis and causal past-only inputs.')
            proposal = {'hypothesis': text, 'engine': REPLAY_ENGINE}
            self.progress(context, 'code')
            with role_scope(broker, roles['coder'], 'coder', active=active, max_calls=1, max_chars=65536):
                source = BrokerBackend().build_messages_and_create_chat_completion(
                    text, 'Return Python source only. Implement def decide(frame,state): returning target invested fraction 0..1. '
                    'frame columns Open/High/Low/Close/Volume contain only history through this decision; state has actual trusted cash/equity/quantity. '
                    'Use arbitrary new causal indicators and signal/position logic. One total account uses equal symbol sleeves; the trusted Backtesting.py Strategy '
                    'handles actual rebalance at next open and costs. This is a target-policy ABI, NOT an arbitrary native Strategy equivalence. No files/network/labels.')
        require(type(source) is str and 0 < len(source.encode()) <= MAX_CODE, 'SOURCE_SIZE_INVALID')
        ast.parse(source)  # Syntax only; never an execution/isolation claim.
        self.commit_stage_artifacts(context, begun, chunks('proposal','application/json',proposal,'proposal')+
                                                  chunks(kind,'text/x-python',source,'source'))
        frozen_options = spec['engine_options']
        options = {'cash':frozen_options['cash'],'fee_rate':float(frozen_options['fee_rate']),
                   'spread_rate':float(frozen_options['spread_rate']),'seed':frozen_options['seed']}
        # Initial engine policy is frozen by this candidate version. Future API
        # options must be explicit immutable protocol revisions, not model text.
        requests = {}; causal = {}; train_end = None
        sleeves=sleeve_options(options,rows_by_symbol)
        completed_prefixes=0;total_prefixes=sum(len(v)for v in rows_by_symbol.values())
        progress_limit = 128
        # Keep slots for the initial and complete positions, and spread the
        # intermediate updates across the full workload even at a slow cadence.
        min_progress_gap = math.ceil(total_prefixes / (progress_limit - 2))
        last_progress=time.monotonic();last_reported=0;progress_messages=1
        self.progress(context, 'execute',completed_prefixes=0,total_prefixes=total_prefixes,
                      current_symbol=None,current_index=None)
        for symbol, rows in sorted(rows_by_symbol.items()):
            cut = len(rows)*7//10
            require(train_end is None or train_end == cut, 'COMPARISON_CALENDAR_MISMATCH')
            train_end = cut
            require(80 <= cut <= len(rows)-40, 'INSUFFICIENT_TRAIN_VALID_HISTORY')
            sleeve = sleeves[symbol]
            request = {'schema': 'fa-experiment-request-1', 'session_id': begun['session_id'],
                       'experiment_id': begun['attempt_id']+':'+symbol, 'attempt_id': begun['attempt_id'],
                       'job_id': context.job_id, 'run_id': context.run.run_id, 'fencing_token': context.fencing_token,
                       'reservation_id': begun['attempt_id'], 'phase': 'development', 'kind': kind,
                       'source': source, 'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                       'input': {}, 'input_sha256': digest({}), 'options': sleeve,
                       'runtime_sha256': self.controller.runtime_sha256}
            def reserve(index, request_hash):
                nonlocal prefix_count
                active(); require(prefix_count < reservation.get('prefixes', 2000), 'EXPERIMENT_PREFIX_CAP')
                self.reserve_prefix(context, begun, symbol, index, request_hash)
                prefix_count += 1
            def state_for(index, outputs, states):
                if kind == 'factor':
                    return {'cash': sleeve['cash'], 'equity': sleeve['cash'], 'quantity': 0}
                return account_state(rows, outputs, states, cut, sleeve, index)
            def completed(index):
                nonlocal completed_prefixes,last_progress,last_reported,progress_messages
                completed_prefixes+=1;now=time.monotonic()
                require(completed_prefixes<=total_prefixes,'COMPLETED_PREFIX_COUNT_INVALID')
                gap = completed_prefixes - last_reported
                complete = completed_prefixes == total_prefixes
                due = completed_prefixes == 1 or (
                    gap >= min_progress_gap and (gap >= 16 or now-last_progress >= 5 or index == len(rows)-1)
                )
                if ((complete and progress_messages < progress_limit)
                        or (due and progress_messages < progress_limit - 1)):
                    self.progress(context,'execute',completed_prefixes=completed_prefixes,total_prefixes=total_prefixes,
                                  current_symbol=symbol,current_index=index)
                    last_progress=now;last_reported=completed_prefixes;progress_messages+=1
            causal[symbol] = collect(request, rows, self.controller.execute, active=active, reserve_prefix=reserve,
                                    state_provider=state_for,on_completed=completed)
            requests[symbol] = {k: v for k, v in request.items() if k not in {'input', 'source'}}
        active()
        self.progress(context, 'evaluate')
        evaluation = evaluate(rows_by_symbol, causal, kind=kind, train_end=train_end, options=options, scoring=spec['scoring'])
        signature = training_signature(causal, train_end)
        evaluation['training_redundancy'] = redundancy(signature, history)
        if kind == 'factor':
            evaluation['quantile_returns'] = quantile_returns(rows_by_symbol, causal, train_end)
        self.progress(context, 'feedback')
        native_metrics = evaluation['native_feedback_metrics']
        if saved_backtest:
            feedback = {'kind':'DETERMINISTIC_SAVED_CANDIDATE_BACKTEST', 'ai_commentary':False,
                        'metrics':evaluation['metrics'],'score':evaluation['score'],
                        'claim':evaluation['claim'], 'model_calls':0,
                        'next_action':'OPTIONAL_NEW_RESEARCH_SESSION_FOR_AI_COMMENTARY'}
        elif native is not None and 'IC' in native_metrics:
            # Explicit actual baseline, not zero-filled absent measurements.
            # IC baseline cannot be invented: no native best-factor comparison
            # in first round. Use neutral thin feedback until actual persisted
            # prior native metrics exist; below branch checks that evidence.
            prior = next((h.get('native_feedback_metrics') for h in reversed(history) if h.get('native_feedback_metrics') is not None), None)
            if prior and set(native_metrics) <= set(prior):
                feedback = native.feedback(hypothesis, native_metrics, prior, broker, roles['critic'], active)
            else:
                feedback = self._critique(broker, roles['critic'], proposal, evaluation, active)
        else:
            feedback = self._critique(broker, roles['critic'], proposal, evaluation, active)
        report = None
        if not saved_backtest and 'reporter' in roles:
            with role_scope(broker, roles['reporter'], 'reporter', active=active, max_calls=1, max_chars=65536):
                report = BrokerBackend().build_messages_and_create_chat_completion(
                    encoded({'hypothesis':proposal,'actual_metrics':evaluation['metrics'],'score':evaluation['score'],'feedback':feedback}).decode(),
                    '用中文报告本轮实际开发实验、费用与不足。不得虚构指标、最终测试、实际市场有效性。简洁区分执行成功与研究成功。')
        value = {'ohlcv_binding':OHLCV_BINDING, 'proposal': proposal, 'evaluation': evaluation, 'feedback': feedback,
                 'not_supported':['correlation_redundancy','automatic_duplicate_budget_refund'],
                 'native_feedback_metrics': native_metrics, 'causal': causal, 'request_bindings': requests,
                 'input_receipt': input_receipt, 'model_calls': calls, 'reporter_text':report,
                 'retained_capacity':capacity_receipt,
                 'actual_prefix_executions': prefix_count, 'active_seconds': time.monotonic()-started}
        from .reporting import portfolio_view
        value['backtest']=portfolio_view(evaluation,rows_by_symbol,train_end,options,
                                        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                                        input_receipt_sha256=digest(input_receipt),
                                        research_market=input_receipt[0].get('research_market'))
        value['mode']='SAVED_CODE_DETERMINISTIC_BACKTEST' if saved_backtest else 'ADAPTIVE_DEVELOPMENT_RESEARCH'
        value['holding_rule'] = ({'factor_threshold':'MEDIAN_OF_FIRST70_PERCENT_TRAIN_ONLY',
                                 'target':'0.999_IF_FINITE_FACTOR_ABOVE_THRESHOLD_ELSE_ZERO',
                                 'sizing':'INTEGER_UNITS_EQUAL_SYMBOL_CAPITAL_SLEEVES'} if kind=='factor' else
                                {'strategy_target':'SAVED_DECIDE_FRAME_STATE_FRACTION_0_TO_1',
                                 'sizing':'INTEGER_UNITS_EQUAL_SYMBOL_CAPITAL_SLEEVES'})
        value['execution_costs']={'version':COST_MODEL, 'cash':options['cash'],'commission_both_sides':options['fee_rate'],
                                  'spread_rate':options['spread_rate'],'fill':'NEXT_OPEN',
                                  'forced_final_close':False}
        value['completed_prefix_executions']=completed_prefixes
        if hasattr(context,'activity_counters'):value['active_read_counters']=context.activity_counters()
        checkpoint = {'schema':'research-iteration-checkpoint-2','kind':kind,'source':source,
                      'ohlcv_binding':OHLCV_BINDING,
                      'abi':'compute(frame)' if kind=='factor' else 'decide(frame,state)',
                      'attempt_id':begun['attempt_id'],'input_receipt_sha256':digest(input_receipt),
                      'training_output_sha256':signature,'engine_options':options,
                      'engine':REPLAY_ENGINE,'cost_model':COST_MODEL,
                      'frozen_factor_thresholds':{v['symbol']:v['train_threshold'] for v in evaluation['ts_ic']},
                      'native_feedback_metrics': native_metrics, 'proposal': proposal, 'feedback': feedback,
                      'next_kind':'strategy' if kind=='factor' else 'factor',
                      'source_sha256': hashlib.sha256(source.encode()).hexdigest(), 'evaluation_sha256': digest(evaluation)}
        artifacts = (chunks('proposal','application/json',proposal,'proposal')+
                     chunks(kind,'text/x-python',source,'source')+
                     chunks('feedback','application/json',feedback,'feedback')+
                     chunks('checkpoint','application/json',checkpoint,'checkpoint'))
        if report is not None:
            artifacts+=chunks('feedback','text/plain',report,'reporter')
        artifacts+=pack(value,maximum_parts=256-len(artifacts))
        require(sum(len(a['content'].encode()) for a in artifacts)<=reservation['artifact_bytes'],'ARTIFACT_RESERVATION_EXCEEDED')
        active()
        return {'status': 'SUCCEEDED', 'error_code': None,
                'metrics': {k: float(v) for k, v in evaluation['metrics'].items() if v is not None},
                'artifacts': artifacts}

    @staticmethod
    def _critique(broker, profile, proposal, evaluation, active):
        summary = {k: evaluation[k] for k in ('metrics', 'score', 'label', 'development', 'claim')}
        with role_scope(broker, profile, 'critic', active=active, max_calls=1, max_chars=65536):
            text = BrokerBackend().build_messages_and_create_chat_completion(
                encoded({'proposal': proposal, 'actual_trusted_evaluation': summary}).decode(),
                'Critique actual development evidence and propose a different next experiment. Missing statistics remain insufficient; no final-test or market validity claims.')
        return {'kind': 'THIN_ADAPTER_DEVELOPMENT_FEEDBACK_NOT_NATIVE_BEST_FACTOR_COMPARISON', 'text': text}
