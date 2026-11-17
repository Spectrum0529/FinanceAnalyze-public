import { record } from './native-research-api';

export const benchmarkIds = ['sp500', 'nasdaq100', 'csi300'] as const;
export type BenchmarkId = (typeof benchmarkIds)[number];
export const indexEtfs = {
  sp500: { label: '标普500（ETF SPY）', provider: 'yfinance', ticker: 'SPY', color: '#376fbd' },
  nasdaq100: { label: '纳斯达克100（ETF QQQ）', provider: 'yfinance', ticker: 'QQQ', color: '#bc6b24' },
  csi300: { label: '沪深300（美股ETF ASHR）', provider: 'yfinance', ticker: 'ASHR', color: '#9a54b2' },
} satisfies Record<BenchmarkId, { label: string; provider: string; ticker: string; color: string }>;

export function benchmarkId(value: unknown): BenchmarkId | undefined {
  return typeof value === 'string' && Object.hasOwn(indexEtfs, value) ? value as BenchmarkId : undefined;
}

export function indexDatasetRows(value: unknown, id: BenchmarkId) {
  return Array.isArray(value) ? value.map(record).filter(row => record(row.benchmark_identity).index_id === id &&
    typeof row.dataset_id === 'string' && row.dataset_id.length > 0) : [];
}

// Preserve only the backtest fields this journey can populate.
const returnFields = ['strategy_instance_id', 'builtin_strategy', 'dataset_id', 'start_date', 'end_date', 'index_benchmarks', 'portfolio_job', ...benchmarkIds] as const;
const backtestAmountFields = ['initial_cash', 'fee_per_share', 'minimum_fee', 'spread_bps'] as const;
export function backtestReturnParams(source: URLSearchParams) {
  const result = new URLSearchParams();
  for (const field of returnFields) {
    const value = source.get(field);
    if (value && /^[a-zA-Z0-9_-]{1,128}$/.test(value) &&
        (!field.endsWith('_date') || /^\d{4}-\d{2}-\d{2}$/.test(value))) result.set(field, value);
  }
  for (const field of backtestAmountFields) {
    const value = source.get(field);
    if (value !== null && /^[\d+eE. -]{0,48}$/.test(value)) result.set(field, value);
  }
  const version = source.get('dataset_version'), hash = source.get('dataset_hash');
  if (version !== null) result.set('dataset_version', /^\d{1,9}$/.test(version) ? version : 'invalid');
  if (hash !== null) result.set('dataset_hash', /^[0-9a-f]{64}$/.test(hash) ? hash : 'invalid');
  return result;
}

export function datasetBacktestHref(id: unknown, version: unknown, hash: unknown) {
  if (typeof id !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(id) ||
      typeof version !== 'number' || !Number.isSafeInteger(version) || version < 1 ||
      typeof hash !== 'string' || !/^[0-9a-f]{64}$/.test(hash)) return null;
  return '#research?' + new URLSearchParams({
    dataset_id: id, dataset_version: String(version), dataset_hash: hash, builtin_strategy: 'buy_hold',
  });
}

