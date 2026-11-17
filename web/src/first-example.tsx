import { useEffect, useEffectEvent, useRef, useState } from 'react';
import { ApiError, request } from './api';
import { ErrorBox, Notice, Panel, useResource } from './components';
import { record } from './native-research-api';
import { ResultPreviewLink } from './result-preview';

const path = '/onboarding/first-example';
const statuses = {
  pending: '正在提交任务',
  queued: '排队中',
  running: '正在运行',
  needs_advance: '行情已保存，准备回测',
  succeeded: '回测已完成',
  failed: '失败',
  cancelled: '已取消',
};
type Intent = {
  id: string;
  revision: number;
  phase: 'download' | 'backtest' | 'result';
  status: keyof typeof statuses;
  cancel_requested: boolean;
  download_job_id: string | null;
  backtest_job_id: string | null;
  result_id: string | null;
  job_status: string | null;
  error_code: string | null;
  next_action: 'advance' | 'retry' | 'open_report' | null;
  progress?: number | null;
};
type Receipt = {
  example: {
    id: string;
    symbol: string;
    start_date: string;
    end_date: string;
    initial_cash: string;
    fee_per_share: string;
    minimum_fee: string;
    spread_bps: string;
    currency: string;
  };
  intent: Intent | null;
};
type Action = 'advance' | 'retry' | 'cancel';
function receipt(data: unknown): Receipt | null {
  const row = record(data);
  const example = record(row.example);
  if (!['id', 'symbol', 'start_date', 'end_date', 'initial_cash', 'fee_per_share', 'minimum_fee', 'spread_bps', 'currency']
    .every(key => typeof example[key] === 'string' && example[key] !== '')) return null;
  if (row.intent !== null) {
    const intent = record(row.intent);
    if (typeof intent.id !== 'string' || !intent.id || typeof intent.revision !== 'number' || !Number.isSafeInteger(intent.revision) ||
        !['download', 'backtest', 'result'].includes(String(intent.phase)) || !Object.hasOwn(statuses, String(intent.status))) return null;
  }
  return data as Receipt;
}
function inProgress(intent: Intent | null | undefined) {
  return intent != null && (intent.status === 'pending' || intent.status === 'queued' || intent.status === 'running');
}
const stop = (data: Receipt) => !inProgress(receipt(data)?.intent);

