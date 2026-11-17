import { ApiError, post, request } from './api';
import type { RecordData } from './api';
import { assertUsSecurity } from './history-view-model';
import type { HistoryIntent, ResultCommand, ManagedResult } from './history-view-model';
// New requests follow FA015 open_source_reuse CONTRACT.r01; historical lifecycle routes stay unchanged.
// HTTP bindings are candidate code; offline tests do not certify a running service or market data.
export function researchHistoryBody(intent: HistoryIntent, key: string) {
  if (Object.hasOwn(intent, 'slippageBps') || Object.hasOwn(intent, 'slippage_bps'))
    throw new ApiError(
      422,
      'DAILY_LEGACY_SLIPPAGE_UNSUPPORTED',
      '旧版滑点不再支持。新回测请使用高级设置中的买卖价差；不会自动换算。',
    );
  assertUsSecurity(intent.security, intent.provider);
  if (
    intent.history &&
    (!intent.history.id ||
      !Number.isSafeInteger(intent.history.version) ||
      intent.history.version <= 0 ||
      intent.historyPolicy === 'refresh')
  )
    throw new Error('指定历史必须绑定正整数不可变版本，且不能同时重新获取。');
  return {
    ticker: intent.security.symbol,
    start_date: intent.start,
    end_date: intent.end,
    strategy: intent.strategy,
    provider: intent.provider,
    history_policy: intent.historyPolicy === 'refresh' ? 'refresh' : 'reuse_or_download',
    history_id: intent.history?.id ?? null,
    history_version: intent.history?.version ?? null,
    initial_cash: intent.initialCash,
    fee_per_share: intent.feePerShare,
    minimum_fee: intent.minimumFee,
    spread_bps: intent.spreadBps ?? '0',
    ...(intent.cachePolicy === 'bypass' && intent.history ? { cache_policy: 'bypass' } : {}),
    idempotency_key: key,
  };
}
export function submitResearchHistory(intent: HistoryIntent, key: string) {
  return post<RecordData>('/research/daily/backtests', researchHistoryBody(intent, key));
}
export function resultListPath(trash: boolean, offset: number, limit = 20) {
  return `/research/daily/results?state=${trash ? 'deleted' : 'active'}&limit=${limit}&offset=${offset}`;
}
export function seriesPath(
  run: string,
  kind: 'equity_curve' | 'trades' | 'decisions' | 'orders' | 'rejections' | 'unsubmitted_outputs',
  offset: number,
  limit = 25,
) {
  return `/research/daily/results/${encodeURIComponent(run)}/series?kind=${kind}&limit=${Math.min(1000, limit)}&offset=${offset}`;
}
export function resultLifecycleBody(
  command: ResultCommand,
  target: { id: string; revision: string },
  index: number,
) {
  const revision = Number(target.revision);
  if (!Number.isSafeInteger(revision) || revision < 0)
    throw new Error('结果修订无效，请重新加载。');
  return {
    action: command.kind,
    ...(command.kind === 'rename' ? { name: command.name } : {}),
    expected_revision: revision,
    idempotency_key: `${command.requestId}-${index}`,
  };
}
export function changeSingleResult(command: ResultCommand, index: number) {
  const target = command.targets[index];
  if (!target) throw new Error('未选择结果。');
  return post<RecordData>(
    `/research/daily/results/${encodeURIComponent(target.id)}/lifecycle`,
    resultLifecycleBody(command, target, index),
  );
}
export function readResultForReconcile(id: string) {
  return request<RecordData>(
    `/research/daily/results/${encodeURIComponent(id)}?include_deleted=true`,
  );
}
export function normalizeManagedResult(value: RecordData): ManagedResult {
  if (
    typeof value.run_id !== 'string' ||
    typeof value.deleted !== 'boolean' ||
    !Number.isSafeInteger(value.revision) ||
    Number(value.revision) < 0
  )
    throw new Error('结果列表缺少有效的生命周期记录。');
  return {
    id: value.run_id,
    revision: String(value.revision),
    name: typeof value.name === 'string' ? value.name : '未命名结果',
    symbol: String(value.ticker ?? ''),
    provider: value.provider === 'akshare' ? 'AKShare' : String(value.provider ?? ''),
    createdAt: String(value.created_at ?? ''),
    deleted: value.deleted,
    canManage: true,
  };
}
