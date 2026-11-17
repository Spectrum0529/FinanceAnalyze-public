import { AutonomousBacktestReport } from './autonomous-backtest-report';
import { AutonomousSessionDetail, AutonomousArtifactReader } from './autonomous-research-workspace';
import { StrategySearchReport, isStrategySearch } from './strategy-search-report';
import { useEffect, useRef, useState } from 'react';
import { ResultCatalog } from './result-catalog';
import { ErrorBox, Notice, Panel, Resource, JsonView } from './components';
import { NativeResearchReport } from './native-research-report';
import { record } from './native-research-api';
import { HistoryReport } from './history-report-page';
import { HistoricalReport } from './historical-report';
import { PortfolioAnalytics } from './portfolio-analytics';
import { SavedDatasetReport } from './dataset-research-workspace';
import { matchesSavedResultSummary, useSavedResultSummary } from './result-preview';
export function UnifiedResultsWorkspace({ userId, canWrite = false }: { userId: string; canWrite?: boolean }) {
  const [refresh, setRefresh] = useState(0);
  const [query, setQuery] = useState(() => new URLSearchParams(window.location.hash.split('?')[1] ?? ''));
  useEffect(() => {
    const changed = () => setQuery(new URLSearchParams(window.location.hash.split('?')[1] ?? ''));
    window.addEventListener('hashchange', changed);
    return () => window.removeEventListener('hashchange', changed);
  }, []);
  const autonomous=query.get('autonomous_session'),artifact=query.get('autonomous_artifact');
  const id = query.get('result_id'), run = query.get('portfolio_run'), legacy = query.get('run');
  const report = useRef<HTMLDivElement>(null), detailKey = autonomous ?? id ?? run ?? legacy;
  useEffect(() => {
    const target = report.current;
    if (!detailKey || !target) return;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: 'start', behavior: 'instant' });
  }, [detailKey]);
  return <>
    <ResultCatalog canWrite={canWrite} routeKind={query.get('kind')} manageId={query.get('manage_result')}
      changed={() => setRefresh(n => n + 1)} />
    {detailKey && <div ref={report} tabIndex={-1}>
      {autonomous&&<><Notice>查看研究步骤和保存的文件。是否已完成、是否用单独留出的数据检验，请看任务状态。</Notice><AutonomousSessionDetail sessionId={autonomous} userId={userId}/>{artifact&&<AutonomousArtifactReader sessionId={autonomous} artifactId={artifact} userId={userId}/>}</>}
      {id && <Panel title="已保存报告"><UnifiedSavedReport key={id + ":" + refresh} id={id} userId={userId}/></Panel>}
      {run && <SavedDatasetReport key={run + ':' + refresh} run={run} />}
      {legacy && <Panel title="研究报告"><Resource key={legacy + ':' + refresh} path={`/research/results/${encodeURIComponent(legacy)}`}>{(value) => <NativeResearchReport value={value} runId={legacy} userId={userId} />}</Resource></Panel>}
    </div>}
  </>;
}


function AutonomousSavedResult({id}:{id:string}){
 const summary=useSavedResultSummary(id);
 const body=record(summary.data?.summary);
 if(summary.error)return <ErrorBox error={summary.error}/>;
 if(!summary.data)return <p role="status">正在打开报告…</p>;
 if(!matchesSavedResultSummary(summary.data,id))return <Notice>无法确认摘要属于所选报告。请刷新结果列表后再打开。</Notice>;
 if(body.family==='autonomous-research-report-1'&&typeof body.session_id==='string')return <><Notice>这份旧报告没有可展示的收益图表。可打开原记录查看保存的文件。</Notice><a href={`#my-results?autonomous_session=${encodeURIComponent(body.session_id)}`}>打开原研究记录和文件 →</a></>;
 if(body.family==='autonomous-backtest-result-1'){
  if(typeof body.session_id!=='string'||typeof body.attempt_id!=='string')return <Notice>这份自主回测报告缺少编号，无法打开走势图。请从原研究记录查看。</Notice>;
  return <AutonomousBacktestReport sessionId={body.session_id} attemptId={body.attempt_id}/>;
 }
 return <Notice>暂不能打开报告。请刷新结果列表后重试。</Notice>;
}

function UnifiedSavedReport({id,userId}:{id:string;userId:string}){
 if(id.startsWith('autonomous:'))return <AutonomousSavedResult id={id}/>;
 if(id.startsWith('migration:'))return <Resource path={`/results/${encodeURIComponent(id)}`}>{value => <HistoricalReport report={value} />}</Resource>;
 return <Resource key={id} path={`/results/${encodeURIComponent(id)}`}>{(value) => value.family==='autonomous-backtest-result-1'&&typeof value.session_id==='string'&&typeof value.attempt_id==='string'?<AutonomousBacktestReport sessionId={value.session_id} attemptId={value.attempt_id}/>:isStrategySearch(value) ? <StrategySearchReport value={value} /> : value.family === 'dataset-portfolio-result-1' || value.family === 'factor-validation-result-1' || value.family === 'dataset-research-result-1' || value.family === 'model-tool-research-result-1' || value.family === 'factor-exploration-result-1' || value.family === 'document-evidence-analysis-1' ? <PortfolioAnalytics value={value} /> : value.family === 'tradingagents-native-report-1' || value.family === 'qlib-workflow-result-1' ? <NativeResearchReport value={value} runId={String(value.run_id)} userId={userId} /> : String(value.family).startsWith('daily-') ? <HistoryReport run={String(value.run_id)} /> : <><Notice>可展开下方的原始记录查看报告。</Notice><details><summary>查看原报告记录</summary><JsonView value={value} /></details></>}</Resource>;
}
