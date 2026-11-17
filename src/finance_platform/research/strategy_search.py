"""Typed long/cash trials: train-only selection, one sealed later validation."""
import time
from copy import deepcopy
from .portfolio import validate_dataset,execute,digest,fail,number

TOOLS=[{'type':'function','function':{'name':name,'description':description,'parameters':schema,'strict':False}}for name,description,schema in (
 ('strategy_trial','实际回测一个受控参数候选；仅返回训练期证据。',{'type':'object','properties':{'template':{'type':'string','enum':['buy_hold','sma','sma_cross']},'fast':{'type':'integer','minimum':2,'maximum':15},'slow':{'type':'integer','minimum':5,'maximum':30}},'required':['template'],'additionalProperties':False}),
 ('strategy_compare','比较已运行候选的训练期净收益和费用，不读取验证期。',{'type':'object','properties':{},'additionalProperties':False}),
 ('strategy_finalize','冻结训练期最优候选，随后只验证该候选一次并结束。',{'type':'object','properties':{},'additionalProperties':False}))]


def candidate(value):
    if type(value)is not dict:fail('SEARCH_CANDIDATE_INVALID')
    template=value.get('template');keys={'buy_hold':{'template'},'sma':{'template','slow'},'sma_cross':{'template','fast','slow'}}
    if template not in keys or set(value)!=keys[template]:fail('SEARCH_CANDIDATE_INVALID')
    if template!='buy_hold'and(type(value['slow'])is not int or not 5<=value['slow']<=30):fail('SEARCH_CANDIDATE_INVALID')
    if template=='sma_cross'and(type(value['fast'])is not int or not 2<=value['fast']<=15 or value['fast']>=value['slow']):fail('SEARCH_CANDIDATE_INVALID')
    return deepcopy(value)


def limits(value=None):
    value={}if value is None else value
    caps={'max_trials':(6,1,12),'max_model_calls':(12,3,32),'wall_seconds':(90,1,120),'max_engine_runs':(120,2,600),'max_result_bytes':(8388608,65536,25165824)}
    if type(value)is not dict or set(value)-set(caps):fail('SEARCH_LIMIT_INVALID')
    out={}
    for k,(default,low,high)in caps.items():
        out[k]=value.get(k,default)
        if type(out[k])is not int or not low<=out[k]<=high:fail('SEARCH_LIMIT_INVALID')
    return out


