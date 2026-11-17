import { useRef, useState } from 'react';
import { ApiError, post, request } from './api';
import type { RecordData } from './api';
import { ErrorBox, JsonView, Notice, Panel, useResource } from './components';
import { record, terminalResearchJob } from './native-research-api';
import { datasetBacktestHref } from './index-benchmarks';
import { HistoricalReport } from './historical-report';

const path = '/onboarding/migration';
const format = 'financeanalyze-transfer/1';
const statuses: Record<string, string> = {
  importable: '可迁入', reused: '已迁入，无需重复', conflict: '有冲突，保留目标机版本',
  rejected: '暂不能迁入', denied: '暂不能迁入', completed: '已完成', imported: '已迁入', partial: '部分已迁入',
  pending: '正在准备迁入行情', ready_to_link: '行情已核对，可完成迁入',
};
const kindNames = { datasets: '已保存的文件行情', strategies: '已保存的策略版本', results: '已保存的研究报告' };
const migrationReasons: Record<string, string> = {
  MIGRATION_YAHOO_JOB_CANCELLED: '原行情迁入任务已取消。',
  MIGRATION_YAHOO_CAPTURE_CHANGED: 'Yahoo 返回的行情与旧电脑保存的版本不一致，不能迁入这份固定行情。',
};
const retryErrors: Record<string, string> = {
  MIGRATION_YAHOO_RETRY_SUPERSEDED: '已有新的尝试，请重新核对这份文件并查看最新任务。',
  MIGRATION_YAHOO_RETRY_NOT_TERMINAL: '原任务状态已变，请重新核对这份文件。',
  MIGRATION_PREVIEW_CHANGED: '原文件与预览不一致，请重新选择这份迁移文件。',
  MIGRATION_VERSION_CONFLICT: '无法确认这次文件绑定的是原行情版本，请重新预览。',
};
type RetryReceipt = { previous: string; job: string };
type Kind = keyof typeof kindNames;
type Choice = { kind: Kind; value: RecordData; title: string };
function list(value: unknown) { return Array.isArray(value) ? value.map(record) : []; }
function choices(kind: Kind, row: RecordData): Choice | null {
  if (kind === 'datasets') {
    if (!['csv', 'yfinance'].includes(String(record(row.source).provider)) || !['public', 'synthetic'].includes(String(row.scope)) ||
        typeof row.dataset_id !== 'string' || typeof row.version !== 'number' || typeof row.content_hash !== 'string') return null;
    return { kind, title: String(row.name), value: { dataset_id: row.dataset_id, version: row.version, content_hash: row.content_hash } };
  }
  if (kind === 'strategies') {
    if (typeof row.strategy_id !== 'string' || typeof row.version !== 'string' && typeof row.version !== 'number' ||
        typeof row.package_hash !== 'string') return null;
    return { kind, title: String(row.strategy_id) + ' · ' + row.version,
      value: { strategy_id: row.strategy_id, version: String(row.version) } };
  }
  if (row.can_read !== true || typeof row.result_id !== 'string') return null;
  return { kind, title: String(row.name ?? row.result_id), value: { result_id: row.result_id } };
}
const pickerPaths = { datasets: '/data/library/datasets?kind=dataset&trash=false&', strategies: '/strategies?', results: '/results?state=active&' };
function MigrationPicker({ kind, selected, active, changed }: {
  kind: Kind; selected: Record<string, Choice>; active: boolean; changed: (key: string, value: Choice, checked: boolean) => void;
}) {
  const [offset, setOffset] = useState(0);
  const state = useResource<RecordData>(active ? pickerPaths[kind] + new URLSearchParams({ limit: '30', offset: String(offset) }) : null);
  const rows = list(state.data?.items).map(row => choices(kind, row)).filter(row => row !== null);
  return <fieldset><legend>{kindNames[kind]}</legend>
    {state.loading ? <p role="status">正在读取可选记录…</p> : state.error != null ? <ErrorBox error={state.error} /> :
      !rows.length ? <p>这一页没有可选记录。</p> : rows.map(row => {
        const key = kind + ':' + JSON.stringify(row.value);
        return <label key={key}><input type="checkbox" checked={Object.hasOwn(selected, key)}
          onChange={e => changed(key, row, e.target.checked)} />{row.title}</label>;
      })}
    <div className="form-actions">
      <button disabled={offset === 0 || state.loading} onClick={() => setOffset(page => page - 30)}>上一页</button>
      <button disabled={state.loading || offset + 30 >= Number(state.data?.total ?? 0)} onClick={() => setOffset(page => page + 30)}>下一页</button>
    </div>
  </fieldset>;
}
function jobHref(id: string) { return '#jobs?job=' + encodeURIComponent(id); }

