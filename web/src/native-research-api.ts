import { post, request } from './api';
import type { RecordData } from './api';

export const record = (value: unknown): RecordData =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as RecordData : {};
export const terminalResearchJob = (job: RecordData) => ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(job.status));
export const researchResultPath = (run: string) => `/research/results/${encodeURIComponent(run)}`;
export const researchJobPath = (id: string, debate: boolean) => debate
  ? `/research/debates/${encodeURIComponent(id)}` : `/jobs/${encodeURIComponent(id)}`;

export function versionReference(value: unknown) {
  const ref = record(value);
  if (typeof ref.resource_id !== 'string' || !ref.resource_id || typeof ref.version !== 'string' || !ref.version ||
      typeof ref.content_hash !== 'string' || !/^[a-f0-9]{64}$/.test(ref.content_hash))
    throw new Error('请先从“管理数据”选择已有不可变数据版本；不会猜测引用。');
  return { resource_id: ref.resource_id, version: ref.version, content_hash: ref.content_hash };
}

export function debateCapabilities(raw: RecordData) {
  if (raw.engine !== 'TradingAgents 0.7.0' || typeof raw.model_status !== 'string' || typeof raw.can_submit !== 'boolean' ||
      raw.offline_fixture_is_product_backend !== false)
    throw new Error('研究能力响应不完整，暂不能提交。');
  const limits = record(raw.limits);
  const rounds = (key: string) => {
    const choices = limits[key];
    if (!Array.isArray(choices) || !choices.length || choices.some((v) => ![1, 2].includes(Number(v)) || typeof v !== 'number'))
      throw new Error('服务未提供明确的讨论轮数范围。');
    return choices as number[];
  };
  return {
    modelStatus: raw.model_status,
    canSubmit: raw.can_submit === true && !['UNCONFIGURED', 'UNKNOWN', 'UNVERIFIED', 'OFFLINE_FIXTURE'].includes(raw.model_status),
    message: typeof raw.message === 'string' ? raw.message : '模型状态以服务端为准。',
    debateRounds: rounds('debate_rounds'), riskRounds: rounds('risk_rounds'), toolRounds: rounds('tool_rounds'),
  };
}

export function debateRequest(raw: RecordData, input: {
  dataReference: unknown; ticker: string; date: string; rounds: number;
  evidence: readonly { kind: 'backtesting' | 'qlib'; run_id: string }[];
}, key: string) {
  const capability = debateCapabilities(raw);
  if (!capability.canSubmit) throw new Error('尚未配置可用的真实模型后端，未发起任务。');
  const ticker = input.ticker.trim().toUpperCase();
  if (!/^[A-Z][A-Z0-9.-]{0,17}$/.test(ticker) || !/^\d{4}-\d{2}-\d{2}$/.test(input.date) ||
      !Number.isFinite(Date.parse(input.date)) || new Date(input.date).toISOString().slice(0, 10) !== input.date)
    throw new Error('请检查准确股票代码和研究日期。');
  if (!capability.debateRounds.includes(input.rounds) || !capability.riskRounds.includes(input.rounds) || !capability.toolRounds.includes(1))
    throw new Error('所选讨论深度不在服务范围内。');
  if (input.evidence.length > 4 || input.evidence.some((r) => !['backtesting', 'qlib'].includes(r.kind) || !r.run_id))
    throw new Error('最多关联四条已保存回测或模型结果。');
  return {
    data_reference: versionReference(input.dataReference), ticker, trade_date: input.date, as_of: input.date,
    evidence_results: input.evidence.map((r) => ({ kind: r.kind, run_id: r.run_id })),
    options: { debate_rounds: input.rounds, risk_rounds: input.rounds, tool_rounds: 1 }, idempotency_key: key,
  };
}

