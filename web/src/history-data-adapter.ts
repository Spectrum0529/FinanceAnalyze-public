import { post, request } from './api';
import type { RecordData } from './api';
import type {
  HistoryCoverage,
  HistoryIntent,
  HistoryUiPort,
  SecurityChoice,
} from './history-view-model';
import { typedSecurity } from './history-view-model';
import { researchHistoryBody } from './history-research-adapter';
import { isCertainDailyRejection } from './provider-backtest-api';
import { catalogSuggestions } from './history-catalog-adapter';
const record = (v: unknown): RecordData =>
  v && typeof v === 'object' && !Array.isArray(v) ? (v as RecordData) : {};
const sha = (v: unknown) => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
const date = (v: unknown) => (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : null);
export function normalizeHistorySummary(value: RecordData): HistoryCoverage {
  const coverage = record(value.actual_coverage),
    observation = record(value.observation_ref);
  if (
    typeof value.history_id !== 'string' ||
    !value.history_id ||
    !Number.isSafeInteger(value.version) ||
    Number(value.version) <= 0 ||
    !Number.isSafeInteger(value.revision) ||
    Number(value.revision) < 0 ||
    value.deleted !== false ||
    !['yfinance', 'akshare'].includes(String(value.provider)) ||
    typeof value.ticker !== 'string' ||
    !Number.isSafeInteger(coverage.row_count) ||
    Number(coverage.row_count) < 1 ||
    Number(coverage.row_count) > 9000 ||
    !date(coverage.actual_start) ||
    !date(coverage.actual_end) ||
    !sha(value.content_hash) ||
    !sha(value.snapshot_content_hash) ||
    !sha(observation.content_hash)
  )
    throw new Error('已保存历史的身份、版本、覆盖或原观察引用不完整。');
  const nativeQfq =
    value.provider === 'akshare' &&
    value.price_policy === 'AKSHARE_STOCK_US_DAILY_NATIVE_QFQ_ROUND4_V1' &&
    value.quantity_basis === 'ADJUSTED_SIMULATION_UNITS' &&
    record(value.source_ref).source_id === 'history-v4-akshare-stock-us-daily-native-qfq';
  return {
    id: value.history_id,
    version: Number(value.version),
    revision: String(value.revision),
    observationHash: String(observation.content_hash),
    historyHash: String(value.snapshot_content_hash),
    selectionHash: String(value.content_hash),
    symbol: value.ticker,
    provider: String(value.provider),
    start: date(coverage.actual_start),
    end: date(coverage.actual_end),
    rows: Number(coverage.row_count),
    status: 'partial',
    message:
      '实际保存覆盖；来源响应完整性未认证，节假日、停牌和缺失不作推断。' +
      (nativeQfq
        ? ' AKShare原生前复权（上游四位舍入）；上游对完整表前向/后向填充。成交量单位与公司行为数量未认证，不代表独立原始价格或因子链认证。'
        : '') +
      (record(value.freshness).stale === true
        ? ' 本版已过来源新鲜度提示，默认仍复用原版；需要更新请明确重新获取。'
        : ''),
    savedAt: typeof value.observed_received_at === 'string' ? value.observed_received_at : null,
  };
}
export function downloadHistoryBody(intent: HistoryIntent, key: string) {
  const body = researchHistoryBody(intent, key);
  return {
    ticker: body.ticker,
    provider: body.provider,
    start_date: body.start_date,
    end_date: body.end_date,
    history_policy: body.history_policy,
    history_id: body.history_id,
    history_version: body.history_version,
    idempotency_key: key,
  };
}
export function normalizeSearchResult(value: RecordData, provider: string): SecurityChoice[] {
  if (
    !Array.isArray(value.items) ||
    value.items.length > 10 ||
    value.identity_verified !== false ||
    value.identity_lookup_required !== true
  )
    throw new Error('搜索结果未说明身份仍待核实，未当作已验证证券。');
  return value.items.map((item) => {
    const row = record(item);
    if (
      row.identity_verified !== false ||
      row.requires_exact_chart_identity_lookup !== true ||
      typeof row.ticker !== 'string' ||
      typeof row.name !== 'string'
    )
      throw new Error('搜索候选字段不完整。');
    return {
      ...typedSecurity(row.ticker, provider),
      name: row.name,
      identityState: 'candidate' as const,
    };
  });
}
const delay = (milliseconds: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    signal.throwIfAborted();
    const abort = () => {
      window.clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, milliseconds);
    signal.addEventListener('abort', abort, { once: true });
  });
