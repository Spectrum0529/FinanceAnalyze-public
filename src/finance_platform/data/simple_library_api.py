"""Optional composition router. API and MCP share DatasetLibrary, no raw paths."""
from .simple_datasets import DatasetLibrary
from .simple_inputs import universe_catalog,resolve_universe,preview_csv
from .contracts import DataError
from predictor.access import AccessPolicy
from finance_platform.core.auth import AccountDelegation
from finance_platform.core.errors import PlatformError
from finance_platform.core.mcp_data_policy import require_dataset, shareable_dataset

_SOURCE_COPY = {
    "csv": ("本地 CSV 文件", "导入你自己的行情文件。导入时请按文件实际情况选择价格类型，例如是否按拆股调整过。"),
    "yfinance": ("Yahoo 财经（yfinance）", "无需密钥，可下载美国股票和 ETF 的每日行情。历史价格已按拆股调整。回测中的持有数量也按拆股调整后的单位计算，收益不计现金分红。"),
    "akshare": ("AKShare 美股（新浪财经）", "无需密钥，可下载部分美国股票的每日行情，此来源不支持 ETF。新浪为方便比较调整过历史价格（前复权）。回测使用这些价格和对应换算后的持有数量，不会再单独加上现金分红。"),
    "akshare_cn": ("AKShare 沪深（东方财富）", "无需密钥，可下载部分沪深股票的人民币每日行情。保留当时的成交价格，未因拆股或分红重新计算。A 页美股组合回测无法使用这类人民币数据。B 页沪深研究仍需核对股票身份、交易日和历史数据够不够。"),
    "massive": ("Massive 美股", "需要在本机保存密钥。可选择保留当时成交价格，或只按拆股调整价格。可下载哪些股票和 ETF、哪些日期，以及用量上限，取决于你的账户权限。"),
    "tushare": ("Tushare Pro 沪深", "需要在本机保存密钥。可下载沪深股票的人民币每日行情，保留当时的成交价格。可下载的股票和日期取决于你的账户权限。"),
    "openbb_cboe": ("OpenBB / Cboe 日线", "需要启用本机可选组件，无需密钥。可以保存和查看来源提供的价格。目前还无法确认这些价格如何处理拆股和分红。"),
    "openbb_nasdaq": ("OpenBB / Nasdaq 日线", "需要启用本机可选组件，无需密钥。可以保存和查看来源提供的价格。目前还无法确认这些价格如何处理拆股和分红。"),
    "wrds": ("WRDS / CRSP 美股日线", "在本机使用，需要 WRDS 账号和 CRSP 数据权限。支持符合要求的美国股票和 ETF。日线保留当时成交价格，本机价格回测不计分红。行情和报告留在本机工作台，不能通过外部连接读取或导出。"),
}

