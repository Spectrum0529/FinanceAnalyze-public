import type { RecordData } from './api';
// Internal view-model; new requests use spread, while saved family1/2 data keeps its stored meaning.
export type SecurityChoice = {
  key: string;
  symbol: string;
  name: string;
  provider: string;
  market: string | null;
  currency: string | null;
  assetClass: string | null;
  identityState: 'verified' | 'candidate' | 'typed';
  identity: RecordData;
};
export type HistoryCoverage = {
  id: string;
  revision: string;
  version?: number;
  observationHash?: string;
  historyHash?: string;
  selectionHash?: string;
  symbol: string;
  provider: string;
  start: string | null;
  end: string | null;
  rows: number;
  status: 'complete' | 'partial' | 'none' | 'unknown';
  message: string;
  savedAt: string | null;
};
export type HistoryIntent = {
  security: SecurityChoice;
  start: string;
  end: string;
  strategy: string;
  provider: string;
  historyPolicy: 'prefer_local' | 'refresh';
  history?: { id: string; version: number; observationHash?: string } | null;
  cachePolicy?: 'bypass';
  initialCash: string;
  feePerShare: string;
  minimumFee: string;
  spreadBps: string;
};
export type HistoryUiConfig = {
  canBypassCache?: boolean;
  commonSecurities?: SecurityChoice[];
  providers: {
    id: string;
    label: string;
    help: string;
    pricePolicyLabel?: string;
  }[];
  strategies: { id: string; label: string; help: string }[];
  defaultProvider: string;
  defaultStrategy: string;
  defaultStart: string;
  defaultEnd: string;
  earliestStart: string;
  latestEnd: string;
  maxCalendarDays: number;
  initialCash: string;
  feePerShare: string;
  minimumFee: string;
  spreadBps: string;
  engine?: RecordData;
  costModel?: RecordData;
  strategyVersion?: string;
  calculationVersion?: string;
};
export type ManagedResult = {
  id: string;
  revision: string;
  name: string;
  symbol: string;
  provider: string;
  createdAt: string;
  deleted: boolean;
  canManage: boolean;
};
export type ViewPage<T> = {
  items: T[];
  total: number;
  offset: number;
  limit: number;
};
export type ResultCommand = {
  requestId: string;
  kind: 'rename' | 'delete' | 'restore';
  targets: { id: string; revision: string }[];
  name?: string;
};
export type ResultOutcome = {
  confirmed: boolean;
  completed: string[];
  failures: { id: string; message: string }[];
};
export type HistoryUiPort = {
  search(query: string, provider: string, signal: AbortSignal): Promise<SecurityChoice[]>;
  resolve(query: string, provider: string, signal: AbortSignal): Promise<SecurityChoice>;
  coverage(
    intent: Pick<HistoryIntent, 'security' | 'start' | 'end' | 'provider'>,
    signal: AbortSignal,
  ): Promise<HistoryCoverage>;
  listHistory(
    offset: number,
    limit: number,
    signal: AbortSignal,
  ): Promise<ViewPage<HistoryCoverage>>;
  listResults(
    trash: boolean,
    offset: number,
    limit: number,
    signal: AbortSignal,
  ): Promise<ViewPage<ManagedResult>>;
  changeResults(command: ResultCommand): Promise<ResultOutcome>;
  lookupChange(requestId: string, signal: AbortSignal): Promise<ResultOutcome>;
  start(intent: HistoryIntent, requestId: string): Promise<{ jobId: string }>;
  save(intent: HistoryIntent, requestId: string): Promise<{ jobId: string }>;
};
export function assertUsSecurity(security: SecurityChoice, provider: string) {
  if (
    !['verified', 'candidate', 'typed'].includes(security.identityState) ||
    !security.key ||
    !security.symbol ||
    security.provider !== provider ||
    (security.market !== null && security.market !== 'US') ||
    (security.currency !== null && security.currency !== 'USD') ||
    (security.assetClass !== null && !['US_EQUITY', 'US_ETF'].includes(security.assetClass)) ||
    (security.identityState === 'verified' &&
      (!security.market || !security.currency || !security.assetClass))
  )
    throw new Error(
      '证券身份或市场不匹配。当前仅支持美元美股/ETF；候选及输入代码仍须由服务在执行前核实。',
    );
  return security;
}
export function typedSecurity(query: string, provider: string): SecurityChoice {
  const symbol = query.trim().toUpperCase();
  if (!/^[A-Z][A-Z0-9.-]{0,17}$/.test(symbol))
    throw new Error('请输入代码，或从搜索建议中选择证券；不会猜测代码别名。');
  return {
    key: `typed:${provider}:${symbol}`,
    symbol,
    name: symbol,
    provider,
    market: null,
    currency: null,
    assetClass: null,
    identityState: 'typed',
    identity: {},
  };
}
export function presetStart(end: string, years: number): string {
  if (![1, 5, 10, 20, 30].includes(years)) throw new Error('请选择支持的年限。');
  const date = new Date(`${end}T00:00:00Z`);
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== end)
    throw new Error('结束日期无效。');
  const day = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCFullYear(date.getUTCFullYear() - years);
  const last = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(day, last));
  return date.toISOString().slice(0, 10);
}
export function validateHistoryDates(start: string, end: string, config: HistoryUiConfig) {
  for (const value of [start, end]) {
    const d = new Date(`${value}T00:00:00Z`);
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(value) ||
      !Number.isFinite(d.getTime()) ||
      d.toISOString().slice(0, 10) !== value
    )
      throw new Error('请填写有效的开始和结束日期。');
  }
  if (start > end) throw new Error('开始日期不能晚于结束日期。');
  if (start < config.earliestStart || end > config.latestEnd)
    throw new Error(`服务允许的日期为${config.earliestStart}至${config.latestEnd}。`);
  if ((Date.parse(end) - Date.parse(start)) / 86400000 + 1 > config.maxCalendarDays)
    throw new Error('所选范围超过服务允许的历史长度，请缩短日期。');
}