function MigrationItems({ rows, openArchive, confirmRetry, retried }: {
  rows: RecordData[]; openArchive?: (archiveId: string) => void; confirmRetry?: (id: string) => void; retried?: string;
}) {
  return <ul>{rows.map((row, index) => {
    const target = record(row.target);
    const backtest = datasetBacktestHref(target.dataset_id, target.version, target.content_hash);
    const kind = ({ dataset: '文件行情', yahoo_dataset: 'Yahoo 行情', strategy: '策略版本', report: '研究报告' } as Record<string, string>)[String(row.kind)];
    const strategy = row.kind === 'strategy' && typeof target.strategy_id === 'string' && typeof target.version === 'string';
    const title = typeof row.display_title === 'string' ? row.display_title : typeof row.title === 'string' ? row.title
      : strategy ? '策略 ' + target.strategy_id + ' · ' + target.version : (kind ?? '迁入项目') + ' ' + (index + 1);
    const symbols = Array.isArray(row.display_symbols) ? row.display_symbols.filter(value => typeof value === 'string').join('、') : '';
    const period = typeof row.display_start === 'string' && typeof row.display_end === 'string' ? row.display_start + ' 至 ' + row.display_end : '';
    const jobId = typeof row.job_id === 'string' ? row.job_id : '';
    const failedYahoo = jobId && row.kind === 'yahoo_dataset' && row.status === 'rejected';
    return <li key={index}>
      <strong>{title}</strong> · {statuses[String(row.status)] ?? '已保存迁入记录'}
      {(symbols || period) && <p>{[symbols, period].filter(Boolean).join(' · ')}</p>}
      {failedYahoo && <p>{migrationReasons[String(row.reason_code)] ?? '原行情迁入任务失败，请查看任务里的原因。'}</p>}
      {row.reason_code != null && <details><summary>查看原因</summary><JsonView value={row.reason_code} /></details>}
      <details><summary>查看原引用和处理记录</summary><JsonView value={row} /></details>
      {openArchive && typeof target.archive_id === 'string' &&
        <button type="button" onClick={() => openArchive(String(target.archive_id))}>打开迁入报告</button>}
      {openArchive && typeof target.result_id === 'string' && target.result_id.startsWith('migration:') &&
        <a href={'#module-d?kind=imported&result_id=' + encodeURIComponent(target.result_id)}>在研究结果中打开</a>}
      {openArchive && backtest && <a href={backtest}>打开迁入的固定行情</a>}
      {openArchive && strategy && <a href="#strategies">在策略页审核</a>}
      {jobId && <a href={jobHref(jobId)}>{failedYahoo ? '查看原失败任务' : '查看行情迁入进度'}</a>}
      {failedYahoo && row.retry_available === true && confirmRetry && jobId !== retried &&
        <button type="button" onClick={() => confirmRetry(jobId)}>重新尝试迁入</button>}
    </li>;
  })}</ul>;
}

