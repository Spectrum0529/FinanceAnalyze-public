import { ProviderCapabilities, DatedRosterImport, BoundedReferenceSelection, SavedDownloadProgress, DataProductJob } from './data-development-tools';
import { MassiveConnectionStatus } from './massive-connection-status';
import { NativeDateInput } from './native-date-input';
import { useEffect, useId, useState } from 'react';
import { post, download } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, Panel, useResource } from './components';
import { record } from './native-research-api';
import { uxMutation, uxPending } from './ux-mutation';
import { UxWriteRecovery, useUxPending } from './ux-write-recovery';
import { LibraryImport, ImportJob } from './library-import';
import { CredentialEditor } from './mcp-connections';
import { DataCenter } from './data-page';
import { goalOnlyProvider, goalProviders } from './goal-research-api';
import { GoalProviderSettings } from './goal-research-provider-settings';
import { WrdsCredential, WrdsLibrary, WrdsSavedJob, wrdsError } from './wrds-library';
import { sourceDescription } from './source-labels';
import { IndexBenchmarkDownload } from './index-benchmark-download';
import { MarketHelp, SourceConnectionCheck, marketLabels, preferredSource, sourceMismatch } from './source-onboarding';
import type { Market } from './source-onboarding';

import { MarketDataManager } from './market-data-manager';
import { MarketConnectionsManager } from './market-connections-manager';
export function DataLibraryWorkspace({ userId, canWrite }: { userId: string; canWrite: boolean }) {
  const [links,setLinks]=useState(()=>new URLSearchParams(location.hash.split('?')[1]??''));
  useEffect(()=>{const changed=()=>setLinks(new URLSearchParams(location.hash.split('?')[1]??''));window.addEventListener('hashchange',changed);return()=>window.removeEventListener('hashchange',changed);},[]);
  const downloadJob=links.get('download_job'),exportJob=links.get('research_export_job'),goalDownloadJob=links.get('goal_download_job'),wrdsDownloadJob=links.get('wrds_download_job');
  const importJob = links.get('import_job');
  const [credentialRef, setCredentialRef] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [market, setMarket] = useState<Market | ''>('');
  const marketId = useId();
  const [newConnection, setNewConnection] = useState(false);
  const [tab, setTab] = useState('new'), [refresh, setRefresh] = useState(0), [connection, setConnection] = useState(''), [provider, setProvider] = useState('csv'), [connectionName, setConnectionName] = useState('我的文件导入'), [groups, setGroups] = useState<string[]>([]), [manual, setManual] = useState(''), [exclude, setExclude] = useState(''), [selection, setSelection] = useState<RecordData | null>(null), [start, setStart] = useState(''), [end, setEnd] = useState(''), [name, setName] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null);
  const [recoveredImport, setRecoveredImport] = useState('');
  const pendingData = useUxPending(userId, 'data');
  const connections = useResource<RecordData>('/data/library/connections', refresh), universe = useResource<RecordData>('/data/library/universes', refresh);
  const connectionRows = Array.isArray(connections.data?.items) ? connections.data.items.map(record) : [];
  const providerCatalog = useResource<RecordData>('/data/library/providers', refresh);
  const availableProviders = goalProviders(providerCatalog.data);
  const extraProviders = availableProviders.filter((row) => goalOnlyProvider(row.id));
  const describeSource = (id: string) => availableProviders.find((row) => row.id === id) ?? sourceDescription(id);
  const selectedExtra = extraProviders.find((row) => row.id === provider);
  const credentialProvider = provider === 'massive' || provider === 'tushare' || provider === 'wrds';
  const wrds = Array.isArray(providerCatalog.data?.items)
    ? providerCatalog.data.items.map(record).find(row=>row.provider==='wrds') : undefined;
  const wrdsReady = wrds?.lookup===true && wrds.history===true && record(wrds.data_capability).profile==='crsp_ciz_us_daily_trade_v1';
  const wrdsConnectionBlocked = provider==='wrds' && (!wrdsReady || !credentialRef);
  function showSaved(){
    setRefresh(n=>n+1);setTab('saved');
  }
  function rememberJob(id: string, isFile: boolean) {
    const [route, search] = location.hash.split('?');
    const next = new URLSearchParams(search);
    next.delete('download_job');
    next.delete('import_job');
    next.set(isFile ? 'import_job' : 'download_job', id);
    history.replaceState(null, '', route + '?' + next);
  }
  const selectedConnection = Array.isArray(connections.data?.items) ? connections.data.items.map(record).find((row) => row.connection_id === connection) ?? null : null;
  const sourceCapabilities = Array.isArray(providerCatalog.data?.items) ? providerCatalog.data.items.map(record)
    .find(row => row.provider === selectedConnection?.provider) : undefined;
  const mismatch = sourceMismatch(market, selectedConnection?.provider, sourceCapabilities);
  function selectMarket(next: Market | '') {
    setMarket(next);
    if (!next) return;
    const recommended = preferredSource(providerCatalog.data, next) ??
      { US_EQUITY: 'yfinance', US_ETF: 'yfinance', CN_EQUITY: 'akshare_cn', CSV: 'csv' }[next];
    if (typeof recommended !== 'string') return;
    const saved = connectionRows.find(row => row.provider === recommended && row.archived !== true);
    setProvider(recommended);
    setConnectionName(describeSource(recommended).name);
    setCredentialRef(null);
    setConnection(typeof saved?.connection_id === 'string' ? saved.connection_id : '');
    setNewConnection(!saved);
    setSelection(null);
  }
  const rows = Array.isArray(universe.data?.items) ? universe.data.items.map(record) : [];
  const categories = [...new Set(rows.map((row) => Array.isArray(row.path) ? String(row.path[0]) : '其他'))];
  async function resolve() {
    if (busy) return; setBusy(true); setError(null); setSelection(null);
    try { const value = await post<RecordData>('/data/library/universes/resolve', { groups, manual, exclude }); if (!Array.isArray(value.tickers) || value.historical_constituents !== false) throw new Error('未能确认股票清单。请重新选择，再点“查看所选代码”。'); setSelection(value); } catch (e) { setError(e); } finally { setBusy(false); }
  }
  async function saveConnection() {
    if (!canWrite || busy || pendingData || uxPending(userId, 'data') || wrdsConnectionBlocked ||
        goalOnlyProvider(provider) && !selectedExtra?.connectionConfigurable) return;
    setBusy(true); setError(null);
    try {
      const value = await uxMutation(userId, 'data', '/data/library/connections', {
        connection: { name: connectionName, provider, credential_ref: credentialProvider ? credentialRef : null },
      });
      setConnection(String(value.connection_id)); setRefresh(n => n + 1);
    } catch (e) { setError(provider === 'wrds' ? wrdsError(e) : e); }
    finally { setBusy(false); }
  }

  function prepareVerification(spec: RecordData) {
    if (typeof spec.provider !== 'string' || typeof spec.start !== 'string' || typeof spec.end !== 'string' ||
        !Array.isArray(spec.tickers) || !spec.tickers.every(ticker => typeof ticker === 'string')) return;
    const compatible = connectionRows.find(row => row.connection_id === connection && row.provider === spec.provider) ??
      connectionRows.find(row => row.provider === spec.provider);
    setConnection(String(compatible?.connection_id ?? ''));
    setProvider(spec.provider); setGroups([]); setExclude(''); setSelection(null);
    setManual(spec.tickers.join(',')); setStart(spec.start); setEnd(spec.end);
    setName('用于核对的完整行情 ' + spec.tickers.join('、')); setTab('new');
  }
  return <div className="v434-data-layout" data-tab={tab}>
    <section className="panel data-workbench-toolbar" aria-label="管理数据"><p><a href="#settings">更改新数据保存位置 →</a> 已保存的数据留在原处。</p><div className="form-actions"><button aria-pressed={tab === 'new'} onClick={() => setTab('new')}>新增数据</button><button aria-pressed={tab === 'saved'} onClick={() => setTab('saved')}>我的数据集</button><button aria-pressed={tab === 'connections'} onClick={() => setTab('connections')}>管理来源连接</button><button className="text-button" onClick={() => setRefresh((n) => n + 1)}>刷新数据</button></div><Notice>回测前请确认：这些资料在模拟交易的日期是否已经公布。没有记录的名单或发布时间会标为未知。</Notice></section>
    {downloadJob&&<Panel title="上次下载进度" className="data-job-panel"><SavedDownloadProgress key={downloadJob} jobId={downloadJob} owner={userId} canWrite={canWrite}/></Panel>}{exportJob&&<Panel title="上次数据导出" className="data-job-panel"><DataProductJob id={exportJob} owner={userId} handler="data.research-export" completed={()=>void download(`/data/library/exports/${encodeURIComponent(exportJob)}/content`,'研究数据.json').catch(setError)}/></Panel>}
    {importJob && <Panel title="上次导入进度"><ImportJob id={importJob} owner={userId} canWrite={canWrite} changed={showSaved} /></Panel>}
    {goalDownloadJob && <Panel title="上次补充数据的进度" className="data-job-panel"><DataProductJob key={goalDownloadJob}
      id={goalDownloadJob} owner={userId} handler="data.goal-prepare" completed={() => setTab('saved')} /></Panel>}
    {wrdsDownloadJob&&<Panel title="上次 WRDS 下载进度" className="data-job-panel"><WrdsSavedJob key={wrdsDownloadJob} id={wrdsDownloadJob} owner={userId} canWrite={canWrite} saved={showSaved}/></Panel>}
    {tab === 'new' ? <>
      <IndexBenchmarkDownload key={links.toString()} owner={userId} canWrite={canWrite} links={links} revision={refresh}
        connections={connectionRows} />
      <Panel title="1. 选数据来源"><label htmlFor={marketId}>想添加哪类数据</label><select id={marketId} value={market} onChange={event => selectMarket(event.target.value as Market | '')}><option value="">自己选择来源</option>{Object.entries(marketLabels).map(([id,label]) => <option key={id} value={id}>{label}</option>)}</select><MarketHelp />{market === 'CN_EQUITY' && <Notice>沪深股票使用人民币行情。可保存后用于支持沪深的历史研究；此工作台的美元组合回测不接收沪深行情。</Notice>}{mismatch && <Notice>{mismatch}</Notice>}{connections.error != null && <ErrorBox error={connections.error} />}{connections.loading && <p className="workbench-loading" role="status">正在读取来源连接…</p>}{!connections.loading && !connections.error && Array.isArray(connections.data?.items) && connections.data.items.length === 0 && <p>还没有保存的数据来源。点“新增来源连接”，选择文件导入或行情网站。</p>}<label>已有连接<select value={connection} onChange={(e) => setConnection(e.target.value)}><option value="">请选择</option>{Array.isArray(connections.data?.items) && connections.data.items.map((raw) => { const row = record(raw); return <option key={String(row.connection_id)} value={String(row.connection_id)}>{String(row.name)} · {describeSource(String(row.provider)).name}</option>; })}</select></label>{selectedConnection && <SourceConnectionCheck key={connection + ':' + String(selectedConnection.revision)} owner={userId} canWrite={canWrite} connection={selectedConnection} capability={sourceCapabilities} />}{selectedConnection&&selectedConnection.provider!=='wrds'&&!goalOnlyProvider(selectedConnection.provider)&&<ProviderCapabilities key={String(selectedConnection.connection_id)} provider={String(selectedConnection.provider)}/>} {selectedConnection?.provider==='massive'&&<MassiveConnectionStatus key={String(selectedConnection.connection_id)} connectionId={String(selectedConnection.connection_id)} owner={userId} canWrite={canWrite}/>}<details open={newConnection} onToggle={event => setNewConnection(event.currentTarget.open)}><summary>新增来源连接</summary><p>给连接起个好认的名字。若来源需要登录凭据，请先在下方填写并保存。</p><label>连接名称<input value={connectionName} onChange={(e) => setConnectionName(e.target.value)} /></label><label>来源<select value={provider} onChange={(e) => { setProvider(e.target.value); setCredentialRef(null); }}>{['csv', 'yfinance', 'akshare', 'openbb_cboe', 'massive'].map(id => <option key={id} value={id}>{describeSource(id).name}</option>)}{wrds && <option value="wrds" disabled={!wrdsReady}>{describeSource('wrds').name}</option>}{extraProviders.map((row) => <option key={row.id} value={row.id} disabled={!row.connectionConfigurable}>{row.name}</option>)}</select></label>{provider !== 'csv'&&provider!=='wrds'&&!goalOnlyProvider(provider)&&<ProviderCapabilities key={provider} provider={provider}/>} {!selectedExtra && describeSource(provider).summary && <Notice>{describeSource(provider).summary}</Notice>}{selectedExtra && <GoalProviderSettings providers={[selectedExtra]} showConnectionLinks={false}/>} {provider==='wrds'?<WrdsCredential key={userId} canWrite={canWrite} onReference={reference=>setCredentialRef(reference||null)}/>:credentialProvider && <CredentialEditor key={provider} canWrite={canWrite} provider={provider} onReference={(reference) => setCredentialRef(reference || null)} />}<button disabled={busy || !canWrite || pendingData || wrdsConnectionBlocked || goalOnlyProvider(provider) && (!selectedExtra?.connectionConfigurable || provider === 'tushare' && !credentialRef)} onClick={() => void saveConnection()}>保存来源连接</button></details></Panel>
      {selectedConnection?.provider==='wrds' ?
        <WrdsLibrary key={JSON.stringify([userId,connection])} owner={userId} connectionId={connection} canWrite={canWrite} enabled={wrdsReady} saved={showSaved}/>
        : <>
      <Panel title="2. 选股票和基金">{universe.error != null && <ErrorBox error={universe.error} />}<label>手动添加股票或基金代码<textarea value={manual} placeholder="逗号、空格或换行均可" onChange={(e) => { setManual(e.target.value); setSelection(null); }} /></label><label>不需要的代码<input value={exclude} onChange={(e) => { setExclude(e.target.value); setSelection(null); }} /></label><details><summary>从已有名单选择或使用更多选项</summary>{categories.map((category) => <details key={category} open><summary>{category}</summary>{rows.filter((row) => (Array.isArray(row.path) ? String(row.path[0]) : '其他') === category).map((row) => <div key={String(row.id)}><label><input type="checkbox" disabled={busy || row.available !== true} checked={groups.includes(String(row.id))} onChange={(e) => { setGroups((ids) => e.target.checked ? [...ids, String(row.id)] : ids.filter((id) => id !== row.id)); setSelection(null); }} />{Array.isArray(row.path) ? row.path.map(String).join(' / ') : String(row.id)}{row.available !== true ? '（缺少有日期的名单）' : ''}</label><small>{row.as_of == null ? '名单日期未知' : `名单日期：${row.as_of}`}；来源：{row.source == null ? '未提供' : String(row.source)}</small></div>)}</details>)}<MembershipSelector owner={userId} canWrite={canWrite} connection={selectedConnection} changed={() => { setRefresh((n) => n + 1); setSelection(null); }} /><BoundedReferenceSelection key={connection} owner={userId} canWrite={canWrite} connection={selectedConnection} changed={()=>{setRefresh(n=>n+1);setSelection(null);}}/><DatedRosterImport owner={userId} canWrite={canWrite} changed={()=>{setRefresh(n=>n+1);setSelection(null);}}/><p>DIA、IWM、QQQ 和 SPY 是基金代码。选中后只加入基金本身。</p></details><button disabled={busy} onClick={() => void resolve()}>查看所选代码</button>{selection && <p>最终 {Array.isArray(selection.tickers) ? selection.tickers.length : 0} 个证券代码：{Array.isArray(selection.tickers) ? selection.tickers.join('、') : ''}。回测期间会一直使用这份名单。</p>}</Panel>
      <Panel title="3. 选日期并命名"><label>开始日期<NativeDateInput value={start} onChange={(e) => setStart(e.target.value)} /></label><label>结束日期<NativeDateInput value={end} onChange={(e) => setEnd(e.target.value)} /></label><label>自定义名称（可留空）<input value={name} onChange={(e) => setName(e.target.value)} /></label><p>保存为：{`${name || '日线数据'}_${start || '开始日期'}至${end || '结束日期'}_导入${new Date().toLocaleDateString('en-CA')}`}。</p></Panel>
      <LibraryImport key={connection} owner={userId} canWrite={canWrite} connection={mismatch ? null : selectedConnection} selection={selection} start={start} end={end} name={name} accepted={rememberJob} changed={showSaved} />
    </>}</> : null}
    <div hidden={tab !== 'saved'}>
      <MarketDataManager owner={userId} canWrite={canWrite} revision={refresh} connections={connectionRows}
        refreshed={() => setRefresh(n => n + 1)} prepareVerification={prepareVerification} />
    </div>
    {tab === 'connections' && <MarketConnectionsManager canWrite={canWrite} revision={refresh}
      changed={() => setRefresh(n => n + 1)} />}
    {error != null && <ErrorBox error={error} />}<UxWriteRecovery owner={userId} area="data" busy={busy} canWrite={canWrite} recovered={resource => {
      if (resource.kind === 'connection') setConnection(resource.id);
      setError(null); setRefresh(n => n + 1);
    }} />
    <UxWriteRecovery owner={userId} area="import" busy={busy} canWrite={canWrite} allowSameRequestRetry={false} recovered={(resource,path) => {
      if (resource.kind === 'job') {
        rememberJob(resource.id, path === '/data/library/import');
        setRecoveredImport(resource.id);
      }
    }} />
    {recoveredImport && <Panel title="找回的导入进度"><ImportJob id={recoveredImport} owner={userId} canWrite={canWrite}
      changed={() => { setRecoveredImport(''); showSaved(); }} retry={() => setRecoveredImport('')} /></Panel>}
    <details onToggle={(event) => setAdvanced(event.currentTarget.open)}><summary>更多数据工具</summary>{advanced && <DataCenter canWrite={canWrite} />}</details>
  </div>;
}