class Search:
    def __init__(self,dataset,parameters=None,*,search_limits=None,active=lambda:None,progress=lambda n:None,clock=time.monotonic,native_reader=None,retain=lambda v:None):
        self.dataset=dataset;self.native_reader=native_reader;self.retain=retain;self.ref={k:dataset[k]for k in('dataset_id','version','content_hash')}
        self.params=deepcopy(parameters or{});self.bounds=limits(search_limits);self.active=active;self.progress=progress;self.clock=clock;self.started=clock()
        if set(self.params)-{'initial_cash','fee_per_share','minimum_fee','spread_bps','start_date','end_date'}:fail('SEARCH_PARAMETERS_INVALID')
        self.check();symbols=validate_dataset(dataset)
        dates=sorted({b.session_date for bars in symbols.values()for b in bars if self.params.get('start_date',bars[0].session_date)<=b.session_date<=self.params.get('end_date',bars[-1].session_date)})
        cut=len(dates)*7//10
        self.training=dates[:cut];self.validation=dates[cut:];self.trials=[];self.steps=[];self.finished=None;self.attempts=[]
        if len(dataset['rows'])>100000:fail('SEARCH_DATA_BUDGET_EXCEEDED')
        self.engine_runs=0;self.saved_result_bytes=0
        if len(symbols)*2>self.bounds['max_engine_runs']:fail('SEARCH_ENGINE_BUDGET_TOO_SMALL')
        self.sufficient=bool(self.training and self.validation)and all(sum(b.session_date in set(self.training)for b in bars)>=80 and sum(b.session_date in set(self.validation)for b in bars)>=40 and {self.training[0],self.training[-1],self.validation[0],self.validation[-1]}<={b.session_date for b in bars}for bars in symbols.values())
    def check(self):
        self.active()
        if self.clock()-self.started>=self.bounds['wall_seconds']:fail('SEARCH_WALL_BUDGET_EXCEEDED')
    def run(self,choice,period):
        self.check();params={k:v for k,v in self.params.items()if k not in{'start_date','end_date'}}
        strategy={'buy_hold':'buy_hold','sma':'sma20','sma_cross':'sma_cross_5_20'}[choice['template']]
        def reserve():
            self.check()
            if self.engine_runs>=self.bounds['max_engine_runs']:fail('SEARCH_ENGINE_BUDGET_EXCEEDED')
            self.engine_runs+=1 # before call, including failed native executions
        data=self.native_reader(period[0],period[-1])if self.native_reader else self.dataset
        result=execute(data,{**params,'strategy':strategy,'start_date':period[0],'end_date':period[-1]},strategy_parameters=choice,active=self.check,reserve_engine=reserve)
        import json
        size=len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())
        if self.saved_result_bytes+size>self.bounds['max_result_bytes']:fail('SEARCH_RESULT_BYTES_EXCEEDED')
        self.saved_result_bytes+=size
        self.check()
        return {'result_id':'trial:'+digest({'dataset':self.ref,'candidate':choice,'period':period,'result':result}),
            'dataset_reference':self.ref,'candidate':choice,'period':{'first':period[0],'last':period[-1]},'result':result}
    def comparison(self):
        return [{'result_id':t['result_id'],'candidate':t['candidate'],'training_net_profit':t['result']['reconciliation']['net_profit'],'fees':t['result']['reconciliation']['fees']}for t in sorted(self.trials,key=lambda t:(-number(t['result']['reconciliation']['net_profit']),digest(t['candidate'])))]
    def tool(self,name,args):
        self.check()
        if self.finished is not None:fail('SEARCH_ALREADY_FINALIZED')
        if not self.sufficient:fail('SEARCH_INSUFFICIENT_SESSIONS')
        if name=='strategy_trial':
            value=candidate(args)
            if len(self.attempts)>=self.bounds['max_trials']:fail('SEARCH_TRIAL_BUDGET_EXCEEDED')
            if any(t['candidate']==value for t in self.attempts):fail('SEARCH_DUPLICATE_CANDIDATE')
            attempt={'candidate':value,'attempt':len(self.attempts)+1,'dataset_reference':self.ref,'status':'STARTED'};self.attempts.append(attempt)
            self.retain({'event':'TRIAL_STARTED','attempt':deepcopy(attempt),'engine_runs':self.engine_runs})
            from .daily import DailyError
            try:
                trial=self.run(value,self.training);self.trials.append(trial);attempt.update(status='SUCCEEDED',result_id=trial['result_id']);output=self.comparison()
                self.retain({'event':'TRIAL_COMPLETED','attempt':deepcopy(attempt),'trial':trial,'engine_runs':self.engine_runs,'saved_result_bytes':self.saved_result_bytes})
            except (DailyError,ValueError)as error:
                code=getattr(error,'code','SEARCH_ENGINE_FAILED')
                if code in{'SEARCH_WALL_BUDGET_EXCEEDED','SEARCH_ENGINE_BUDGET_EXCEEDED','SEARCH_RESULT_BYTES_EXCEEDED','CANCELLED','LEASE_LOST'}:raise
                attempt.update(status='FAILED',code=code);output={'failed_attempt':attempt}
                self.retain({'event':'TRIAL_FAILED','attempt':deepcopy(attempt),'engine_runs':self.engine_runs,'saved_result_bytes':self.saved_result_bytes})
        elif name in {'strategy_compare','strategy_finalize'}:
            if args!={}:fail('SEARCH_TOOL_ARGUMENTS_INVALID')
            if not self.trials:fail('SEARCH_TRIAL_REQUIRED')
            output=self.comparison()
            if name=='strategy_finalize':
                best=next(t for t in self.trials if t['result_id']==output[0]['result_id'])
                self.retain({'event':'VALIDATION_STARTED','selected_training_result_id':best['result_id'],'candidate':best['candidate'],'engine_runs':self.engine_runs})
                self.finished=self.run(best['candidate'],self.validation)
                self.retain({'event':'VALIDATION_COMPLETED','validation':self.finished,'engine_runs':self.engine_runs,'saved_result_bytes':self.saved_result_bytes})
                output={'selected_training_result_id':best['result_id'],'validation_result_id':self.finished['result_id']}
        else:fail('SEARCH_TOOL_UNSUPPORTED')
        step={'step':len(self.steps)+1,'tool':name,'arguments':deepcopy(args),'dataset_reference':self.ref,'output':output}
        step['step_id']='step:'+digest(step);self.steps.append(step)
        self.retain({'event':'TOOL_COMPLETED','step':step,'engine_runs':self.engine_runs,'saved_result_bytes':self.saved_result_bytes})
        self.progress(min(90,10+len(self.steps)*5));return step
    def result(self):
        return {'family':'strategy-search-result-1','status':'INSUFFICIENT_DATA'if not self.sufficient else'COMPLETED_EXPLORATORY_NOT_MARKET_VALIDATED'if self.finished else'BUDGET_STOPPED_TRAINING_ONLY',
            'dataset_reference':self.ref,'limits':self.bounds,'usage':{'engine_runs':self.engine_runs,'saved_native_result_bytes':self.saved_result_bytes},'steps':self.steps,'attempts':self.attempts,'training_trials':self.trials,'training_comparison':self.comparison(),'selected_validation':self.finished,
            'split':{'policy':'CHRONOLOGICAL70_30_NO_ADAPTIVE_VALIDATION','training':self.training,'validation':self.validation,'minimum_per_symbol':{'training':80,'validation':40},'warmup':'INDEPENDENT_EMPTY_START_ACCOUNTS_WARMUP_WITHIN_EACH_PERIOD'},
            'bias':'Multiple train trials/iterations are exploratory. Validation is evaluated once AFTER training-only selection; repeated user runs can overfit this later period too. Not a formal holdout, PIT, or predictive success certification.',
            'capital_policy':'Each candidate uses SAME one total initial capital and fixed sleeves; trial accounts are alternatives, never summed.', 'engine':'financeanalyze.daily1.0.0/controlled-long-cash-parameters-1'}


def run_search(dataset,parameters,candidates,*,search_limits=None,active=lambda:None,progress=lambda n:None,native_reader=None,retain=lambda v:None):
    bounds=limits(search_limits)
    if type(candidates)is not list or not 1<=len(candidates)<=bounds['max_trials']:fail('SEARCH_CANDIDATE_LIMIT')
    values=[candidate(c)for c in candidates]
    if len({digest(c)for c in values})!=len(values):fail('SEARCH_DUPLICATE_CANDIDATE')
    session=Search(dataset,parameters,search_limits=bounds,active=active,progress=progress,native_reader=native_reader,retain=retain)
    if session.sufficient:
        for c in values:session.tool('strategy_trial',c)
        session.tool('strategy_compare',{});session.tool('strategy_finalize',{})
    return session.result()
