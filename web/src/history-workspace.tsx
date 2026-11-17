import { eligibleCacheKinds } from './computation-cache-api';
import { useCallback, useEffect, useState } from 'react';
import type { RecordData } from './api';
import { ApiError, display } from './api';
import { ErrorBox, JsonView, Notice, Panel, isTerminalJob, useResource } from './components';
import { HistoryQuickForm } from './history-quick-form';
import { HistoryResultManager } from './history-result-manager';
import { SavedHistoryList } from './saved-history-list';
import { HistoryReport } from './history-report-page';
import { normalizeHistoryCatalog } from './history-catalog-adapter';
import {
  createHistoryDataAdapter,
  historyDownloadSlot,
  recoverHistoryDownload,
  normalizeHistorySummary,
} from './history-data-adapter';
import { createResultLifecycleAdapter } from './history-result-adapter';
import { createHistorySubmissionAdapter } from './history-submission-adapter';
import { dailyStorageKey } from './provider-backtest-page';
import { dailyFailureHint } from './provider-backtest-api';
import { typedSecurity } from './history-view-model';
import type { HistoryCoverage, HistoryUiConfig, HistoryUiPort } from './history-view-model';
type Receipt = { request: RecordData; jobId?: string };
const record = (v: unknown): RecordData =>
  v && typeof v === 'object' && !Array.isArray(v) ? (v as RecordData) : {};
