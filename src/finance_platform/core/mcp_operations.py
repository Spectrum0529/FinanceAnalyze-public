"""Curated business operations, never an arbitrary HTTP/path/code proxy."""
from dataclasses import dataclass, replace
from typing import Literal
from pydantic import Field,create_model
from .team_profiles import Strict,TeamConfiguration
from .model_library_api import SaveProfile,UpdateProfile,CopyProfile,ComposeTeam
from .mcp_domain_models import ConnectionSave,Universe,CSVPreview,DatasetImport,DatasetDownload,Portfolio,TaskPlan,TaskStart,CredentialWrite,Schedule,NativeDebate,NativeQlib,DailyBacktest,DailyLifecycle,StrategyTemplate,StrategyConfig,HistoryDownload,SimulationStart,VersionReference
from .mcp_domain_models import (
    ConnectionLifecycle, MarketLifecycle, MarketMerge, MarketMergePreview, MarketReuse, PublicDatasetDownload,
    WorkbenchPortfolio,
)
from .result_lifecycle import BatchLifecycleCommand, LifecycleCommand
from .onboarding_example import ExampleAction, ExampleStart

class Empty(Strict):pass
class Page(Strict):
    limit:int=Field(default=50,ge=1,le=100)
    offset:int=Field(default=0,ge=0,le=10000)
class Key(Strict):idempotency_key:str=Field(min_length=1,max_length=128)
class ProfileSave(Strict):body:SaveProfile
class ProfileUpdate(Strict):body:UpdateProfile
class ProfileCopy(Strict):body:CopyProfile
class TeamCompose(Strict):body:ComposeTeam
class Preview(Strict):configuration:TeamConfiguration

@dataclass(frozen=True)
class Operation:
    name:str
    description:str
    method:str
    path:str
    input_model:object
    path_fields:tuple=()
    query_fields:tuple=()
    body_field:str|None=None
    body_rest:bool=False
    idempotent:bool=False
    destructive:bool=False
    external:bool=False
    response_format:str='json'

