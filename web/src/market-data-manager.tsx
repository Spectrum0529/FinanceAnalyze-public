import { useEffect, useRef, useState } from 'react';
import { ApiError, post, request } from './api';
import type { RecordData } from './api';
import { plainError } from './area-help';
import { ErrorBox, JsonView, Notice, Panel, useResource } from './components';
import { DatasetResearchExport } from './data-development-tools';
import { record } from './native-research-api';
import { definitelyRejected } from './ux-mutation';
import { MarketMergeForm } from './market-merge-form';

export type MarketRef = { source: 'library' | 'history'; id: string; version: number; content_hash: string };
export const marketPath = '/data/library/managed';
export function marketRef(value: unknown): MarketRef | null {
  const row = record(value);
  if ((row.source !== 'library' && row.source !== 'history') || typeof row.id !== 'string' ||
      typeof row.version !== 'number' || !Number.isSafeInteger(row.version) ||
      typeof row.content_hash !== 'string' || !/^[0-9a-f]{64}$/.test(row.content_hash)) return null;
  return { source: row.source, id: row.id, version: row.version, content_hash: row.content_hash };
}
const entityKey = (ref: MarketRef) => ref.source + ':' + ref.id;
export const marketRefKey = (ref: MarketRef) => JSON.stringify([ref.source, ref.id, ref.version, ref.content_hash]);
const refPath = (ref: MarketRef) => marketPath + '/' + ref.source + '/' + encodeURIComponent(ref.id);
type Selection = { ref: MarketRef; title: string; revision: number; trashed: boolean; canMerge: boolean; kind: string };
type BatchItem = Selection & { key: string; status: 'waiting' | 'success' | 'failed' | 'unknown'; reason?: string };
type Batch = { action: 'trash' | 'restore'; items: BatchItem[] };
const batchStatus = { waiting: '还未开始', success: '已确认', failed: '失败', unknown: '需要核对' };

function selection(value: RecordData, ref: MarketRef): Selection {
  return { ref, title: String(value.name ?? ref.id), revision: Number(value.revision),
    trashed: value.trashed === true, canMerge: record(value.capabilities).merge === true, kind: String(value.kind ?? 'dataset') };
}

