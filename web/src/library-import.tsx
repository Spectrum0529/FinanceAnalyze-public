import { NativeDownloadProgress } from './data-development-tools';
import { parseISODate } from './v434/assets/date-picker.js';
import { SavedDataBacktestLink } from './saved-data-backtest-link';
import { MassiveDownloadProgress } from './massive-connection-status';
import { useState } from 'react';
import { post } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, Panel, useResource } from './components';
import { record, terminalResearchJob } from './native-research-api';
import { AreaHelp } from './area-help';
import { uxMutation } from './ux-mutation';
import { UxWriteRecovery, useUxPending } from './ux-write-recovery';
import { goalOnlyProvider } from './goal-research-api';
const metadataLabels: Record<string, string> = { price_unit: '价格单位', volume_unit: '成交量单位', adjustment: '历史价格如何调整', currency: '币种', timezone: '时区' };
const columnLabels: Record<string, string> = { ticker: '股票代码', session_date: '日期', open: '开盘价', high: '最高价', low: '最低价', close: '收盘价', volume: '成交量' };
export function LibraryImport({ owner, canWrite, connection, selection, start, end, name, changed, accepted }: { owner: string; canWrite: boolean; connection: RecordData | null; selection: RecordData | null; start: string; end: string; name: string; changed: () => void; accepted?: (id: string, isFile: boolean) => void }) {
  const [content, setContent] = useState(''), [filename, setFilename] = useState(''), [preview, setPreview] = useState<RecordData | null>(null), [metadata, setMetadata] = useState<Record<string, string>>({}), [mapping, setMapping] = useState<Record<string, string>>({}), [scope, setScope] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null), [jobId, setJobId] = useState(''), [benchmark, setBenchmark] = useState(false), [instrument, setInstrument] = useState('');
  const pendingPreview = useUxPending(owner, 'preview');
  const pendingImport = useUxPending(owner, 'import');
  const csv = connection?.provider === 'csv';
  const goalSource = goalOnlyProvider(connection?.provider);
  const catalogue=useResource<RecordData>('/data/library/providers');
  const capability=Array.isArray(catalogue.data?.items)?catalogue.data.items.map(record).find(row=>row.provider===connection?.provider):undefined;
  const tickers=Array.isArray(selection?.tickers)?selection.tickers.map(String):[];
  const dateSpan=(Date.parse(end)-Date.parse(start))/86400000;
  const constraint=connection?.provider==='openbb_cboe'&&capability?.available!==true?'还不能使用 OpenBB / Cboe 下载。请展开“来源支持什么”查看原因。':!csv&&capability?(typeof capability.max_symbols==='number'&&tickers.length>capability.max_symbols?`此来源每次最多${capability.max_symbols}个证券，请缩小范围。`:Array.isArray(capability.unsupported_symbols)&&tickers.some(t=>Array.isArray(capability.unsupported_symbols)&&capability.unsupported_symbols.includes(t))?'这个来源不支持部分已选代码，请调整名单。':typeof capability.maximum_calendar_days==='number'&&dateSpan>capability.maximum_calendar_days?`此来源日期跨度最多${capability.maximum_calendar_days}个日历日。`:typeof capability.earliest_start==='string'&&start&&start<capability.earliest_start?'开始日期早于此来源支持范围。':capability.end_policy==='BEFORE_CURRENT_UTC_DATE'&&end&&end>=new Date().toISOString().slice(0,10)?'这个来源的结束日期必须早于今天（按 UTC 时间算）。':null):null;
  async function file(file: File | undefined) {
    setPreview(null); setContent(''); setError(null); if (!file) return;
    try { if (file.size > 1024 * 1024) throw new Error('文件超过 1 MiB，无法预览。请选择较小的文件。'); const bytes = new Uint8Array(await file.arrayBuffer()); let binary = ''; for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192)); setContent(btoa(binary)); setFilename(file.name); } catch (e) { setError(e); }
  }
  async function inspect() {
    if (busy || pendingPreview) return; setBusy(true); setError(null);
    try { const value = await uxMutation(owner, 'preview', '/data/library/import-preview', { content_base64: content, options: { mapping, metadata: Object.fromEntries(Object.entries(metadata).filter(([, v]) => v)) } }); if (!['READY', 'NEEDS_INPUT'].includes(String(value.status))) throw new Error('无法确认预览是否成功。请重新选择文件并预览。'); setPreview(value); } catch (e) { setError(e); } finally { setBusy(false); }
  }
  const validDates = Boolean(parseISODate(start) && parseISODate(end) && start <= end);
  async function submit() {
    if (busy || !canWrite || !selection || !connection || jobId || pendingImport) return;
    setBusy(true); setError(null);
    try {
      if (!validDates) throw new Error('请选择开始和结束日期，结束日期不能早于开始日期。');
      if (constraint || goalSource && capability?.availability !== 'BOUND_UNVERIFIED') throw new Error(constraint || '还不能确认这个来源能下载。');
      const asBenchmark = benchmark && !goalSource;
      const base = { connection_id: connection.connection_id, name: name.trim() || null, universe: selection, start, end, kind: asBenchmark ? 'benchmark' : 'dataset', benchmark: asBenchmark ? { instrument_label: instrument, return_basis: 'price', dividend_policy: 'PRICE_ONLY_DIVIDENDS_NOT_INCLUDED' } : null };
      let result: RecordData;
      if (csv) {
        if (preview?.status !== 'READY' || !Array.isArray(preview.rows) || !['synthetic', 'public'].includes(scope)) throw new Error('请先预览文件，再选择文件里的数据类型。');
        result = await uxMutation(owner, 'import', '/data/library/import', { dataset: { ...base, metadata: preview.metadata, rows: preview.rows, original_upload_id: record(preview.original_upload).upload_id, csv_mapping: preview.mapping, source: { provider: 'csv', reference: filename || 'user CSV', universe_as_of: null }, received_at: new Date().toISOString(), scope } });
      } else result = await uxMutation(owner, 'import', '/data/library/download', { request: { ...base, metadata: null } });
      if (typeof result.job_id !== 'string' || result.owner_id !== owner) throw new Error('还不能确认是否已提交。请先查看任务记录，以免重复导入。'); setJobId(result.job_id); accepted?.(result.job_id, csv);
    } catch (e) { setError(e); } finally { setBusy(false); }
  }
  const missing = Array.isArray(preview?.missing) ? preview.missing.map(String) : [];
  return <Panel title="4. 检查并保存数据">
    {!connection ? <p>先选择来源连接。</p> : csv ? <><label>CSV 文件<input type="file" accept=".csv,text/csv" disabled={busy || !canWrite} onChange={(e) => void file(e.target.files?.[0])} /></label><button disabled={busy || !content || !canWrite || pendingPreview} onClick={() => void inspect()}>预览文件并识别列名</button><AreaHelp topic="csv" title="CSV 文件预览" />
      {preview?.status === 'NEEDS_INPUT' && <><AreaHelp topic="csv-mapping" title="文件列名和数据单位" /><Notice>有些信息没能自动识别。请按原文件填写，再预览一次。</Notice>{missing.map((key) => metadataLabels[key] ? <label key={key}>{metadataLabels[key]}{key === 'price_unit' || key === 'volume_unit' || key === 'adjustment' ? <select value={metadata[key] ?? ''} onChange={(e) => setMetadata({ ...metadata, [key]: e.target.value })}><option value="">请选择</option>{(key === 'price_unit' ? [['per_share', '每股价格']] : key === 'volume_unit' ? [['shares', '股数']] : [['raw', '当时成交价（未调整）'], ['split_adjusted', '仅按拆股换算的价格']]).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select> : <input value={metadata[key] ?? ''} placeholder={key === 'currency' ? '例如 USD' : '例如 America/New_York'} onChange={(e) => setMetadata({ ...metadata, [key]: e.target.value })} />}</label> : <label key={key}>{columnLabels[key] ?? key}对应的文件列名<input value={mapping[key] ?? ''} onChange={(e) => setMapping({ ...mapping, [key]: e.target.value })} /></label>)}</>}
      {preview?.status === 'READY' && <p>预览通过，共 {Array.isArray(preview.rows) ? preview.rows.length : '未提供'} 条；保存时还会检查日期和记录是否齐全。</p>}<label>文件数据类型<select value={scope} onChange={(e) => setScope(e.target.value)}><option value="">请明确选择</option><option value="synthetic">合成练习数据</option><option value="public">公开数据（需另查最早可取得日期）</option></select></label></> : <Notice>{goalSource?'可下载范围取决于市场和价格类型。实际日期请看下载结果。':connection.provider==='openbb_cboe'?'OpenBB / Cboe 无需密钥。价格是否经过调整仍待核对；实际日期请看下载结果。':connection.provider === 'massive' ? '下载范围和费用取决于你的 Massive 套餐。本机所有 Massive 任务共用额度。' : '无需密钥。网站拒绝访问或有股票数据缺失时，任务会说明原因。'}下载完成后，去“我的数据集”查看日期和价格算法。回测只支持其中部分市场和价格类型。</Notice>}
    <UxWriteRecovery owner={owner} area="preview" busy={busy} canWrite={canWrite} recovered={() => setError(null)} />
    {!goalSource && <details><summary>另存为回测用的对照价格（可选）</summary><label><input type="checkbox" checked={benchmark} onChange={(e) => setBenchmark(e.target.checked)} />在以后的回测中可选作对照</label>{benchmark && <label>对照名称（写清指数或基金）<input value={instrument} onChange={(e) => setInstrument(e.target.value)} placeholder="例如 QQQ ETF（纳斯达克100），不是纳斯达克综合指数" /></label>}<p>这份数据只用价格算收益，不包含现金分红。</p></details>}
    {constraint&&<Notice>{constraint}</Notice>}{goalSource && capability?.availability !== 'BOUND_UNVERIFIED' && <Notice>还不能确认这个来源能下载。请检查来源设置和密钥，再重试。</Notice>}<button className="primary-action" disabled={!!constraint || goalSource && capability?.availability !== 'BOUND_UNVERIFIED' || busy || !canWrite || !connection || !selection || !validDates || !!jobId || pendingImport || csv && (preview?.status !== 'READY' || !scope) || !goalSource && benchmark && !instrument.trim()} onClick={() => void submit()}>{csv ? '导入并保存数据' : '开始下载并保存数据'}</button>{error != null && <ErrorBox error={error} />}
    {jobId && <ImportJob id={jobId} owner={owner} canWrite={canWrite} changed={changed} massive={connection?.provider==='massive'} native={['yfinance','akshare','openbb_cboe'].includes(String(connection?.provider))} continued={id=>setJobId(id)} />}
  </Panel>;
}
export function ImportJob({ id, owner, canWrite, changed, label = '查看我的数据集', massive=false, native=false, continued, retry }: { id: string; owner: string; canWrite: boolean; changed: () => void; label?: string; massive?:boolean; native?:boolean; continued?:(id:string)=>void; retry?: () => void }) {
  const [refresh, setRefresh] = useState(0), [error, setError] = useState<unknown>(null), [busy, setBusy] = useState(false);
  const state = useResource<RecordData>(`/jobs/${encodeURIComponent(id)}`, refresh, 1500, terminalResearchJob), job = state.data;
  const valid = job?.job_id === id && job.owner_id === owner && ['data.library-import', 'data.library-download', 'data.goal-prepare'].includes(String(job.handler));
  return <>{native&&valid&&<NativeDownloadProgress key={id} jobId={id} owner={owner} canWrite={canWrite} finished={terminalResearchJob(job)} changed={newId=>continued?.(newId)}/>} {massive&&valid&&<MassiveDownloadProgress key={id} jobId={id} owner={owner} canWrite={canWrite} changed={newId=>continued?.(newId)}/>} {state.error ? <ErrorBox error={state.error} /> : valid ? <><p><a href={job.handler==='data.library-download'?`#data?download_job=${encodeURIComponent(id)}`:'#jobs'}>{job.handler==='data.library-download'?'重开下载任务进度 →':'查看数据任务记录'}</a></p><p>导入任务：{({ QUEUED: '排队中', RUNNING: '执行中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消' } as Record<string, string>)[String(job.status)] ?? '待确认'}；进度 {String(job.progress ?? '未提供')}。</p>{job.status === 'SUCCEEDED' ? <div className="form-actions"><button onClick={changed}>{label}</button>{canWrite && <SavedDataBacktestLink reference={record(job.result)} jobId={id} />}</div> : retry && terminalResearchJob(job) ? <button type="button" onClick={retry}>返回选择并重新下载</button> : <button disabled={busy || !canWrite || terminalResearchJob(job) || job.cancel_requested === true} onClick={async () => { setBusy(true); try { await post(`/jobs/${encodeURIComponent(id)}/cancel`); setRefresh((n) => n + 1); } catch (e) { setError(e); } finally { setBusy(false); } }}>请求停止导入</button>}<p>失败或取消的任务不会标为已导入。若只完成部分股票，请查看任务详情。</p></> : <p>正在核对导入任务…</p>}{error != null && <ErrorBox error={error} />}</>;
}