// Derived from the readonly server catalogue; readiness is rechecked on submission.
export type QlibTemplateChoice = {
  label: string; exportReference: unknown; workflow: RecordData; ready: boolean; featureProfile: string | null;
};
export const qlibBasicProfile = 'basic-close-open-volume-lgb-1';
export function qlibCatalogue(capability: RecordData, templates: RecordData, exports: RecordData, offset: number) {
  const engine = record(capability.engine);
  if (engine.name !== 'Qlib' || engine.version !== '0.9.8' || engine.commit !== '54355232463878d2eebb91fe0ee5fa7fa1f5976c' ||
      typeof capability.worker_ready !== 'boolean' || typeof capability.can_submit !== 'boolean' || capability.TA_model_configuration_required !== false ||
      !Array.isArray(templates.items) || !Array.isArray(exports.items) || exports.items.length > 25 || exports.limit !== 25 || exports.offset !== offset ||
      (exports.next_offset !== null && exports.next_offset !== offset + 25))
    throw new Error('服务尚未提供兼容的模板、导出分页或当前执行状态。');
  const definitions = templates.items;
  const choices: QlibTemplateChoice[] = exports.items.map((entry) => {
    const item = record(entry), coverage = record(item.coverage);
    const template = definitions.find((row) => record(row).id === item.template_id);
    const definition = record(template);
    if (!template || definition.scope !== 'SYNTHETIC_ONLY' || item.scope !== 'SYNTHETIC' || typeof item.ready !== 'boolean')
      throw new Error('服务返回的模板类型或数据范围不兼容。');
    const reference = versionReference(item.export_reference);
    const workflow = record(item.workflow);
    const featureProfile = typeof workflow.feature_profile === 'string' ? workflow.feature_profile : null;
    const ready = item.ready === true && item.native_dump_succeeded === true && capability.worker_ready === true &&
      definition.available !== false && featureProfile === qlibBasicProfile && definition.feature_profile === featureProfile;
    return {
      label: `${String(definition.name ?? 'Qlib 模板')} · ${String(coverage.first ?? '未提供')} 至 ${String(coverage.last ?? '未提供')} · ${String(coverage.sessions ?? '未提供')} 日（合成）${ready ? '' : ' · 尚未就绪'}`,
      exportReference: reference, workflow, ready, featureProfile,
    };
  });
  return {
    choices, nextOffset: exports.next_offset === null ? null : Number(exports.next_offset),
    reason: capability.worker_ready !== true ? '执行条件尚未就绪，以服务端状态为准。' :
      choices.some((choice) => choice.ready) ? '已取得服务声明就绪的合成模板；提交时仍会重新核验。' :
        exports.next_offset !== null ? '本页没有就绪导出，可以继续翻页。' : '本次分页未取得就绪导出，可刷新状态；不据此推断所有历史。',
  };
}
export function qlibRequest(template: QlibTemplateChoice, key: string, selectedProfile: string) {
  if (!template.ready) throw new Error('模板的数据或执行条件尚未就绪，未提交训练。');
  if (selectedProfile !== qlibBasicProfile || template.featureProfile !== selectedProfile || template.workflow.feature_profile !== selectedProfile)
    throw new Error('请明确选择基础三列；Alpha158 暂未通过，不会自动替换特征方案。');
  const required = ['feature_profile', 'train', 'valid', 'test', 'train_cutoff', 'valid_cutoff'];
  const allowed = [...required, 'seed', 'num_boost_round', 'topk', 'n_drop', 'initial_cash', 'open_cost', 'close_cost', 'min_cost'];
  if (required.some((name) => !Object.hasOwn(template.workflow, name)) || Object.keys(template.workflow).some((name) => !allowed.includes(name)))
    throw new Error('模板未提供兼容的固定训练参数。');
  return { export_reference: versionReference(template.exportReference), workflow: template.workflow, idempotency_key: key };
}

