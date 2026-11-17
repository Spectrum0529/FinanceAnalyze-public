"""Narrow source/snapshot admission for the exact native team adapter.

This grants no network, process, file or SQLite permission. It does not accept
arbitrary Runnable implementations, provider clients or class-name matches.
"""
import hashlib
import json
from pathlib import Path

ROLES = frozenset(('market_analyst', 'social_analyst', 'news_analyst', 'fundamentals_analyst',
    'bull_researcher', 'bear_researcher', 'research_manager', 'trader', 'risky_analyst',
    'safe_analyst', 'neutral_analyst', 'risk_manager'))


def fail():
    raise ValueError('TA_EXACT_TEAM_SOURCE_SNAPSHOT_REQUIRED')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def offline_choice(value):
    """Only legacy four-field or canonical six-field deterministic choices."""
    old={'provider':'offline','model':'deterministic-v1','effort':None,'secret_ref':None}
    return type(value)is dict and (value==old or value=={**old,'endpoint_profile':None,'native_parameters':{}})


def approved_team(value,profile,approval):
    if approval is not None and (type(approval)is not dict
            or digest({key:item for key,item in approval.items() if key!='execution_sha256'})!=approval.get('execution_sha256')
            or approval.get('approved_profile')!={key:profile[key] for key in ('profile_id','profile_sha256')}
            or approval.get('approve_source_transfer') is not True
            or value['configuration']['execution']['retry']!=0):
        fail()
    metered=False
    for resolved in value['resolved_roles'].values():
        choice=resolved['model'];target=resolved.get('execution_target')
        if offline_choice(choice) and target is None:
            continue
        if (approval is None or choice.get('provider')!='openai' or choice.get('endpoint_profile') is not None
                or choice.get('native_parameters') or not (choice.get('secret_ref') or '').startswith('credential:')
                or type(target)is not dict or target.get('provider')!='openai'
                or target.get('endpoint_id') is not None or target.get('base_url')!='https://api.openai.com/v1'):
            fail()
        metered=True
    if (approval is not None)!=metered:
        fail()


def snapshot(packet):
    request = packet['admission']['request']
    bound = request['binding']
    value = request['input_bundle'].get('team_snapshot')
    profile = bound['model_profile']
    if (type(value) is not dict or value.get('owner_id') != bound['owner_id']
            or profile != {'mode': 'SERVER_PROFILE', 'profile_id': 'team-' + str(value.get('snapshot_id')), 'profile_sha256': value.get('sha256')}
            or type(value.get('resolved_roles')) is not dict or set(value['resolved_roles']) != ROLES
            or digest({key: value[key] for key in ('configuration', 'resolved_roles')}) != value.get('sha256')):
        fail()
    approval=request['input_bundle'].get('provider_approval')
    approved_team(value,profile,approval)
    if approval is not None and (approval.get('options')!=bound['options']
            or approval.get('input_hash')!=digest({key:item for key,item in request['input_bundle'].items()
                                                  if key!='provider_approval'})):
        fail()
    return value


def source(code, module, filename, sources, helpers, site):
    if code is None or not Path(code.co_filename).is_absolute():
        fail()
    path = str(Path(code.co_filename).absolute())
    row = sources.get(path)
    if row is None or Path(path).name != filename or row.get('installed_dependency'):
        fail()
    helpers.verify_source(row, site)
    return row


def model(instance, packet, sources, helpers, role=None):
    value = snapshot(packet)
    bound = packet['admission']['request']['binding']
    cls = type(instance)
    function = getattr(cls, '_generate', None)
    code = getattr(function, '__code__', None)
    if (cls.__module__ != 'finance_platform.research.team_models' or code is None
            or function.__globals__.get('__name__') != cls.__module__):
        fail()
    source(code, cls.__module__, 'team_models.py', sources, helpers, packet['site_root'])
    closure = dict(zip(code.co_freevars, (cell.cell_contents for cell in function.__closure__ or ())))
    selected=closure.get('role')
    if selected not in ROLES or role is not None and role!=selected:
        fail()
    if offline_choice(value['resolved_roles'][selected]['model']):
        if (cls.__name__!='DeterministicChatModel' or code.co_qualname!='offline_model.<locals>.DeterministicChatModel._generate'
                or closure.get('as_of')!=bound['as_of'] or closure.get('view_hash')!=bound['selected_view_hash']):
            fail()
    else:
        exchange=closure.get('exchange')
        exchange_code=getattr(exchange,'__code__',None)
        source_row=packet['pins']['child']
        if (cls.__name__!='BrokerChatModel' or code.co_qualname!='broker_model.<locals>.BrokerChatModel._generate'
                or exchange_code is None or exchange_code.co_qualname!='run.<locals>.ProductControls.invoke_provider'
                or Path(exchange_code.co_filename).absolute()!=Path(source_row['path']).absolute()
                or getattr(getattr(exchange,'__self__',None),'packet',None) is not packet):
            fail()
        helpers.verify_pin(source_row)
    return True


def runnable(frame, instance, packet, sources, helpers):
    value = snapshot(packet)
    cls = type(instance)
    if cls.__module__ != 'finance_platform.research.team_execution' or cls.__name__ != 'RoleRunnable':
        fail()
    constructor = frame.f_code.co_name == '__init__'
    function = getattr(cls, '__init__' if constructor else 'invoke', None)
    code = getattr(function, '__code__', None)
    if (code is None or frame.f_code is not code or code.co_qualname not in {'RoleRunnable.__init__', 'RoleRunnable.invoke'}
            or function.__globals__.get('__name__') != cls.__module__):
        fail()
    source(code, cls.__module__, 'team_execution.py', sources, helpers, packet['site_root'])
    execution = frame.f_locals.get('execution') if constructor else getattr(instance, 'execution', None)
    if (type(execution).__module__ != cls.__module__ or type(execution).__name__ != 'TeamExecution'
            or getattr(execution, 'owner_id', None) != value['owner_id'] or getattr(execution, 'snapshot', None) != value):
        fail()
    if set(getattr(execution, 'models', {})) != ROLES:
        fail()
    for role,item in execution.models.items():
        model(item, packet, sources, helpers,role)
    return True


def fallback_accounting(refusals, trace):
    """Denied probes need consistency, not mandatory execution on every import."""
    if type(refusals) is not list or type(trace) is not dict:
        return False
    none = {'native_getwindowsversion_calls': 0, 'syscmd_default_returns': 0, 'native_fallback_returns': 0}
    full = {'native_getwindowsversion_calls': 1, 'syscmd_default_returns': 1, 'native_fallback_returns': 1}
    if set(trace) != set(none) or any(type(value) is not int for value in trace.values()):
        return False
    return (len(refusals) == 0 and trace == none) or (len(refusals) == 3 and trace == full)
