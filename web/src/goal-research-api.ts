import { post, request } from './api';
import { record } from './native-research-api';
import type { GoalProvider } from './goal-research-provider-settings';
import type { GoalAction, GoalReadiness, GoalTaskView } from './goal-research-workspace';
import { sourceDescription } from './source-labels';

export const goalPath = '/research/goals';
export const goalTaskPath = (id: string) => goalPath + '/' + encodeURIComponent(id);
export const goalTaskLink = (id: string) => '#intelligent?goal_task=' + encodeURIComponent(id);
const identifier = (value: unknown): value is string =>
  typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
const counter = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const label = (value: unknown) => typeof value === 'string' && value.trim() ? value : undefined;
const strings = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => !!label(item)) : [];
const actions = new Set<unknown>(['pause', 'cancel', 'resume']);
const isAction = (value: unknown): value is GoalAction => actions.has(value);
const statuses = {
  QUEUED: '已排队', EXECUTING: '正在准备', CHILD_RUNNING: '子任务进行中', PAUSE_REQUESTED: '正在请求暂停',
  CANCEL_REQUESTED: '正在请求取消', RESUME_REQUESTED: '正在请求继续', PAUSED: '已暂停', WAITING: '等待处理',
  SUCCEEDED: '已完成', FAILED: '未完成', CANCELLED: '已取消',
};
const stages = ['PLAN', 'DATA', 'FREEZE', 'START', 'RESEARCH', 'REVIEW'];
const reviewDecisions = {
  NO_MORE_DATA: '复核完成，无需补充数据。',
  NO_EXPANSION_RANGE: '当前授权范围内无法扩大数据区间，已保留首轮结果。',
  NO_VALID_RECOMMENDATION: '未取得有效补数建议，已保留首轮结果。',
} as const;
const reasons: Record<string, string> = {
  GOAL_DEFAULTS_UNAVAILABLE: '目标研究配置暂不可用。',
  GOAL_DEFAULTS_INVALID: '请核对研究默认设置。',
  GOAL_MODEL_REQUIRED: '请选择已保存的模型或团队。',
  GOAL_CREDENTIAL_REQUIRED: '缺少所选模型的本机密钥。',
  GOAL_SOURCE_REQUIRED: '缺少能处理所需范围的数据源连接。',
  GOAL_BUDGET_REQUIRED: '请选择研究预算预设。',
  GOAL_EFFECT_UNCERTAIN: '上一步的实际结果待核对。已占用额度保留。',
  GOAL_DEADLINE_EXCEEDED: '本次研究已到截止时间。',
  GOAL_BUDGET_EXCEEDED: '本次研究剩余额度不足。',
  GOAL_DATA_ROUND_LIMIT: '已达到补数据次数上限。',
  GOAL_RESEARCH_NO_RESULT: '子研究没有可读取的成功结果。',
  GOAL_RESEARCH_CANCELLED: '子研究已取消。',
  GOAL_DATA_MISSING_SESSIONS: '数据仍有交易日缺口。',
  GOAL_DATA_REQUIREMENT_UNSUPPORTED: '当前来源或后台服务无法处理这个市场或价格类型。',
  GOAL_DATA_CREDENTIAL_REQUIRED: '所选数据源缺少本机密钥。',
  GOAL_DATA_RUNTIME_UNAVAILABLE: '所选数据源暂不可执行下载。',
  GOAL_DATA_BUDGET_EXCEEDED: '补数据需要的额度超过任务剩余限制。',
  GOAL_REVIEW_NEED_INVALID: '复核提出的数据范围未通过校验。',
  GOAL_REVIEW_DATA_WINDOW_CHANGED: '返回的数据日期与复核确认的范围不一致。',
};
const settingLabels = {
  model: '请检查保存的模型、团队与本机密钥配置。',
  data: '请检查可用数据源、保存连接与来源密钥配置。',
  budget: '请检查已保存的研究预算。',
  executor: '请检查自主研究执行环境。',
};
const settingTargets = { model: 'models', data: 'data', budget: undefined, executor: undefined } as const;
const budgetLabels: Record<string, [string, string]> = {
  model_requests: ['模型请求', '次'], tokens: ['模型用量', ' token'], download_jobs: ['下载任务', '次'],
  download_rows: ['下载数据', '行'], research_attempts: ['研究实验', '次'], compute_seconds: ['计算时间', '秒'],
  download_requests: ['下载请求', '次'], download_bytes: ['下载量', '字节'],
  artifact_bytes: ['产物空间', '字节'], prefixes: ['历史时点计算', '次'],
};
const statusText = (value: unknown) => typeof value === 'string' && Object.hasOwn(statuses, value)
  ? statuses[value as keyof typeof statuses] : undefined;

