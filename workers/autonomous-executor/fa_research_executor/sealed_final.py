"""Separate nonadaptive final evaluator; no broker, hypothesis or codegen port.

The trusted composition must supply a phase-bound isolated prefix collector.
The adaptive development controller is deliberately not a fallback for it.
"""
import hashlib
import math
from .protocol import require, digest, sha, OHLCV_BINDING, validate_ohlcv_binding
from .inputs import merge_packages
from .evaluation import evaluate, sleeve_options, account_state
from .artifacts import chunks


class SealedFinalEvaluator:
    def __init__(self, *, export_final, collect_final, reserve, current_seal):
        self.export_final, self.collect_final = export_final, collect_final
        self.reserve, self.current_seal = reserve, current_seal

    def __call__(self, context, begun):
        require(begun['phase']=='final_test', 'FINAL_PHASE_REQUIRED')
        seal = self.current_seal(context, begun)
        require(type(seal) is dict and seal.get('schema')=='research-final-seal-1', 'FINAL_SEAL_REQUIRED')
        validate_ohlcv_binding(seal)
        sha(seal.get('checkpoint_sha256'))
        require(seal['session_id']==begun['session_id'] and seal['attempt_id']==begun['attempt_id']
                and seal['candidate_hash']==begun['candidate_hash'] and seal['exposure_recorded'] is True,
                'FINAL_SEAL_BINDING_MISMATCH')
        source = seal['source']; kind = seal['kind']
        require(kind in {'factor','strategy'} and hashlib.sha256(source.encode()).hexdigest()==seal['candidate_hash'], 'FINAL_SOURCE_MISMATCH')
        # These options/thresholds are sealed before any final export. The
        # evaluator never learns a preprocessing parameter from final labels.
        options = seal['engine_options']; thresholds = seal['factor_thresholds']
        require(digest(options)==seal['engine_options_sha256'], 'FINAL_OPTIONS_MISMATCH')
        require(digest(thresholds)==seal['factor_thresholds_sha256'], 'FINAL_THRESHOLD_MISMATCH')
        context.require_active()
        packages = self.export_final(context, begun, seal)
        rows, receipts = merge_packages(packages)
        require(digest(receipts)==seal['final_input_receipts_sha256'], 'FINAL_INPUT_MISMATCH')
        warmup = seal['warmup_rows']
        require(type(warmup) is int and warmup>=80 and all(len(r)-warmup>=40 for r in rows.values()), 'FINAL_WINDOW_INSUFFICIENT')
        if kind=='factor':
            require(set(thresholds)==set(rows) and all(type(v) in {int,float} and math.isfinite(v) for v in thresholds.values()), 'FINAL_THRESHOLD_MISSING')
        require(sum(map(len,rows.values()))<=begun['reservation']['prefixes'], 'FINAL_PREFIX_BUDGET_EXCEEDED')
        self.reserve(context, begun, rows, seal)
        # This is a trusted phase-specific service contract, not a callable
        # selected by model/client JSON. It must enforce exact seal/source/
        # prefix/runtime/hash/closure and original cumulative reservations.
        causal = self.collect_final(context, begun, rows, seal)
        require(set(causal)==set(rows), 'FINAL_CAUSAL_BINDING_MISMATCH')
        for symbol, values in causal.items():
            require(values.get('rows_hash')==digest(rows[symbol]) and values.get('source_sha256')==seal['candidate_hash']
                    and values.get('phase')=='final_test' and values.get('sealed_descriptor_sha256')==digest(seal),
                    'FINAL_CAUSAL_BINDING_MISMATCH')
        context.require_active()
        result = evaluate(rows, causal, kind=kind, train_end=warmup, options=options,
                          scoring=seal['scoring'], frozen_thresholds=thresholds if kind=='factor' else {}, final=True)
        value = {'schema':'research-sealed-final-result-1','candidate_hash':seal['candidate_hash'],
                 'ohlcv_binding':OHLCV_BINDING,
                 'seal_sha256':digest(seal),'input_receipts':receipts,'evaluation':result,
                 'model_calls':0,'adaptive_feedback':False,'formal_research_approved':False}
        from .reporting import portfolio_view
        value['backtest']=portfolio_view(result,rows,warmup,options,
                                        source_sha256=seal['candidate_hash'],input_receipt_sha256=digest(receipts))
        artifacts = chunks('evaluation','application/json',value,'sealed-final-evaluation')
        require(sum(len(a['content'].encode()) for a in artifacts)<=begun['reservation']['artifact_bytes'], 'ARTIFACT_RESERVATION_EXCEEDED')
        return {'status':'SUCCEEDED','error_code':None,
                'metrics':{k:float(v) for k,v in result['metrics'].items() if v is not None},'artifacts':artifacts}


class SealedPrefixCollector:
    """Existing per-prefix guest lifecycle, entered only with a frozen seal."""
    def __init__(self, controller, reserve_prefix):
        self.controller, self.reserve_prefix = controller, reserve_prefix

    def __call__(self, context, begun, rows_by_symbol, seal):
        from .protocol import validate_final_request, validate_response, MAX_CODE
        validate_ohlcv_binding(seal)
        require(0<len(seal['source'].encode())<=MAX_CODE, 'SOURCE_SIZE_INVALID')
        sleeves = sleeve_options(seal['engine_options'], rows_by_symbol)
        result = {}; total = 0
        for symbol, rows in sorted(rows_by_symbol.items()):
            outputs = []; states = []; receipts = []
            for index, row in enumerate(rows):
                context.require_active()
                require(total<begun['reservation']['prefixes'], 'FINAL_PREFIX_BUDGET_EXCEEDED')
                initial = {'cash':sleeves[symbol]['cash'],'equity':sleeves[symbol]['cash'],'quantity':0}
                state = initial if seal['kind']=='factor' else account_state(rows,outputs,states,seal['warmup_rows'],sleeves[symbol],index)
                request = {'schema':'fa-sealed-final-request-1','session_id':begun['session_id'],
                           'experiment_id':begun['attempt_id']+':'+symbol,'attempt_id':begun['attempt_id'],
                           'job_id':context.job_id,'run_id':context.run.run_id,'fencing_token':context.fencing_token,
                           'reservation_id':begun['attempt_id'],'phase':'final_test','kind':seal['kind'],
                           'source':seal['source'],'source_sha256':seal['candidate_hash'],
                           'input':{'scope':'AUTHORIZED_SEALED_FINAL_PREFIX','decision_at':row['at'],'rows':rows[:index+1],'state':state},
                           'options':sleeves[symbol],'runtime_sha256':self.controller.runtime_sha256,'seal_sha256':digest(seal)}
                request['input_sha256']=digest(request['input']);validate_final_request(request)
                self.reserve_prefix(context,begun,symbol,index,digest(request));total+=1
                raw, closure = self.controller.execute_sealed(request,context.require_active,seal=seal)
                require(closure=={'request_sha256':digest(request),'writers_closed':True,'owned_tree_empty':True}, 'EXECUTOR_CLOSURE_UNPROVEN')
                response = validate_response(raw,request)
                require(response['status']=='SUCCEEDED', 'PREFIX_EXECUTION_FAILED')
                outputs.append(response['payload']);states.append(state)
                receipts.append({'request_sha256':digest(request),'response_sha256':digest(response),'decision_at':row['at']})
            result[symbol]={'rows_hash':digest(rows),'outputs':outputs,'states':states,'prefix_receipts':receipts,
                            'source_sha256':seal['candidate_hash'],'phase':'final_test','sealed_descriptor_sha256':digest(seal)}
        return result
