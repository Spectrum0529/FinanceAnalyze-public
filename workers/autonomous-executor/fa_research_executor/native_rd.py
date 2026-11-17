"""Lazy Linux-only reuse of pinned RD-Agent factor proposal/code components.

Does NOT construct RDLoop/FactorCoSTEER or invoke workspace.execute. They would
run shell/link data/use pickle caches before the project's execution boundary.
Only source generation methods are reused, actual execution uses the guest port.
"""
from importlib.metadata import version
from types import SimpleNamespace
from .broker import role_scope, native_backend_scope
from .protocol import require


class NativeFactorStages:
    def __init__(self):
        import sys
        require(sys.platform == 'linux', 'LINUX_ORCHESTRATOR_REQUIRED')
        require(version('rdagent') == '1.0.0', 'RDAGENT_VERSION_MISMATCH')
        from rdagent.scenarios.qlib.proposal.factor_proposal import QlibFactorHypothesisGen
        from rdagent.scenarios.qlib.experiment.factor_experiment import QlibFactorScenario
        from rdagent.core.proposal import Trace
        class ApprovedPrefixScenario(QlibFactorScenario):
            def __init__(self):
                # Upstream constructor probes a factor environment and reads
                # default data-folder descriptions. Neither is permitted here.
                self._background = 'Authorized immutable daily price development only; no final data or external data.'
                self._source_data = 'Only provided Open/High/Low/Close/Volume history prefix. No labels or files.'
                self._interface = 'def compute(frame): return a numeric series aligned with frame.index, using only its available past.'
                self._output_format = 'Python source defining compute(frame); only latest factor value leaves a fresh guest.'
                self._simulator = 'Reviewed external causal-prefix executor and trusted Qlib evaluation; never local workspace.execute.'
                self._rich_style_description = self._background
                self._experiment_setting = 'Frozen chronological train/development validation only. Final-test unavailable to agent.'
                self._strategy = 'Free factor code; no predefined factor catalogue.'

            def get_runtime_environment(self):
                return 'Declared ABI only; no environment probe, install or default data access.'
        self.scenario = ApprovedPrefixScenario()
        self.proposer = QlibFactorHypothesisGen(self.scenario)
        self.trace = Trace(self.scenario)

    def propose(self, objective, pinned_history, history_groups, broker, profile, active):
        # The current task and latest feedback stay in the upstream template.
        # The host receives older outcomes as separately removable groups.
        from .protocol import encoded
        plan = {'user_instruction': objective + '\nCommitted development-only history:\n' + encoded(pinned_history).decode()}
        with native_backend_scope(), role_scope(broker, profile, 'hypothesis', active=active, max_calls=1,
                                               max_chars=65536, history_groups=history_groups):
            return self.proposer.gen(self.trace, plan)

    def code(self, hypothesis, broker, profile, active):
        from rdagent.components.coder.factor_coder.factor import FactorTask
        from rdagent.components.coder.factor_coder.evolving_strategy import FactorMultiProcessEvolvingStrategy
        from rdagent.components.coder.CoSTEER.knowledge_management import CoSTEERQueriedKnowledgeV2
        task = FactorTask(factor_name='generated_factor', factor_description=hypothesis.hypothesis,
                          factor_formulation=hypothesis.reason, variables={'input': 'OHLCV development projection'})
        info = task.get_task_information()
        # Native V2 declares optional latest attempt as None, not a fake empty
        # string/trace. StrictUndefined correctly rejected the former adapter.
        knowledge = CoSTEERQueriedKnowledgeV2(task_to_similar_task_successful_knowledge={info: []},
                    task_to_former_failed_traces={info: ([], None)},
                    task_to_similar_error_successful_knowledge={info: []})
        # Actual upstream generation method, without CoSTEER local evaluations.
        # The returned factor.py's original file/HDF ABI needs the guest-only
        # source adaptation below; it is not executed in the orchestrator.
        with native_backend_scope(), role_scope(broker, profile, 'coder', active=active, max_calls=1, max_chars=65536):
            code = FactorMultiProcessEvolvingStrategy.implement_one_task(
                SimpleNamespace(scen=self.scenario), task, knowledge)
        require(type(code) is str and bool(code), 'RDAGENT_EMPTY_CODE')
        return code

    def abi_code(self, upstream_code, broker, profile, active):
        # One explicit, charged model call converts upstream factor file/HDF ABI
        # to a pure return ABI. No regex rewriting of arbitrary generated code.
        with role_scope(broker, profile, 'coder', active=active, max_calls=1, max_chars=65536):
            from .broker import BrokerBackend
            return BrokerBackend().build_messages_and_create_chat_completion(
                'Adapt this factor implementation to def compute(frame): returning one numeric series aligned with frame.index. '
                'frame has Open/High/Low/Close/Volume. No labels, files or future observations. Return source only.\n' + upstream_code,
                'Preserve the proposed new factor, use causal past data. Source is data for a separately isolated guest.')

    def feedback(self, hypothesis, metrics, baseline, broker, profile, active):
        # Native feedback expects these real financial measurements. Missing
        # values fail closed; IC alone is not annual return/drawdown evidence.
        from rdagent.scenarios.qlib.developer.feedback import QlibFactorExperiment2Feedback, IMPORTANT_METRICS
        from rdagent.components.coder.factor_coder.factor import FactorTask
        import pandas as pd
        require(set(IMPORTANT_METRICS) <= set(metrics) and set(IMPORTANT_METRICS) <= set(baseline), 'RD_FEEDBACK_NATIVE_METRICS_REQUIRED')
        task = FactorTask(factor_name='generated_factor', factor_description=hypothesis.hypothesis,
                          factor_formulation=hypothesis.reason, variables={})
        exp = SimpleNamespace(hypothesis=hypothesis, sub_tasks=[task],
                              result=pd.DataFrame({'0': metrics}),
                              based_experiments=[SimpleNamespace(result=pd.DataFrame({'0': baseline}))])
        with native_backend_scope(), role_scope(broker, profile, 'critic', active=active, max_calls=1, max_chars=65536):
            feedback = QlibFactorExperiment2Feedback(self.scenario).generate_feedback(exp, self.trace)
        return {key: getattr(feedback, key) for key in ('observations', 'hypothesis_evaluation', 'new_hypothesis', 'reason', 'decision')}