export function goalReadiness(value: unknown): GoalReadiness {
  const row = record(value);
  if (typeof row.ready !== 'boolean' || typeof row.can_create !== 'boolean' || !Array.isArray(row.blockers))
    throw Error('尚未读到完整的研究设置。请重新打开。');
  return {
    status: row.ready ? 'ready' : 'blocked',
    canCreate: row.can_create,
    missing: row.blockers.map((value) => {
      const blocker = record(value);
      const setting = label(blocker.setting);
      const key = setting && Object.hasOwn(settingLabels, setting) ? setting as keyof typeof settingLabels : undefined;
      const code = label(blocker.code);
      return { label: code && Object.hasOwn(reasons, code) ? reasons[code]
        : key ? settingLabels[key] : '有一项研究配置待处理。', target: key && settingTargets[key], code };
    }),
  };
}

export function goalDefaults(value: unknown) {
  const defaults = record(record(value).defaults);
  const row = record(value);
  const limits = record(row.limits);
  return [
    label(defaults.team_snapshot_id) ? Object.keys(record(defaults.team_role_map)).length ? '已分配团队成员' : '团队默认模型'
      : label(record(defaults.model_profile).profile_id) ? '已保存模型' : undefined,
    Array.isArray(defaults.connection_ids) ? '保存连接 ' + strings(defaults.connection_ids).length + ' 个' : undefined,
    counter(row.deadline_seconds) ? '截止时限 ' + row.deadline_seconds + ' 秒' : undefined,
    ...Object.entries(budgetLabels).flatMap(([key, [name, unit]]) => counter(limits[key]) ? [name + ' ' + limits[key] + unit] : []),
  ].filter((item): item is string => item !== undefined);
}

function savedReport(value: unknown) {
  const result = record(value);
  return identifier(result.session_id) && identifier(result.attempt_id)
    ? { sessionId: result.session_id, attemptId: result.attempt_id } : undefined;
}

function savedDataset(value: unknown, round?: string) {
  const data = record(value);
  const ref = record(data.reference);
  if (!identifier(ref.dataset_id)) return undefined;
  const provenance = record(data.provenance);
  const coverage = record(data.coverage);
  const window = record(data.window);
  return {
    id: ref.dataset_id, name: label(ref.name) ?? ref.dataset_id, round,
    version: label(ref.version) ?? (counter(ref.version) ? String(ref.version) : undefined),
    source: label(record(provenance.source).provider), market: capabilityText(provenance.market), kind: capabilityText(provenance.kind),
    frequency: capabilityText(provenance.frequency), adjustment: capabilityText(provenance.adjustment), currency: label(provenance.currency),
    start: label(window.start) ?? label(coverage.first), end: label(window.end) ?? label(coverage.last),
    symbols: strings(data.symbols), hash: label(ref.content_hash),
  };
}