def model(schema_name,*,base=Empty,**fields):return create_model(schema_name,__base__=base,**fields)
ID=(str,Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$'))
CONSENT_FIELDS = {
    "max_model_calls": (int | None, Field(default=None, ge=1, le=32)),
    "max_input_tokens": (int | None, Field(default=None, ge=256, le=2097152)),
    "max_output_tokens": (int | None, Field(default=None, ge=64, le=262144)),
}

def operations():
    out=[]
    def add(name,desc,method,path,kind=Empty,**kwargs):
        out.append(Operation(name,desc,method,path,kind,**kwargs))
    from .computation_cache_api import CacheClear,CacheSettings
    add('computation_cache_get','查看本人的计算缓存开关、容量、统计与适用范围。','GET','/computation-cache')
    add('computation_cache_settings','按预期版本设置本人的计算缓存开关和有效字节上限。','PUT','/computation-cache',CacheSettings,body_rest=True,idempotent=True)
    add('computation_cache_clear','确认清理本人可重建计算缓存。SQLite 可复用释放空间。','POST','/computation-cache/clear',CacheClear,body_rest=True,idempotent=True,destructive=True)
    from .mcp_domain_models import StorageLocationCheck,StorageLocationSave
    add('storage_locations_get','查看三个分类的当前、默认存储位置及生效范围；不读取目录内容。','GET','/storage-locations')
    add('storage_location_check','检查指定分类的 Windows 保存目录；仅运行服务内有界探测，不列举或读取任意文件。','POST','/storage-locations/{category}/check',StorageLocationCheck,path_fields=('category',),body_rest=True)
    add('storage_location_save','按预期版本保存指定分类位置；directory=null 恢复默认，仅影响新分配，旧数据不搬不删。','PUT','/storage-locations/{category}',StorageLocationSave,path_fields=('category',),body_rest=True,idempotent=True)
    add('application_capabilities','查看当前已支持的功能、限制与运行状态。','GET','/capabilities')
    add('application_overview','查看本地应用概览。','GET','/overview')
    add('model_capabilities','列出供应商、模型与已验证参数；未知推理档位不可使用。','GET','/model-profiles/capabilities')
    add('model_profiles_list','查看自己的模型配置库。','GET','/model-profiles',Page,query_fields=('limit','offset'))
    add('model_profile_create','新增独立模型配置；只保存密钥引用。相同调用标识不重复创建。','POST','/model-profiles',ProfileSave,body_field='body',idempotent=True)
    for name,desc,method,path,base in (
        ('model_profile_read','查看模型配置。','GET','/model-profiles/{profile_id}',Empty),
        ('model_profile_update','按预期版本编辑配置；旧运行不变。','PUT','/model-profiles/{profile_id}',ProfileUpdate),
        ('model_profile_copy','复制指定版本为独立配置。','POST','/model-profiles/{profile_id}/copy',ProfileCopy)):
        add(name,desc,method,path,model(name+'Input',base=base,profile_id=ID),path_fields=('profile_id',),body_field=None if method=='GET' else 'body',idempotent=method!='GET')
    add('model_profile_revision','查看历史配置的不可变版本。','GET','/model-profiles/{profile_id}/revisions/{revision}',model('ProfileRevisionInput',profile_id=ID,revision=(int,Field(ge=1))),path_fields=('profile_id','revision'))
    add('model_profile_delete','从配置库移除；保留被旧运行引用的历史版本。','DELETE','/model-profiles/{profile_id}',model('DeleteProfileInput',base=Key,profile_id=ID,expected_revision=(int,Field(ge=1))),path_fields=('profile_id',),query_fields=('expected_revision','idempotency_key'),idempotent=True,destructive=True)
    add('team_compose_from_profiles','明确选择多角色团队模式，将一个或多个独立模型配置复制入团队。单个模型会承担多个角色。','POST','/model-profiles/compose-team',TeamCompose,body_field='body',idempotent=True)
    add('team_capabilities','查看固定团队角色和模型能力。','GET','/agent-teams/capabilities')
    add('team_preview','校验团队并预览每个角色最终模型、提示词与预算。','POST','/agent-teams/preview',Preview,body_rest=True,idempotent=True)
    add('team_presets_list','查看自己的已保存团队。','GET','/agent-teams/presets',Page,query_fields=('limit','offset'))
    team_save=model('TeamSaveInput',base=Key,name=(str,Field(min_length=1,max_length=80)),configuration=(TeamConfiguration,...))
    add('team_preset_create','保存新的团队配置；相同标识不重复创建。','POST','/agent-teams/presets',team_save,body_rest=True,idempotent=True)
    add('team_preset_read','查看已保存团队。','GET','/agent-teams/presets/{preset_id}',model('TeamReadInput',preset_id=ID),path_fields=('preset_id',))
    add('team_preset_update','按预期版本更新团队，历史快照保持不变。','PUT','/agent-teams/presets/{preset_id}',model('TeamUpdateInput',base=team_save,preset_id=ID,expected_revision=(int,Field(ge=1))),path_fields=('preset_id',),body_rest=True,idempotent=True)
    add('team_preset_copy','复制指定团队版本为独立配置。','POST','/agent-teams/presets/{preset_id}/copy',model('TeamCopyInput',base=Key,preset_id=ID,revision=(int,Field(ge=1)),name=(str,Field(min_length=1,max_length=80))),path_fields=('preset_id',),body_rest=True,idempotent=True)
    add('team_preset_delete','从团队列表移除，历史运行引用保留。','DELETE','/agent-teams/presets/{preset_id}',model('TeamDeleteInput',base=Key,preset_id=ID,expected_revision=(int,Field(ge=1))),path_fields=('preset_id',),query_fields=('expected_revision','idempotency_key'),destructive=True,idempotent=True)
    add('team_snapshot_create','将指定团队版本固定为不可变运行输入。','POST','/agent-teams/snapshots',model('SnapshotInput',base=Key,preset_id=ID,revision=(int,Field(ge=1))),body_rest=True,idempotent=True)
    add('team_snapshot_read','查看固定团队快照及摘要。','GET','/agent-teams/snapshots/{snapshot_id}',model('SnapshotReadInput',snapshot_id=ID),path_fields=('snapshot_id',))
    add('results_list','汇总全部已保存报告，功能分类引用同一份结果。','GET','/results',model('ResultsPage',base=Page,kind=(Literal['all','backtest','research','ai','model'],'all')),query_fields=('kind','limit','offset'))
    add('result_read','按原权限读取报告摘要与完整分区清单；大曲线或长文本明确分页，不静默截断。','GET','/results/{result_id}/summary',model('ResultInput',result_id=ID),path_fields=('result_id',))
    add('result_export','取得完整分页导出清单；按源摘要和next_offset读取所有分区，不接受本机文件路径。','GET','/results/{result_id}/export-manifest',model('ExportResultInput',result_id=ID),path_fields=('result_id',))
    add(
        'result_section',
        '按摘要清单分页读取报告。每页列表或对象最多 1000 项，文本最多 8192 字符，@ 开头的原生序列最多 500 项。',
        'GET', '/results/{result_id}/series',
        model(
            'ResultSectionInput', result_id=ID,
            section=(str, Field(default='', max_length=1024,
                description='摘要清单中的 JSON 指针或 @ 开头的原生序列名。空串读取根对象。')),
            limit=(int, Field(default=500, ge=1, le=8192,
                description='列表或对象最多 1000 项，文本最多 8192 字符，@ 原生序列最多 500 项。')),
            offset=(int, Field(default=0, ge=0, le=2000000)),
            expected_sha256=(str | None, None),
        ),
        path_fields=('result_id',), query_fields=('section', 'limit', 'offset', 'expected_sha256'),
    )
    add('jobs_list','查看自己的任务状态。','GET','/jobs',Page,query_fields=('limit','offset'))
    for name,desc,path in (('job_status','查看实际进度、失败原因和结果标识。','/jobs/{job_id}'),('job_events','查看任务真实事件。','/jobs/{job_id}/events')):
        add(name,desc,'GET',path,model(name+'Input',base=Page if name=='job_events'else Empty,job_id=ID),path_fields=('job_id',),query_fields=('limit','offset')if name=='job_events'else())
    add('job_cancel','请求停止本人的任务；不代表已经保存断点。','POST','/jobs/{job_id}/cancel',model('CancelJobInput',job_id=ID),path_fields=('job_id',),idempotent=True)
    add('job_retry','明确重新执行已失败或取消的任务；不声称从断点恢复。','POST','/jobs/{job_id}/retry',model('RetryJobInput',base=Key,job_id=ID),path_fields=('job_id',),body_rest=True,idempotent=True,external=True)
    add('job_verified_resume','仅在真实封存断点可用时继续；无断点拒绝，不静默变为新运行。','POST','/jobs/{job_id}/resume',model('ResumeJobInput',base=Key,job_id=ID),path_fields=('job_id',),body_rest=True,idempotent=True,external=True)
    from .mcp_domain_models import WrdsHistory, WrdsLookup
    add("data_provider_catalog", "查看已登记的数据能力及验证状态。", "GET", "/data/library/providers")
    add("data_wrds_security_lookup", "提交固定日期的 WRDS 证券查询，返回任务 ID。",
        "POST", "/data/library/wrds/securities/lookup", WrdsLookup,
        body_rest=True, idempotent=True, external=True)
    add("data_wrds_history_download", "用已有连接和证券标识下载有界历史行情，返回任务 ID。",
        "POST", "/data/library/download", WrdsHistory,
        body_rest=True, idempotent=True, external=True)
    # Owners' exact API bodies remain validated by the same product services.
    # Each tool is scoped to a named domain and a fixed reviewed route.
    domain=[
      ('data_connections_list','查看数据供应商连接（不返回密钥）。','GET','/data/library/connections',Empty,(),()),
      ('data_universes_list','查看带来源日期的股票快捷分类。','GET','/data/library/universes',Empty,(),()),
      ('data_datasets_list','查看已保存或回收站数据集。','GET','/data/library/datasets',model('DatasetsPage',base=Page,trash=(bool,False),kind=(Literal['dataset','benchmark'],'dataset')),(),('limit','offset','trash','kind')),
      ('data_dataset_read','查看数据集版本、覆盖、质量和来源，不读取底层任意文件。','GET','/data/library/datasets/{dataset_id}',model('DatasetInput',dataset_id=ID),('dataset_id',),()),
      ('benchmarks_list','查看本地保存的基准输入版本。','GET','/data/library/benchmarks',Page,(),('limit','offset')),
      ('backtest_catalog','查看支持的数据集回测策略与明确限制。','GET','/research/portfolio/catalog',Empty,(),()),
      ('daily_backtest_catalog','查看现有日线回测能力。','GET','/research/daily/catalog',Empty,(),()),
      ('strategies_list','查看已保存策略。','GET','/strategies',Page,(),('limit','offset')),
      ('strategy_configs_list','查看已保存策略配置。','GET','/strategies/configs',Page,(),('limit','offset')),
      ('strategy_instances_list','查看策略实例。','GET','/strategies/instances',Page,(),('limit','offset')),
      ('schedules_list','查看当前本地产品任务计划。','GET','/schedules',Page,(),('limit','offset')),
    ]
    for name,desc,method,path,kind,paths,queries in domain:add(name,desc,method,path,kind,path_fields=paths,query_fields=queries)
    for name,desc,path,body in (
        ('data_connection_create','新增供应商连接，仅保存凭据引用。','/data/library/connections',ConnectionSave),
        ('data_universe_resolve','合并快捷分类与手动代码，去重并应用排除。','/data/library/universes/resolve',Universe),
        ('data_csv_preview','识别用户提供CSV并指出真正需要补充的映射。','/data/library/import-preview',CSVPreview),
        ('data_dataset_import','将已提供的数据固定为新的数据集版本，立即返回任务ID。','/data/library/import',DatasetImport),
        ('data_provider_download','使用已配置连接下载行情或解析有日期的QQQ/行业候选名单，返回任务ID；不接受密钥或任意URL。','/data/library/download',DatasetDownload),
        ('backtest_start','使用已保存数据集和单一总资金开始真实回测，返回任务ID。','/research/portfolio/backtests',Portfolio),
        ('ai_task_plan','明确选择工具、单助手、模型小组或多角色团队，形成支持范围内的实际计划。','/research/tasks/plan',TaskPlan),
        ('ai_task_start','按不可变计划引用开始实际任务，返回任务ID；相同标识不重复提交。','/research/tasks/start',TaskStart)):
        add(name,desc,'POST',path,body,body_rest=True,idempotent=name!='ai_task_plan',external=name in {'data_provider_download','ai_task_start'})
    for action in ('trash','restore'):
        add('data_dataset_'+action,'将数据集'+('移入回收站，新任务不再可选。' if action=='trash' else '从回收站恢复。'),'POST','/data/library/datasets/{dataset_id}/'+action,model('Dataset'+action+'Input',base=Key,dataset_id=ID),path_fields=('dataset_id',),body_rest=True,idempotent=True,destructive=action=='trash')
    for action in ('status','cancel'):
        add('ai_task_'+action,{'status':'查看研究任务实际状态。','cancel':'请求停止研究任务。','resume':'仅在现有服务验证断点可用后继续；新的任务ID不等于恢复成功。'}[action], 'GET' if action=='status' else 'POST','/research/tasks/{job_id}'+('' if action=='status' else '/'+action),model('Task'+action+'Input',job_id=ID),path_fields=('job_id',),idempotent=action=='cancel')
    add('ai_task_verified_resume','委托同一研究服务恢复：仅已核验原生TA断点可用时继续，固定数据集任务需显式新开始；不静默降级。','POST','/research/tasks/{job_id}/resume',model('TaskResumeInput',base=Key,job_id=ID),path_fields=('job_id',),body_rest=True,idempotent=True,external=True)
    add('mcp_connection_config','读取当前实际启动地址生成的本地MCP连接配置，不猜端口，不返回凭据。','GET','/operations/mcp-connection')
    for action,method in (('status','GET'),('set','PUT'),('delete','DELETE')):
        kind=model('Credential'+action+'Input',base=CredentialWrite if action=='set' else Empty,slot=(str,Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')))
        add('provider_credential_'+action,{'status':'查看指定凭据槽是否配置及其供应商/目标绑定，不解密。','set':'将调用方提供的值只写入本机保险库，并绑定供应商/目标；不返回值。','delete':'删除指定凭据槽的本机保存值，不解密。'}[action],method,'/operations/provider-credentials/{slot}',kind,path_fields=('slot',),body_rest=action=='set',idempotent=True,destructive=action in {'set','delete'})
    for action in ('pause','resume','trigger','archive'):
        kind=model('Schedule'+action+'Input',base=Key if action=='trigger' else Empty,schedule_id=ID)
        add('schedule_'+action,'对已保存本地计划执行'+action+'；仍受原产品校验。','DELETE'if action=='archive'else'POST','/schedules/{schedule_id}'+(''if action=='archive'else'/'+action),kind,path_fields=('schedule_id',),body_rest=action=='trigger',idempotent=True,destructive=action=='archive',external=action in {'resume','trigger'})
    add('schedule_read','查看一个本地计划。','GET','/schedules/{schedule_id}',model('ScheduleReadInput',schedule_id=ID),path_fields=('schedule_id',))
    add('schedule_history','查看本地计划实际触发记录。','GET','/schedules/{schedule_id}/history',model('ScheduleHistoryInput',base=Page,schedule_id=ID),path_fields=('schedule_id',),query_fields=('limit','offset'))
    schedule_input=model('ScheduleCreateInput',base=Schedule,idempotency_key=(str,Field(min_length=1,max_length=128)))
    add('schedule_create','保存本地产品任务计划，不增加新常驻调度服务；重复标识不会重复创建。','POST','/schedules',schedule_input,body_rest=True,idempotent=True,external=True)
    add('schedule_update','更新指定本地计划；相同调用标识不会重复提高版本。','PUT','/schedules/{schedule_id}',model('ScheduleUpdateInput',base=schedule_input,schedule_id=ID),path_fields=('schedule_id',),body_rest=True,idempotent=True,external=True)
    add('data_original_csv_export','取得当前用户上传的原始CSV完整字节、SHA-256与base64；不接受文件路径。','GET','/data/library/datasets/{dataset_id}/original',model('OriginalCSVInput',dataset_id=ID),path_fields=('dataset_id',),response_format='csv_base64')
    add('daily_results_list','查看日线回测报告或回收站，恢复操作仍引用原报告。','GET','/research/daily/results',model('DailyResultsInput',base=Page,state=(Literal['active','deleted','all'],'active')),query_fields=('limit','offset','state'))
    add('daily_result_read','读取日线回测原报告及序列清单；使用result_section读取大序列。','GET','/research/daily/results/{run_id}',model('DailyResultInput',run_id=ID,include_deleted=(bool,False)),path_fields=('run_id',),query_fields=('include_deleted',))
    add('daily_result_lifecycle','重命名、移入回收站或恢复本人日线报告，使用版本检查和调用标识。','POST','/research/daily/results/{run_id}/lifecycle',model('DailyLifecycleInput',base=DailyLifecycle,run_id=ID),path_fields=('run_id',),body_rest=True,idempotent=True,destructive=True)
    add('daily_backtest_start','开始已有日线服务的真实回测并返回任务ID；可能下载数据。优先选择已保存数据集的backtest_start。','POST','/research/daily/backtests',DailyBacktest,body_rest=True,idempotent=True,external=True)
    add('native_ta_capabilities','查询原生TradingAgents实际可用性、离线范围和运行限制。','GET','/research/debate-capabilities')
    add('native_ta_start','使用固定数据引用及明确团队快照开始原生研究；返回任务ID，不声称模型已经调用或任务成功。','POST','/research/debates',NativeDebate,body_rest=True,idempotent=True,external=True)
    for suffix in ('capabilities','templates','exports'):
        add('native_qlib_'+suffix,'查看原生Qlib的'+suffix+'，仅已支持能力可执行。','GET','/research/qlib/'+suffix,Page if suffix=='exports'else Empty,query_fields=('limit','offset')if suffix=='exports'else())
    add('native_qlib_input_prepare','创建已支持的固定离线Qlib输入准备任务，返回任务ID。','POST','/research/qlib/inputs/prepare',model('QlibPrepareInput',base=Key,profile_id=(Literal['FA014-QLIB-US-OFFLINE-V2'],'FA014-QLIB-US-OFFLINE-V2')),body_rest=True,idempotent=True)
    add('native_qlib_input_register','注册已成功准备并经过当前服务校验的Qlib输入；不接受本机路径。','POST','/research/qlib/inputs/{job_id}/register',model('QlibRegisterInput',base=Key,job_id=ID),path_fields=('job_id',),body_rest=True,idempotent=True)
    add('native_qlib_start','使用受控导出引用与工作流开始Qlib任务，服务仍校验模板与能力限制。','POST','/research/qlib',NativeQlib,body_rest=True,idempotent=True)
    add('trained_models_list','查看已保存的训练模型记录。','GET','/models',Page,query_fields=('limit','offset'))
    add('trained_model_read','查看已保存模型元数据与来源。','GET','/models/{model_id}',model('TrainedModelRead',model_id=ID),path_fields=('model_id',))
    add('strategy_template_create','按产品内置模板注册策略，不授予可信代码或实盘权限；无自动重试。','POST','/strategies/template',StrategyTemplate,body_rest=True)
    add('strategy_config_create','按已注册策略保存回测或本地模拟配置；无自动重试，不开放LIVE。','POST','/strategies/configs',StrategyConfig,body_rest=True)
    add('strategy_config_read','查看本人保存的策略配置。','GET','/strategies/configs/{config_id}',model('StrategyConfigRead',config_id=ID),path_fields=('config_id',))
    add('strategy_instance_create','由已保存配置建立策略实例；无自动重试。','POST','/strategies/instances',model('StrategyInstanceCreate',config_id=ID),body_rest=True)
    add('strategy_instance_read','查看本人策略实例实际状态。','GET','/strategies/instances/{instance_id}',model('StrategyInstanceRead',instance_id=ID),path_fields=('instance_id',))
    for action in ('validate','pause','stop','archive'):
        add('strategy_instance_'+action,'对本人策略实例执行'+action+'；现有产品权限及订单取消规则仍生效，无自动重试。','POST','/strategies/instances/{instance_id}/'+action,model('StrategyInstance'+action,instance_id=ID,cancel_orders=(bool,False)),path_fields=('instance_id',),body_rest=True,destructive=action!='validate')
    for name,path in (('legacy_data_sources','/data/sources'),('legacy_data_catalog','/data/catalog'),('legacy_data_datasets','/data/datasets'),('legacy_data_snapshots','/data/snapshots')):
        add(name,'查看旧数据服务的受控元数据；新增数据请使用固定数据集工具。','GET',path,Page,query_fields=('limit','offset'))
    add('history_search','按证券查询创建有界的历史行情搜索任务，返回任务ID；可能查询外部提供方。','POST','/data/history/search',model('HistorySearchInput',base=Key,query=(str,Field(min_length=1,max_length=80))),body_rest=True,idempotent=True,external=True)
    add('history_download','按证券与日期创建历史行情下载任务；相同标识不重复创建。旧AKShare诊断不自动恢复。','POST','/data/history/download',HistoryDownload,body_rest=True,idempotent=True,external=True)
    add('histories_list','查看本人保存的历史行情或回收站。','GET','/data/history',model('HistoriesInput',base=Page,state=(Literal['active','deleted','all'],'active')),query_fields=('limit','offset','state'))
    add('history_read','查看历史行情精确版本、覆盖和来源。','GET','/data/history/{history_id}',model('HistoryReadInput',history_id=ID,version=(int|None,Field(default=None,ge=1)),include_deleted=(bool,False)),path_fields=('history_id',),query_fields=('version','include_deleted'))
    add('history_rows','分页读取历史行情已保存版本，最多100行，不接受本机路径。','GET','/data/history/{history_id}/rows',model('HistoryRowsInput',base=Page,history_id=ID,version=(int,Field(ge=1))),path_fields=('history_id',),query_fields=('version','limit','offset'))
    add('history_lifecycle','重命名、移入回收站或恢复本人历史行情，旧任务固定引用仍受原服务规则保护。','POST','/data/history/{history_id}/lifecycle',model('HistoryLifecycleInput',base=DailyLifecycle,history_id=ID),path_fields=('history_id',),body_rest=True,idempotent=True,destructive=True)
    add('strategy_simulation_status','查看本地模拟的实际检查点及运行状态。','GET','/strategies/instances/{instance_id}/simulation',model('SimulationStatusInput',instance_id=ID),path_fields=('instance_id',))
    for action in ('start','resume'):
        add('strategy_simulation_'+action,'对受控实例执行本地模拟'+action+'，返回实际任务；不开放外部实盘。重复请求可能被原状态检查拒绝，不自动重试。','POST','/strategies/instances/{instance_id}/'+action+'-simulation',model('Simulation'+action+'Input',base=SimulationStart,instance_id=ID),path_fields=('instance_id',),body_rest=True)
    add('research_backtest_start','使用已有策略实例与固定样本引用开始原回测服务任务，返回任务ID。','POST','/research/backtests',model('ResearchBacktestInput',base=Key,instance_id=ID,sample_reference=(VersionReference,...),backend=(Literal['event','batch'],'event'),model_id=(str|None,None)),body_rest=True,idempotent=True)
    add('research_results_compare','对两个原研究结果按现有可比性规则进行比较。','POST','/research/compare',model('CompareResultsInput',left_run_id=ID,right_run_id=ID),body_rest=True,idempotent=True)
    from .mcp_domain_models import StrategySearchPlan
    add('strategy_search_plan','为本地客户端准备有界策略搜索计划：只接受内置候选模板；先训练期比较，冻结选择后仅验证胜者。返回READY计划后用ai_task_start提交真实任务。','POST','/research/tasks/plan',StrategySearchPlan,body_rest=True)
    add('strategy_search_report','读取已保存搜索报告摘要与分区，查看实际训练比较、冻结选择及一次验证；大结果继续result_section，不从提示词伪造排名。','GET','/results/{result_id}/summary',model('StrategySearchReportInput',result_id=ID),path_fields=('result_id',))
    add('strategy_search_catalog','读取实际strategy_search能力、模型提供方限制和总预算；先确认目录，不假定所有模型都支持搜索。','GET','/research/portfolio/catalog')
    add('strategy_search_evidence','分页读取本人搜索尝试的有界摘要，包括失败/取消任务。原始证据完整保留；使用sequence和sha256继续分区读取，不是成功报告或恢复断点。','GET','/research/tasks/{job_id}/search-evidence',model('SearchEvidenceInput',job_id=ID,limit=(int,Field(default=20,ge=1,le=50)),offset=(int,Field(default=0,ge=0,le=10000))),path_fields=('job_id',),query_fields=('limit','offset'))
    add('strategy_search_evidence_section','按摘要给出的sequence和源sha256分区读取完整原始搜索证据；section是JSON指针而不是文件路径。保持源hash，跟随next_offset和nested_sections读取，无静默截断。','GET','/research/tasks/{job_id}/search-evidence/{sequence}/series',model('SearchEvidenceSectionInput',job_id=ID,sequence=(int,Field(ge=1,le=9223372036854775807)),section=(str,Field(default='',max_length=1024)),limit=(int,Field(default=500,ge=1,le=8192)),offset=(int,Field(default=0,ge=0,le=2000000)),expected_sha256=(str,Field(pattern=r'^[0-9a-f]{64}$'))),path_fields=('job_id','sequence'),query_fields=('section','limit','offset','expected_sha256'))
    from .autonomous_mcp import operations as autonomous_operations
    out.extend(autonomous_operations())
    from .goal_research_api import operations as goal_operations
    out.extend(goal_operations())
    return tuple(out)


def account_extensions():
    """Operations whose input contracts only exist for explicitly upgraded grants."""
    from .mcp_research import ExternalNativeDebate
    results = model(
        'ManagedResultsInput', base=Page, kind=(Literal['all','backtest','research','ai','model'], 'all'),
        state=(Literal['active','deleted','all'], 'active'), q=(str | None, Field(default=None, max_length=120)),
        source=(Literal['all','tasks','daily','research','jobs','autonomous'], 'all'),
        from_date=(str | None, Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')),
        to_date=(str | None, Field(default=None, pattern=r'^\d{4}-\d{2}-\d{2}$')),
    )
    market_page = model('ManagedMarketInput', base=Page,
                        q=(str | None, Field(default=None, max_length=120)), provider=(str | None, None),
                        source=(Literal['all','library','history'], 'all'),
                        state=(Literal['active','trash','all'], 'active'),
                        kind=(Literal['all','dataset','benchmark'], 'all'))
    market_source = (Literal['library','history'], ...)
    market_detail = model('ManagedMarketDetail', source=market_source, id=ID, version=(int | None, Field(default=None, ge=1)),
                          include_trash=(bool, False), version_limit=(int, Field(default=50, ge=1, le=100)),
                          version_offset=(int, Field(default=0, ge=0, le=10000)))
    connection_page = model('ConnectionManagementInput', base=Page, q=(str | None, Field(default=None, max_length=120)),
                            state=(Literal['active','archived','all'], 'active'))
    return (
        Operation('onboarding_first_example_status', '读取首次真实行情示例、现有任务和报告链接。', 'GET',
                  '/onboarding/first-example', Empty),
        Operation('onboarding_first_example_start', '用户明确同意后启动 Yahoo 行情与虚拟资金回测示例。', 'POST',
                  '/onboarding/first-example', ExampleStart, body_rest=True, idempotent=True, external=True),
        Operation('onboarding_first_example_action', '按示例当前状态推进、重试或取消。按返回 revision 提交。', 'POST',
                  '/onboarding/first-example/{intent_id}/actions',
                  model('ExampleActionInput', base=ExampleAction, intent_id=ID), ('intent_id',),
                  body_rest=True, idempotent=True, external=True),
        Operation('results_search', '搜索所有来源的本人报告和回收站，返回可用能力及原因。', 'GET', '/results',
                  results, query_fields=tuple(results.model_fields)),
        Operation('result_management', '查看结果目前的名称、版本、回收状态和可用能力。', 'GET',
                  '/results/{result_id}/management', model('ResultManagementInput', result_id=ID), ('result_id',)),
        Operation('result_lifecycle', '按版本重命名、回收或恢复本人已保存报告。', 'POST',
                  '/results/{result_id}/lifecycle', model('ResultLifecycleInput', base=LifecycleCommand, result_id=ID),
                  ('result_id',), body_rest=True, idempotent=True, destructive=True),
        Operation('results_lifecycle', '在同一事务中回收或恢复多条本人报告。', 'POST', '/results/lifecycle',
                  BatchLifecycleCommand, body_rest=True, idempotent=True, destructive=True),
        Operation('market_data_list', '统一查看固定数据集、日线历史和回收站。', 'GET', '/data/library/managed',
                  market_page, query_fields=tuple(market_page.model_fields)),
        Operation('market_data_read', '读取本人行情和精确版本摘要。', 'GET', '/data/library/managed/{source}/{id}',
                  market_detail, ('source','id'), ('version','include_trash','version_limit','version_offset')),
        Operation('market_data_lifecycle', '按版本改名、回收或恢复保存的行情。', 'POST',
                  '/data/library/managed/{source}/{id}/lifecycle',
                  model('MarketLifecycleInput', base=MarketLifecycle, source=market_source, id=ID),
                  ('source','id'), body_rest=True, idempotent=True, destructive=True),
        Operation('market_data_reuse', '复用已保存数据集时只提交原引用和幂等标识。固定日线历史时可另填名称和连接。', 'POST', '/data/library/managed/reuse',
                  MarketReuse, body_rest=True, idempotent=True),
        Operation('market_merge_preview', '核对两个版本及可选整窗验证版本，只读，给出合并摘要与冲突。', 'POST',
                  '/data/library/managed/merge-preview', MarketMergePreview, body_rest=True, idempotent=True),
        Operation('market_data_merge', '将已经预览并明确选择冲突策略的行情保存为新固定版本。', 'POST',
                  '/data/library/managed/merge', MarketMerge, body_rest=True, idempotent=True),
        Operation('data_connections_manage_list', '查看本人启用或归档的供应商连接和引用状态。', 'GET',
                  '/data/library/connections/manage', connection_page, query_fields=tuple(connection_page.model_fields)),
        Operation('data_connection_lifecycle', '按版本编辑显示名称或凭据引用、归档或恢复本人连接。', 'POST',
                  '/data/library/connections/{id}/manage', model('ConnectionLifecycleInput', base=ConnectionLifecycle, id=ID),
                  ('id',), body_rest=True, idempotent=True, destructive=True),
        Operation('jobs_capabilities', '查看当前任务可取消、重试和继续的能力，以及不可用原因。', 'GET',
                  '/jobs/{job_id}/capabilities', model('JobCapabilitiesInput', job_id=ID), ('job_id',)),
        Operation('native_ta_status', '查看本人原生 TradingAgents 作业与已验证检查点状态。', 'GET',
                  '/mcp-research/debates/{job_id}', model('NativeTaStatusInput', job_id=ID), ('job_id',)),
        Operation('native_ta_cancel', '请求取消本人原生 TradingAgents 作业。', 'POST',
                  '/research/debates/{job_id}/cancel', model('NativeTaCancelInput', job_id=ID), ('job_id',),
                  idempotent=True),
        Operation('native_ta_external_capabilities', '查看原生付费分析实际已就绪的服务端模型及可批准上限。',
                  'GET', '/mcp-research/debates/external-capabilities',
                  model('ExternalTaCapabilitiesInput', base=Page,
                        profile_id=(str | None, Field(default=None, min_length=1, max_length=128))),
                  query_fields=("limit", "offset", "profile_id")),
        Operation('native_ta_external_start', '按精确付费模型和本次已批准额度开始合成来源的原生分析。',
                  'POST', '/mcp-research/debates/external', ExternalNativeDebate,
                  body_rest=True, idempotent=True, external=True),
        Operation('research_synthetic_sample', '准备原生研究所需的服务端固定合成样本，仅限已支持的粒度和条数。', 'POST',
                  '/research/samples', model('DelegatedSyntheticSampleInput',
                                             frequency=(Literal["1d","1m"], "1d"),
                                             count=(int, Field(default=8, ge=2, le=8)), two_assets=(bool, False)),
                  body_rest=True, idempotent=True),
        Operation('portfolio_backtest_start', '运行内置或本人可用策略实例，最多对照 SPY、QQQ、ASHR 三个精确基准数据集。',
                  'POST', '/research/portfolio/backtests', WorkbenchPortfolio, body_rest=True, idempotent=True),
        Operation('account_ai_task_start',
                  '开始本人冻结的研究计划。模型计划需 external 授权并用三个总上限明确批准此次执行。',
                  'POST', '/research/tasks/start', model('AccountTaskStartInput', base=TaskStart, **CONSENT_FIELDS),
                  query_fields=tuple(CONSENT_FIELDS), body_rest=True, idempotent=True, external=True),
    )


def account_variant(operation):
    """Keep local-personal models compatible with older installed endpoints."""
    name = operation.name
    projected = {
        "goal_research_list", "goal_research_read", "goal_research_control", "goal_research_save_defaults",
        "goal_research_create",
        "autonomous_sessions_list", "autonomous_session_read", "autonomous_session_progress",
        "autonomous_prepare", "autonomous_session_create", "autonomous_session_control",
    }
    if name in projected:
        operation = replace(operation, path=operation.path.replace("/research/", "/mcp-research/", 1))
    if name == "autonomous_session_control":
        control = model("DelegatedAutonomousControl", session_id=ID,
                        action=(Literal["pause","cancel","run","resume","restart"], ...),
                        expected_revision=(int, Field(ge=1)),
                        idempotency_key=(str, Field(min_length=1, max_length=128)),
                        approved_spec_sha256=(str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")))
        return replace(operation, input_model=control, query_fields=("approved_spec_sha256",), external=False)
    if name == "goal_research_control":
        from .mcp_research import GoalResume
        control = model("DelegatedGoalControl", base=GoalResume, task_id=ID)
        return replace(operation, input_model=control)
    if name == "goal_research_create":
        from .mcp_research import GoalExecution
        return replace(operation, input_model=GoalExecution, external=True)
    if name == "native_ta_start":
        team_request = model("DelegatedOfflineDebateInput", base=NativeDebate,
                             agent_team_snapshot_id=(str, Field(min_length=1, max_length=128)))
        return replace(operation, path="/mcp-research/debates", input_model=team_request,
                       description="运行已核验离线团队和合成来源的原生 TradingAgents 分析。",
                       external=False)
    if name == "strategy_search_evidence_section":
        return replace(operation, description="读取已授权证据的分页分区，以摘要返回的 SHA 核对账号视图。")
    if name == "strategy_template_create":
        body = model("ApprovedStrategyTemplateInput", base=Key,
                     strategy_id=(str, Field(min_length=1, max_length=64)),
                     style=(Literal["event", "batch", "target"], ...))
    elif name in {"strategy_config_create", "strategy_instance_create", "strategy_instance_validate"}:
        body = model("Delegated" + operation.input_model.__name__, base=operation.input_model,
                     idempotency_key=(str, Field(min_length=1, max_length=128)))
    elif name == "data_provider_download":
        return replace(operation, description="通过本人公开 Yahoo 连接下载有界历史行情或固定价格基准。",
                       input_model=PublicDatasetDownload)
    elif name == "job_retry":
        body = model("DelegatedRetryJobInput", base=operation.input_model, **CONSENT_FIELDS)
        return replace(operation, input_model=body, query_fields=tuple(CONSENT_FIELDS))
    elif name == "job_verified_resume":
        body = model("DelegatedResumeJobInput", base=operation.input_model,
                     approved_execution_sha256=(str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")))
        return replace(operation, input_model=body, query_fields=("approved_execution_sha256",))
    else:
        return operation
    return replace(operation, input_model=body, idempotent=True)