export function validateResearchJob(job: RecordData, userId: string, expectedId?: string, expectedHandler?: string) {
  if (typeof job.job_id !== 'string' || !job.job_id || typeof job.run_id !== 'string' || !job.run_id ||
      job.owner_id !== userId || job.mode !== 'BACKTEST' ||
      !['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(job.status)) ||
      (expectedId !== undefined && job.job_id !== expectedId) || (expectedHandler !== undefined && job.handler !== expectedHandler))
    throw new Error('任务身份或状态不匹配，未显示其他任务。');
  return job;
}

export const researchAttemptSlot = (userId: string) => `fa-native-research-attempt-v1:${userId}`;
export function pendingResearchAttempt(userId: string) {
  try { return !!sessionStorage.getItem(researchAttemptSlot(userId)); } catch { return false; }
}
export function beginResearchAttempt(userId: string, body: unknown) {
  if (!userId) throw new Error('未提供当前工作区身份，未提交。');
  if (pendingResearchAttempt(userId)) throw new Error('已有待核对提交，未新建编号或重复发送。');
  try { sessionStorage.setItem(researchAttemptSlot(userId), JSON.stringify(body)); }
  catch { throw new Error('无法记录这次提交，任务尚未发出。'); }
}
export function clearResearchAttempt(userId: string) {
  try { sessionStorage.removeItem(researchAttemptSlot(userId)); } catch { /* Keep pending if storage fails. */ }
}

const retrySlot = (userId: string, job: RecordData) =>
  `fa-native-research-retry-v1:${JSON.stringify([userId, job.job_id, job.run_id])}`;
type RetryState = { status: 'UNKNOWN' | 'ACKNOWLEDGED'; childJobId?: string; childRunId?: string };
function readRetryState(userId: string, job: RecordData): RetryState | null {
  try {
    const saved = sessionStorage.getItem(retrySlot(userId, job));
    if (saved === null) return null;
    const value = record(JSON.parse(saved));
    if (value.status === 'ACKNOWLEDGED' && typeof value.childJobId === 'string' && value.childJobId &&
        typeof value.childRunId === 'string' && value.childRunId)
      return { status: 'ACKNOWLEDGED', childJobId: value.childJobId, childRunId: value.childRunId };
  } catch { /* Missing storage or damaged intent cannot prove no retry was sent. */ }
  return { status: 'UNKNOWN' };
}

// A parent readback cannot establish whether a retry child was created.
export function researchCommands(userId: string, canWrite: boolean) {
  let busy = false, uncertain = false;
  const acknowledged = new Set<string>();
  return {
    retryState(job: RecordData) { validateResearchJob(job, userId); return readRetryState(userId, job); },
    reconcile(job: RecordData) { validateResearchJob(job, userId); uncertain = false; },
    async act(job: RecordData, action: 'cancel' | 'retry', debate: boolean) {
      validateResearchJob(job, userId, undefined, debate ? 'research.debate' : 'research.qlib');
      const commandKey = `${String(job.job_id)}:${action}`;
      if (!canWrite || busy || uncertain || acknowledged.has(commandKey)) throw new Error('当前操作不可用；请先核对任务记录，未重复提交。');
      if ((action === 'cancel' && (terminalResearchJob(job) || job.cancel_requested === true)) ||
          (action === 'retry' && !['FAILED', 'CANCELLED'].includes(String(job.status))))
        throw new Error('当前任务状态不允许这项操作。');
      if (action === 'retry') {
        if (readRetryState(userId, job)) throw new Error('上次重试正在核对或已有新任务。请先到任务记录查看。请查看任务记录；刷新原任务不能证明子任务未创建。');
        try { sessionStorage.setItem(retrySlot(userId, job), JSON.stringify({ status: 'UNKNOWN' })); }
        catch { throw new Error('无法记录这次重试，任务尚未发出。'); }
      }
      busy = true;
      try {
        const result = await post<RecordData>(`${researchJobPath(String(job.job_id), debate)}/${action}`);
        validateResearchJob(result, userId, action === 'cancel' ? String(job.job_id) : undefined, debate ? 'research.debate' : 'research.qlib');
        if (action === 'retry' && (result.run_id === job.run_id || result.job_id === job.job_id || result.parent_run_id !== job.run_id))
          throw new Error('服务未返回新任务与原任务的编号，还不能确认重试是否成功。');
        if (action === 'retry') sessionStorage.setItem(retrySlot(userId, job), JSON.stringify({
          status: 'ACKNOWLEDGED', childJobId: result.job_id, childRunId: result.run_id,
        }));
        acknowledged.add(commandKey);
        return result;
      } catch (error) { uncertain = true; throw error; }
      finally { busy = false; }
    },
  };
}

export const submitQlib = (body: ReturnType<typeof qlibRequest>) => post<RecordData>('/research/qlib', body);
export const submitDebate = (body: ReturnType<typeof debateRequest>) => post<RecordData>('/research/debates', body);
export const readDebateCapabilities = () => request<RecordData>('/research/debate-capabilities');
