import { ApiError, request } from './api.ts';

export const cacheMiB = 1024 * 1024;
export const cacheLimitMiB = 256;
const cacheKinds = ['dataset_parse', 'trusted_indicator', 'exact_backtest'] as const;
const dispositions = ['hit', 'miss', 'bypassed', 'disabled', 'ineligible'] as const;
type CacheKind = typeof cacheKinds[number];
export type CacheView = {
  schema: 'computation-cache-1';
  settings: { enabled: boolean; max_bytes: number; revision: number };
  stats: { entries: number; bytes: number; hits: number; misses: number; bypasses: number; evictions: number };
  location: 'platform_database';
  eligible: CacheKind[];
};

type Confirmation = { expected_revision: number; idempotency_key: string };
export type CacheIntent =
  | { kind: 'save'; body: Confirmation & { enabled: boolean; max_bytes: number } }
  | { kind: 'clear'; body: Confirmation & { confirm: true } };

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const counter = (n: unknown): n is number => typeof n === 'number' && Number.isSafeInteger(n) && n >= 0;
const capacity = (n: unknown): n is number => counter(n) && n >= cacheMiB && n <= cacheLimitMiB * cacheMiB;
const kind = (value: unknown): value is CacheKind => cacheKinds.some(name => name === value);
const badReceipt = () => new ApiError(0, 'CACHE_RECEIPT_INVALID', '无法确认是否已保存结果复用设置，请刷新后核对。');

export function parseCacheCapacity(input: string) {
  if (!/^[1-9]\d{0,2}(?:\.\d{1,20})?$/.test(input)) return null;
  const bytes = Number(input) * cacheMiB;
  return capacity(bytes) ? bytes : null;
}

export function readCacheView(value: unknown): CacheView {
  const row = record(value), s = record(row.settings), t = record(row.stats);
  const { entries, bytes, hits, misses, bypasses, evictions } = t;
  const { enabled, max_bytes, revision } = s;
  if (row.schema !== 'computation-cache-1' || row.location !== 'platform_database'
    || typeof enabled !== 'boolean' || !capacity(max_bytes) || !counter(revision)
    || !counter(entries) || !counter(bytes) || !counter(hits) || !counter(misses)
    || !counter(bypasses) || !counter(evictions) || !Array.isArray(row.eligible)
    || !row.eligible.every(kind)) throw badReceipt();
  return {
    schema: row.schema, location: row.location, eligible: row.eligible,
    settings: { enabled, max_bytes, revision }, stats: { entries, bytes, hits, misses, bypasses, evictions },
  };
}

export function isCacheIntent(value: unknown): value is CacheIntent {
  const row = record(value), body = record(row.body);
  if (Object.keys(row).sort().join(',') !== 'body,kind' || !counter(body.expected_revision)
    || typeof body.idempotency_key !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(body.idempotency_key)) return false;
  const fields = Object.keys(body).sort().join(',');
  if (row.kind === 'clear') return fields === 'confirm,expected_revision,idempotency_key' && body.confirm === true;
  return row.kind === 'save' && fields === 'enabled,expected_revision,idempotency_key,max_bytes'
    && typeof body.enabled === 'boolean' && capacity(body.max_bytes);
}

export function readCacheDisposition(value: unknown) {
  return dispositions.find(disposition => disposition === value);
}
export const cacheKindLabel: Record<CacheKind, string> = {
  dataset_parse: '研究数据解析（自主研究开发阶段的合格分块数据）',
  trusted_indicator: '受支持的指标与因子', exact_backtest: '受支持的确定性回测',
};

export function eligibleCacheKinds(value: unknown): CacheKind[] {
  try { return value == null ? [] : readCacheView(value).eligible; }
  catch { return []; }
}

export async function getCacheView(signal?: AbortSignal) {
  return readCacheView(await request('/computation-cache', { signal }));
}

export async function submitCacheIntent(intent: CacheIntent) {
  const clear = intent.kind === 'clear';
  const raw = record(await request(clear ? '/computation-cache/clear' : '/computation-cache', {
    method: clear ? 'POST' : 'PUT', body: JSON.stringify(intent.body),
  }));
  const view = readCacheView(raw);
  if (view.settings.revision !== intent.body.expected_revision + 1) throw badReceipt();
  if (intent.kind === 'save') {
    if (view.settings.enabled !== intent.body.enabled || view.settings.max_bytes !== intent.body.max_bytes) throw badReceipt();
    return { view };
  }
  if (!counter(raw.cleared_entries) || !counter(raw.cleared_bytes) || view.stats.entries !== 0 || view.stats.bytes !== 0) throw badReceipt();
  return { view, cleared: { entries: raw.cleared_entries, bytes: raw.cleared_bytes } };
}
