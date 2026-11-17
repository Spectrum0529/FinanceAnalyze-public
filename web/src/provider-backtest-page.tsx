import { useState, useEffect } from 'react';
import type { RecordData } from './api';
import { ApiError, post } from './api';
import { ErrorBox, Notice, Panel, JsonView, useResource, isTerminalJob } from './components';
import { ProviderBacktestForm, validateDailyInput } from './provider-backtest-form';
import type { DailyBacktestInput, DailyBacktestFormConfig } from './provider-backtest-form';
import {
  DAILY_CATALOG,
  normalizeDailyCatalog,
  dailyRequest,
  submitDaily,
  isCertainDailyRejection,
  dailyResultLink,
  dailyFailureHint,
} from './provider-backtest-api';
import type { DailyRequest } from './provider-backtest-api';
import { DailyReport } from './provider-backtest-results';

type Attempt = { request: DailyRequest; jobId?: string };
export function dailyStorageKey(userId: string) {
  return `fa-daily-attempt-v1:${userId}`;
}
function restoreAttempt(userId: string): Attempt | null {
  if (!userId) return null;
  const saved = sessionStorage.getItem(dailyStorageKey(userId));
  if (!saved) return null;
  const value = JSON.parse(saved) as Attempt;
  if (
    !value?.request ||
    typeof value.request.idempotency_key !== 'string' ||
    typeof value.request.ticker !== 'string' ||
    (value.jobId !== undefined && typeof value.jobId !== 'string')
  )
    throw new Error('没读到完整的提交记录。请先查看任务记录，以免开始两次。');
  return value;
}
export function DailyBacktestWorkspace({
  canWrite,
  userId,
}: {
  canWrite: boolean;
  userId: string;
}) {
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useResource<RecordData>(DAILY_CATALOG, tick);
  const [initial] = useState(() => {
    try {
      return { attempt: restoreAttempt(userId), error: null as unknown };
    } catch (e) {
      return { attempt: null, error: e };
    }
  });
  const [submitting, setSubmitting] = useState(false);
  const [attempt, setAttempt] = useState<Attempt | null>(initial.attempt);
  const [problem, setProblem] = useState<unknown>(initial.error);
  const [lock] = useState(() => ({ busy: false, attempt: initial.attempt }));
  const [jobFromUrl, setJobFromUrl] = useState(
    () => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('daily_job') ?? '',
  );
  useEffect(() => {
    const change = () =>
      setJobFromUrl(
        new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('daily_job') ?? '',
      );
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);
  let config: DailyBacktestFormConfig | null = null;
  try {
    config = data ? normalizeDailyCatalog(data) : null;
  } catch (e) {
    return <ErrorBox error={e} retry={() => setTick((n) => n + 1)} />;
  }
  const jobId = jobFromUrl || attempt?.jobId || '';
  async function start(input: DailyBacktestInput) {
    if (!canWrite || !userId || lock.busy || lock.attempt || jobFromUrl || initial.error)
      throw new Error('请先核对当前任务；当前不能创建新的回测。');
    if (!config) throw new Error('请等待可用股票和策略加载完成。');
    lock.busy = true;
    setSubmitting(true);
    setProblem(null);
    let recorded = false;
    try {
      const request = dailyRequest(validateDailyInput(input, config), crypto.randomUUID());
      const next: Attempt = { request };
      // Persist only this user's request identity and choices before the write, never credentials or market data.
      sessionStorage.setItem(dailyStorageKey(userId), JSON.stringify(next));
      lock.attempt = next;
      setAttempt(next);
      recorded = true;
      const job = await submitDaily(request);
      if (
        typeof job.job_id !== 'string' ||
        !job.job_id ||
        job.owner_id !== userId ||
        job.handler !== 'research.daily_backtest'
      )
        throw new Error('无法确认返回的是这次回测。请先查看任务记录，以免开始两次。');
      const saved = { request, jobId: job.job_id };
      // Even if storage fails here, keep the memory receipt and exact task URL. Never resend the POST.
      lock.attempt = saved;
      setAttempt(saved);
      try {
        sessionStorage.setItem(dailyStorageKey(userId), JSON.stringify(saved));
      } catch {
        setProblem(new Error('任务已发出，但浏览器没有存下任务编号。请保留本页链接。'));
      }
      window.location.hash = `research?daily_job=${encodeURIComponent(job.job_id)}`;
    } catch (e) {
      if (recorded && isCertainDailyRejection(e)) {
        try {
          sessionStorage.removeItem(dailyStorageKey(userId));
          lock.attempt = null;
          setAttempt(null);
        } catch {
          /* Keep the request locked when browser storage is unavailable. */
        }
      }
      setProblem(e);
      throw e;
    } finally {
      lock.busy = false;
      setSubmitting(false);
    }
  }
  return (
    <>
      {loading ? (
        <Notice>正在读取可用股票和策略…</Notice>
      ) : error ? (
        <ErrorBox error={error} retry={() => setTick((n) => n + 1)} />
      ) : config ? (
        <ProviderBacktestForm
          key={userId}
          config={config}
          canWrite={canWrite && !!userId}
          locked={!!attempt || !!jobFromUrl || !!initial.error}
          confirmedJobId={attempt?.jobId}
          initialValues={
            attempt
              ? {
                  symbol: attempt.request.ticker,
                  start_date: attempt.request.start_date,
                  end_date: attempt.request.end_date,
                  strategy: attempt.request.strategy,
                  provider: attempt.request.provider,
                  initial_cash: attempt.request.initial_cash,
                  fee_per_share: attempt.request.fee_per_share,
                  minimum_fee: attempt.request.minimum_fee,
                  slippage_bps: attempt.request.slippage_bps,
                }
              : undefined
          }
          onStart={start}
        />
      ) : (
        <Notice>服务尚未提供可用股票和策略。</Notice>
      )}
      {problem != null && <ErrorBox error={problem} />}
      {attempt && (!jobFromUrl || attempt.jobId === jobFromUrl) && (
        <Panel title="这次已提交的选择">
          <p>
            {attempt.request.ticker} · {attempt.request.start_date} 至 {attempt.request.end_date}
            （含结束日） · {attempt.request.provider}
          </p>
          <p>
            策略：
            {config?.strategies.find((s) => s.id === attempt.request.strategy)?.label ||
              attempt.request.strategy}
          </p>
          <details>
            <summary>完整请求编号与选择</summary>
            <JsonView value={attempt.request} />
          </details>
        </Panel>
      )}
      {attempt && !attempt.jobId && !jobFromUrl && !submitting && (
        <RecoverDailySubmission
          userId={userId}
          requestKey={attempt.request.idempotency_key}
          onFound={(job) => {
            if (
              !lock.attempt ||
              lock.attempt.request.idempotency_key !== attempt.request.idempotency_key ||
              lock.attempt.jobId
            )
              return;
            const next = { request: lock.attempt.request, jobId: String(job.job_id) };
            lock.attempt = next;
            setAttempt(next);
            setProblem(null);
            try {
              sessionStorage.setItem(dailyStorageKey(userId), JSON.stringify(next));
            } catch {
              setProblem(new Error('已找到任务，但浏览器没有存下编号。请保留任务链接。'));
            }
            window.location.hash = `research?daily_job=${encodeURIComponent(next.jobId)}`;
          }}
        />
      )}
      {jobId && (
        <DailyJob
          key={jobId}
          id={jobId}
          userId={userId}
          canWrite={canWrite}
          canStartNew={!attempt || attempt.jobId === jobId}
          onNew={() => {
            sessionStorage.removeItem(dailyStorageKey(userId));
            lock.attempt = null;
            setAttempt(null);
            setProblem(null);
            window.location.hash = 'research';
            setJobFromUrl('');
          }}
        />
      )}
    </>
  );
}
function terminalDailyJob(job: RecordData) {
  return isTerminalJob(job) || job.handler !== 'research.daily_backtest';
}
export function DailyJob({
  id,
  userId,
  canWrite,
  canStartNew,
  onNew,
}: {
  id: string;
  userId: string;
  canWrite: boolean;
  canStartNew: boolean;
  onNew: () => void;
}) {
  const [refresh, setRefresh] = useState(0),
    [error, setError] = useState<unknown>(null),
    [busy, setBusy] = useState(false);
  const [guard] = useState(() => ({ busy: false }));
  const {
    data: job,
    error: readError,
    loading,
  } = useResource<RecordData>(`/jobs/${encodeURIComponent(id)}`, refresh, true, terminalDailyJob);
  if (loading) return <Notice>正在查询这次回测进度…</Notice>;
  if (readError) return <ErrorBox error={readError} retry={() => setRefresh((n) => n + 1)} />;
  if (!job) return <Notice>服务未返回任务记录。</Notice>;
  if (job.owner_id !== userId || job.handler !== 'research.daily_backtest' || job.job_id !== id)
    return <ErrorBox error={new Error('找不到属于当前账号和这次回测的记录。')} />;
  if (!['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(job.status)))
    return <ErrorBox error={new Error('暂不能确认任务状态。请先查看任务详情，以免开始两次。')} />;
  const terminal = isTerminalJob(job);
  const p = Number(job.progress);
  const status =
    job.status === 'QUEUED'
      ? '等待开始'
      : job.status === 'SUCCEEDED'
        ? '回测完成'
        : job.status === 'FAILED'
          ? '回测失败'
          : job.status === 'CANCELLED'
            ? '已取消'
            : p >= 95
              ? '正在保存结果'
              : p >= 45
                ? '正在回测策略'
                : p >= 40
                  ? '正在检查数据'
                  : p >= 5
                    ? '正在下载行情'
                    : '正在准备数据';
  const result =
    job.result && typeof job.result === 'object' && !Array.isArray(job.result)
      ? (job.result as RecordData)
      : null;
  const run =
    job.status === 'SUCCEEDED' &&
    result?.family === 'daily-simulation-result-1' &&
    result.run_id === job.run_id &&
    typeof job.run_id === 'string'
      ? job.run_id
      : '';
  return (
    <Panel title="回测进度">
      <div role="status">
        <strong>{status}</strong>
        <p>进度：{Number.isFinite(p) ? `${p}%` : '服务未提供'}</p>
      </div>
      {job.cancel_requested === true && !terminal && (
        <Notice>已请求停止。任务可能还会运行一会儿，请查看最终状态。</Notice>
      )}
      {job.status === 'FAILED' && (
        <Notice>
          {dailyFailureHint(job.error_code)}
        </Notice>
      )}
      {job.status === 'SUCCEEDED' && !run && (
        <ErrorBox error={new Error('任务已结束，但无法确认报告属于这次回测。请查看任务记录。')} />
      )}
      <div className="form-actions">
        <button className="secondary" onClick={() => setRefresh((n) => n + 1)}>
          刷新进度
        </button>
        {canWrite && !terminal && (
          <button
            className="secondary"
            disabled={busy || job.cancel_requested === true}
            onClick={async () => {
              if (guard.busy) return;
              guard.busy = true;
              setBusy(true);
              setError(null);
              try {
                await post(`/jobs/${encodeURIComponent(id)}/cancel`);
                setRefresh((n) => n + 1);
              } catch (e) {
                setError(e);
              } finally {
                guard.busy = false;
                setBusy(false);
              }
            }}
          >
            请求停止回测
          </button>
        )}
        {canWrite && canStartNew && terminal && (
          <button className="secondary" onClick={onNew}>
            开始新的回测
          </button>
        )}
      </div>
      {error != null && <ErrorBox error={error} />}
      {run && (
        <>
          <a className="primary-link" href={dailyResultLink(run)}>
            打开已保存结果 →
          </a>
          <DailyReport run={run} />
        </>
      )}
      <details>
        <summary>任务编号与技术详情</summary>
        <JsonView value={job} />
        <a href={`#jobs?job=${encodeURIComponent(id)}`}>查看任务事件与日志 →</a>
      </details>
    </Panel>
  );
}

export function RecoverDailySubmission({
  userId,
  requestKey,
  onFound,
}: {
  userId: string;
  requestKey: string;
  onFound: (job: RecordData) => void;
}) {
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useResource<RecordData>(
    `/research/daily/submissions/${encodeURIComponent(requestKey)}`,
    refresh,
  );
  const matched =
    !!data &&
    data.owner_id === userId &&
    data.handler === 'research.daily_backtest' &&
    typeof data.job_id === 'string' &&
    !!data.job_id;
  useEffect(() => {
    if (data && matched) onFound(data);
  }, [data, matched, onFound]);
  return (
    <Panel title="核对这次提交">
      <Notice>
        还不能确认回测是否开始。正在查询已有任务，不会重复开始。
      </Notice>
      {loading ? (
        <p role="status">正在查询这次提交…</p>
      ) : error instanceof ApiError && error.status === 404 ? (
        <p>还没查到任务，原提交可能还在处理。请稍后再查。</p>
      ) : error ? (
        <ErrorBox error={error} />
      ) : data && !matched ? (
        <ErrorBox error={new Error('没找到与这次提交相符的任务。')} />
      ) : matched ? (
        <p>已找到这次回测，正在恢复进度…</p>
      ) : null}
      <button className="secondary" onClick={() => setRefresh((n) => n + 1)}>
        重新查询这次提交
      </button>
      <a className="text-button" href="#jobs">
        查看任务记录 →
      </a>
    </Panel>
  );
}