export function FirstExample({ canWrite }: { canWrite: boolean }) {
  const [refresh, setRefresh] = useState(0);
  const [written, setWritten] = useState<Receipt | null>(null);
  const state = useResource<Receipt>(canWrite ? path : null, refresh, 2000, stop);
  const read = receipt(state.data);
  const result = read && (!written?.intent || read.intent && (read.intent.id !== written.intent.id ||
    read.intent.revision >= written.intent.revision)) ? read : written;
  const intent = result?.intent;
  const example = result?.example;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const attempted = useRef('');
  const pending = useRef(false);
  const life = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    life.current = controller;
    return () => { controller.abort(); life.current = null; };
  }, []);
  async function send(action?: Action) {
    const signal = life.current?.signal;
    if (pending.current || !example || !canWrite || !signal || (action && !intent)) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      const body = action && intent ? { action, expected_revision: intent.revision } : { example_id: example.id };
      const endpoint = action && intent ? path + '/' + encodeURIComponent(intent.id) + '/actions' : path;
      const next = receipt(await request<Receipt>(endpoint, { body: JSON.stringify(body), method: 'POST', signal }));
      if (!next?.intent) throw new Error('暂时无法确认这次操作的结果。请核对任务状态。');
      if (!signal.aborted) {
        setWritten(next);
        setRefresh(value => value + 1);
      }
    } catch (cause) {
      if (!signal.aborted) setError(cause);
    } finally {
      pending.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  const advance = useEffectEvent(() => {
    if (!intent || intent.next_action !== 'advance' || intent.cancel_requested || busy || state.loading || error || state.error) return;
    const revision = intent.id + ':' + intent.revision;
    if (attempted.current === revision) return;
    attempted.current = revision;
    void send('advance');
  });
  useEffect(() => { advance(); }, [intent, busy, error, state.error, state.loading]);
  function check() {
    attempted.current = '';
    setError(null);
    setRefresh(value => value + 1);
  }

  if (!canWrite) return <Panel title="先跑一个例子" helpTopic="first-example"><Notice>当前账户只可查看。可到研究结果查看你有权打开的报告。</Notice><a href="#my-results">查看研究结果</a></Panel>;
  const issue = error ?? state.error ?? (state.data && !read ? new Error('暂时无法读取本次示例。请再检查一次。') : null);
  const complete = intent?.status === 'succeeded' && intent.next_action === 'open_report' && typeof intent.result_id === 'string' && intent.result_id;
  return <Panel title="先跑一个例子" helpTopic="first-example">
    {example ? <>
      <p>从 Yahoo Finance 下载 {example.symbol} 在 {example.start_date} 至 {example.end_date} 的日线并保存，再用 {example.initial_cash} {example.currency} 的虚拟本金模拟买入持有。</p>
      <p>这个示例使用真实历史行情。模拟包含手续费，回测不会下单。结果不保证未来收益。</p>
      <details><summary>查看示例费用和价格说明</summary>
        <p>每股手续费 {example.fee_per_share} {example.currency}，每笔最低 {example.minimum_fee} {example.currency}。模拟买卖价差为 {Number(example.spread_bps) / 100}%（{example.spread_bps} 个基点）。0% 表示无额外价差。Yahoo 日线按拆股换算，回测不计现金分红。</p>
      </details>
    </> : !issue && <p role="status">正在读取示例设置…</p>}
    <div role="status">
      {intent && <p>{intent.phase === 'download' ? '行情下载' : '回测'}：{statuses[intent.status]}
        {typeof intent.progress === 'number' && <> · {intent.progress}%</>}</p>}
      {intent?.cancel_requested && <p>已请求停止。请等待任务状态更新。</p>}
      {intent?.status === 'failed' && <p>{intent.phase === 'download' ? '行情下载失败。' : '回测未完成。'}可以查看任务详情。{intent.next_action === 'retry' ? '也可以只重试失败的步骤。' : ''}</p>}
    </div>
    {(intent?.download_job_id || intent?.backtest_job_id) && <>
      <div className="form-actions">
        {intent.download_job_id && <a href={'#jobs?job=' + encodeURIComponent(intent.download_job_id)}>查看下载进度</a>}
        {intent.backtest_job_id && <a href={'#jobs?job=' + encodeURIComponent(intent.backtest_job_id)}>查看回测进度</a>}
      </div>
      <details><summary>查看任务编号</summary><dl className="key-value">
        {intent.download_job_id && <><dt>下载任务编号</dt><dd>{intent.download_job_id}</dd></>}
        {intent.backtest_job_id && <><dt>回测任务编号</dt><dd>{intent.backtest_job_id}</dd></>}
      </dl></details>
    </>}
    <div className="form-actions">
      {example && !intent && !issue && <button className="primary-action" disabled={busy || state.loading} onClick={() => void send()}>确认并开始示例</button>}
      {complete && <ResultPreviewLink className="primary-action" resultId={complete} title="首次示例回测结果">查看结果</ResultPreviewLink>}
      {intent?.next_action === 'retry' && <button disabled={busy} onClick={() => void send('retry')}>重试这一步</button>}
      {intent && (inProgress(intent) || intent.next_action === 'advance') &&
        <button disabled={busy || intent.cancel_requested} onClick={() => void send('cancel')}>请求停止</button>}
      {issue && <button disabled={busy || state.loading} onClick={check}>核对并继续</button>}
      <a href="#workspace">{intent ? '返回工作台' : '暂时跳过'}</a>
    </div>
    {intent && inProgress(intent) && <p>离开后，已开始的任务会继续运行。回来可接着完成示例。</p>}
    {issue != null && <ErrorBox error={issue} />}
    {intent?.status === 'failed' && intent.error_code && <ErrorBox error={new ApiError(0, intent.error_code,
      intent.phase === 'download' ? '本次未能下载真实行情。请查看任务详情或重试。' : '已保存的行情仍可使用。请查看或重试回测任务。')} />}
  </Panel>;
}