export function goalTask(value: unknown, expected?: string): GoalTaskView {
  const row = record(value);
  const status = statusText(row.status);
  const index = typeof row.stage === 'string' ? stages.indexOf(row.stage) : -1;
  if (!identifier(row.task_id) || expected !== undefined && row.task_id !== expected || !label(row.goal) ||
      !status || index < 0 || !counter(row.revision) || row.revision === 0 || !Array.isArray(row.allowed_actions))
    throw Error('任务记录与所选研究不一致，请刷新后查看。');
  const progress = record(row.progress);
  const data = record(progress.DATA);
  const plan = record(progress.PLAN);
  const child = record(row.child);
  const reason = label(row.reason_code);
  const spent = record(row.spent), limits = record(row.limits), remaining = record(row.remaining);
  const terminal = ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(row.status));
  const state = row.status === 'WAITING' || row.status === 'PAUSED' ? 'blocked'
    : row.status === 'FAILED' ? 'failed' : ['EXECUTING', 'CHILD_RUNNING'].includes(String(row.status)) ? 'active' : undefined;
  const planNeeds = Array.isArray(plan.data_needs) ? plan.data_needs.map(record) : [];
  const history = Array.isArray(progress.additional_data) ? progress.additional_data.map(record) : [];
  const prior = history.at(-1);
  const priorChild = record(prior?.prior_child);
  const priorDataset = savedDataset(prior?.prior_data, '首轮数据');
  const currentDataset = savedDataset(data, priorDataset ? '第二轮数据' : undefined);
  const newDataset = currentDataset && (!priorDataset || currentDataset.id !== priorDataset.id &&
    (!priorDataset.hash || currentDataset.hash !== priorDataset.hash)) ? currentDataset : undefined;
  const review = record(progress.REVIEW);
  const extension = record(prior?.result);
  const extensionNeed = record(extension.need);
  const requestedNeeds = Object.keys(extensionNeed).length ? [extensionNeed] : planNeeds;
  const extensionReason = extension.reason_code === 'HISTORY_SENSITIVITY' ? '检查结果对历史区间的敏感性。'
    : extension.reason_code === 'LONGER_DEVELOPMENT_SAMPLE' ? '扩大开发期的历史样本。' : undefined;
  const childSession = child.kind === 'session' && identifier(child.id) ? child.id : undefined;
  const priorSession = priorChild.kind === 'session' && identifier(priorChild.id) ? priorChild.id : undefined;
  const acquisition = record(data.provenance).acquisition;
  const dataState = newDataset && (data.reused === true ? '复用已保存数据' : data.reused === false ? '已保存新数据版本' : undefined);
  const acquisitionDetail = data.reused !== false ? undefined
    : acquisition === 'FULL_WINDOW_REFRESH' ? '为保持复权基准一致，已重新获取整个所需日期范围。'
    : acquisition === 'GAP_FILL' ? '已补充缺失数据并保存新版本。'
    : acquisition === 'SINGLE_SNAPSHOT_SLICE' ? '已从同一保存快照截取所需日期范围。' : undefined;
  const previousResult = savedReport(prior?.prior_result);
  const latestResult = savedReport(progress.RESEARCH);
  const report = latestResult && latestResult.sessionId !== (previousResult?.sessionId ?? priorSession) ? latestResult : undefined;
  const noNewData = reason === 'GOAL_REVIEW_NO_NEW_DATA';
  const stageRows: GoalTaskView['stages'] = [];
  if (Object.hasOwn(progress, 'PLAN') || index === 0 && state)
    stageRows.push({ id: 'understand', state: Object.hasOwn(progress, 'PLAN') ? 'done' : state! });
  if (dataState || index === 1 && state)
    stageRows.push({ id: 'find', state: dataState ? 'done' : state!, detail: dataState });
  if (dataState)
    stageRows.push({ id: 'fill', state: data.reused === true ? 'skipped' : 'done', detail: acquisitionDetail });
  else if (index === 1 && (prior || child.kind === 'job' && identifier(child.id))) {
    const fillState = state ?? (row.status === 'QUEUED' ? 'queued' : undefined);
    if (fillState) stageRows.push({ id: 'fill', state: fillState });
  }
  if (report || childSession || (index === 3 || index === 4) && state)
    stageRows.push({ id: 'start', state: report ? 'done' : state ?? 'blocked' });
  if (row.stage === 'REVIEW' || prior) {
    const uncertain = reason === 'GOAL_EFFECT_UNCERTAIN' && row.stage === 'REVIEW';
    const outcome = typeof review.decision === 'string' && Object.hasOwn(reviewDecisions, review.decision)
      ? review.decision as keyof typeof reviewDecisions : undefined;
    const decision = uncertain ? '复核结果待核对。'
      : outcome ? reviewDecisions[outcome]
      : prior ? '复核建议延长历史范围。'
      : terminal && report ? '已保留首轮结果。' : '判断是否补充数据。';
    const reviewState = uncertain ? 'blocked'
      : prior || outcome === 'NO_MORE_DATA' ? 'done'
      : outcome || row.status === 'SUCCEEDED' ? 'ended'
      : state ?? (row.status === 'QUEUED' ? 'queued' : undefined);
    if (reviewState) stageRows.push({ id: 'review', state: reviewState, detail: decision });
  }
  if (report || previousResult) stageRows.push({ id: 'results', state: 'done' });

  return {
    id: row.task_id, goal: String(row.goal), status, revision: row.revision,
    terminal, updatedAt: label(row.updated_at), stages: stageRows,
    allowedActions: terminal ? [] : [...new Set(row.allowed_actions.filter(isAction))],
    failure: reason && !noNewData ? (Object.hasOwn(reasons, reason) ? reasons[reason] : '服务记录了待处理或失败原因。') : undefined,
    noNewData,
    extensionReason,
    reasonCode: reason && /^[A-Z][A-Z0-9_]{0,63}$/.test(reason) ? reason : undefined,
    deadline: typeof row.deadline === 'number' && row.deadline > 0 && row.deadline <= 8.64e12 ? row.deadline : undefined,
    billingUnknown: row.billing_status === 'UNKNOWN',
    missing: reason && Object.hasOwn(reasons, reason) && ['GOAL_CREDENTIAL_REQUIRED', 'GOAL_MODEL_REQUIRED', 'GOAL_DATA_CREDENTIAL_REQUIRED', 'GOAL_SOURCE_REQUIRED'].includes(reason)
      ? [{ label: reason === 'GOAL_CREDENTIAL_REQUIRED' || reason === 'GOAL_MODEL_REQUIRED' ? '打开模型配置' : '打开数据源配置',
        target: reason === 'GOAL_CREDENTIAL_REQUIRED' || reason === 'GOAL_MODEL_REQUIRED' ? 'models' : 'data' }]
      : [],
    budgets: Object.entries(budgetLabels).flatMap(([key, [name, unit]]) => {
      const limit = limits[key], used = spent[key], available = remaining[key];
      return [limit, used, available].some(counter) ? [{ label: name, unit,
        limit: counter(limit) ? limit : undefined, spent: counter(used) ? used : undefined,
        remaining: counter(available) ? available : undefined }] : [];
    }),
    requirements: requestedNeeds.map((need) => ({
      market: capabilityText(need.market), symbols: strings(need.symbols), kind: capabilityText(need.kind), frequency: capabilityText(need.frequency),
      adjustment: capabilityText(need.adjustment), currency: label(need.currency), start: label(need.start), end: label(need.end),
    })),
    datasets: [priorDataset, newDataset].filter((dataset) => dataset !== undefined),
    downloadJobId: child.kind === 'job' && index === 1 && identifier(child.id) ? child.id : undefined,
    childSessionId: childSession ?? report?.sessionId,
    previousSessionId: priorSession,
    additionalDataRequested: history.length > 0,
    previousResult,
    result: report,
  };
}

