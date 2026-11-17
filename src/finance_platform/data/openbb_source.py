"""Optional Cboe source contracts and existing Windows SDKRunner composition."""
from datetime import date,timedelta
from importlib.metadata import distributions
import json,re
from pathlib import Path
from .contracts import DataError

PROVIDER='openbb_cboe'
META={'currency':'USD','timezone':'America/New_York','price_unit':'per_provider_observed_unit',
      'volume_unit':'provider_reported_unverified','adjustment':'provider_observed_unverified'}

def capability(*,enabled=False,runtime_binding=None):
    if type(enabled)is not bool:raise DataError('OPENBB_FEATURE_CONFIGURATION_INVALID')
    installed={}
    expected={'openbb-core':'2.0.1','openbb-cboe':'2.0.0'}
    bound=type(runtime_binding)is dict and runtime_binding.get('schema_version')=='sdk-direct-base-runtime-binding-1' and runtime_binding.get('expected_version')==[3,12,14] and type(runtime_binding.get('site_root'))is str
    if enabled and bound:
        try:installed={dist.metadata['Name']:dist.version for dist in distributions(path=[runtime_binding['site_root']]) if dist.metadata['Name'] in expected}
        except (OSError,ValueError):installed={}
    state=('DISABLED' if not enabled else 'RUNTIME_UNBOUND' if not bound else 'DEPENDENCY_MISSING' if set(installed)!=set(expected) else 'VERSION_UNSUPPORTED' if installed!=expected else 'ENABLED_RUNTIME_UNVERIFIED')
    return {'provider':PROVIDER,'label':'OpenBB / Cboe 日线','credentials_required':False,'credentials_allowed':False,
            'enabled':enabled,'available':state=='ENABLED_RUNTIME_UNVERIFIED','status':state,'installed':installed,'required_dependencies':expected,
            'assets':['US_EQUITY','US_ETF'],'max_symbols':16,'maximum_calendar_days':11346,'metadata':dict(META),
            'endpoint':'https://cdn.cboe.com','interval':'1d','transport':'EXISTING_ADMITTED_WINDOWSJOb_SDK_CHILD_FIXED_PINNED_PUBLIC_HTTPS',
            'sdk':'CboeEquityHistoricalFetcher.aextract_data/transform_data','raw_compatible':False,'qlib_raw_compatible':False,
            'backtest_mode':'OBSERVED_PRICE_RESEARCH_ONLY','adjustment_status':'UNVERIFIED_DO_NOT_RELABEL_RAW',
            'resume':{'unit':'COMPLETE_SYMBOL','attempts_maximum':8,'sdk_units_maximum':288,'seconds_per_attempt':1800},
            'physical_limits_per_child':{'seconds':45,'requests':8,'response_bytes':2097152,'unit_bytes':8388608},
            'provider_validation':'NOT_RUN','history_coverage':'ONLY_PROVIDER_RESPONSE_NO_MARKET_COMPLETENESS_CLAIM'}

def require_enabled(*,enabled,runtime_binding=None):
    state=capability(enabled=enabled,runtime_binding=runtime_binding)
    if not state['enabled']:raise DataError('OPENBB_FEATURE_DISABLED')
    if state['status']=='RUNTIME_UNBOUND':raise DataError('OPENBB_RUNTIME_UNBOUND')
    if state['status']=='DEPENDENCY_MISSING':raise DataError('OPENBB_DEPENDENCY_MISSING')
    if not state['available']:raise DataError('OPENBB_VERSION_UNSUPPORTED')
    return state

def validate_window_symbol(ticker,start,end):
    from .provider_sdk.history_contracts import history_window
    if type(ticker)is not str or re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,15}',ticker)is None or ticker in {'NDX','RUT'}:raise DataError('SECURITY_SYMBOL_INVALID')
    try:history_window(start,(date.fromisoformat(end)+timedelta(days=1)).isoformat())
    except OverflowError:raise DataError('SDK_DATE_RANGE_VIOLATION') from None

def validate_request(p):
    if 'membership' in p:raise DataError('NATIVE_MEMBERSHIP_UNSUPPORTED')
    if not 1<=len(p['universe']['tickers'])<=16:raise DataError('NATIVE_SYMBOL_LIMIT')
    for ticker in p['universe']['tickers']:validate_window_symbol(ticker,p['start'],p['end'])
    if p['metadata'] is not None and p['metadata']!=META:raise DataError('OPENBB_PRICE_BASIS_MISMATCH')

def gateway_and_runner(runtime):
    from .provider_sdk.bootstrap import runtime_components
    from .history_gateway import HistoryGateway
    service,identity_runner=runtime_components(runtime)
    binding=runtime.openbb_runtime_binding
    require_enabled(enabled=runtime.openbb_cboe_enabled,runtime_binding=binding)
    pins=json.loads(Path(__file__).with_name('provider_sdk').joinpath('openbb_pins.json').read_text(encoding='utf-8'))
    from .provider_sdk.runner import SDKRunner
    runner=SDKRunner(identity_runner.policy,python=binding['venv_python']['path'],pins=pins,source_root=identity_runner.source_root,runtime_binding=binding)
    return HistoryGateway(service,identity_runner),runner
