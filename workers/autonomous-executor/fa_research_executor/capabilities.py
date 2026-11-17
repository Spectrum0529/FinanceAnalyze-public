"""Pure capability DTO; existence is not native execution acceptance."""
from .evaluation import NORMALIZATION, REPLAY_ENGINE, COST_MODEL
from .protocol import OHLCV_BINDING
from .autonomous_receipts import CN_MARKET


class EvaluatorCapabilities:
    def capabilities(self):
        reservation = {'attempts': 1, 'model_requests': 6, 'tokens': 600000, 'cost_microunits': 0,
                       'compute_seconds': 600, 'artifact_bytes': 16777216, 'prefixes': 2000}
        return {'version': 'fa-causal-target-qlib-evaluation-3', 'metrics': list(NORMALIZATION),
                'engine': REPLAY_ENGINE, 'cost_model_version': COST_MODEL,
                'ohlcv_binding': OHLCV_BINDING,
                'market_semantics': [CN_MARKET],
                'goal_round_limits': {
                    'balanced': {'compute_seconds': 600, 'max_prefix_workload': 500},
                    'deep': {'compute_seconds': 1500, 'max_prefix_workload': 1000},
                    'extended': {'compute_seconds': 3000, 'max_prefix_workload': 2000},
                },
                'normalization': NORMALIZATION, 'scoring': 'sum(weight*direction*metric/fixed_scale)',
                'reservations': {k: dict(reservation) for k in ('strategy', 'factor', 'joint')},
                'saved_backtest_reservation':{**reservation,'model_requests':0,'tokens':0,'cost_microunits':0},
                'saved_backtest_requires_model':False,
                'strategy_abi': 'decide(frame,state)->target_fraction0..1; sealed intents only',
                'factor_abi': 'compute(history_prefix)->series; only last value used',
                'engine_options': {'cash': {'default': 10000.0, 'minimum': 1, 'maximum': 1000000000},
                                   'fee_rate': {'default': .001, 'minimum': 0, 'maximum': .05},
                                   'spread_rate': {'default': .0002, 'minimum': 0, 'maximum': .05},
                                   'seed': {'default': 14027, 'minimum': 0, 'maximum': 4294967295}},
                'split': 'CHRONOLOGICAL70_30_MIN80_40', 'cost_model': 'PROPORTIONAL_COMMISSION_BOTH_SIDES_ENTRY_SPREAD_ONLY',
                'scope': 'AUTHORIZED_SYNTHETIC_OR_PUBLIC_IMPORT_DEVELOPMENT; PIT_NOT_CERTIFIED',
                'final_test': 'UNAVAILABLE_UNTIL_SEPARATE_AUTHORIZED_FROZEN_EVALUATOR',
                'development_diagnostics':{'quantile_returns':'GROSS_FORWARD_LABEL_MIN5_MEMBERS_MIN20_DATES','redundancy':'EXACT_TRAIN_OUTPUT_HASH_INFORMATION_ONLY'},
                'reusable_candidates':{'generation_context':True,'isolated_reevaluation':True,'host_execution':False},
                'sealed_final_evaluator_source':True,'sealed_final_protocol':'fa-sealed-final-request-1',
                'not_supported':['correlation_redundancy','automatic_duplicate_budget_refund'],
                'retained_capacity':{'policy':'retained-capacity-1','prefix_objects_including_reservations':8192,
                    'retained_bytes_reservation':8589934592,'journal_bytes_cap':134217728,'journal_event_cap':100000,
                    'stdout_per_prefix':65536,'stderr_per_prefix':65536,'pre_model_admission':True,'automatic_deletion':False},
                'pricing': 'FA013_COUNTERS_ONLY_OR_PRICED_CAP; engineering makes zero real calls and does not claim known monetary spend',
                'runtime_acceptance': 'PENDING_NATIVE_IMPORT_CONTAINER_AND_FINANCIAL_ACCEPTANCE'}