export function MigrationWorkspace({ ownerId }: { ownerId: string }) {
  const [refresh, setRefresh] = useState(0);
  const capability = useResource<RecordData>(path, refresh);
  const available = capability.data?.format === format;
  const [exportOpen, setExportOpen] = useState(false);
  const [selected, setSelected] = useState<Record<string, Choice>>({});
  const [preview, setPreview] = useState<RecordData | null>(null);
  const [received, setReceived] = useState<RecordData | null>(null);
  const [raw, setRaw] = useState<Blob | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [info, setInfo] = useState('');
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [archive, setArchive] = useState('');
  const [retryFor, setRetryFor] = useState('');
  const [retry, setRetry] = useState<RetryReceipt | null>(null);
  const retryKeys = useRef(new Map<string, string>());
  const jobView = useResource<RecordData>(retry ? '/jobs/' + encodeURIComponent(retry.job) : null, 0, 1500, terminalResearchJob);
  const retryJob = jobView.data?.job_id === retry?.job && jobView.data?.owner_id === ownerId &&
    jobView.data?.handler === 'onboarding.migration-yahoo' ? jobView.data : null;
  const canRetry = record(capability.data?.actions).retry_yahoo === true;
  const archiveView = useResource<RecordData>(archive ? path + '/reports/' + encodeURIComponent(archive) : null);
  const selectedRows = Object.values(selected);
  const maxBytes = Number(capability.data?.maximum_bytes);
  const maxItems = Number(capability.data?.maximum_items);
  async function perform(operation: () => Promise<void>) {
    if (lock.current || !available) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    setInfo('');
    try { await operation(); } catch (cause) { setError(cause); }
    finally { lock.current = false; setBusy(false); }
  }
  function choose(key: string, value: Choice, checked: boolean) {
    setSelected(old => {
      if (checked) return { ...old, [key]: value };
      const next = { ...old };
      delete next[key];
      return next;
    });
  }
  async function exportFile() {
    const value = await post<RecordData>(path + '/export', {
      dataset_references: selectedRows.filter(choice => choice.kind === 'datasets').map(choice => choice.value),
      strategy_versions: selectedRows.filter(choice => choice.kind === 'strategies').map(choice => choice.value),
      result_ids: selectedRows.filter(choice => choice.kind === 'results').map(choice => choice.value.result_id),
    });
    if (value.format !== format || !Array.isArray(value.items)) throw new Error('没有收到可迁移的文件。请核对所选记录。');
    const url = URL.createObjectURL(new Blob([JSON.stringify(value)], { type: 'application/json' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'FinanceAnalyze-transfer.json';
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    setInfo('已交给浏览器下载。请把迁移文件带到新电脑，再打开此页预览。');
  }
  async function readPreview(file: Blob) {
    if (Number.isFinite(maxBytes) && maxBytes > 0 && file.size > maxBytes) throw new Error('迁移文件超过当前服务允许的大小。');
    const value = await request<RecordData>(path + '/preview', { method: 'POST', body: file });
    if (typeof value.bundle_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(value.bundle_sha256) ||
        typeof value.can_import !== 'boolean' || !Array.isArray(value.items)) {
      throw new Error('没有收到可以核对的迁入预览。请重新选择文件。');
    }
    setRaw(file);
    setPreview(value);
    setReceived(null);
    setRetryFor('');
    setRetry(null);
  }
  async function previewFile(file: File) {
    setRaw(null);
    setPreview(null);
    setReceived(null);
    setRetryFor('');
    setRetry(null);
    await readPreview(file);
  }
  async function importFile() {
    if (!raw || typeof preview?.bundle_sha256 !== 'string' ||
        preview.can_import !== true && retryJob?.status !== 'SUCCEEDED') return;
    const value = await request<RecordData>(path + '/import', { method: 'POST', body: raw,
      headers: { 'X-Migration-SHA256': preview.bundle_sha256 } });
    if (value.bundle_sha256 !== preview.bundle_sha256 || !['completed', 'partial', 'rejected', 'pending'].includes(String(value.status))) {
      throw new Error('暂时无法确认迁入是否完成。请先查看下面的迁入记录。');
    }
    setReceived(value);
    setRefresh(current => current + 1);
  }
  async function retryYahoo() {
    const hash = preview?.bundle_sha256;
    const oldJob = retryFor;
    if (!canRetry || !raw || typeof hash !== 'string' || !oldJob) return;
    const storageKey = ['migration-yahoo-retry', ownerId, hash, oldJob].join(':');
    let key = retryKeys.current.get(storageKey);
    try { key ??= sessionStorage.getItem(storageKey) ?? undefined; } catch { /* Storage can be unavailable. */ }
    key ??= crypto.randomUUID();
    retryKeys.current.set(storageKey, key);
    try { sessionStorage.setItem(storageKey, key); } catch { /* Keep the key for this page. */ }
    try {
      const value = await request<RecordData>(path + '/yahoo/' + encodeURIComponent(oldJob) + '/retry', {
        method: 'POST', body: raw, headers: { 'X-Migration-SHA256': hash, 'X-Migration-Retry-Key': key },
      });
      if (value.bundle_sha256 !== hash || value.previous_job_id !== oldJob ||
          typeof value.job_id !== 'string' || !value.job_id || value.job_id === oldJob ||
          !['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(value.job_status))) {
        throw new Error('暂未确认重试任务。可重新核对这份文件，再查看任务记录。');
      }
      setRetry({ previous: oldJob, job: value.job_id });
      setRetryFor('');
    } catch (cause) {
      if (cause instanceof ApiError && retryErrors[cause.code]) throw new Error(retryErrors[cause.code]);
      throw cause;
    }
  }
  const history = list(capability.data?.imported);
  const retryAction = canRetry && !busy && !retryFor && !retry ? setRetryFor : undefined;
  return <Panel title="迁机助手" helpTopic="migration">
    <p>在旧电脑选择要带走的文件行情、策略版本和报告，下载迁移文件。在新电脑登录自己的账户，先预览，再确认迁入。</p>
    <Notice>到新机后请重新保存所需密钥，在账户安全页配置验证，并重新连接 Codex 或 DeepSeek。迁移文件不带登录凭据、授权和正在运行的任务。受限数据不会打包。</Notice>
    <a href="#settings">返回服务设置</a>
    {capability.loading && <p role="status">正在读取此版本可迁移的范围…</p>}
    {capability.error != null && <ErrorBox error={capability.error} retry={() => setRefresh(value => value + 1)} />}
    {available && <>
      <details onToggle={event => setExportOpen(event.currentTarget.open)}><summary>在旧电脑准备迁移文件</summary>
        <p>可以选择公开或练习文件行情、Yahoo 固定行情、策略版本和已保存报告。服务会核对每项是否可迁移。</p>
        {(Object.keys(kindNames) as Kind[]).map(kind => <MigrationPicker key={kind} kind={kind} active={exportOpen} selected={selected} changed={choose} />)}
        <button className="primary-action" type="button" disabled={busy || !selectedRows.length || Number.isFinite(maxItems) && maxItems > 0 && selectedRows.length > maxItems}
          onClick={() => void perform(exportFile)}>导出所选记录（{selectedRows.length} 项）</button>
      </details>
      <section aria-label="在新电脑迁入">
        <h3>在新电脑预览并迁入</h3>
        <label>选择迁移文件<input type="file" accept=".json,application/json" disabled={busy} onChange={e => {
          const file = e.target.files?.[0];
          e.currentTarget.value = '';
          if (file) void perform(() => previewFile(file));
        }} /></label>
        {preview && <><p>请查看哪些项目可以迁入、重复或有冲突。冲突会保留新电脑现有的版本。</p>
          <MigrationItems rows={list(preview.items)} confirmRetry={retryAction} retried={retry?.previous} />
          {!received && !retry && preview.can_import === true && <button className="primary-action" disabled={busy}
            onClick={() => void perform(importFile)}>确认迁入这份文件</button>}
          {!retry && (received?.status === 'partial' || received?.status === 'pending') &&
            <button disabled={busy} onClick={() => void perform(importFile)}>重查并继续未完成项目</button>}
        </>}
        {retryFor && <fieldset>
          <legend>重新尝试迁入 Yahoo 行情</legend>
          <p>将使用这份文件重新向 Yahoo 核对行情。原任务和失败记录仍可查看。</p>
          <button type="button" disabled={busy} onClick={() => void perform(retryYahoo)}>确认重新请求 Yahoo</button>
          <button type="button" disabled={busy} onClick={() => setRetryFor('')}>返回</button>
        </fieldset>}
        {retry && <div aria-label="行情迁入重试">
          <div className="form-actions"><a href={jobHref(retry.previous)}>查看原失败任务</a><a href={jobHref(retry.job)}>查看重试任务</a></div>
          {jobView.error != null ? <ErrorBox error={jobView.error} /> : <p role="status">
            {retryJob?.status === 'SUCCEEDED' ? '已核对这次行情，可以确认保存。' :
              retryJob?.status === 'FAILED' || retryJob?.status === 'CANCELLED' ? '这次重试未完成。请查看任务原因或重新核对文件。' :
                '已受理重试，等待行情核对。'}
          </p>}
          {retryJob?.status === 'SUCCEEDED' && received?.status !== 'completed' &&
            <button type="button" disabled={busy} onClick={() => void perform(importFile)}>确认保存这次迁入的行情</button>}
        </div>}
        {raw && canRetry && (retryFor || retry) &&
          <button type="button" disabled={busy} onClick={() => void perform(() => readPreview(raw))}>重新核对这份文件</button>}
      </section>
      {received && <section aria-label="本次迁入结果"><p role="status">{statuses[String(received.status)] ?? '迁入记录已返回'}</p>
        <MigrationItems rows={list(received.items)} openArchive={setArchive} confirmRetry={retryAction} retried={retry?.previous} />
      </section>}
      <div className="form-actions"><button type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>刷新迁入记录</button></div>
      {history.length > 0 && <><h3>已保存的迁入记录</h3><MigrationItems rows={history} openArchive={setArchive} /></>}
      {archiveView.error != null ? <ErrorBox error={archiveView.error} /> : archiveView.data && <section aria-label="迁入的历史报告">
        <HistoricalReport report={record(archiveView.data.report)} technical={archiveView.data} />
        <button onClick={() => setArchive('')}>返回迁入记录</button>
      </section>}
    </>}
    {info && <p role="status">{info}</p>}
    {error != null && <ErrorBox error={error} />}
  </Panel>;
}
