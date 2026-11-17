"""Fixed typed MCP additions. Append these operations in the shared registry."""
from pydantic import Field
from datetime import date
from typing import Literal
from pydantic import model_validator
from .mcp_operations import Operation, Page, Empty, ID, model
from .autonomous_models import CreateSession, Control, PrepareSession, SaveResearchCandidate, SavedCandidateBacktest


class MassiveProbe(Empty):
    connection_id:str=Field(min_length=1,max_length=128)
    ticker:str=Field(min_length=1,max_length=18)
    start:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    adjustment:Literal['raw','split_adjusted']

    @model_validator(mode='after')
    def bounded_dates(self):
        days=(date.fromisoformat(self.end)-date.fromisoformat(self.start)).days
        if not 0<=days<=6:raise ValueError('probe range exceeds seven inclusive dates')
        return self


class MassiveCheck(Empty):
    request:MassiveProbe
    idempotency_key:str=Field(min_length=1,max_length=128)


class CursorPage(Page):
    cursor:str|None=Field(default=None,max_length=1024)


class TelemetryPage(Empty):
    limit:int=Field(default=50,ge=1,le=100)
    offset:int=Field(default=0,ge=0)


class ArtifactCursorPage(CursorPage):
    attempt_id:str|None=Field(default=None,min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')


class DatedRoster(Empty):
    label:str=Field(min_length=1,max_length=120)
    members:list[str]=Field(min_length=1,max_length=1000,description='已排序去重的证券代码；服务校验格式。')
    as_of:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    source:str=Field(min_length=1,max_length=500,description='来源说明；不会抓取这个地址或据此证明PIT。')


class UniverseImport(Empty):
    roster:DatedRoster
    idempotency_key:str=Field(min_length=1,max_length=128)


class ExportReference(Empty):
    dataset_id:str=Field(min_length=1,max_length=128)
    version:int=Field(ge=1)
    content_hash:str=Field(pattern=r'^[0-9a-f]{64}$')


class ResearchExportRequest(Empty):
    reference:ExportReference
    start:str|None=Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}$')
    end:str|None=Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}$')
    format:Literal['finance-research-input-1','finance-openbb-equity-input-1']='finance-research-input-1'


class ResearchExport(Empty):
    dataset_id:str=Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')
    request:ResearchExportRequest
    idempotency_key:str=Field(min_length=1,max_length=128)

    @model_validator(mode='after')
    def same_reference(self):
        if self.dataset_id!=self.request.reference.dataset_id:raise ValueError('dataset reference differs from route')
        return self