def create_router(runtime,*,library=None):
    from fastapi import APIRouter,Request
    import base64,binascii
    router=APIRouter(prefix='/data/library',tags=['管理数据'])
    if library is None:library=DatasetLibrary(runtime.database,AccessPolicy(runtime.root))
    from .market_lifecycle import require_credential
    from .market_management import MarketManagement, approval
    from .market_management_api import mount_market_routes
    from .source_onboarding import PRESET, add_onboarding_catalog, ensure_yahoo_connection
    management=MarketManagement(library)
    mount_market_routes(router,runtime,library,service=management)
    def actor(request,write=False):
        p=runtime.require_principal(request,write=write);runtime.require_owner(p,p.user_id,write=write);return p
    def owner(request,write=False):
        return actor(request,write).user_id
    def call(fn,*args,**kwargs):
        try:return fn(*args,**kwargs)
        except PlatformError:raise
        except (TypeError,KeyError,ValueError):raise PlatformError('INVALID_INPUT',422) from None
    @router.get('/connections')
    def connections(request:Request):return library.list_connections(owner(request))
    @router.get('/providers')
    def providers(request:Request):
        owner_id = owner(request)
        from .native_library_download import provider_capabilities
        result=provider_capabilities()
        from .openbb_source import capability as openbb_source_capability
        result['items'].append(openbb_source_capability(enabled=getattr(runtime,'openbb_cboe_enabled',False),runtime_binding=getattr(runtime,'openbb_runtime_binding',None)))
        from .goal_data.goal_data import CAPABILITIES
        registered=getattr(runtime,'goal_provider_status',{})
        for row in result['items']:
            capability=CAPABILITIES.get(row['provider'])
            if capability is not None:
                row['data_capability']=capability.view(runtime_ready=row['provider'] in registered)
        catalog = {row['provider']: row for row in runtime.goal_provider_catalog(owner_id)}
        for provider in ('akshare_cn','tushare','openbb_nasdaq'):
            capability=CAPABILITIES[provider]
            result['items'].append({'provider':provider,'credentials_required':capability.credentials,
                'available':catalog[provider]['availability']=='BOUND_UNVERIFIED',
                'status':catalog[provider]['availability'],
                'data_capability':catalog[provider]})
        from .wrds_library import capability as wrds_capability
        result['items'].append(wrds_capability(runtime))
        for row in result['items']:
            display = _SOURCE_COPY.get(row['provider'])
            if display:
                row['display_name'], row['display_summary'] = display
        service=getattr(runtime,'dataset_research_exports',None)
        result['download_service_registered']=callable(getattr(runtime,'dataset_download_submit',None))
        result['universe_import_registered']=callable(getattr(runtime,'dataset_universe_submit',None))
        result['research_exports']=service.capabilities() if service else {'formats':[],'status':'SERVICE_UNREGISTERED'}
        return add_onboarding_catalog(result,check_registered=callable(getattr(runtime,'connection_check_submit',None)))
    @router.post('/connections/ensure')
    def ensure_connection(request:Request,body:dict):
        principal=actor(request,True)
        if set(body)!={'preset','idempotency_key'} or body['preset']!=PRESET:raise DataError('INVALID_INPUT')
        return call(ensure_yahoo_connection,library,principal,body['idempotency_key'])
    @router.post('/connections/{ident}/check')
    def check_saved_connection(request:Request,ident:str,body:dict):
        principal=actor(request,True)
        submit=getattr(runtime,'connection_check_submit',None)
        if not callable(submit):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return call(submit,principal,ident,body)
    @router.get('/connections/{ident}/check')
    def read_saved_connection_check(request:Request,ident:str,job_id:str):
        principal=actor(request)
        read=getattr(runtime,'connection_check_read',None)
        if not callable(read):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return call(read,principal,ident,job_id)
    @router.post('/connections')
    def save_connection(request:Request,body:dict):
        principal=actor(request,True)
        config=body['connection']
        guard=approval(principal)
        def check(con):
            guard(con)
            provider,reference=config['provider'],config['credential_ref']
            if provider=='wrds':raise PlatformError('MCP_LOCAL_DATA_ONLY',403)
            if reference is not None:
                if provider not in {'massive','tushare'}:raise DataError('MARKET_CONNECTION_CREDENTIAL_UNSUPPORTED')
                require_credential(runtime,principal.user_id,provider,reference)
        return call(library.save_connection,principal.user_id,config,body['idempotency_key'],
                    publication_guard=check if guard is not None else None)
    @router.post('/connections/check')
    def check_connection(request:Request,body:dict):
        p=runtime.require_principal(request,write=True);runtime.require_owner(p,p.user_id,write=True)
        if not callable(getattr(runtime,'massive_check_submit',None)):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return call(runtime.massive_check_submit,p,body)
    @router.get('/downloads/{job_id}/progress')
    def download_progress(request:Request,job_id:str):
        user=owner(request)
        if not callable(getattr(runtime,'dataset_download_progress',None)):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return call(runtime.dataset_download_progress,user,job_id)
    @router.post('/wrds/securities/lookup')
    def wrds_lookup(request:Request,body:dict):
        p=runtime.require_principal(request,write=True);runtime.require_owner(p,p.user_id,write=True)
        if not callable(getattr(runtime,'wrds_lookup_submit',None)):raise DataError('WRDS_UNAVAILABLE')
        return call(runtime.wrds_lookup_submit,p,body)
    @router.get('/universes')
    def groups(request:Request):return library.universe_catalog(owner(request))
    @router.post('/universes/import')
    def import_roster(request:Request,body:dict):
        p=runtime.require_principal(request,write=True);runtime.require_owner(p,p.user_id,write=True)
        if not callable(getattr(runtime,'dataset_universe_submit',None)):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return call(runtime.dataset_universe_submit,p,body)
    @router.post('/universes/resolve')
    def selection(request:Request,body:dict):return call(library.resolve_universe,owner(request),body)
    @router.post('/import-preview')
    def preview(request:Request,body:dict):
        user=owner(request,True)
        if type(body.get('content_base64'))is not str or len(body['content_base64'])>1400000:raise PlatformError('INVALID_INPUT',422)
        try:raw=base64.b64decode(body['content_base64'],validate=True)
        except (ValueError,binascii.Error):raise PlatformError('INVALID_INPUT',422) from None
        result=call(preview_csv,raw,body.get('options'))
        if body.get('idempotency_key'):result['original_upload']=call(library.upload_csv,user,raw,body['idempotency_key'])
        return result
    @router.post('/import')
    def submit(request:Request,body:dict):
        p=runtime.require_principal(request,write=True);runtime.require_owner(p,p.user_id,write=True)
        # Provider work must use existing jobs; no download happens on request threads.
        if not callable(getattr(runtime,'dataset_import_submit',None)):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return runtime.dataset_import_submit(p,body)
    @router.get('/datasets')
    def datasets(request:Request,trash:bool=False,kind:str='dataset',limit:int=50,offset:int=0):
        principal=runtime.require_principal(request)
        page=call(library.list_datasets,principal.user_id,trash=trash,kind=kind,limit=limit,offset=offset)
        if isinstance(principal,AccountDelegation):
            page['items']=[record if shareable_dataset(record) else {'availability':'LOCAL_ONLY'}
                           for record in page['items']]
        return page
    @router.post('/download')
    def download(request:Request,body:dict):
        p=runtime.require_principal(request,write=True);runtime.require_owner(p,p.user_id,write=True)
        if not callable(getattr(runtime,'dataset_download_submit',None)):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return call(runtime.dataset_download_submit,p,body)
    @router.get('/benchmarks')
    def benchmarks(request:Request):
        from .benchmark_sources import presets
        result=library.list_datasets(owner(request),kind='benchmark')
        available='yfinance' in getattr(runtime,'goal_provider_status',{})
        result['presets']=[{**item,'available':available} for item in presets()]
        return result
    @router.get('/datasets/{ident}')
    def dataset(request:Request,ident:str):
        principal=runtime.require_principal(request)
        record=call(library.get,principal.user_id,ident,selectable=True)
        if isinstance(principal,AccountDelegation):require_dataset(record)
        return record
    @router.post('/datasets/{ident}/research-export')
    def research_export(request:Request,ident:str,body:dict):
        p=runtime.require_principal(request,write=True);runtime.require_owner(p,p.user_id,write=True)
        if not callable(getattr(runtime,'dataset_research_export_submit',None)):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        if type(body.get('request'))is not dict or type(body['request'].get('reference'))is not dict or body['request']['reference'].get('dataset_id')!=ident:raise DataError('RESEARCH_EXPORT_SCHEMA')
        return call(runtime.dataset_research_export_submit,p,body)
    @router.get('/exports/{job_id}/page')
    def research_export_page(request:Request,job_id:str,offset:int=0,limit:int=65536):
        if not callable(getattr(runtime,'dataset_research_export_page',None)):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return call(runtime.dataset_research_export_page,owner(request),job_id,offset=offset,limit=limit)
    @router.get('/exports/{job_id}/content')
    def research_export_content(request:Request,job_id:str):
        from fastapi.responses import Response
        if not callable(getattr(runtime,'dataset_research_export_content',None)):raise PlatformError('DATASET_IMPORT_JOB_UNCONFIGURED',409)
        return Response(call(runtime.dataset_research_export_content,owner(request),job_id),media_type='application/json',headers={'Content-Disposition':'attachment; filename="research-input.json"'})
    @router.get('/datasets/{ident}/original')
    def original(request:Request,ident:str):
        from fastapi.responses import Response
        return Response(call(library.original_csv,owner(request),ident),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="source.csv"'})
    @router.post('/datasets/{ident}/trash')
    def trash(request:Request,ident:str,body:dict):
        return call(management.legacy_set_trash,actor(request,True),ident,True,body['idempotency_key'])
    @router.post('/datasets/{ident}/restore')
    def restore(request:Request,ident:str,body:dict):
        return call(management.legacy_set_trash,actor(request,True),ident,False,body['idempotency_key'])
    # Reference pins are internal result-service writes, never arbitrary client claims.
    return router