export const goalOnlyProvider = (provider: unknown) => ['tushare', 'akshare_cn', 'openbb_nasdaq'].includes(String(provider));
const readable: Record<string, string> = {
  CN: '中国市场', US: '美国市场', CN_EQUITY: '沪深 A 股', US_EQUITY: '美股', US_ETF: '美国 ETF',
  bars: '行情', '1Day': '日线', raw: '不复权', qfq: '前复权', split_adjusted: '拆股调整',
  provider_observed_unverified: '尚未确认来源怎样调整价格', REQUIRED: '需要本机密钥', NONE: '无需密钥',
  RUNTIME_UNAVAILABLE: '本机运行环境不可用', CREDENTIAL_REQUIRED: '缺少本机密钥',
  BOUND_UNVERIFIED: '本机组件已就绪，尚未验证能否取到数据', UNVERIFIED: '未核验', PUBLIC_UNVERIFIED: '公开来源，实际访问未核验',
};
function capabilityText(value: unknown) {
  const items = Array.isArray(value) ? strings(value) : [label(value)].filter((v): v is string => !!v);
  return items.map((item) => Object.hasOwn(readable, item) ? readable[item] : item).join('、') || undefined;
}

export function goalProviders(value: unknown): GoalProvider[] {
  const items = record(value).items;
  if (!Array.isArray(items)) return [];
  return items.map(record).filter((row) => identifier(row.provider)).map((row) => {
    const id = String(row.provider);
    return {
      id, ...sourceDescription(id, row.display_name, row.display_summary),
      market: capabilityText(row.market), kind: capabilityText(row.kind), assets: capabilityText(row.assets),
      frequency: capabilityText(row.frequency), adjustment: capabilityText(row.adjustment ?? row.adjustments ?? record(row.metadata).adjustment),
      currency: label(row.currency), credentials: capabilityText(row.credentials),
      entitlement: capabilityText(row.entitlement), coverage: capabilityText(row.coverage),
      readiness: capabilityText(row.availability) ?? '服务未提供执行状态',
      connectionConfigurable: row.connection_configurable === true ||
        goalOnlyProvider(id) && ['CREDENTIAL_REQUIRED', 'BOUND_UNVERIFIED'].includes(String(row.availability)),
    };
  });
}

