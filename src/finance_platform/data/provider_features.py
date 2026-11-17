"""Preflight contracts; catalogue readiness never means provider execution PASS."""
from datetime import date,timedelta
from .contracts import DataError

def validate_native_request(p,provider):
    if provider not in {'yfinance','akshare'}:raise DataError('PROVIDER_UNSUPPORTED')
    if 'membership' in p:raise DataError('NATIVE_MEMBERSHIP_UNSUPPORTED')
    symbols=p['universe']['tickers']
    if not 1<=len(symbols)<=32:raise DataError('NATIVE_SYMBOL_LIMIT')
    low,high=date.fromisoformat(p['start']),date.fromisoformat(p['end'])
    from .provider_sdk.history_contracts import history_window,symbol
    try:history_window(p['start'],(high+timedelta(days=1)).isoformat())
    except OverflowError:raise DataError('SDK_DATE_RANGE_VIOLATION') from None
    if any(symbol(t)!=t for t in symbols):raise DataError('SECURITY_SYMBOL_INVALID')
    expected={'currency':'USD','timezone':'America/New_York','price_unit':'per_adjusted_simulation_unit',
              'volume_unit':'provider_reported_unverified','adjustment':'qfq' if provider=='akshare' else 'split_adjusted'}
    if p['metadata'] is not None and p['metadata']!=expected:raise DataError('NATIVE_PRICE_BASIS_MISMATCH')
    if provider=='akshare':
        from .provider_sdk.history_contracts import sina_symbol
        if any(t in {'AI','CIEN'} for t in symbols):raise DataError('SINA_KNOWN_FACTOR_QUALITY_UNSUPPORTED')
        if any(not sina_symbol(t) for t in symbols):raise DataError('SINA_SYMBOL_MAPPING_UNVERIFIED')
    return expected