export async function readSearchJob(
  userId: string,
  id: string,
  signal: AbortSignal,
  pollDelay = 1500,
): Promise<RecordData> {
  const deadline = Date.now() + 245000;
  while (Date.now() < deadline) {
    const job = await request<RecordData>(`/jobs/${encodeURIComponent(id)}`, {
      signal,
    });
    if (job.owner_id !== userId || job.job_id !== id || job.handler !== 'data.history_search')
      throw new Error('搜索任务身份不匹配。');
    if (job.status === 'SUCCEEDED') return record(job.result);
    if (['FAILED', 'CANCELLED'].includes(String(job.status)))
      throw new Error(
        `搜索未完成：${String(job.error_code ?? job.status)}。可直接输入准确代码，由执行任务核实身份。`,
      );
    if (!['QUEUED', 'RUNNING'].includes(String(job.status)))
      throw new Error('搜索任务状态无法识别。');
    await delay(pollDelay, signal);
  }
  throw new Error('本次搜索等待已结束；可直接输入准确代码，未自动重启搜索。');
}
export const historyDownloadSlot = (userId: string) => `fa-history-download-attempt-v1:${userId}`;
export async function recoverHistoryDownload(
  userId: string,
  key: string,
  continueSameRequest = false,
) {
  const slot = historyDownloadSlot(userId),
    raw = sessionStorage.getItem(slot);
  if (!raw) throw new Error('找不到上次下载的任务编号。请先查看任务记录。');
  const saved = JSON.parse(raw),
    body = record(saved.request);
  if (
    !userId ||
    body.idempotency_key !== key ||
    typeof body.ticker !== 'string' ||
    typeof body.start_date !== 'string' ||
    typeof body.end_date !== 'string'
  )
    throw new Error('原下载编号或选择不匹配。');
  // An explicit continuation reuses EXACT stored body/key. It is never automatic after a timeout.
  const receipt = continueSameRequest
    ? await post<RecordData>('/data/history/download', body)
    : await request<RecordData>(`/data/history/submissions/${encodeURIComponent(key)}`);
  if (
    typeof receipt.job_id !== 'string' ||
    !receipt.job_id ||
    receipt.operation !== 'data.history_download'
  )
    throw new Error('原下载查询返回的任务类型不匹配。');
  const job = await request<RecordData>(`/jobs/${encodeURIComponent(receipt.job_id)}`);
  if (
    job.job_id !== receipt.job_id ||
    job.owner_id !== userId ||
    job.handler !== 'data.history_download' ||
    job.run_id !== receipt.run_id
  )
    throw new Error('原下载任务归属或运行编号不匹配。');
  try {
    sessionStorage.setItem(slot, JSON.stringify({ ...saved, jobId: receipt.job_id }));
  } catch {
    /* Keep original key and current memory receipt. */
  }
  return { jobId: receipt.job_id };
}
export function createHistoryDataAdapter(
  userId: string,
  canWrite: boolean,
): Pick<HistoryUiPort, 'search' | 'resolve' | 'coverage' | 'listHistory' | 'save'> {
  return {
    async search(query, provider, signal) {
      if (!canWrite) {
        const catalog = await request<RecordData>(
          `/research/daily/catalog?query=${encodeURIComponent(query)}`,
          { signal },
        );
        return catalogSuggestions(catalog, provider);
      }
      signal.throwIfAborted();
      const job = await post<RecordData>('/data/history/search', {
        query,
        idempotency_key: crypto.randomUUID(),
      });
      if (typeof job.job_id !== 'string' || job.operation !== 'data.history_search')
        throw new Error('搜索未返回可核实的任务编号。');
      return normalizeSearchResult(await readSearchJob(userId, job.job_id, signal), provider);
    },
    async resolve(query, provider, signal) {
      signal.throwIfAborted();
      return typedSecurity(query, provider);
    },
    async coverage(intent, signal) {
      signal.throwIfAborted();
      return {
        id: '',
        revision: '0',
        symbol: intent.security.symbol,
        provider: intent.provider,
        start: null,
        end: null,
        rows: 0,
        status: 'unknown',
        message:
          '执行时由服务匹配兼容的已保存版本；可在下方选择并固定已有历史。尚未取得市场覆盖结论。',
        savedAt: null,
      };
    },
    async listHistory(offset, limit, signal) {
      const page = await request<RecordData>(
        `/data/history?state=active&limit=${limit}&offset=${offset}`,
        { signal },
      );
      if (
        !Array.isArray(page.items) ||
        page.items.length > limit ||
        page.offset !== offset ||
        page.limit !== limit ||
        !Number.isSafeInteger(page.total) ||
        Number(page.total) < 0
      )
        throw new Error('历史列表分页不匹配。');
      return {
        items: page.items.map((v) => normalizeHistorySummary(record(v))),
        total: Number(page.total),
        offset,
        limit,
      };
    },
    async save(intent, key) {
      if (!canWrite || !userId) throw new Error('当前账号不能保存历史。');
      const slot = historyDownloadSlot(userId),
        body = downloadHistoryBody(intent, key),
        raw = sessionStorage.getItem(slot);
      if (raw) {
        const old = JSON.parse(raw);
        if (
          JSON.stringify(old.request) === JSON.stringify(body) &&
          typeof old.jobId === 'string' &&
          old.jobId
        )
          return { jobId: old.jobId };
        throw new Error('已有历史下载待核对；不会更换编号或重新提交。请查看任务记录。');
      }
      sessionStorage.setItem(slot, JSON.stringify({ request: body }));
      try {
        const job = await post<RecordData>('/data/history/download', body);
        if (
          typeof job.job_id !== 'string' ||
          !job.job_id ||
          job.operation !== 'data.history_download'
        )
          throw new Error('无法确认上次下载已提交，请先查看任务记录。');
        try {
          sessionStorage.setItem(slot, JSON.stringify({ request: body, jobId: job.job_id }));
        } catch {
          /* Keep the persisted original key and returned task URL. */
        }
        return { jobId: job.job_id };
      } catch (error) {
        if (isCertainDailyRejection(error)) sessionStorage.removeItem(slot);
        throw error;
      }
    },
  };
}