export type GoalCommand =
  | { kind: 'create'; goal: string; idempotency_key: string }
  | { kind: 'control'; id: string; action: GoalAction; expected_revision: number; idempotency_key: string };

export type ProfileReference = { profile_id: string; revision: number };
export const goalResearchRoles = ['hypothesis', 'coder', 'critic', 'reporter'] as const;
type TeamRoleMap = Partial<Record<typeof goalResearchRoles[number], string>>;
export type GoalDefaults = {
  planner: ProfileReference | null;
  model_profile: ProfileReference | null;
  team_snapshot_id: string | null;
  team_role_map?: TeamRoleMap;
  connection_ids: string[];
  budget_preset_id: string | null;
};
export type SettingsCommand = { defaults: GoalDefaults; expected_revision: number; idempotency_key: string };
const ref = (value: unknown): value is ProfileReference => {
  const row = record(value);
  return identifier(row.profile_id) && counter(row.revision) && row.revision > 0;
};
const nullableId = (value: unknown) => value === null || identifier(value);
function isDefaults(value: unknown): value is GoalDefaults {
  const row = record(value);
  const mapping = row.team_role_map === undefined ? [] : Object.entries(record(row.team_role_map));
  if (row.team_role_map !== undefined && (row.team_role_map === null || typeof row.team_role_map !== 'object' ||
      Array.isArray(row.team_role_map))) return false;
  return (row.planner === null || ref(row.planner)) && (row.model_profile === null || ref(row.model_profile)) &&
    nullableId(row.team_snapshot_id) && nullableId(row.budget_preset_id) &&
    (!mapping.length || identifier(row.team_snapshot_id)) &&
    mapping.every(([role, member]) => goalResearchRoles.some((key) => role === key) && identifier(member)) &&
    !(row.model_profile && row.team_snapshot_id) && Array.isArray(row.connection_ids) &&
    row.connection_ids.length <= 8 && row.connection_ids.every(identifier) &&
    new Set(row.connection_ids).size === row.connection_ids.length;
}
export function settingsCommand(value: unknown): value is SettingsCommand {
  const row = record(value);
  return identifier(row.idempotency_key) && counter(row.expected_revision) && isDefaults(row.defaults);
}
export function goalSettings(value: unknown) {
  const row = record(value);
  if (!counter(row.revision) || !isDefaults(row.defaults)) throw Error('暂时无法确认研究默认设置是否保存。请刷新查看。');
  return { revision: row.revision, defaults: row.defaults };
}
export async function saveGoalSettings(command: SettingsCommand) {
  return goalSettings(await request<unknown>(goalPath + '/defaults', {
    method: 'PUT', body: JSON.stringify(command),
  }));
}

export function goalCommand(value: unknown): value is GoalCommand {
  const row = record(value);
  return identifier(row.idempotency_key) && (row.kind === 'create'
    ? typeof row.goal === 'string' && !!row.goal.trim() && row.goal.length <= 2000
    : row.kind === 'control' && identifier(row.id) && isAction(row.action) &&
      counter(row.expected_revision) && row.expected_revision > 0);
}

export async function sendGoal(command: GoalCommand) {
  const { idempotency_key } = command;
  if (command.kind === 'create') return goalTask(await post<unknown>(goalPath, { goal: command.goal, idempotency_key }));
  const { id, action, expected_revision } = command;
  return goalTask(await post<unknown>(goalTaskPath(id) + '/control', { action, expected_revision, idempotency_key }), id);
}

