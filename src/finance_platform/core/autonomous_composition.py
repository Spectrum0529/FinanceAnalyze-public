"""Trusted composition port. No default executor or generated-code host fallback."""
from contextlib import closing
from .auth import Principal
from .errors import PlatformError
from .security import now_text
from .autonomous_api import create_router
from .autonomous_sessions import AutonomousSessions, HANDLER


class ResearchScheduler:
    def __init__(self, original, service):
        self.original, self.service = original, service

    def __getattr__(self, key):
        return getattr(self.original, key)

    def tick(self):
        result = self.original.tick()
        db = self.service.database
        with closing(db.connect()) as con:
            rows = con.execute("SELECT s.session_id,u.user_id,u.username,u.role FROM autonomous_sessions s JOIN users u ON s.owner_id=u.user_id WHERE u.enabled=1 AND s.status IN ('READY','SEALED','RUNNING','PAUSE_REQUESTED','CANCEL_REQUESTED') ORDER BY s.updated_at,s.session_id LIMIT 100").fetchall()
        for row in rows:
            principal = Principal(row['user_id'], row['username'], row['role'])
            try:
                self.service.recover(principal, row['session_id'])
                self.service.dispatch(principal, row['session_id'])
            except PlatformError as error:
                # Isolate a failed research admission from unrelated queued work.
                with db.transaction() as con:
                    con.execute("UPDATE autonomous_sessions SET status='BLOCKED',revision=revision+1,updated_at=? WHERE session_id=? AND active_attempt IS NULL AND status IN ('READY','SEALED')", (now_text(), row['session_id']))
                    self.service._event(con, row['session_id'], 'ADMISSION_BLOCKED', {'code': error.code})
        return result


def install(runtime, *, resolve_inputs, adapter):
    """adapter(context,envelope) must enforce broker/executor per-attempt caps.

    FA015 supplies a reviewed adapter; this function never imports a user path,
    spawns a process, resolves a credential or silently runs host Python.
    """
    if not callable(adapter) or getattr(runtime, 'autonomous_research', None) is not None:
        raise PlatformError('INVALID_INPUT', 422)
    from .autonomous_windows import WindowsInputs
    service = AutonomousSessions(runtime.database, runtime.jobs, resolve_inputs=resolve_inputs,
                                 require_current_ohlcv=isinstance(resolve_inputs, WindowsInputs))
    from .errors import MESSAGES
    MESSAGES.update({'AUTONOMOUS_TEAM_MEMBER_PROFILE_REQUIRED':'所选团队成员需关联已保存模型配置；请先保存模型配置并重新冻结团队。','AUTONOMOUS_TEAM_MEMBER_PROFILE_CHANGED':'团队成员与已保存模型版本不一致，请检查映射后重新准备。','AUTONOMOUS_TEAM_MAPPING_AMBIGUOUS':'同一研究角色不能同时指定团队成员和单独模型，请选择一种来源。','AUTONOMOUS_TEAM_SNAPSHOT_REQUIRED':'请先选择已冻结的团队快照。'})
    MESSAGES['AUTONOMOUS_RESEARCH_MODELS_REQUIRED']='请选择一个已保存的默认模型，或为研究角色分别指定模型；仅内嵌模型的团队快照还需明确选择默认模型。'
    MESSAGES['AUTONOMOUS_PRICE_REQUIRED']='金额预算缺少可信报价，请使用次数、token和时长限额模式；此模式不保证费用上限。'
    MESSAGES['AUTONOMOUS_FINAL_UNAVAILABLE']='当前执行器尚未具备匹配的封存最终测试运行环境，请留空最终测试数据或等待运行环境准备完成。'
    MESSAGES.update({'SAVED_BACKTEST_UNAVAILABLE':'当前执行器尚不支持零模型候选回测，请等待匹配的运行环境准备完成。','SAVED_BACKTEST_MODEL_FORBIDDEN':'保存候选回测不允许模型调用；如需AI分析，请另建研究会话。','FINAL_ADAPTIVE_PORT_FORBIDDEN':'封存最终测试禁止模型调用或读取自适应研究历史。','FINAL_SEAL_BINDING_MISMATCH':'最终测试的候选、输入或封存记录不一致，已停止执行。'})
    MESSAGES.update({'BACKTEST_WINDOW_OUTSIDE_DATASET':'回测日期必须位于所选数据集范围内，且开始日期不能晚于结束日期。','SAVED_BACKTEST_WINDOW_BINDING_MISMATCH':'回测日期、数据版本与已冻结窗口不一致，已停止执行。'})
    MESSAGES['AUTONOMOUS_BACKTEST_VIEW_UNAVAILABLE']='这份历史报告没有结构化引擎结果，无法显示收益图表；原始报告仍保留。'
    MESSAGES.update({'FINAL_CANDIDATE_DEVELOPMENT_OVERLAP':'该候选在相同数据谱系和时期已接触开发数据，不能作为独立最终测试。','FINAL_DEVELOPMENT_IN_PROGRESS':'该数据范围仍有开发尝试进行中，请等待完成后再核验最终测试。'})
    MESSAGES.update({'SAVED_BACKTEST_SCORING_INVALID':'请去掉重复的评分指标。IC 指标仅用于因子回测。','SAVED_BACKTEST_DATASET_ID_INVALID':'所选数据集编号无效或重复，请重新选择数据集。'})
    MESSAGES.update({'OHLCV_BINDING_MISMATCH':'旧检查点的行情字段绑定与当前版本不一致。请新建研究会话，或重新回测已保存的固定源码后再封存。','CHECKPOINT_BINDING_MISMATCH':'检查点的来源或摘要不一致，请保留记录并重新评估。'})
    service.migrate()
    from finance_platform.research.autonomous_library import ResearchCandidateLibrary
    runtime.research_candidates=ResearchCandidateLibrary(runtime,service)

    def validate(parameters):
        from .contracts import identifier
        if set(parameters) != {'session_id'}:
            raise PlatformError('INVALID_INPUT', 422)
        identifier(parameters['session_id'])

    def handle(context, parameters):
        envelope = service.begin(context)
        outcome = adapter(context, envelope)
        return service.complete(context, outcome)

    # Nonrecoverable: uncertain model effects must be journal-reconciled, never
    # automatically requeued by generic Jobs lease recovery.
    from .contracts import RunMode
    runtime.handlers.register(HANDLER, handle, validate=validate, recoverable=False,auto_recovery_limit=0,allowed_modes=(RunMode.LOCAL_SIMULATION,))
    runtime.autonomous_research = service
    runtime.routers.append(create_router(runtime, service=service,inputs=resolve_inputs if isinstance(resolve_inputs,WindowsInputs) else None))
    runtime.scheduler = ResearchScheduler(runtime.scheduler, service)
    return service