const params = () => new URLSearchParams(window.location.hash.split('?')[1] ?? '');
export function readHistoryReceipt(slot: string): Receipt | null {
  const raw = sessionStorage.getItem(slot);
  if (!raw) return null;
  const value = JSON.parse(raw) as Receipt;
  if (
    !value?.request ||
    typeof value.request.ticker !== 'string' ||
    typeof value.request.idempotency_key !== 'string' ||
    !value.request.idempotency_key ||
    typeof value.request.start_date !== 'string' ||
    typeof value.request.end_date !== 'string' ||
    (value.jobId !== undefined && (typeof value.jobId !== 'string' || !value.jobId))
  )
    throw new Error('没读到上次提交的信息。请先查看任务记录。');
  return value;
}
export function HistoryWorkspace({
  userId,
  canWrite,
  resultsOnly = false,
}: {
  userId: string;
  canWrite: boolean;
  resultsOnly?: boolean;
}) {
  return (
    <HistoryWorkspaceBody
      key={`${userId}:${canWrite}`}
      userId={userId}
      canWrite={canWrite}
      resultsOnly={resultsOnly}
    />
  );
}
export function HistoryWorkspaceBody({
  userId,
  canWrite,
  resultsOnly = false,
}: {
  userId: string;
  canWrite: boolean;
  resultsOnly?: boolean;
}) {
  const [tick, setTick] = useState(0),
    [formKey, setFormKey] = useState(0),
    [selected, setSelected] = useState<HistoryCoverage | null>(null),
    [selectionError, setSelectionError] = useState<unknown>(null),
    [route, setRoute] = useState(params);
  const [initial] = useState(() => {
    try {
      return {
        research: readHistoryReceipt(dailyStorageKey(userId)),
        download: readHistoryReceipt(historyDownloadSlot(userId)),
        error: null as unknown,
      };
    } catch (error) {
      return { research: null, download: null, error };
    }
  });
  const [research, setResearch] = useState<Receipt | null>(initial.research),
    [download, setDownload] = useState<Receipt | null>(initial.download),
    [problem, setProblem] = useState<unknown>(initial.error),
    [recovering, setRecovering] = useState(false),
    [downloadNotFound, setDownloadNotFound] = useState(false);
  const [recoveryGuard] = useState(() => ({ busy: false }));
  const [submission] = useState(() => createHistorySubmissionAdapter(userId));
  const [port] = useState<HistoryUiPort>(() => {
    const dataPort = createHistoryDataAdapter(userId, canWrite);
    const syncReceipt = (kind: 'start' | 'save') => {
      try {
        const saved = readHistoryReceipt(
          kind === 'start' ? dailyStorageKey(userId) : historyDownloadSlot(userId),
        );
        if (kind === 'start') setResearch(saved);
        else setDownload(saved);
      } catch (error) {
        setProblem(error);
      }
    };
    return {
      ...dataPort,
      ...createResultLifecycleAdapter(),
      save: async (intent, key) => {
        try {
          return await dataPort.save(intent, key);
        } finally {
          syncReceipt('save');
        }
      },
      start: async (intent, key) => {
        if (!canWrite) throw new Error('当前账号不能开始回测。');
        try {
          return await submission.start(intent, key);
        } finally {
          syncReceipt('start');
        }
      },
    };
  });
  const { data, error, loading } = useResource<RecordData>('/research/daily/catalog', tick);
  const cache = useResource<unknown>('/computation-cache');
  useEffect(() => {
    const update = () => setRoute(params());
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  let config: HistoryUiConfig | null = null;
  let configError: unknown = null;
  try {
    config = data ? { ...normalizeHistoryCatalog(data), canBypassCache: !resultsOnly && eligibleCacheKinds(cache.data).includes('exact_backtest') } : null;
  } catch (error) {
    configError = error;
  }
  const researchJob = route.get('daily_job') || research?.jobId || '',
    downloadJob = route.get('history_job') || download?.jobId || '',
    run = route.get('daily_run') || '';
  const stored = research ?? download;
  const locked = !!stored || !!researchJob || !!downloadJob || !!initial.error;
  // A reload shows the exact saved request choices while waiting, never a fresh form over a pending write.
  const submitted = stored?.request;
  const legacyRequest = !!submitted && Object.hasOwn(submitted, 'slippage_bps');
  const formConfig =
    config && submitted
      ? {
          ...config,
          defaultStart: String(submitted.start_date),
          defaultEnd: String(submitted.end_date),
          defaultStrategy:
            typeof submitted.strategy === 'string' ? submitted.strategy : config.defaultStrategy,
          defaultProvider: String(submitted.provider),
          initialCash: String(submitted.initial_cash ?? config.initialCash),
          feePerShare: String(submitted.fee_per_share ?? config.feePerShare),
          minimumFee: String(submitted.minimum_fee ?? config.minimumFee),
          spreadBps: String(submitted.spread_bps ?? config.spreadBps),
        }
      : config;
  const chosen = selected
    ? typedSecurity(selected.symbol, selected.provider)
    : submitted
      ? typedSecurity(String(submitted.ticker), String(submitted.provider))
      : undefined;
  function refreshReceipt(kind: 'start' | 'save', jobId: string) {
    try {
      const slot = kind === 'start' ? dailyStorageKey(userId) : historyDownloadSlot(userId),
        saved = readHistoryReceipt(slot);
      if (!saved) throw new Error('找不到上次提交的记录。');
      const next = { ...saved, jobId };
      if (kind === 'start') setResearch(next);
      else setDownload(next);
      window.location.hash = `research?${kind === 'start' ? 'daily_job' : 'history_job'}=${encodeURIComponent(jobId)}`;
    } catch (error) {
      setProblem(error);
    }
  }
  function clearCompleted(kind: 'start' | 'save', jobId: string) {
    try {
      const current = kind === 'start' ? research : download;
      if (current && current.jobId !== jobId) throw new Error('无法确认这份保存记录属于当前任务，请刷新任务记录。');
      sessionStorage.removeItem(
        kind === 'start' ? dailyStorageKey(userId) : historyDownloadSlot(userId),
      );
      if (kind === 'start') setResearch(null);
      else setDownload(null);
      setSelected(null);
      setProblem(null);
      setFormKey((n) => n + 1);
      window.location.hash = 'research';
    } catch (error) {
      setProblem(error);
    }
  }
  async function recoverDownload(continueSameRequest: boolean) {
    if (
      recoveryGuard.busy ||
      !download ||
      (continueSameRequest && (!canWrite || !downloadNotFound))
    )
      return;
    recoveryGuard.busy = true;
    setRecovering(true);
    try {
      const receipt = await recoverHistoryDownload(
        userId,
        String(download.request.idempotency_key),
        continueSameRequest,
      );
      refreshReceipt('save', receipt.jobId);
      setDownloadNotFound(false);
      setProblem(null);
    } catch (error) {
      setProblem(error);
      setDownloadNotFound(error instanceof ApiError && error.status === 404);
    } finally {
      recoveryGuard.busy = false;
      setRecovering(false);
    }
  }
  return (
    <>
      {!resultsOnly &&
        (legacyRequest ? (
          <Notice>
            这次任务用的是旧版“滑点”设置。请先核对任务记录。新回测使用买卖价差，不会自动换算旧设置。
          </Notice>
        ) : loading ? (
          <Notice>正在读取可用股票、策略和行情设置…</Notice>
        ) : error || configError ? (
          <ErrorBox error={error ?? configError} retry={() => setTick((n) => n + 1)} />
        ) : formConfig ? (
          <HistoryQuickForm
            key={formKey}
            config={formConfig}
            port={port}
            canWrite={canWrite}
            locked={locked}
            initialChoice={chosen}
            initialHistory={selected ?? undefined}
            onReceipt={(receipt, _intent, _key, kind) => refreshReceipt(kind, receipt.jobId)}
          />
        ) : (
          <Notice>暂未读到长周期回测的设置。</Notice>
        ))}
      {problem != null && <ErrorBox error={problem} />}
      {stored && (
        <Panel title="这次提交的选择">
          <p>
            {display(stored.request.ticker)} · {display(stored.request.start_date)} 至{' '}
            {display(stored.request.end_date)} · {display(stored.request.provider)}
          </p>
          <details>
            <summary>原请求编号和选择</summary>
            <JsonView value={stored.request} />
          </details>
        </Panel>
      )}
      {research && !research.jobId && (
        <Panel title="核对回测提交">
          <Notice>只查询这次回测的状态，不会重复开始。</Notice>
          <button
            className="secondary"
            disabled={recovering}
            onClick={async () => {
              if (recoveryGuard.busy) return;
              recoveryGuard.busy = true;
              setRecovering(true);
              try {
                const receipt = await submission.lookup(String(research.request.idempotency_key));
                refreshReceipt('start', receipt.jobId);
                setProblem(null);
              } catch (error) {
                setProblem(error);
              } finally {
                recoveryGuard.busy = false;
                setRecovering(false);
              }
            }}
          >
            查询这次回测进度
          </button>
          <a href="#jobs">查看任务记录</a>
        </Panel>
      )}
      {download && !download.jobId && (
        <Panel title="核对历史下载">
          <Notice>
            已保留上次选择。查询只检查进度，不会自动重复下载。
            {downloadNotFound
              ? '还没查到记录，任务可能仍在处理。可稍后再查，或点“重试上次下载”。'
              : ''}
          </Notice>
          <button
            className="secondary"
            disabled={recovering}
            onClick={() => void recoverDownload(false)}
          >
            查询这次历史进度
          </button>
          {downloadNotFound && canWrite && (
            <button
              className="secondary"
              disabled={recovering}
              onClick={() => void recoverDownload(true)}
            >
              重试上次下载
            </button>
          )}
          <a href="#jobs">查看任务记录</a>
        </Panel>
      )}
      {researchJob && (
        <HistoryJob
          key={researchJob}
          id={researchJob}
          kind="start"
          userId={userId}
          canStartNew={canWrite && !download}
          onNew={() => clearCompleted('start', researchJob)}
        />
      )}
      {downloadJob && (
        <HistoryJob
          key={downloadJob}
          id={downloadJob}
          kind="save"
          userId={userId}
          canStartNew={canWrite && !research}
          onNew={() => clearCompleted('save', downloadJob)}
        />
      )}
      {!resultsOnly && (
        <SavedHistoryList
          key={`history:${tick}:${researchJob}:${downloadJob}`}
          port={port}
          onUse={(history) => {
            if (locked) {
              setSelectionError(new Error('请先查询当前任务的进度，再选择其他行情。'));
              return;
            }
            setSelected(history);
            setSelectionError(null);
            setFormKey((n) => n + 1);
          }}
        />
      )}
      {selectionError != null && <ErrorBox error={selectionError} />}
      <HistoryResultManager
        port={port}
        canWrite={canWrite}
      />
      {run && <HistoryReport key={run} run={run} />}
    </>
  );
}
export function HistoryJob({
  id,
  kind,
  userId,
  canStartNew,
  onNew,
}: {
  id: string;
  kind: 'start' | 'save';
  userId: string;
  canStartNew: boolean;
  onNew: () => void;
}) {
  const [tick, setTick] = useState(0);
  const handler = kind === 'start' ? 'research.daily_backtest' : 'data.history_download';
  const stopPolling = useCallback(
    (value: RecordData) =>
      isTerminalJob(value) || value.handler !== handler || value.owner_id !== userId,
    [handler, userId],
  );
  const {
    data: job,
    error,
    loading,
  } = useResource<RecordData>(`/jobs/${encodeURIComponent(id)}`, tick, true, stopPolling);
  if (loading) return <Notice>正在读取任务进度…</Notice>;
  if (error) return <ErrorBox error={error} retry={() => setTick((n) => n + 1)} />;
  if (
    !job ||
    job.job_id !== id ||
    job.owner_id !== userId ||
    job.handler !== handler ||
    !['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(job.status))
  )
    return <ErrorBox error={new Error('查到的记录与这次任务或账号不符，无法显示进度。')} />;
  const result = record(job.result),
    success = job.status === 'SUCCEEDED',
    terminal = isTerminalJob(job),
    run =
      success &&
      kind === 'start' &&
      typeof job.run_id === 'string' &&
      result.run_id === job.run_id &&
      [
        'daily-simulation-result-1',
        'daily-simulation-result-2',
        'daily-simulation-result-3',
      ].includes(String(result.family))
        ? job.run_id
        : '';
  let savedHistory: HistoryCoverage | null = null,
    saveError: unknown = null;
  if (success && kind === 'save') {
    try {
      savedHistory = normalizeHistorySummary(record(result.history));
    } catch (error) {
      saveError = error;
    }
  }
  return (
    <Panel title={kind === 'start' ? '回测进度' : '行情下载进度'}>
      <p role="status">
        {
          {
            QUEUED: '等待开始',
            RUNNING: '正在处理',
            SUCCEEDED: '已完成',
            FAILED: '失败',
            CANCELLED: '已取消',
          }[String(job.status)]
        }{' '}
        · {display(job.progress)}%
      </p>
      {job.status === 'FAILED' && (
        <Notice>
          {dailyFailureHint(job.error_code)}
          <details><summary>查看错误代码</summary>{display(job.error_code)}</details>
        </Notice>
      )}
      {success && kind === 'start' && !run && (
        <ErrorBox error={new Error('任务已完成，但无法确认报告属于这次回测。请查看任务记录。')} />
      )}
      {success && kind === 'save' && (
        <>
          {saveError ? (
            <ErrorBox error={saveError} />
          ) : (
            savedHistory && (
              <p>
                已保存 {savedHistory.symbol} 的第 {savedHistory.version} 版：{savedHistory.start} 至{' '}
                {savedHistory.end}，共 {savedHistory.rows} 条。{savedHistory.message}
              </p>
            )
          )}
          <details>
            <summary>查看保存版本和来源的原始记录</summary>
            <JsonView value={job.result} />
          </details>
        </>
      )}
      <div className="form-actions">
        <button className="secondary" onClick={() => setTick((n) => n + 1)}>
          刷新进度
        </button>
        {terminal && canStartNew && (
          <button className="secondary" onClick={onNew}>
            开始新的选择
          </button>
        )}
        {run && (
          <a className="primary-link" href={`#results?daily_run=${encodeURIComponent(run)}`}>
            打开已保存结果 →
          </a>
        )}
        <a href={`#jobs?job=${encodeURIComponent(id)}`}>查看任务记录</a>
      </div>
    </Panel>
  );
}