def operations():
    base = '/research/autonomous/sessions'
    ref = model('AutonomousSessionInput', session_id=ID)
    page = model('AutonomousPageInput', base=CursorPage, session_id=ID)
    usage_page=model('AutonomousUsagePageInput',base=TelemetryPage,session_id=ID)
    artifact_page=model('AutonomousArtifactPageInput',base=ArtifactCursorPage,session_id=ID)
    control = model('AutonomousControlInput', base=Control, session_id=ID)
    out = [
        Operation('data_universe_import','保存本人提供的有日期静态名单，返回任务ID；不抓取来源、不认定历史成分或PIT。','POST','/data/library/universes/import',UniverseImport,body_rest=True,idempotent=True),
        Operation('data_research_export','将固定数据版本按包含首尾的日期范围导出，返回任务ID；start/end=null表示原范围。OpenBB格式须服务实际可用，不下载供应商数据。','POST','/data/library/datasets/{dataset_id}/research-export',ResearchExport,path_fields=('dataset_id',),body_rest=True,idempotent=True),
        Operation('data_research_export_page','读取本人成功导出的固定内容，每页最多65536字节；按sha256/bytes/offset和next_offset完整拼接base64，不接受本机路径。','GET','/data/library/exports/{job_id}/page',model('ResearchExportPage',job_id=ID,offset=(int,Field(default=0,ge=0,le=67108864)),limit=(int,Field(default=65536,ge=1,le=65536))),path_fields=('job_id',),query_fields=('offset','limit')),
        Operation('research_candidate_backtest','按固定候选、数据、费用和ABI回测一次；0模型调用、不要求模型或密钥。仅提交隔离执行任务，不代表回测已完成。','POST','/research/autonomous/candidates/{candidate_id}/backtests',model('SavedCandidateBacktestInput',base=SavedCandidateBacktest,candidate_id=ID),path_fields=('candidate_id',),body_rest=True,idempotent=True,external=True),
        Operation('data_connection_check','明确检查所选Massive连接的单标的最多七个日历日权限；不推断全账号套餐，返回任务ID。','POST','/data/library/connections/check',MassiveCheck,body_rest=True,idempotent=True,external=True),
        Operation('data_download_progress','查看同owner下载原请求、累计额度、冷却与可续传原因；续传复用data_provider_download和原请求+resume_id。','GET','/data/library/downloads/{job_id}/progress',model('AutonomousDownloadProgress',job_id=ID),path_fields=('job_id',)),
        Operation('research_candidates_list','查看本人保存的策略/因子候选；代码是惰性数据，不授予主机执行权限。','GET','/research/autonomous/candidates',model('ResearchCandidatesInput',base=Page,kind=(Literal['strategy','factor']|None,None)),query_fields=('kind','limit','offset')),
        Operation('research_candidate_read','读取保存候选及原始来源、逻辑hash；不运行代码。','GET','/research/autonomous/candidates/{candidate_id}',model('ResearchCandidateInput',candidate_id=ID),path_fields=('candidate_id',)),
        Operation('research_candidate_save','将成功开发轮次的源产物保存为不可变候选；由服务校验原产物，不接受源码或路径。','POST','/research/autonomous/candidates',SaveResearchCandidate,body_rest=True,idempotent=True),
        Operation('autonomous_backtest_summary','读取本人成功轮次的实际引擎结果摘要和分区清单；数组须分页读取，历史无结构化结果明确报错，不生成空图或估算收益。','GET',base+'/{session_id}/attempts/{attempt_id}/backtest/summary',model('AutonomousBacktestSummary',session_id=ID,attempt_id=ID),path_fields=('session_id','attempt_id')),
        Operation('autonomous_backtest_series','按摘要分区读取实际曲线、日历、逐股盈亏、交易和持仓；携带同一source_view_sha256为expected_sha256，按next_offset读完，不接受文件路径。','GET',base+'/{session_id}/attempts/{attempt_id}/backtest/series',model('AutonomousBacktestSeries',session_id=ID,attempt_id=ID,section=(str,Field(default='',max_length=1024)),limit=(int,Field(default=500,ge=1,le=1000)),offset=(int,Field(default=0,ge=0,le=2000000)),expected_sha256=(str|None,Field(default=None,pattern='^[0-9a-f]{64}$'))),path_fields=('session_id','attempt_id'),query_fields=('section','limit','offset','expected_sha256')),
        Operation('autonomous_capabilities','查看实际评价指标、执行器就绪原因与预算单位。','GET','/research/autonomous/capabilities',Empty),
        Operation('autonomous_prepare','由服务器解析已有数据/模型/团队版本及保守实验预算，不自行构造hash。','POST','/research/autonomous/prepare',PrepareSession,body_rest=True,idempotent=True),
        Operation('autonomous_session_create', '固定研究目标、评分、输入与累计预算；不直接执行模型。', 'POST', base, CreateSession, body_rest=True, idempotent=True),
        Operation('autonomous_sessions_list', '查看自己的长期研究会话。', 'GET', base, Page, query_fields=('limit', 'offset')),
        Operation('autonomous_session_progress','查看自主循环各轮历史、当前进度、停止原因、累计保守预算和持久检查点；未知费用不补零，不重置预算。','GET',base+'/{session_id}/progress',model('AutonomousProgressInput',session_id=ID,limit=(int,Field(default=20,ge=1,le=100)),before_ordinal=(int|None,Field(default=None,ge=1))),path_fields=('session_id',),query_fields=('limit','before_ordinal')),
        Operation('autonomous_session_read', '查看持久化状态与跨恢复累计预算。', 'GET', base+'/{session_id}', ref, path_fields=('session_id',)),
        Operation('autonomous_model_usage_read','查看整个会话跨恢复已记录模型用量；仅models分页，未知不是零，累计非账单。','GET',base+'/{session_id}/model-usage',usage_page,path_fields=('session_id',),query_fields=('limit','offset')),
        Operation('ai_task_model_usage_read','查看自己任务及已验证TA祖先的已记录用量；仅models分页。','GET','/research/tasks/{job_id}/model-usage',model('AITaskModelUsage',base=TelemetryPage,job_id=ID),path_fields=('job_id',),query_fields=('limit','offset')),
        Operation('ai_task_model_trace_read','分页读取已保存安全角色轨迹；失败和缺失不代表成功报告或零调用。','GET','/research/tasks/{job_id}/model-trace',model('AITaskModelTrace',base=TelemetryPage,job_id=ID),path_fields=('job_id',),query_fields=('limit','offset')),
        Operation('autonomous_session_control', '运行、暂停、恢复、取消、重启或冻结最终候选；未知请求保留预算且不自动重试。', 'POST', base+'/{session_id}/control', control, path_fields=('session_id',), body_rest=True, idempotent=True, external=True),
    ]
    for kind in ('attempts', 'events', 'artifacts'):
        out.append(Operation('autonomous_'+kind+'_list', '按单调游标分页读取；空cursor开始，保留next_cursor供后续增量轮询；产物可按attempt_id筛选。', 'GET', base+'/{session_id}/'+kind, artifact_page if kind=='artifacts' else page, path_fields=('session_id',), query_fields=('limit','offset','cursor','attempt_id') if kind=='artifacts' else ('limit','offset','cursor')))
    out.append(Operation('autonomous_artifact_read', '按受控标识读取产物；代码只作文本，不在主机执行。', 'GET', base+'/{session_id}/artifacts/{artifact_id}', model('AutonomousArtifactInput', session_id=ID, artifact_id=ID), path_fields=('session_id', 'artifact_id')))
    return out
