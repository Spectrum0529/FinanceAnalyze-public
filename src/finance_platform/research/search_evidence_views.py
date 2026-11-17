"""Bounded trajectory summaries; originals remain immutable and section-readable."""
def scalars(value,names):
    if type(value)is not dict:return{}
    return {k:v for k in names if k in value and(type(v:=value[k])in(int,float,bool)or type(v)is str and len(v)<=256 or v is None)}


def trial_summary(trial):
    if type(trial)is not dict:return None
    result=trial.get('result',{})
    return {**scalars(trial,('result_id',)),
        'candidate':scalars(trial.get('candidate'),('template','fast','slow')),
        'period':scalars(trial.get('period'),('first','last')),
        'dataset_reference':scalars(trial.get('dataset_reference'),('dataset_id','version','content_hash')),
        'metrics':scalars(result.get('reconciliation'),('initial_capital','net_profit','fees','security_net_pnl','cash_residual','external_contributions')),
        'price_basis':scalars(result,('quantity_basis','fee_basis','return_basis','display_name')),
        'detail_available':True}


def summary(value):
    out=scalars(value,('event','engine_runs','saved_result_bytes','profile_id','revision','call_number','selected_training_result_id'))
    for key in('trial','validation'):
        if type(value.get(key))is dict:out[key]=trial_summary(value[key])
    if type(value.get('attempt'))is dict:
        a=value['attempt'];out['attempt']={**scalars(a,('attempt','status','result_id','code')),'candidate':scalars(a.get('candidate'),('template','fast','slow')),'dataset_reference':scalars(a.get('dataset_reference'),('dataset_id','version','content_hash'))}
    if type(value.get('call'))is dict:out['call']=scalars(value['call'],('profile_id','revision','provider','model','tool_call_id','tool'))
    if type(value.get('coverage'))is dict:out['coverage']=scalars(value['coverage'],('scope','aggregate_complete','result_rows_omitted','evidence_events_dropped','unknown_usage_possible'))
    if type(value.get('model_observation'))is dict:
        row=value['model_observation']
        out['model_observation']=scalars(row,('schema','call_id','provider','model','role','profile_id','revision','status','started_at','ended_at'))
        out['model_observation']['metrics']=scalars(row.get('metrics'),('input_tokens','output_tokens','total_tokens','cached_input_tokens','reasoning_output_tokens','usage_source','elapsed_ms','output_tokens_per_second','speed_basis','configured_input_limit','configured_output_limit','configured_history_messages','context_policy','known_model_context_window','known_model_context_window_source'))
    if type(value.get('usage'))is dict:out['usage']=scalars(value['usage'],('model','tool','chars','wall_millis'))
    if type(value.get('step'))is dict:
        s=value['step'];out['step']={**scalars(s,('step','step_id','tool')),'arguments':scalars(s.get('arguments'),('template','fast','slow')),'dataset_reference':scalars(s.get('dataset_reference'),('dataset_id','version','content_hash'))}
        if type(s.get('output'))is list:
            out['step']['output']=[{**scalars(r,('result_id','training_net_profit','fees')),'candidate':scalars(r.get('candidate'),('template','fast','slow'))}for r in s['output'][:12]if type(r)is dict]
        elif type(s.get('output'))is dict:out['step']['output']=scalars(s['output'],('selected_training_result_id','validation_result_id'))
    return out


ACCOUNT_VIEW_BASIS = "MCP_APPROVED_FACTS"
ACCOUNT_SERIES_FIELDS = {
    "equity_curve": ("at", "equity", "contributed_capital", "external_flow", "net_profit",
                     "return_fraction", "daily_pnl", "daily_return", "valuation_status", "cash",
                     "dividend_receivable"),
    "fills": ("at", "ticker", "side", "quantity", "price", "fee"),
}


def account_view(value, facts):
    """Keep searchable attempt facts and structured financial series."""
    out = {**facts, "view_basis": ACCOUNT_VIEW_BASIS}
    for key in ("trial", "validation"):
        if key not in facts:
            continue
        result = value[key].get("result", {})
        out[key] = {**facts[key], "result": {
            name: [scalars(row, fields) for row in result[name] if type(row) is dict]
            for name, fields in ACCOUNT_SERIES_FIELDS.items() if type(result.get(name)) is list
        }}
    return out
