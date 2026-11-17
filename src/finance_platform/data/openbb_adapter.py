"""Export exact accepted official Cboe model snapshots, no SDK import in Main.

The capture child actually invoked official models. Reopen validates original
raw values, immutable SDK frame hashes and decimal/float correspondence. The
original decimal finance package remains authoritative for calculations.
"""
from hashlib import sha256
import json
from .contracts import DataError
FORMAT='finance-openbb-equity-input-1'

def capability(*,enabled=False):
    if type(enabled)is not bool:raise DataError('OPENBB_FEATURE_CONFIGURATION_INVALID')
    return {'format':FORMAT,'enabled':enabled,'available':enabled,'status':'ENABLED_FOR_ACCEPTED_CBOE_SNAPSHOTS' if enabled else 'DISABLED',
            'source_provider_required':'openbb_cboe','operation':'VERIFIED_OFFICIAL_MODEL_SNAPSHOT_EXPORT',
            'capture_dependencies':{'openbb-core':'2.0.1','openbb-cboe':'2.0.0'},'requires_main_openbb_install':False,'validation':'NOT_RUN'}

def require_enabled(*,enabled):
    if not capability(enabled=enabled)['available']:raise DataError('OPENBB_FEATURE_DISABLED')

def adapt(package,*,library,record,owner,enabled,context):
    from .research_inputs import verify_research_input,MAX_EXPORT_BYTES
    from .openbb_library_download import export_official_rows
    require_enabled(enabled=enabled);verify_research_input(package);context.require_active()
    window=package['payload']['window']
    rows=export_official_rows(library,owner,record,start=window['start'],end=window['end'],context=context)
    if len(rows)!=len(package['payload']['rows']) or [(r['symbol'],r['date']) for r in rows]!=[(r['ticker'],r['session_date']) for r in package['payload']['rows']]:raise DataError('OPENBB_TRANSFORM_MISMATCH')
    result={'format':FORMAT,'official_model':'openbb_cboe.models.equity_historical.CboeEquityHistoricalData',
            'capture_dependency_versions':{'openbb-core':'2.0.1','openbb-cboe':'2.0.0'},'source_package':package,'rows':rows,
            'metadata':package['payload']['metadata'],'price_basis':'UNCHANGED_SOURCE_METADATA',
            'numeric_policy':'OPENBB_FLOAT_INTEROPERABILITY_VIEW_ORIGINAL_DECIMAL_SOURCE_AUTHORITATIVE',
            'pit_status':'UNVERIFIED','provider_network':'NOT_INVOKED_DURING_EXPORT'}
    raw=json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(raw)>MAX_EXPORT_BYTES:raise DataError('RESEARCH_INPUT_LIMIT')
    context.require_active()
    return raw,{'format':FORMAT,'sha256':sha256(raw).hexdigest(),'bytes':len(raw),'rows':len(rows)}