function MembershipSelector({owner, canWrite, connection, changed}: {owner:string;canWrite:boolean;connection:RecordData|null;changed:()=>void}) {
  const [date,setDate]=useState(''),[job,setJob]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null);
  async function load() { if (busy || !canWrite || connection?.provider !== 'massive' || !date || job) return; setBusy(true);setError(null); try { const result=await uxMutation(owner,'membership','/data/library/download',{request:{connection_id:connection.connection_id,membership:{etf:'QQQ',effective_date:date}}});setJob(String(result.job_id)); } catch(e){setError(e);}finally{setBusy(false);} }
  return <details><summary>获取某天 QQQ 持有的股票</summary><p>需要 Massive 连接和名单权限。只保存当天的股票清单，不下载价格，也不会随回测日期更新。</p><label>查看哪一天的名单<NativeDateInput value={date} onChange={(e)=>setDate(e.target.value)} /></label><button disabled={busy||!canWrite||connection?.provider!=='massive'||!date||!!job||uxPending(owner,'membership')} onClick={()=>void load()}>获取并保存名单</button>{connection?.provider!=='massive'&&<p>先选 Massive 连接。也可直接选已有名单或输入代码。</p>}{error!=null&&<ErrorBox error={error}/>} {job&&<ImportJob id={job} owner={owner} canWrite={canWrite} changed={()=>{changed();setJob('');}} label="刷新并选择这份名单"/>}<UxWriteRecovery owner={owner} area="membership" busy={busy} canWrite={canWrite} recovered={value=>{if(value.kind==='job'){setJob(value.id);setError(null);}}} /></details>;
}
