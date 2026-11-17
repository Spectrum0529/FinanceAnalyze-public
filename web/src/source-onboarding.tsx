import { useEffect, useRef, useState } from 'react';
import { post } from './api';
import type { RecordData } from './api';
import { AreaHelp } from './area-help';
import { ErrorBox, Notice, Status, useResource } from './components';
import { useAutonomousIntent } from './autonomous-request-intent';
import { record, terminalResearchJob } from './native-research-api';
import { definitelyRejected } from './ux-mutation';

export const marketLabels = { US_EQUITY: '美国股票', US_ETF: '美国 ETF', CN_EQUITY: '沪深股票', CSV: '导入 CSV 文件' };
export type Market = keyof typeof marketLabels;

export function preferredSource(catalog: RecordData | null, market: Market) {
  const preference = record(record(catalog?.onboarding).defaults)[market];
  return typeof preference === 'string' ? preference : record(preference).provider;
}
export function sourceMismatch(market: Market | '', provider: unknown, capabilities: RecordData | undefined) {
  if (!market || typeof provider !== 'string') return '';
  const china = provider === 'akshare_cn' || provider === 'tushare';
  const supported = capabilities?.assets;
  const mismatch = market === 'CSV' ? provider !== 'csv'
    : market === 'CN_EQUITY' ? !china
      : provider === 'csv' || china || market === 'US_ETF' && provider === 'akshare' ||
        Array.isArray(supported) && !supported.includes(market);
  return mismatch ? '当前来源和所选数据类型不匹配。请在上方选择适用的来源。' : '';
}
const configuration = { SAVED: '已保存', CREDENTIAL_REQUIRED: '还需在本机填写密钥', RUNTIME_UNAVAILABLE: '本机环境还没准备好', ARCHIVED: '已停用' };
const reach = { NOT_TESTED: '尚未测试网络', REACHED: '已收到网站响应', UNCONFIRMED: '未能确认连接', NOT_APPLICABLE: '文件导入不需要连接网络' };
const access = { NOT_TESTED: '尚未验证样本权限', RETURNED_ROWS: '本次样本返回了行情', NO_ROWS: '本次样本没有返回行情', DENIED_OR_RANGE: '该样本未获授权或超出范围' };
function label(values: Record<string, string>, key: unknown, fallback: string) {
  return typeof key === 'string' && Object.hasOwn(values, key) ? values[key] : fallback;
}

export function SourceConnectionCheck({ owner, connection, capability, canWrite }: {
  owner: string; connection: RecordData; capability?: RecordData; canWrite: boolean;
}) {
  const id = typeof connection.connection_id === 'string' ? connection.connection_id : '';
  const mode = capability?.check_mode;
  const checkPath = '/data/library/connections/' + encodeURIComponent(id) + '/check';
  const journal = useAutonomousIntent<{ idempotency_key: string }>(owner, 'source-check:' + id + ':' + connection.revision,
    (value): value is { idempotency_key: string } => typeof record(value).idempotency_key === 'string');
  const [jobId, setJobId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [refresh, setRefresh] = useState(0);
  const [expired, setExpired] = useState(false);
  const gate = useRef(false);
  const state = useResource<RecordData>(jobId ? checkPath + '?job_id=' + encodeURIComponent(jobId) : null, refresh, 1500, terminalResearchJob);
  const job = state.data;
  const matchesJob = job?.job_id === jobId;
  const checked = record(matchesJob && job.status === 'SUCCEEDED' ? job.result : null);
  const matched = checked.connection_id === id && checked.provider === connection.provider &&
    typeof checked.revision === 'number' && checked.revision === connection.revision && connection.archived !== true;
  const until = Date.parse(String(checked.expires_at ?? checked.valid_until ?? ''));
  const fresh = matched && job?.stale !== true && !expired && until > Date.now();
  useEffect(() => {
    setExpired(false);
    if (!Number.isFinite(until)) return;
    const timer = window.setTimeout(() => setExpired(true), Math.max(0, until - Date.now()));
    return () => window.clearTimeout(timer);
  }, [until]);
  async function check() {
    if (gate.current || !canWrite || journal.error) return;
    gate.current = true;
    setBusy(true);
    setError(null);
    try {
      if (!journal.current.current) journal.save({ idempotency_key: crypto.randomUUID() });
      const value = await post<RecordData>(checkPath, journal.current.current);
      if (typeof value.job_id !== 'string' || !value.job_id) throw new Error('还不能确认检查是否已提交。请先查看任务记录。');
      setJobId(value.job_id);
      journal.clear();
    } catch (cause) {
      setError(cause);
      if (definitelyRejected(cause)) journal.clear();
    } finally {
      gate.current = false;
      setBusy(false);
    }
  }
  const actual = record(checked.actual_coverage);
  if (!id || (mode !== 'PUBLIC_SAMPLE' && mode !== 'CONFIGURATION_ONLY')) return <p>已保存来源。{mode === 'IMPORT_PREVIEW' ? '选择文件后可预览内容。' : '下载能力尚未验证。'}</p>;
  return <section aria-label="来源连接状态">
    <dl className="key-value">
      <dt>保存状态</dt><dd>{matched ? label(configuration, checked.configuration, '已保存') : '已保存'}</dd>
      <dt>网络连接</dt><dd>{fresh ? label(reach, checked.reachability, '尚未确认') : '尚未验证'}</dd>
      <dt>实际样本</dt><dd>{fresh ? label(access, checked.sample_access, '尚未验证') : '尚未验证'}</dd>
    </dl>
    {connection.provider === 'yfinance' ? <p>检查会向 Yahoo 请求少量历史行情。通过的结果只适用于这次样本。</p>
      : <p>这一步只核对本机保存的设置。网络访问和套餐权限仍需另行确认。</p>}
    {fresh && checked.sample_access === 'RETURNED_ROWS' && typeof actual.rows === 'number' &&
      typeof actual.start === 'string' && typeof actual.end === 'string' &&
      <p>{String(actual.ticker ?? '本次样本')} 实际取到 {actual.rows} 条，日期 {actual.start} 至 {actual.end}。</p>}
    {canWrite && <button type="button" disabled={busy || !!journal.error || !!jobId && !terminalResearchJob(record(job))}
      onClick={() => void check()}>{journal.current.current ? '核对上次检查' : '检查此连接'}</button>}
    {jobId && state.error != null && <button type="button" onClick={() => setRefresh(value => value + 1)}>刷新检查记录</button>}
    {journal.current.current && <p><a href="#jobs">先在任务记录查看上次检查 →</a></p>}
    {jobId && <p><a href={'#jobs?job=' + encodeURIComponent(jobId)}>查看检查任务进度 →</a>
      {matchesJob && <> · <Status value={job.status} /></>}</p>}
    {(journal.error ?? error ?? state.error) != null && <ErrorBox error={journal.error ?? error ?? state.error} />}
    {matchesJob && job.status === 'FAILED' && <Notice>检查未完成。请打开任务详情查看原因。</Notice>}
  </section>;
}

export function MarketHelp() {
  return <AreaHelp title="怎样选行情来源" topic="market-onboarding" />;
}
