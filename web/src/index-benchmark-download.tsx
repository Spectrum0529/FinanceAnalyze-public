import { useRef, useState } from 'react';
import { post } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, Panel, useResource } from './components';
import { ImportJob } from './library-import';
import { NativeDateInput } from './native-date-input';
import { record } from './native-research-api';
import { uxMutation } from './ux-mutation';
import { useUxPending } from './ux-write-recovery';
import { backtestReturnParams, benchmarkId, benchmarkIds, indexDatasetRows, indexEtfs } from './index-benchmarks';
import type { BenchmarkId } from './index-benchmarks';

export function IndexBenchmarkDownload({ owner, canWrite, connections, links, revision }: {
  owner: string; canWrite: boolean; connections: RecordData[]; links: URLSearchParams; revision: number;
}) {
  const [id, setId] = useState<BenchmarkId>(() => benchmarkId(links.get('benchmark_preset')) ?? 'sp500');
  const [connection, setConnection] = useState('');
  const [start, setStart] = useState(backtestReturnParams(links).get('start_date') ?? '');
  const [end, setEnd] = useState(backtestReturnParams(links).get('end_date') ?? '');
  const gate = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [jobId, setJobId] = useState('');
  const [refresh, setRefresh] = useState(0);
  const catalogue = useResource<RecordData>('/data/library/benchmarks', refresh + revision);
  const pendingImport = useUxPending(owner, 'import');
  const presets = Array.isArray(catalogue.data?.presets) ? catalogue.data.presets.map(record) : [];
  if (!presets.length && !links.has('benchmark_preset') && !catalogue.error) return null;
  const etf = indexEtfs[id];
  const available = presets.some(row => row.index_id === id && row.available === true &&
    row.provider === etf.provider && row.ticker === etf.ticker);
  const matching = connections.filter(row => row.provider === etf.provider && typeof row.connection_id === 'string');
  const selected = matching.find(row => row.connection_id === connection) ?? matching[0];
  const saved = indexDatasetRows(catalogue.data?.items, id);
  const returnParams = backtestReturnParams(links);
  returnParams.set('index_benchmarks', '1');
  const disabled = !canWrite || !available || !selected || !start || !end || end < start ||
    busy || Boolean(jobId) || pendingImport;

  async function download() {
    if (gate.current || disabled || !selected) return;
    gate.current = true;
    setBusy(true);
    setError(null);
    try {
      const universe = await post<RecordData>('/data/library/universes/resolve', { groups: [], manual: etf.ticker, exclude: '' });
      if (!Array.isArray(universe.tickers) || universe.tickers.length !== 1 || universe.tickers[0] !== etf.ticker ||
          universe.historical_constituents !== false)
        throw new Error('无法确认结果属于你选的基金。请刷新后再选。');
      const accepted = await uxMutation(owner, 'import', '/data/library/download', { request: {
        connection_id: selected.connection_id, name: etf.label, universe, start, end, metadata: null, kind: 'benchmark',
        benchmark: { instrument_label: etf.label, return_basis: 'price', dividend_policy: 'excluded' },
      } });
      if (accepted.owner_id !== owner || typeof accepted.job_id !== 'string')
        throw new Error('还不能确认下载已提交。请先查看任务记录。');
      setJobId(accepted.job_id);
    } catch (error) { setError(error); }
    finally { gate.current = false; setBusy(false); }
  }

  return <Panel title="下载回测用的对照价格" className="data-job-panel">
    <p>选一只跟踪指数的基金和日期。保存后，回到回测页面选它作对照。</p>
    <div className="v434-date-range">
      <label>用来对照的基金<select value={id} disabled={busy || Boolean(jobId)} onChange={event => {
        const next = benchmarkId(event.target.value);
        if (next) { setId(next); setConnection(''); setError(null); }
      }}>{benchmarkIds.map(key => <option key={key} value={key}>{indexEtfs[key].label}</option>)}</select></label>
      <label>下载来源<select value={String(selected?.connection_id ?? '')} disabled={busy || Boolean(jobId) || !matching.length}
        onChange={event => setConnection(event.target.value)}>
        {!matching.length && <option value="">请先新增来源连接</option>}
        {matching.map(row => <option key={String(row.connection_id)} value={String(row.connection_id)}>{String(row.name)}</option>)}
      </select></label>
      <label>开始日期<NativeDateInput value={start} onChange={event => setStart(event.target.value)} disabled={busy || Boolean(jobId)} /></label>
      <label>结束日期<NativeDateInput value={end} onChange={event => setEnd(event.target.value)} disabled={busy || Boolean(jobId)} /></label>
    </div>
    {!available && <Notice>还不能确认这只基金能下载。请刷新后再试。</Notice>}
    {!matching.length && <p>请用下方“新增来源连接”保存 Yahoo 财经（yfinance）来源。</p>}
    {saved.length > 0 && <p>最近保存的数据 {String(saved[0].version)} · {String(record(saved[0].quality).first ?? '开始未知')} 至 {String(record(saved[0].quality).last ?? '结束未知')}</p>}
    <div className="form-actions"><button type="button" className="primary-action" disabled={disabled} onClick={() => void download()}>
      {busy ? '正在提交下载…' : '下载并保存对照价格'}
    </button><a href={'#research?' + returnParams.toString()}>回到回测选择对照</a></div>
    {error != null && <ErrorBox error={error} />}
    {catalogue.error != null && <ErrorBox error={catalogue.error} retry={() => setRefresh(n => n + 1)} />}
    {jobId && <ImportJob key={jobId} id={jobId} owner={owner} canWrite={canWrite} label="查看是否已提交下载"
      retry={() => setJobId('')} native continued={setJobId}
      changed={() => { setJobId(''); setRefresh(n => n + 1); }} />}
  </Panel>;
}