export function MarketDataManager({ owner, canWrite, revision, connections, refreshed, prepareVerification }: {
  owner: string; canWrite: boolean; revision: number; connections: RecordData[]; refreshed: () => void;
  prepareVerification: (spec: RecordData) => void;
}) {
  const [state, setState] = useState('active'), [source, setSource] = useState('all'), [kind, setKind] = useState('all');
  const [search, setSearch] = useState(''), [q, setQuery] = useState('');
  const [offset, setOffset] = useState(0), [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<Selection[]>([]);
  const [detail, setDetail] = useState<Selection | null>(null), [versionsOffset, setVersionsOffset] = useState(0);
  const [renaming, setRenaming] = useState<{ target: Selection; key: string } | null>(null), [name, setName] = useState('');
  const [reuse, setReuse] = useState<{ target: Selection; key: string } | null>(null), [connection, setConnection] = useState('');
  const [mergedId, setMergedId] = useState(''), [error, setError] = useState<unknown>(null), [busy, setBusy] = useState(false);
  const journal = 'fa-market-batch:' + owner;
  const [batch, setBatch] = useState<Batch | null>(() => {
    try {
      const saved: Batch | null = JSON.parse(sessionStorage.getItem(journal) ?? 'null');
      return saved && ['trash', 'restore'].includes(saved.action) && Array.isArray(saved.items) ? saved : null;
    } catch { return null; }
  });
  const [confirm, setConfirm] = useState<Selection[] | null>(null);
  const [closeUnknown, setCloseUnknown] = useState(false);
  const [processing, setProcessing] = useState(false);
  const operation = useRef<AbortController | null>(null);
  useEffect(() => () => operation.current?.abort(), []);
  const params = new URLSearchParams({ q, source, state, kind, limit: '50', offset: String(offset) });
  const records = useResource<RecordData>(marketPath + '?' + params, revision + refresh);
  const rows = Array.isArray(records.data?.items) ? records.data.items.map(record) : [];
  const detailParams = detail && new URLSearchParams({
    version: String(detail.ref.version), include_trash: String(detail.trashed),
    version_limit: '25', version_offset: String(versionsOffset),
  });
  const details = useResource<RecordData>(detail && detailParams ? refPath(detail.ref) + '?' + detailParams : null, revision + refresh);
  const versions = record(details.data?.versions);
  const versionRows = Array.isArray(versions.items) ? versions.items : [];
  const library = useResource<RecordData>(detail?.ref.source === 'library' ? '/data/library/datasets/' + encodeURIComponent(detail.ref.id) : null, revision + refresh);
  const reset = () => setOffset(0);
  function invalidate() { setRefresh(n => n + 1); refreshed(); }
  function pick(row: Selection, checked: boolean) {
    setSelected(previous => {
      const others = previous.filter(item => marketRefKey(item.ref) !== marketRefKey(row.ref));
      return checked ? [...others, row] : others;
    });
  }
  function saveBatch(value: Batch | null) {
    if (value) sessionStorage.setItem(journal, JSON.stringify(value));
    else sessionStorage.removeItem(journal);
    setBatch(value);
  }

  async function processBatch(current: Batch, mode: 'waiting' | 'lookup' | 'repeat' | 'close') {
    if (operation.current || !canWrite) return;
    const controller = new AbortController();
    operation.current = controller; setProcessing(true); setError(null);
    let next = current;
    try {
      for (const item of current.items) {
        if (controller.signal.aborted || item.status !== (mode === 'waiting' ? 'waiting' : 'unknown')) continue;
        const path = refPath(item.ref) + '/lifecycle';
        const update = (status: BatchItem['status'], reason?: string) => {
          next = { ...next, items: next.items.map(row => row.key === item.key ? { ...row, status, reason } : row) };
          saveBatch(next);
        };
        if (mode === 'waiting') update('unknown');
        try {
          if (mode === 'lookup' || mode === 'close') {
            const receipt = await request<RecordData>('/operations/pending-write/' + mode, {
              method: 'POST', signal: controller.signal,
              body: JSON.stringify({ method: 'POST', path, idempotency_key: item.key, ...(mode === 'close' ? { confirm: true } : {}) }),
            });
            if (receipt.status === 'COMMITTED' && record(receipt.resource).kind === 'dataset' &&
                record(receipt.resource).id === item.ref.id) update('success');
            if (receipt.status === 'CLOSED' && receipt.resource === null) update('failed', '这次操作已关闭，未保存更改。');
            continue;
          }
          const receipt = await request<RecordData>(path, { method: 'POST', signal: controller.signal,
            body: JSON.stringify({ action: current.action, expected_revision: item.revision, idempotency_key: item.key }),
          });
          if (receipt.source !== item.ref.source || receipt.id !== item.ref.id || receipt.trashed !== (current.action === 'trash') ||
              receipt.revision !== item.revision + 1) throw Error('还不能确认这项数据是否处理成功。');
          update('success');
        } catch (cause) {
          const explicit = definitelyRejected(cause) || cause instanceof ApiError && cause.status === 409 &&
            ['MARKET_REVISION_CONFLICT', 'HISTORY_REVISION_CONFLICT'].includes(cause.code);
          if (!controller.signal.aborted) update(mode === 'waiting' && explicit ? 'failed' : 'unknown',
            cause instanceof ApiError ? plainError(cause.message, cause.status, cause.code) :
              cause instanceof Error ? cause.message : '请核对任务记录。');
        }
      }
      if (!controller.signal.aborted) invalidate();
    } catch (cause) { if (!controller.signal.aborted) setError(cause); }
    finally {
      operation.current = null;
      if (!controller.signal.aborted) { setProcessing(false); setCloseUnknown(false); }
    }
  }

  function beginBatch() {
    if (!confirm || !confirm.length || !canWrite) return;
    const next: Batch = { action: confirm[0].trashed ? 'restore' : 'trash',
      items: confirm.map(item => ({ ...item, key: crypto.randomUUID(), status: 'waiting' })) };
    try {
      saveBatch(next);
      setConfirm(null);
      setSelected([]);
      void processBatch(next, 'waiting');
    } catch (cause) { setError(cause); }
  }
  async function applyName() {
    if (!renaming || !name.trim() || busy || !canWrite) return;
    setBusy(true); setError(null);
    try {
      const { target, key } = renaming;
      const receipt = await post<RecordData>(refPath(target.ref) + '/lifecycle', {
        action: 'rename', expected_revision: target.revision, idempotency_key: key, name: name.trim(),
      });
      if (receipt.id !== target.ref.id || receipt.source !== target.ref.source || receipt.name !== name.trim()) throw Error('无法确认所选行情已改名。请刷新行情后检查名称。');
      setRenaming(null); invalidate();
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  }
  async function applyReuse() {
    if (!reuse || busy || !canWrite) return;
    setBusy(true); setError(null);
    try {
      const receipt = await post<RecordData>(marketPath + '/reuse', {
        reference: reuse.target.ref, idempotency_key: reuse.key, ...(connection ? { connection_id: connection } : {}),
      });
      if (typeof receipt.dataset_id !== 'string' || typeof receipt.content_hash !== 'string') throw Error('还不能确认行情已保存。请刷新行情列表查看。');
      setMergedId(receipt.dataset_id); setReuse(null); invalidate();
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  }
  const selectedForState = [...new Map(selected.filter(row => row.trashed === (state === 'trash'))
    .map(row => [entityKey(row.ref), row])).values()];
  const canStart = !batch && !processing && !confirm;
  return <Panel title="已保存行情">
    <div className="form-actions">
      <button type="button" aria-pressed={state === 'active'} onClick={() => { setState('active'); reset(); }}>可用行情</button>
      <button type="button" aria-pressed={state === 'trash'} onClick={() => { setState('trash'); reset(); }}>行情回收站</button>
      <button type="button" onClick={() => setRefresh(n => n + 1)}>刷新行情</button>
    </div>
    <form onSubmit={event => { event.preventDefault(); setQuery(search.trim()); reset(); }}>
      <label>搜索行情<input value={search} onChange={event => setSearch(event.target.value)} placeholder="名称、证券或编号" /></label>
      <button>搜索</button>
    </form>
    <label>行情来源<select value={source} onChange={event => { setSource(event.target.value); reset(); }}>
      <option value="all">全部</option><option value="library">我的数据集</option><option value="history">历史行情</option>
    </select></label>
    <label>行情类型<select value={kind} onChange={event => { setKind(event.target.value); reset(); }}>
      <option value="all">全部</option><option value="dataset">策略与研究数据</option><option value="benchmark">对照用价格</option>
    </select></label>
    {canWrite && selectedForState.length > 0 && <div className="form-actions">
      <button type="button" disabled={!canStart} onClick={() => setConfirm(selectedForState)}>
        {state === 'trash' ? '恢复' : '回收'}所选 {selectedForState.length} 项
      </button>
      <button type="button" onClick={() => setSelected([])}>清除选择</button>
    </div>}
    {confirm && <fieldset>
      <legend>{confirm[0].trashed ? '确认恢复行情' : '确认移入回收站'}</legend>
      <p>共 {confirm.length} 项，每项会单独处理，可能只成功一部分。历史行情会连同它的所有版本一起移入回收站或恢复。</p>
      <button type="button" onClick={beginBatch}>确认处理所选行情</button>
      <button type="button" onClick={() => setConfirm(null)}>返回</button>
    </fieldset>}
    {batch && <fieldset>
      <legend>每项处理结果</legend>
      <ul>{batch.items.map(item => <li key={item.key}>{item.title} · {batchStatus[item.status]} {item.reason ?? ''}</li>)}</ul>
      {processing && <button type="button" onClick={() => { operation.current?.abort(); setProcessing(false); }}>停止等待（已发送的操作可能完成）</button>}
      {batch.items.some(item => item.status === 'waiting') && <button type="button" disabled={processing} onClick={() => void processBatch(batch, 'waiting')}>处理还未开始的项</button>}
      {batch.items.some(item => item.status === 'unknown') && <>
        <button type="button" disabled={processing} onClick={() => void processBatch(batch, 'lookup')}>查看待确认项是否完成</button>
        <button type="button" disabled={processing} onClick={() => void processBatch(batch, 'repeat')}>重试上次未确认的操作</button>
        {!closeUnknown && <button type="button" disabled={processing} onClick={() => setCloseUnknown(true)}>核对后结束未确认项</button>}
        {closeUnknown && <div>
          <p>已完成的更改会保留；还没完成的请求会关闭。</p>
          <button type="button" disabled={processing} onClick={() => void processBatch(batch, 'close')}>确认结束未完成项</button>
          <button type="button" disabled={processing} onClick={() => setCloseUnknown(false)}>返回核对</button>
        </div>}
      </>}
      {!batch.items.some(item => item.status === 'unknown' || item.status === 'waiting') &&
        <button type="button" disabled={processing} onClick={() => saveBatch(null)}>关闭处理记录</button>}
    </fieldset>}
    {records.error != null ? <ErrorBox error={records.error} retry={() => setRefresh(n => n + 1)} /> :
      records.loading ? <p role="status">正在读取已保存行情…</p> : !rows.length && <Notice>{state === 'trash' ? '未找到已回收的行情。可清空搜索或调整筛选。' : '未找到行情。可调整筛选，或在“新增数据”下载或导入。'}</Notice>}
    <div className="table-scroll"><table className="saved-dataset-table">
      <thead><tr><th>名称及证券</th><th>实际取到的日期</th><th>数据版本与使用次数</th><th>操作</th></tr></thead>
      <tbody>{rows.map(row => {
        const ref = marketRef(row.reference);
        if (!ref) return null;
        const item = selection(row, ref), coverage = record(row.coverage);
        const backtest = ref.source === 'library' ? datasetBacktestHref(ref.id, ref.version, ref.content_hash) : null;
        return <tr key={entityKey(ref)}>
          <td><strong>{item.title}</strong><br />{String(row.provider ?? '来源未提供')} · {Array.isArray(row.tickers) ? row.tickers.join('、') : '证券未提供'}</td>
          <td>{String(coverage.first ?? coverage.start ?? '未知')} 至 {String(coverage.last ?? coverage.end ?? '未知')}
            <br /><small>请求 {String(row.start ?? '未知')} 至 {String(row.end ?? '未知')}，含首尾</small></td>
          <td>版本 {ref.version} · {String(row.reference_count ?? '未知')} 处引用</td>
          <td><div className="dataset-actions">
            <button type="button" onClick={() => { setDetail(item); setVersionsOffset(0); }}>查看数据和日期</button>
            {canWrite && <>
              <label><input type="checkbox" checked={selected.some(value => marketRefKey(value.ref) === marketRefKey(ref))}
                onChange={event => pick(item, event.target.checked)} />选择 {item.title}</label>
              {!item.trashed && <button type="button" onClick={() => { setName(item.title); setRenaming({ target: item, key: crypto.randomUUID() }); }}>改名</button>}
              <button type="button" disabled={!canStart} onClick={() => setConfirm([item])}>{item.trashed ? '恢复' : '移入回收站'}</button>
            </>}
            {!item.trashed && item.kind === 'dataset' && (ref.source === 'library'
              ? backtest && <a href={backtest}>用这份数据回测 →</a>
              : canWrite && record(row.capabilities).reuse === true &&
                <button type="button" onClick={() => { setConnection(''); setReuse({ target: item, key: crypto.randomUUID() }); }}>存为可回测的数据</button>)}
          </div></td>
        </tr>;
      })}</tbody>
    </table></div>
    <div className="pagination">
      <button type="button" disabled={!offset || records.loading} onClick={() => setOffset(n => Math.max(0, n - 50))}>上一页行情</button>
      <button type="button" disabled={records.loading || offset + 50 >= Number(records.data?.total ?? 0)} onClick={() => setOffset(n => n + 50)}>下一页行情</button>
    </div>
    {detail && <section aria-label="行情各版本的详情">
      <h3>{detail.title} · 这次保存的数据</h3>
      {details.error != null ? <ErrorBox error={details.error} /> : details.data && <>
        <p>所选日期 {String(details.data.start)} 至 {String(details.data.end)}，含首尾。{Array.isArray(details.data.tickers) && details.data.tickers.join('、')}</p>
        <details><summary>查看实际日期和价格算法的原始记录</summary><JsonView value={{ coverage: details.data.coverage, metadata: details.data.metadata }} /></details>
        <ul>{versionRows.map(raw => {
          const ref = marketRef(raw);
          if (!ref) return null;
          return <li key={marketRefKey(ref)}>
            版本 {ref.version} <details><summary>查看内容校验码</summary><code>{ref.content_hash}</code></details>
            <button type="button" onClick={() => setDetail({ ...detail, ref })}>查看该版本</button>
            {canWrite && !detail.trashed && <label><input type="checkbox"
              checked={selected.some(item => marketRefKey(item.ref) === marketRefKey(ref))}
              onChange={event => pick({ ...detail, ref }, event.target.checked)} />选择版本 {ref.version}</label>}
          </li>;
        })}</ul>
        <button type="button" disabled={!versionsOffset} onClick={() => setVersionsOffset(n => Math.max(0, n - 25))}>上一页版本</button>
        <button type="button" disabled={versionsOffset + 25 >= Number(versions.total ?? 0)} onClick={() => setVersionsOffset(n => n + 25)}>下一页版本</button>
      </>}
      {canWrite && !detail.trashed && detail.ref.source === 'history' && record(details.data?.capabilities).reuse === true &&
        <button type="button" onClick={() => { setConnection(''); setReuse({ target: detail, key: crypto.randomUUID() }); }}>把这个版本存为回测数据</button>}
      {library.data?.dataset_id === detail.ref.id && record(library.data.source).provider !== 'wrds' &&
        <DatasetResearchExport key={detail.ref.id} dataset={library.data} owner={owner} canWrite={canWrite && !detail.trashed} />}
    </section>}
    {renaming && <fieldset><legend>改名 · {renaming.target.title}</legend>
      <label>行情新名称<input value={name} onChange={event => setName(event.target.value)} maxLength={120} disabled={busy} /></label>
      <button type="button" disabled={busy || !name.trim()} onClick={() => void applyName()}>保存行情名称</button>
      <button type="button" disabled={busy} onClick={() => setRenaming(null)}>返回</button>
    </fieldset>}
    {reuse && <fieldset><legend>保存历史行情副本 · {reuse.target.title}</legend>
      <label>关联的来源连接<select value={connection} onChange={event => setConnection(event.target.value)}>
        <option value="">用同一来源的已有连接</option>
        {connections.map(row => <option key={String(row.connection_id)} value={String(row.connection_id)}>{String(row.name)}</option>)}
      </select></label>
      <button type="button" disabled={busy} onClick={() => void applyReuse()}>保存行情副本</button>
      <button type="button" disabled={busy} onClick={() => setReuse(null)}>返回</button>
    </fieldset>}
    {canWrite && state === 'active' && <MarketMergeForm
      key={selected.map(item => marketRefKey(item.ref)).join(':')}
      choices={selected.filter(row => row.canMerge && !row.trashed).map(row => ({ reference: row.ref, label: row.title }))}
      connections={connections} prepareVerification={prepareVerification} saved={id => { setMergedId(id); invalidate(); }} />}
    {mergedId && <p role="status">新行情已保存。<a href={'#research?dataset_id=' + encodeURIComponent(mergedId)}>用这份行情回测 →</a></p>}
    {error != null && <ErrorBox error={error} />}
  </Panel>;
}
import { datasetBacktestHref } from './index-benchmarks';
