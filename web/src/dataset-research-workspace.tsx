import { ModelUsagePanel } from './model-usage-panel';
import { SearchTaskEvidence } from './search-task-evidence';
import { StrategySearchReport, isStrategySearch } from './strategy-search-report';
import { NativeDateInput } from './native-date-input';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, post, request } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, Panel, useResource, JsonView } from './components';
import { record, terminalResearchJob } from './native-research-api';
import { uxMutation, uxPending } from './ux-mutation';
import { UxWriteRecovery, useUxPending } from './ux-write-recovery';
import { PortfolioAnalytics } from './portfolio-analytics';
import { ContributionSchedule, contributionRows } from './contribution-schedule';
import type { Contribution } from './contribution-schedule';
import { teamWrite, teamWritePending } from './team-write';
import { TeamWriteRecovery } from './team-write-recovery';
import { AreaHelp } from './area-help';
import { ComputationCacheProvenance } from './computation-cache-provenance';
import { eligibleCacheKinds } from './computation-cache-api';
import { ResultPreviewLink } from './result-preview';
import { builtInStrategyNames as strategyNames, portfolioReportName } from './backtest-presentation';
import { backtestReturnParams, benchmarkIds, indexDatasetRows, indexEtfs } from './index-benchmarks';
import type { BenchmarkId } from './index-benchmarks';
export function DatasetResearchWorkspace({ userId, canWrite, backtest = false, initialTeamSnapshotId }: { userId: string; canWrite: boolean; backtest?: boolean; initialTeamSnapshotId?: string }) {
  const [route] = useState(() => backtestReturnParams(new URLSearchParams(window.location.hash.split('?')[1] ?? '')));
  const [instance, setInstance] = useState(backtest ? route.get('strategy_instance_id') ?? '' : '');
  const [indexCompare, setIndexCompare] = useState(backtest && route.get('index_benchmarks') === '1');
  const [indexSelections, setIndexSelections] = useState<Partial<Record<BenchmarkId, string>>>(() =>
    Object.fromEntries(benchmarkIds.map(id => [id, route.get(id) ?? ''])));
  const [bypassCache, setBypassCache] = useState(false);
  const [workflow,setWorkflow] = useState('analysis'), [maxTrials,setMaxTrials] = useState('6'), [wallSeconds,setWallSeconds] = useState('90'), [maxCalls,setMaxCalls] = useState('12');
  const [evidenceText, setEvidenceText] = useState(''), [evidenceSource, setEvidenceSource] = useState(''), [published, setPublished] = useState(''), [available, setAvailable] = useState(''), [asOf, setAsOf] = useState('');
  const [mode, setMode] = useState(initialTeamSnapshotId ? 'role_team' : 'tools'), [models, setModels] = useState<RecordData[]>([]);
  const [selectedTeam, setSelectedTeam] = useState<RecordData | null>(null), [fixedTeam, setFixedTeam] = useState<RecordData | null>(null), [teamOffset, setTeamOffset] = useState(0), [snapshotLoading, setSnapshotLoading] = useState(false);
  const gate = useRef(false);
  const [finishedJob, setFinishedJob] = useState('');
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [refresh, setRefresh] = useState(0), [dataset, setDataset] = useState(backtest ? route.get('dataset_id') ?? '' : ''), [strategy, setStrategy] = useState(backtest ? route.get('builtin_strategy') ?? '' : ''), [start, setStart] = useState(backtest ? route.get('start_date') ?? '' : ''), [end, setEnd] = useState(backtest ? route.get('end_date') ?? '' : ''), [capital, setCapital] = useState(backtest ? route.get('initial_cash') ?? '10000.00' : '10000.00'), [fee, setFee] = useState(backtest ? route.get('fee_per_share') ?? '0.005' : '0.005'), [minimum, setMinimum] = useState(backtest ? route.get('minimum_fee') ?? '1.00' : '1.00'), [spread, setSpread] = useState(backtest ? route.get('spread_bps') ?? '0' : '0'), [benchmark, setBenchmark] = useState(''), [benchmarkMode, setBenchmarkMode] = useState('buy_hold'), [contributions, setContributions] = useState<Contribution[]>([]), [prompt, setPrompt] = useState(''), [kind, setKind] = useState(''), [factor, setFactor] = useState(''), [plan, setPlan] = useState<RecordData | null>(null), [job, setJob] = useState(backtest ? route.get('portfolio_job') ?? '' : ''), [run, setRun] = useState(() => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('portfolio_run') ?? ''), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null);
  const [datasetOffset, setDatasetOffset] = useState(0);
  const [selectedSnapshot, setSelectedSnapshot] = useState<RecordData | null>(null);
  const [historyOffset, setHistoryOffset] = useState(0);
  const datasets = useResource<RecordData>(`/data/library/datasets?trash=false&kind=dataset&limit=100&offset=${datasetOffset}`, refresh), benchmarks = useResource<RecordData>('/data/library/benchmarks', refresh), catalog = useResource<RecordData>('/research/portfolio/catalog', refresh);
  const sdkRows = Array.isArray(catalog.data?.sdk_instances) ? catalog.data.sdk_instances.map(record)
    .filter(row => typeof row.instance_id === 'string' && typeof row.label === 'string') : [];
  const sdkCapabilities = record(catalog.data?.sdk_capabilities);
  const maxSdkSecurities = typeof sdkCapabilities.max_securities === 'number' && sdkCapabilities.max_securities > 0
    ? sdkCapabilities.max_securities : 1;
  const maxSdkSessions = typeof sdkCapabilities.max_sessions === 'number' && sdkCapabilities.max_sessions > 0
    ? sdkCapabilities.max_sessions : 256;
  const builtIns = Array.isArray(catalog.data?.strategies) ? catalog.data.strategies.map(String) : [];
  const strategyReady = instance ? sdkRows.some(row => row.instance_id === instance) : builtIns.includes(strategy);
  const indexRows = benchmarkIds.map(id => ({
    id, rows: indexDatasetRows(benchmarks.data?.items, id), selected: indexSelections[id] ?? '',
  }));
  const indexSupported = benchmarkIds.every(id => Array.isArray(catalog.data?.benchmark_options) &&
    catalog.data.benchmark_options.map(record).some(row => row.id === id && row.ticker === indexEtfs[id].ticker));
  const indexReady = indexSupported && !benchmarks.loading &&
    indexRows.every(({ rows, selected }) => rows.some(row => row.dataset_id === selected));
  function benchmarkDownloadLink(id: BenchmarkId) {
    const params = new URLSearchParams({ benchmark_preset: id, index_benchmarks: '1' });
    if (dataset) params.set('dataset_id', dataset);
    if (instance) params.set('strategy_instance_id', instance);
    else if (strategy) params.set('builtin_strategy', strategy);
    if (start) params.set('start_date', start);
    if (end) params.set('end_date', end);
    params.set('initial_cash', capital);
    params.set('fee_per_share', fee);
    params.set('minimum_fee', minimum);
    params.set('spread_bps', spread);
    for (const key of benchmarkIds) if (indexSelections[key]) params.set(key, indexSelections[key]);
    const filtered = backtestReturnParams(params);
    filtered.set('benchmark_preset', id);
    return '#data?' + filtered.toString();
  }
  const profiles = useResource<RecordData>('/model-profiles?limit=100&offset=0', refresh);
  const teams = useResource<RecordData>(`/agent-teams/presets?limit=50&offset=${teamOffset}`, refresh);
  const teamRows = Array.isArray(teams.data?.items) ? teams.data.items.map(record) : [];
  useEffect(() => {
    if (!initialTeamSnapshotId) return;
    const controller = new AbortController(); let active = true;
    setMode('role_team'); setSelectedTeam(null); setFixedTeam(null); setPlan(null); setSnapshotLoading(true);
    void request<RecordData>(`/agent-teams/snapshots/${encodeURIComponent(initialTeamSnapshotId)}`, { signal: controller.signal }).then((snapshot) => {
      if (!active) return;
      if (snapshot.snapshot_id !== initialTeamSnapshotId || snapshot.owner_id !== userId || !/^[a-f0-9]{64}$/.test(String(snapshot.sha256))) throw new Error('无法确认返回的是选定的团队版本，请回到团队页重新选。');
      setFixedTeam(snapshot); setError(null);
    }).catch((error) => { if (active) setError(error); }).finally(() => { if (active) setSnapshotLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [initialTeamSnapshotId, userId]);
  const pendingResearch = useUxPending(userId, 'research');
  const activeJob = Boolean(job && finishedJob !== job);
  const onJobFinished = useCallback((id: string, status: string) => {
    setFinishedJob(id);
    if (status === 'SUCCEEDED') setHistoryRefresh(value => value + 1);
  }, []);
  const history = useResource<RecordData>(`/research/portfolio/results?limit=50&offset=${historyOffset}`, refresh + historyRefresh);
  const historyCount = Array.isArray(history.data?.items) ? history.data.items.length : 0;
  const savedReports = Array.isArray(history.data?.items)
    ? history.data.items.map(record).filter(row => backtest === (row.family === 'dataset-portfolio-result-1'))
    : null;
  const cache = useResource<unknown>('/computation-cache');
  const cacheTypes = eligibleCacheKinds(cache.data);
  const supportsStrategyCache = Object.hasOwn(strategyNames, strategy) && cacheTypes.includes('exact_backtest');
  const canBypassCache = backtest ? !instance && !indexCompare && !benchmark && supportsStrategyCache
    : mode === 'tools' && workflow === 'analysis' && (
      (kind === 'strategy_validation' && supportsStrategyCache) ||
      (kind === 'factor_validation' && ['momentum_20', 'return_1', 'sma20_distance'].includes(factor) && cacheTypes.includes('trusted_indicator'))
    );
  const cachePolicy: RecordData = canBypassCache && bypassCache ? { cache_policy: 'bypass' } : {};
  const datasetRows = Array.isArray(datasets.data?.items) ? datasets.data.items.map(record) : [];
  const listedDataset = datasetRows.find(row => row.dataset_id === dataset);
  const hasVersionLink = backtest && route.get('dataset_id') === dataset && (route.has('dataset_version') || route.has('dataset_hash'));
  const selectedDataset = useResource<RecordData>(dataset && (hasVersionLink || !listedDataset) ? '/data/library/datasets/' + encodeURIComponent(dataset) : null, refresh);
  const fixedDataset = selectedDataset.data?.dataset_id === dataset && selectedDataset.data.trashed !== true &&
    selectedDataset.data.kind !== 'benchmark' ? selectedDataset.data : null;
  const matchesVersionLink = fixedDataset && String(fixedDataset.version) === route.get('dataset_version') &&
    fixedDataset.content_hash === route.get('dataset_hash');
  const chosenDataset = hasVersionLink ? matchesVersionLink ? fixedDataset : null : listedDataset ?? fixedDataset;
  const pinnedOption = fixedDataset ?? (selectedSnapshot?.dataset_id === dataset ? selectedSnapshot : null);
  const options = pinnedOption && !listedDataset ? [pinnedOption, ...datasetRows] : datasetRows;
  const wrdsData = chosenDataset?.scope==='licensed_local' || record(chosenDataset?.source).provider==='wrds';
  const wrdsBlocked = wrdsData && (backtest ? Boolean(!indexCompare && !instance && benchmark) :
    mode!=='tools' && (mode!=='single_assistant' || workflow==='strategy_search' || !!evidenceText.trim()));
  function changed() { setPlan(null); }
  function rememberBacktest(id: string) {
    if (!backtest) return;
    const [currentPage, search] = location.hash.split('?');
    const next = new URLSearchParams(search);
    next.set('portfolio_job', id);
    next.delete('portfolio_run');
    window.history.replaceState(null, '', currentPage + '?' + next);
  }
  async function act(planning: boolean) {
    if (gate.current || busy || activeJob || snapshotLoading || wrdsBlocked || !canWrite || !dataset || !catalog.data ||
        uxPending(userId, 'research') || (backtest && backtestBlocked)) return;
    gate.current = true; setBusy(true); setError(null);
    try {
      let result: RecordData;
      if (!backtest && planning) {
        let snapshot = fixedTeam;
        if (mode === 'role_team' && !snapshot) {
          if (!selectedTeam || teamWritePending(userId)) throw new Error('请选择已保存的团队，并确认上次是否已固定所选版本。');
          snapshot = await teamWrite(userId, '/agent-teams/snapshots', { preset_id: selectedTeam.preset_id, revision: selectedTeam.revision, idempotency_key: crypto.randomUUID() });
          setFixedTeam(snapshot);
        }
        const inferredSearch = workflow === 'strategy_search';
        if (inferredSearch && mode !== 'tools') {
          const supported = record(catalog.data?.strategy_search).providers;
          if (!Array.isArray(supported) || !supported.length) throw new Error('服务尚未声明策略试验支持的模型厂商，请刷新能力目录后再查看计划。');
          let chosenProviders: string[];
          if (mode === 'role_team') chosenProviders = Object.values(record(snapshot?.resolved_roles)).map(role => String(record(record(role).model).provider ?? ''));
          else {
            const revisions = await Promise.all(models.map(ref => request<RecordData>(`/model-profiles/${encodeURIComponent(String(ref.profile_id))}/revisions/${encodeURIComponent(String(ref.revision))}`)));
            if (revisions.some((row,index) => row.profile_id !== models[index].profile_id || row.revision !== models[index].revision)) throw new Error('返回的模型版本与所选不一致，请重新选。');
            chosenProviders = revisions.map(row => String(record(record(row.configuration).model).provider ?? ''));
          }
          if (!chosenProviders.length || chosenProviders.some(provider => !provider || !supported.includes(provider))) throw new Error('所选模型或团队含当前策略试验不支持的厂商，请选择能力目录支持的保存模型；原有资料研究入口保留。');
        }
        const search = workflow === 'strategy_search';
        if (search && (!Number.isInteger(Number(maxTrials)) || Number(maxTrials)<1 || Number(maxTrials)>12 || !Number.isInteger(Number(wallSeconds)) || Number(wallSeconds)<1 || Number(wallSeconds)>120 || !Number.isInteger(Number(maxCalls)) || Number(maxCalls)<3 || Number(maxCalls)>32)) throw new Error('请核对试验预算：方案1至12个、运行1至120秒、模型调用3至32次。');
        const searchPayload = search ? { workflow:'strategy_search', search_limits:{max_trials:Number(maxTrials),wall_seconds:Number(wallSeconds),max_model_calls:Number(maxCalls)}, parameters:{initial_cash:capital,fee_per_share:fee,minimum_fee:minimum,spread_bps:spread}, ...(mode==='tools'?{candidates:[{template:'buy_hold'},{template:'sma',slow:20},{template:'sma_cross',fast:5,slow:20}].slice(0,Number(maxTrials))}:{}) } : { workflow:'analysis' };
        result = await post<RecordData>('/research/tasks/plan', { ...searchPayload, ...cachePolicy, prompt, dataset_id: dataset, execution_mode: mode, ...(mode === 'tools' ? (search ? {} : { ...(kind ? { kind } : {}), ...(strategy ? { strategy } : {}), ...(factor ? { factor } : {}) }) : mode === 'role_team' ? { team_snapshot_id: snapshot?.snapshot_id } : { model_profiles: models }), ...(mode !== 'tools' && evidenceText.trim() ? { evidence: [{ id: 'user-evidence-1', source: evidenceSource, text: evidenceText, published_at: published ? new Date(published).toISOString() : null, available_at: available ? new Date(available).toISOString() : null }] } : {}), ...(mode !== 'tools' && asOf ? { as_of: new Date(asOf).toISOString() } : {}) }); setPlan(result); return;
      }
      if (backtest) {
        const legacyBenchmark = !indexCompare && !instance && benchmark;
        const flows = legacyBenchmark && ['dca','dca_reserved'].includes(benchmarkMode) ? contributionRows(contributions) : [];
        result = await uxMutation(userId, 'research', '/research/portfolio/backtests', {
          ...cachePolicy, dataset_id: dataset, ...(instance ? { instance_id: instance } : { strategy }),
          initial_cash: capital, fee_per_share: fee, minimum_fee: minimum, spread_bps: spread,
          ...(start ? { start_date: start } : {}), ...(end ? { end_date: end } : {}),
          ...(indexCompare
            ? { benchmarks: indexRows.map(({ id, selected }) => ({ id, dataset_id: selected })) }
            : legacyBenchmark ? { benchmark_id: legacyBenchmark, benchmark_mode: benchmarkMode } : {}),
          ...(flows.length ? { contributions: flows } : {}),
        });
      } else {
        if (plan?.status !== 'READY' || !plan.plan_reference) throw new Error('请先核对服务端可执行计划。');
        result = await uxMutation(userId, 'research', '/research/tasks/start', { plan_reference: plan.plan_reference });
      }
      if (result.owner_id !== userId || result.handler !== 'research.dataset_task' || typeof result.job_id !== 'string') throw new Error('返回的任务账户或任务类型与所选不一致。请检查任务记录。');
      rememberBacktest(result.job_id);
      setJob(result.job_id); setRun(''); setBypassCache(false); setRefresh((n) => n + 1);
    } catch (e) { setError(e); } finally { gate.current = false; setBusy(false); }
  }

  const adjustment = String(record(chosenDataset?.metadata).adjustment ?? '');
  const backtestMarketBlocked = backtest && record(chosenDataset?.metadata).currency === 'CNY';
  const scopeBlocked = Boolean((instance || indexCompare) && chosenDataset &&
    !['public', 'synthetic'].includes(String(chosenDataset.scope)));
  const backtestBlocked = !chosenDataset || !strategyReady || (indexCompare && !indexReady) || scopeBlocked || backtestMarketBlocked;
  const primaryActions = <div className="form-actions">{(backtest || plan?.status !== 'READY') && <button className={backtest || plan?.status !== 'READY' ? "primary-action" : "secondary"} disabled={busy || snapshotLoading || wrdsBlocked || !canWrite || !dataset || !catalog.data || activeJob || pendingResearch || (backtest ? backtestBlocked : !prompt.trim() || mode === 'role_team' && (!fixedTeam && !selectedTeam || !fixedTeam && teamWritePending(userId)) || !['tools','role_team'].includes(mode) && (models.length < 1 || models.length > (mode === 'single_assistant' ? 1 : 12)))} onClick={() => void act(!backtest)}>{backtest ? '开始回测' : '理解任务并查看计划'}</button>}{!backtest && plan?.status === 'READY' && <button className="primary-action" disabled={busy || wrdsBlocked || !canWrite || plan?.status !== 'READY' || activeJob || pendingResearch} onClick={() => void act(false)}>按计划开始研究</button>}<button className="text-button" onClick={() => setRefresh((n) => n + 1)}>刷新目录与记录</button></div>;
  return <div className={backtest ? "v434-backtest-layout" : "v434-task-layout"}>
    <Panel title={backtest ? '开始回测' : '用一句话开始研究'} helpTopic={backtest ? 'backtest' : 'ai'}>
      <label>已保存数据集<select value={dataset} onChange={(e) => {
        const id = e.target.value;
        setSelectedSnapshot(options.find(row => row.dataset_id === id) ?? null);
        setDataset(id); changed();
      }}><option value="">请选择已保存的数据</option>{options.map((row) => <option value={String(row.dataset_id)}
        key={String(row.dataset_id)}>{String(row.name)}{record(row.source).provider==='wrds'?' · WRDS 本机授权':''}</option>)}</select></label>
      <div className="pagination">
        <button type="button" disabled={!datasetOffset || datasets.loading} onClick={() => setDatasetOffset(n => Math.max(0, n - 100))}>上一页数据</button>
        <button type="button" disabled={datasets.loading || !datasets.data || datasetOffset + 100 >= Number(datasets.data.total ?? datasetRows.length)} onClick={() => setDatasetOffset(n => n + 100)}>下一页数据</button>
      </div>
      {dataset && !chosenDataset && !selectedDataset.loading && <p>这份数据暂不能用于此任务，请另选一份。</p>}
      {backtestMarketBlocked && <Notice>所选数据以人民币计价。该回测以美元计价，请另选适用行情。</Notice>}
      {backtest && canWrite && !datasets.loading && datasets.data && datasetRows.length === 0 && !chosenDataset &&
        <p><a className="text-button" href="#module-a?first_example=1">先跑一个例子</a></p>}
      <a href="#data">新增或管理数据 →</a>{wrdsData&&<Notice>{backtest
        ? 'WRDS 数据只能在这台电脑上用内置策略回测。请选内置策略并取消基准比较。'
        : 'WRDS 行情和计算结果留在本机。资料研究的单模型助手只接收任务说明和字段说明；请勿把授权数据写进任务说明，并清空附加资料。'}</Notice>}{['qfq','split_adjusted'].includes(adjustment) && <Notice>回测使用调整后的价格、股数和费用单位，不能当作实盘股数或费率。未另计分红，也未估算能成交的规模。</Notice>}
      {!backtest && <><div className="task-choice-row"><label>研究方式<select value={mode} onChange={(e) => { setMode(e.target.value); setModels([]); changed(); }}><option value="tools">数据工具（不调用模型）</option><option value="single_assistant">单模型助手</option><option value="model_panel">多个模型分别报告</option><option value="role_team">固定十二角色团队</option></select></label><label>任务目标<select value={workflow} onChange={(e)=>{setWorkflow(e.target.value);changed();}}><option value="analysis">数据与资料研究</option><option value="strategy_search">限额内试验策略</option></select></label></div>{workflow==='strategy_search' && <details><summary>试验限额：最多 {maxTrials} 个方案、{wallSeconds} 秒</summary><AreaHelp topic="strategy-search" title="怎样比较策略" /><p className="search-capability-note">策略试验支持厂商：{Array.isArray(record(catalog.data?.strategy_search).providers) ? (record(catalog.data?.strategy_search).providers as unknown[]).map(String).join('、') || '服务未声明' : '正在等待服务声明'}。其他模型可继续用于资料研究。</p><p>每只股票至少需要 80 个用来选策略的交易日、40 个留作检查的交易日。历史试验不能保证未来收益。</p><label>最多尝试几个方案<input type="number" min={1} max={12} value={maxTrials} onChange={e=>{setMaxTrials(e.target.value);changed();}} /></label><label>最长运行时间（秒）<input type="number" min={1} max={120} value={wallSeconds} onChange={e=>{setWallSeconds(e.target.value);changed();}} /></label><label>起始总资金<input inputMode="decimal" value={capital} onChange={e=>{setCapital(e.target.value);changed();}} /></label><label>每股手续费<input value={fee} onChange={e=>{setFee(e.target.value);changed();}} /></label><label>每笔最低手续费<input value={minimum} onChange={e=>{setMinimum(e.target.value);changed();}} /></label><label>模拟买卖价格差（基点）<input value={spread} onChange={e=>{setSpread(e.target.value);changed();}} /></label><details><summary>高级模型调用预算</summary><label>最多模型调用次数<input type="number" min={3} max={32} value={maxCalls} onChange={e=>{setMaxCalls(e.target.value);changed();}} /></label></details></details>}{mode === 'tools' ? <Notice>数据工具可以查看数据概况、检查因子或回测策略。如果描述不够明确，页面会提示你补充。</Notice> : <><Notice>调用在线模型可能产生厂商费用。离线练习不会调用在线模型；缺资料时会提示补充。</Notice>{mode === 'role_team' ? <><label>选择已保存团队<select value={selectedTeam ? JSON.stringify([selectedTeam.preset_id, selectedTeam.revision]) : fixedTeam ? '__frozen' : ''} disabled={busy || snapshotLoading} onChange={(e) => { const row = teamRows.find((item) => JSON.stringify([item.preset_id, item.revision]) === e.target.value); setSelectedTeam(row ? { preset_id: row.preset_id, revision: row.revision, name: row.name } : null); setFixedTeam(null); changed(); }}><option value="">请选择团队</option>{fixedTeam && !selectedTeam && <option value="__frozen">已从团队页带入 · 固定版本 {String(fixedTeam.preset_revision)}</option>}{selectedTeam && !teamRows.some((row) => row.preset_id === selectedTeam.preset_id && row.revision === selectedTeam.revision) && <option value={JSON.stringify([selectedTeam.preset_id, selectedTeam.revision])}>{String(selectedTeam.name)} · 版本 {String(selectedTeam.revision)}</option>}{teamRows.map((row) => <option key={String(row.preset_id)} value={JSON.stringify([row.preset_id, row.revision])}>{String(row.name)} · 版本 {String(row.revision)}</option>)}</select></label><p>查看计划会记住所选团队版本。点“按计划开始研究”后才会运行。</p>{fixedTeam && <p role="status">已选团队版本 {String(fixedTeam.preset_revision)}，后续编辑团队不会改变此版本。</p>}{snapshotLoading && <p>正在读取从团队页选好的版本…</p>}{teams.error != null && <ErrorBox error={teams.error} />}<div className="pagination"><button disabled={busy || !teamOffset} onClick={() => setTeamOffset((n) => Math.max(0, n - 50))}>上一页团队</button><button disabled={busy || teams.loading || !teams.data || teamOffset + 50 >= Number(teams.data.total)} onClick={() => setTeamOffset((n) => n + 50)}>下一页团队</button></div><a href="#agent-teams">新增或编辑团队 →</a>{teamWritePending(userId) && <TeamWriteRecovery key={userId} owner={userId} disabled={!canWrite||busy} recovered={receipt=>{if(typeof receipt.snapshot_id==='string'&&(!selectedTeam||receipt.preset_id===selectedTeam.preset_id&&receipt.preset_revision===selectedTeam.revision))setFixedTeam(receipt);setError(null);setRefresh(n=>n+1);}}/>}</> : <fieldset><legend>{mode === 'single_assistant' ? '选择一个模型配置' : '选择分别报告的模型配置（最多十二个）'}</legend>{profiles.error != null && <ErrorBox error={profiles.error} />}{Array.isArray(profiles.data?.items) && profiles.data.items.map(record).map((row) => <label key={String(row.profile_id)}><input type={mode === 'single_assistant' ? 'radio' : 'checkbox'} name="research-model" checked={models.some((ref) => ref.profile_id === row.profile_id && ref.revision === row.revision)} onChange={(e) => { const ref = { profile_id: row.profile_id, revision: row.revision }; setModels(mode === 'single_assistant' ? [ref] : e.target.checked ? [...models, ref] : models.filter((item) => item.profile_id !== row.profile_id)); changed(); }} />{String(row.name)} · 版本 {String(row.revision)}</label>)}{!Array.isArray(profiles.data?.items) || !profiles.data.items.length ? <p>先在“模型配置”新增并保存模型。</p> : null}</fieldset>}</>}<label>任务说明<textarea value={prompt} onChange={(e) => { setPrompt(e.target.value); changed(); }} placeholder="例如：用买入后一直持有的方法，看看这批股票过去的表现。" /></label>{mode !== 'tools' && <details><summary>添加研究资料（可选）</summary><p>资料来源由你填写，未另行核实。有研究截止时间时，之后才拿到或取得时间未知的资料不会纳入。</p><label>资料来源<input value={evidenceSource} onChange={(e) => { setEvidenceSource(e.target.value); changed(); }} /></label><label>资料正文<textarea rows={6} value={evidenceText} maxLength={16000} onChange={(e) => { setEvidenceText(e.target.value); changed(); }} /></label>{[['发布时间',published,setPublished],['资料最早能获取的时间',available,setAvailable],['研究截止时刻',asOf,setAsOf]].map(([label,value,update]) => <label key={String(label)}>{String(label)}<input type="datetime-local" value={String(value)} onChange={(e) => { (update as (value:string)=>void)(e.target.value); changed(); }} /></label>)}</details>}{mode === 'tools' && <details><summary>手动选择任务类型或指标（可选）</summary><label>任务类型<select value={kind} onChange={(e) => { setKind(e.target.value); changed(); }}><option value="">按任务说明判断</option><option value="dataset_summary">数据概况研究</option><option value="factor_validation">因子验证</option><option value="strategy_validation">策略验证</option></select></label><label>因子<select value={factor} onChange={(e) => { setFactor(e.target.value); changed(); }}><option value="">按任务说明判断</option><option value="momentum_20">20期动量</option><option value="return_1">单期收益</option><option value="sma20_distance">20期均线距离</option></select></label></details>}</>}
      {(backtest || mode === 'tools') && <label>策略<select value={instance ? 'sdk:' + instance : strategy} onChange={event => {
        const selected = event.target.value;
        setInstance(selected.startsWith('sdk:') ? selected.slice(4) : '');
        setStrategy(selected.startsWith('sdk:') ? '' : selected);
        changed();
      }}>
        <option value="">{backtest ? '请选择策略' : '可留空，按任务说明判断'}</option>
        {builtIns.map(id => <option key={id} value={id}>{strategyNames[id] ?? id}</option>)}
        {backtest && sdkRows.length > 0 && <optgroup label="自建策略">
          {sdkRows.map(row => <option key={String(row.instance_id)} value={'sdk:' + String(row.instance_id)}>
            {String(row.label)}
          </option>)}
        </optgroup>}
        {instance && !strategyReady && <option value={'sdk:' + instance} disabled>
          {catalog.loading ? '正在核对自建策略…' : '该自建策略暂不可用于日线回测'}
        </option>}
      </select></label>}
      {backtest && <a href="#strategies">管理自建策略 →</a>}
      {backtest && instance && strategyReady && <p>
        用自建策略测每日行情，每次最多 {maxSdkSecurities} 只证券、{maxSdkSessions} 个交易日。可发出买入或清仓信号，也可指定整股持仓。已有持仓时只支持保持原股数或全部卖出，不能中途改成另一持有数量，也不支持按权重调仓。
      </p>}
      {backtest && scopeBlocked && <Notice>自建策略和指数基金对比需要公开数据或合成练习数据。请另选数据。</Notice>}
      {backtest && (indexSupported || indexCompare) && <fieldset>
        <legend>指数基金对比</legend><AreaHelp topic="index-etf-benchmarks" title="指数基金怎样比较" />
        <label><input type="checkbox" checked={indexCompare} onChange={event => setIndexCompare(event.target.checked)} />
          加入三个指数基金（ETF）
        </label>
        {indexCompare && <>
          <p>每只基金选一份已保存的数据。按基金价格模拟买入持有，现金分红未另计。</p>
          {!indexSupported && !catalog.loading && <Notice>当前服务还未提供这三个 ETF 的回测能力。</Notice>}
          {benchmarks.error != null && <ErrorBox error={benchmarks.error} />}
          {indexRows.map(({ id, rows, selected }) => <div key={id}>
            <label>{indexEtfs[id].label}<select value={selected} onChange={event => {
              const value = event.target.value;
              setIndexSelections(previous => ({ ...previous, [id]: value }));
            }}>
              <option value="">{benchmarks.loading ? '正在读取已保存的数据…' : '请选择已保存版本'}</option>
              {rows.map(row => {
                const quality = record(row.quality);
                return <option key={String(row.dataset_id)} value={String(row.dataset_id)}>
                  {String(row.name)} · 版本 {String(row.version)} · {String(quality.first ?? '开始日期未知')} 至 {String(quality.last ?? '结束日期未知')}
                </option>;
              })}
            </select></label>
            <a href={benchmarkDownloadLink(id)}>到数据管理下载 {indexEtfs[id].ticker} →</a>
          </div>)}
        </>}
      </fieldset>}
      {backtest && <><p>日期留空会用全部可用记录。有些策略要先积累足够行情，交易可能晚于首日。</p><div className="v434-date-range"><label>开始日期（可选）<NativeDateInput value={start} onChange={(e) => { setStart(e.target.value); changed(); }} /></label><label>结束日期（可选）<NativeDateInput value={end} onChange={(e) => { setEnd(e.target.value); changed(); }} /></label></div><label>起始总资金<input inputMode="decimal" value={capital} onChange={(e) => { setCapital(e.target.value); changed(); }} /></label><Notice>这笔总资金平均分给所选股票。未用金额保留为现金，各股票之间不会再调拨资金。</Notice><details><summary>交易成本和基准比较</summary><AreaHelp topic="costs" title="交易成本" /><AreaHelp topic="benchmark" title="基准比较" /><label>每股手续费<input value={fee} onChange={(e) => { setFee(e.target.value); changed(); }} /></label><label>每笔最低手续费<input value={minimum} onChange={(e) => { setMinimum(e.target.value); changed(); }} /></label><label>模拟买卖价格差（基点）<input value={spread} onChange={(e) => { setSpread(e.target.value); changed(); }} /></label>{!indexCompare && !instance && <><label>已保存基准<select value={benchmark} onChange={(e) => { setBenchmark(e.target.value); changed(); }}><option value="">不比较</option>{Array.isArray(benchmarks.data?.items) && benchmarks.data.items.map((raw) => { const row = record(raw); return <option key={String(row.dataset_id)} value={String(row.dataset_id)}>{String(row.name)}</option>; })}</select></label><label>比较方式<select value={benchmarkMode} onChange={(e) => { setBenchmarkMode(e.target.value); changed(); }}><option value="buy_hold">同起始资金买入持有</option><option value="dca_reserved">用原本金分批买</option><option value="dca">再加钱分批买（另列参考）</option></select></label>{benchmark && ['dca','dca_reserved'].includes(benchmarkMode) && <ContributionSchedule reserved={benchmarkMode === 'dca_reserved'} value={contributions} disabled={busy || !canWrite} changed={(rows) => { setContributions(rows); changed(); }} />}<p>{benchmarkMode === 'dca_reserved' ? '总预算不能超过起始资金，每笔预算包含交易费用。' : benchmarkMode === 'dca' ? '追加资金只进基准账户。两个账户投入不同，不能直接比较账户总额。' : '用相同的起始资金买入后持有；日期和收益算法一致时，曲线可同图比较。'}</p></>}</details></>}
      {canBypassCache && <label>
        <input type="checkbox" checked={bypassCache} disabled={!canWrite || busy || activeJob || pendingResearch} onChange={event => { setBypassCache(event.target.checked); changed(); }} />
        本次强制重算
      </label>}
      {primaryActions}
      {plan && <div><h3>{plan.status === 'READY' ? (job ? '已提交任务' : '计划已准备好，尚未开始') : '需要补充信息'}</h3><p>{String(plan.reason ?? (plan.execution_mode === 'model_panel' ? '各选定模型分别报告。' : plan.execution_mode === 'single_assistant' ? '将由一个模型完成。' : plan.execution_mode === 'role_team' ? '已选固定十二角色团队。' : plan.kind === 'strategy_search' ? '在所设额度内比较方案，再用较晚的数据检查选定的方案。' : `任务类型：${plan.kind}`))}</p><details><summary>计划详细信息</summary>{Array.isArray(plan.tools) && <p>工具名称：{plan.tools.join('、')}</p>}<JsonView value={plan} /></details></div>}
      {(error || catalog.error || datasets.error) != null && <ErrorBox error={error || catalog.error || datasets.error} />}<UxWriteRecovery owner={userId} area="research" busy={busy} canWrite={canWrite} allowSameRequestRetry={!backtest} recovered={value => {
        if (value.kind === 'job') { rememberBacktest(value.id); setJob(value.id); setRun(''); setError(null); setRefresh(n => n + 1); }
      }} />
    </Panel>
    <div className="v434-results-column">{!job && !run && <section className="panel v434-result-empty"><div className="panel-header"><h2>研究结果</h2><span className="panel-index">02 / EVIDENCE</span></div><div className="chart-empty"><h3>还没有本次结果</h3><p>{backtest ? '选好数据和策略后，点击“开始回测”。' : '选好数据并写下问题，先查看计划，再开始研究。'}</p></div></section>}
    {job && <DatasetTaskStatus key={job} id={job} showModelUsage={mode!=='tools'} owner={userId} canWrite={canWrite}
      completed={onJobFinished} done={setRun} />}
    <Panel title={backtest ? '已保存的回测' : '已保存的研究'}>
      {history.error ? <ErrorBox error={history.error} />
        : savedReports === null ? <p role="status">正在读取已保存的报告…</p>
        : savedReports.length === 0 ? <p className="empty">本页没有此类报告。</p>
        : <ul>{savedReports.map(row => (
          <li key={String(row.run_id)}>
            <ResultPreviewLink className="text-button" resultId={`tasks:${String(row.run_id)}`}
              title={backtest ? portfolioReportName(row) ?? '已保存的回测' : '已保存的研究'}>
              {backtest ? portfolioReportName(row) ?? '预览回测报告' : '预览研究报告'} · {String(row.created_at)}
            </ResultPreviewLink>
          </li>
        ))}</ul>}
      <div className="pagination">
        <button type="button" disabled={!historyOffset || history.loading} onClick={() => setHistoryOffset(n => Math.max(0, n - 50))}>上一页报告</button>
        <button type="button" disabled={history.loading || historyCount < 50} onClick={() => setHistoryOffset(n => n + 50)}>下一页报告</button>
        <a href={`#my-results?kind=${backtest ? 'backtest' : 'research'}`}>到结果页搜索 →</a>
      </div>
    </Panel>
    {run && <SavedDatasetReport run={run} />}</div>
  </div>;
}
function DatasetTaskStatus({ id, owner, canWrite, completed, done, showModelUsage }: {
  id: string; owner: string; canWrite: boolean; showModelUsage: boolean;
  completed: (id: string, status: string) => void; done: (run: string) => void;
}) {
  const [refresh, setRefresh] = useState(0), [error, setError] = useState<unknown>(null), [busy, setBusy] = useState(false);
  const stop = useCallback((value: RecordData) => value.job_id === id && value.owner_id === owner &&
    value.handler === 'research.dataset_task' && terminalResearchJob(value), [id, owner]);
  const state = useResource<RecordData>(`/research/tasks/${encodeURIComponent(id)}`, refresh, 1500, stop), job = state.data;
  const valid = job?.job_id === id && job.owner_id === owner && job.handler === 'research.dataset_task';
  const notified = useRef(false);
  useEffect(() => {
    if (job && stop(job) && !notified.current) {
      notified.current = true;
      completed(id, String(job.status));
    }
  }, [job, stop, completed, id]);
  const failureCode = typeof job?.error_code === 'string' && /^[A-Z][A-Z0-9_]{0,95}$/.test(job.error_code) ? job.error_code : 'RESEARCH_TASK_FAILED';
  async function cancel() { if (busy || !valid || !canWrite) return; setBusy(true); try { await post(`/research/tasks/${encodeURIComponent(id)}/cancel`); setRefresh((n) => n + 1); } catch (e) { setError(e); } finally { setBusy(false); } }
  return <Panel title="当前研究任务">{state.error ? <ErrorBox error={state.error} /> : valid ? <><p>状态：{({ QUEUED: '排队中', RUNNING: '执行中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消' } as Record<string, string>)[String(job.status)] ?? '待确认'}；进度：{String(job.progress ?? '未提供')}。</p>{job.status === 'SUCCEEDED' && <ComputationCacheProvenance value={record(job.result).cache_disposition} />}{showModelUsage&&<ModelUsagePanel kind="job" id={id} active={!terminalResearchJob(job)}/>}{job.status === 'FAILED' && <ErrorBox error={new ApiError(0, failureCode, failureCode === 'BENCHMARK_RESERVED_BUDGET_EXCEEDS_INITIAL_CAPITAL' ? '分批买入预算合计超过起始总资金，请减少预算或提高起始资金后重新开始。' : '任务未能完成，请核对数据和参数后重新开始；如仍失败，可展开技术信息查看错误代码。')} />}<button disabled={busy || !canWrite || terminalResearchJob(job) || job.cancel_requested === true} onClick={() => void cancel()}>请求停止</button>{job.cancel_requested === true && <p>停止请求已提交，任务可能仍在运行。</p>}{job.status === 'SUCCEEDED' && <><ResultPreviewLink resultId={`tasks:${String(job.run_id)}`} title="本次任务的结果">查看结果</ResultPreviewLink><button className="text-button" onClick={() => done(String(job.run_id))}>在本页查看完整结果</button></>}<SearchTaskEvidence id={id} terminal={terminalResearchJob(job)} /><p>这个任务还不能从中途继续。重新开始会创建一个新任务。</p></> : <p>正在读取本次任务…</p>}{error != null && <ErrorBox error={error} />}</Panel>;
}
export function SavedDatasetReport({ run }: { run: string }) {
  const state = useResource<RecordData>(`/research/portfolio/results/${encodeURIComponent(run)}`);
  return <Panel title="保存的研究结果">{state.error ? <ErrorBox error={state.error} /> : state.data?.run_id === run ? isStrategySearch(state.data) ? <StrategySearchReport value={state.data} /> : <PortfolioAnalytics value={state.data} /> : <p>正在读取本次保存的结果…</p>}</Panel>;
}
