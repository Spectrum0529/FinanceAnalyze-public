import { ModelUsagePanel } from './model-usage-panel';
import { useEffect, useMemo, useRef, useState } from 'react';
import { request } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, Panel, Resource, useResource } from './components';
import { ServerRows } from './research-visuals';
import { useWorkspaceSelection } from './workspace-selection';
import { TeamResearchRun } from './team-research-run';
import { NativeResearchReport } from './native-research-report';
import { ResultPreviewLink } from './result-preview';
import { QlibInputPreparation } from './qlib-input-preparation';
import { beginResearchAttempt, clearResearchAttempt, debateCapabilities, debateRequest, pendingResearchAttempt, qlibCatalogue, qlibBasicProfile, qlibRequest, record, researchCommands, researchJobPath, researchResultPath, submitDebate, submitQlib, terminalResearchJob, validateResearchJob, versionReference } from './native-research-api';

const stateNames: Record<string, string> = { QUEUED: '排队中', RUNNING: '执行中', SUCCEEDED: '已完成', FAILED: '未完成', CANCELLED: '已取消' };
const today = () => new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' });
const linkedJob = () => {
  const query = new URLSearchParams(window.location.hash.split('?')[1] ?? '');
  const debate = query.get('debate_job'), qlib = query.get('qlib_job');
  return debate && !qlib ? { id: debate, debate: true } : qlib && !debate ? { id: qlib, debate: false } : null;
};

export function IntelligentResearch({ canWrite, userId }: {
  canWrite: boolean; userId: string;
}) {
  const { reference, label } = useWorkspaceSelection();
  const [teamSnapshot, setTeamSnapshot] = useState(() => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('agent_team_snapshot_id') ?? '');
  const capabilities = useResource<RecordData>('/research/debate-capabilities');
  const [exportOffset, setExportOffset] = useState(0), [qlibRefresh, setQlibRefresh] = useState(0);
  const qlibCapabilities = useResource<RecordData>('/research/qlib/capabilities', qlibRefresh);
  const qlibDefinitions = useResource<RecordData>('/research/qlib/templates', qlibRefresh);
  const qlibExports = useResource<RecordData>(`/research/qlib/exports?limit=25&offset=${exportOffset}`, qlibRefresh);
  let qlib: ReturnType<typeof qlibCatalogue> | null = null, qlibError: unknown = null;
  try { if (qlibCapabilities.data && qlibDefinitions.data && qlibExports.data) qlib = qlibCatalogue(qlibCapabilities.data, qlibDefinitions.data, qlibExports.data, exportOffset); }
  catch (error) { qlibError = error; }
  const [featureProfile, setFeatureProfile] = useState('');
  const qlibTemplates = (qlib?.choices ?? []).filter((choice) => choice.featureProfile === featureProfile);
  const [kind, setKind] = useState<'qlib' | 'debate'>(teamSnapshot ? 'debate' : 'qlib');
  const [ticker, setTicker] = useState(''), [date, setDate] = useState(today), [rounds, setRounds] = useState(1);
  const [templateIndex, setTemplateIndex] = useState(0);
  const [job, setJob] = useState<RecordData | null>(() => linkedJob() ? { job_id: linkedJob()!.id } : null), [jobDebate, setJobDebate] = useState(() => linkedJob()?.debate ?? false), [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false), [uncertain, setUncertain] = useState(() => pendingResearchAttempt(userId));
  useEffect(() => {
    const changed = () => { setTeamSnapshot(new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('agent_team_snapshot_id') ?? ''); const next = linkedJob(); if (next) { setJob({ job_id: next.id }); setJobDebate(next.debate); } };
    window.addEventListener('hashchange', changed); return () => window.removeEventListener('hashchange', changed);
  }, []);
  const gate = useRef(false);
  const selectedTemplate = Math.min(templateIndex, Math.max(0, qlibTemplates.length - 1));
  const template = qlibTemplates[selectedTemplate];
  let capability: ReturnType<typeof debateCapabilities> | null = null;
  try { if (capabilities.data) capability = debateCapabilities(capabilities.data); } catch { /* Fail closed below. */ }
  let selectedData = false;
  try { versionReference(reference); selectedData = true; } catch { /* No guessed data reference. */ }
  const enabled = canWrite && !busy && !uncertain && (kind === 'qlib' ? featureProfile === qlibBasicProfile && template?.ready === true : capability?.canSubmit === true && selectedData);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!enabled || gate.current) return;
    gate.current = true; setBusy(true); setError(null);
    let attempted = false;
    try {
      const key = crypto.randomUUID();
      const body = kind === 'qlib' ? qlibRequest(template!, key, featureProfile) : debateRequest(capabilities.data!, { dataReference: reference, ticker, date, rounds, evidence: [] }, key);
      beginResearchAttempt(userId, body);
      attempted = true;
      const receipt = kind === 'qlib' ? await submitQlib(body as ReturnType<typeof qlibRequest>) : await submitDebate(body as ReturnType<typeof debateRequest>);
      setJob(validateResearchJob(receipt, userId, undefined, kind === 'debate' ? 'research.debate' : 'research.qlib')); setJobDebate(kind === 'debate');
      clearResearchAttempt(userId);
      window.location.hash = `intelligent?${kind === 'debate' ? 'debate_job' : 'qlib_job'}=${encodeURIComponent(String(receipt.job_id))}`;
    } catch (e) { setError(e); if (attempted) setUncertain(true); }
    finally { gate.current = false; setBusy(false); }
  }
  return <>
    <Panel title="智能研究" helpTopic="native-research">
      <p>可以按模板训练模型，也可以让多个角色讨论一只股票的利好、利空和风险。</p>
      <div className="form-actions"><button disabled={busy} className={kind === 'qlib' ? 'primary' : 'secondary'} onClick={() => { setKind('qlib'); if (teamSnapshot) { setTeamSnapshot(''); window.location.hash = 'intelligent'; } }}>按模板训练（Qlib）</button><button disabled={busy} className={kind === 'debate' ? 'primary' : 'secondary'} onClick={() => { setKind('debate'); if (teamSnapshot) { setTeamSnapshot(''); window.location.hash = 'intelligent'; } }}>多角色讨论</button><a href="#my-results">我的结果 →</a></div>
      {teamSnapshot ? <TeamResearchRun key={teamSnapshot} snapshotId={teamSnapshot} owner={userId} canWrite={canWrite} reference={reference} label={label || ''} onJob={(receipt) => { setJob(receipt); setJobDebate(true); window.location.hash = `intelligent?debate_job=${encodeURIComponent(String(receipt.job_id))}`; }} /> : <form onSubmit={submit}>
        {kind === 'qlib' ? <>
          <h3>Qlib 训练与预测</h3>
          <p>用收盘价、开盘价和成交量训练模型并查看预测。此模板不做组合回测。</p>
          <QlibInputPreparation owner={userId} canWrite={canWrite} capability={qlibCapabilities.data} onRefresh={() => { setExportOffset(0); setQlibRefresh((n) => n + 1); }} />
          <label>使用哪些行情字段<select value={featureProfile} disabled={busy} onChange={(event) => { setFeatureProfile(event.target.value); setTemplateIndex(0); }}><option value="">请选择使用哪些行情字段</option><option value={qlibBasicProfile}>基础三列（收盘价、开盘价、成交量）</option><option value="alpha158-lgb-us-1" disabled>Alpha158（暂不可用）</option></select></label>
          <Notice>三项行情模板只使用合成练习数据。Alpha158 尚未通过数据访问检查，暂不可用。</Notice>
          <label>数据与日期模板<select value={selectedTemplate} onChange={(event) => setTemplateIndex(Number(event.target.value))} disabled={!qlibTemplates.length || busy}>{qlibTemplates.length ? qlibTemplates.map((item, i) => <option key={i} value={i}>{item.label}</option>) : <option value={0}>还没有可选的模板</option>}</select></label>
          <Notice>{qlib?.reason ?? '尚未取得可用模板信息，当前不提交训练。'}结果只供练习，不能代表真实市场或正式研究。</Notice>
          {(qlibError || qlibCapabilities.error || qlibDefinitions.error || qlibExports.error) && <ErrorBox error={qlibError || qlibCapabilities.error || qlibDefinitions.error || qlibExports.error} />}
          <div className="form-actions"><button type="button" className="text-button" disabled={busy} onClick={() => setQlibRefresh((n) => n + 1)}>刷新模板状态</button><button type="button" className="secondary" disabled={busy || exportOffset === 0} onClick={() => { setExportOffset((n) => Math.max(0, n - 25)); setTemplateIndex(0); }}>上一页导出</button><button type="button" className="secondary" disabled={busy || qlib?.nextOffset == null} onClick={() => { if (qlib?.nextOffset != null) { setExportOffset(qlib.nextOffset); setTemplateIndex(0); } }}>下一页导出</button></div>
          {template && <p>测试日期：{String((template.workflow.test as string[] | undefined)?.[0] ?? '未提供')} 至 {String((template.workflow.test as string[] | undefined)?.[1] ?? '未提供')}；训练和验证窗口随模板固定。</p>}
        </> : <>
          <h3>多空与风险讨论</h3>
          <Notice>{!capability ? '还没确认模型是否可用，暂不能开始。' : capability.modelStatus === 'UNCONFIGURED' ? '请先配置在线模型。普通研究不会改用离线练习模型。' : capability.canSubmit ? '可以提交讨论。调用在线模型可能产生厂商费用。' : '当前所选模型暂不能提交讨论。'}</Notice>
          {capability && <details><summary>模型状态和服务说明（技术信息）</summary><p>{capability.modelStatus} · {capability.message}</p></details>}
          {capabilities.error && <details><summary>查看模型状态读取错误</summary><ErrorBox error={capabilities.error} /></details>}
          <label>股票代码<input value={ticker} onChange={(e) => setTicker(e.target.value)} placeholder="例如 AAPL，需在所选数据中" disabled={busy} /></label>
          <label>研究日期<input type="date" value={date} onChange={(e) => setDate(e.target.value)} disabled={busy} /></label>
          <p>本任务会引用已有数据，汇总利好、利空和风险观点。</p>
          <p>已选数据：{selectedData ? label || '已选保存版本' : '尚未选择'}。<a href="#data">前往管理数据选择 →</a></p>
          <label>讨论深度<select value={rounds} onChange={(e) => setRounds(Number(e.target.value))} disabled={busy}>{(capability?.debateRounds ?? [1]).filter((n) => !capability || capability.riskRounds.includes(n)).map((n) => <option key={n} value={n}>{n === 1 ? '快速 · 一轮多空与风险讨论' : '深入 · 两轮多空与风险讨论'}</option>)}</select></label>
        </>}
        <div className="form-actions"><button type="submit" disabled={!enabled}>{busy ? '正在提交…' : kind === 'qlib' ? '开始训练' : '开始多角色讨论'}</button></div>
      </form>}
      {uncertain && <Notice>上次提交是否成功还不确定。请先到任务记录查看；刷新也不会重新发送。<a href="#jobs">查看任务 →</a><p>新建会产生独立任务，原任务可能仍在运行。</p><button className="secondary" onClick={() => { clearResearchAttempt(userId); if (!pendingResearchAttempt(userId)) { setUncertain(false); setError(null); } }}>已核对原任务，准备新建研究</button></Notice>}
      {error != null && <ErrorBox error={error} />}
    </Panel>
    {job && <NativeResearchJob key={String(job.job_id)} jobId={String(job.job_id)} debate={jobDebate} userId={userId} canWrite={canWrite} />}
  </>;
}

export function NativeResearchJob({ jobId, debate, userId, canWrite }: { jobId: string; debate: boolean; userId: string; canWrite: boolean }) {
  const [refresh, setRefresh] = useState(0), [actionError, setActionError] = useState<unknown>(null), [busy, setBusy] = useState(false);
  const [replacement, setReplacement] = useState<RecordData | null>(null);
  const commands = useMemo(() => researchCommands(userId, canWrite), [userId, canWrite]);
  const currentId = replacement ? String(replacement.job_id) : jobId;
  const state = useResource<RecordData>(researchJobPath(currentId, debate), refresh, 1500, terminalResearchJob);
  let job: RecordData | null = null, identityError: unknown = null;
  try { if (state.data) job = validateResearchJob(state.data, userId, currentId, debate ? 'research.debate' : 'research.qlib'); } catch (e) { identityError = e; }
  async function act(action: 'cancel' | 'retry') {
    if (!job || busy) return;
    setBusy(true); setActionError(null);
    try {
      const receipt = await commands.act(job, action, debate);
      if (action === 'retry') { setReplacement(receipt); window.location.hash = `intelligent?${debate ? 'debate_job' : 'qlib_job'}=${encodeURIComponent(String(receipt.job_id))}`; }
      setRefresh((n) => n + 1);
    } catch (e) { setActionError(e); }
    finally { setBusy(false); }
  }
  async function reconcile() {
    try {
      const latest = await request<RecordData>(researchJobPath(currentId, debate));
      validateResearchJob(latest, userId, currentId, debate ? 'research.debate' : 'research.qlib'); commands.reconcile(latest);
      setActionError(null); setRefresh((n) => n + 1);
    } catch (e) { setActionError(e); }
  }
  const resume = record(job?.resume);
  const retryState = job ? commands.retryState(job) : null;
  return <Panel title="本次研究任务">
    {state.loading ? <p>正在读取任务记录…</p> : state.error ? <ErrorBox error={state.error} /> : identityError ? <ErrorBox error={identityError} /> : job ? <>
      <p>当前状态：{stateNames[String(job.status)]}；进度 {typeof job.progress === 'number' ? `${job.progress}%` : '未提供'}。</p>
      {debate&&<ModelUsagePanel kind="job" id={currentId} active={!terminalResearchJob(job)} trace/>}
      {job.cancel_requested === true && <Notice>停止请求已接收，以任务最终状态为准。</Notice>}
      {job.error_code != null && <details><summary>查看未完成原因（技术信息）</summary><code>{String(job.error_code)}</code></details>}
      {retryState?.status === 'UNKNOWN' && <Notice>暂时无法确认上次重试有没有创建新任务。请到任务记录查找新任务；当前不能再重试，以免重复运行。<a href="#jobs">核对任务记录 →</a></Notice>}
      {retryState?.status === 'ACKNOWLEDGED' && <Notice>已确认这次重试创建了新任务。请在任务记录查看。<a href={`#intelligent?${debate ? 'debate_job' : 'qlib_job'}=${encodeURIComponent(retryState.childJobId!)}`}>查看已确认的新任务 →</a></Notice>}
      <div className="form-actions">
        <button className="secondary" disabled={!canWrite || busy || terminalResearchJob(job) || job.cancel_requested === true} onClick={() => void act('cancel')}>请求停止</button>
        <button className="secondary" disabled={!canWrite || busy || retryState !== null || !['FAILED', 'CANCELLED'].includes(String(job.status))} onClick={() => void act('retry')}>{debate && resume.available === true ? '从确认过的进度继续（新任务）' : '重新运行（新任务）'}</button>
        <button className="text-button" disabled={busy} onClick={() => void reconcile()}>刷新并核对</button>
      </div>
      {debate && resume.available !== true && <p className="muted">暂无保存的进度可继续。重试会新建任务。</p>}
      {replacement && <p>已建立新的运行记录；原失败或取消记录保留。</p>}
      {job.status === 'SUCCEEDED' && typeof job.run_id === 'string' && <ResultPreviewLink resultId={`research:${job.run_id}`}
        title="已保存的研究" href={`#my-results?run=${encodeURIComponent(job.run_id)}`}>预览研究报告</ResultPreviewLink>}
    </> : <Notice>服务尚未返回任务记录。</Notice>}
    {actionError != null && <ErrorBox error={actionError} />}
  </Panel>;
}

export function MyResearchResults({ userId }: { userId: string }) {
  const [run, setRun] = useState(() => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('run') ?? '');
  useEffect(() => {
    const changed = () => setRun(new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('run') ?? '');
    window.addEventListener('hashchange', changed); return () => window.removeEventListener('hashchange', changed);
  }, []);
  return <>
    <Panel title="我的数据与结果"><div className="form-actions"><a href="#results">已保存回测</a><a href="#data">已有数据</a><a href="#intelligent">开始智能研究</a><a href="#jobs">未完成任务与停止</a></div><p>打开报告即可查看研究类型、所用工具和结果。</p>
      <OwnedResearchIndex userId={userId} />
    </Panel>
    {run && <Panel title="已保存研究报告"><Resource key={run} path={researchResultPath(run)}>{(value) => <NativeResearchReport value={value} runId={run} userId={userId} />}</Resource></Panel>}
  </>;
}

function OwnedResearchIndex({ userId }: { userId: string }) {
  const [offset, setOffset] = useState(0), [refresh, setRefresh] = useState(0);
  const state = useResource<RecordData>(`/research/results?limit=20&offset=${offset}`, refresh);
  const page = state.data;
  const valid = page && Array.isArray(page.items) && page.offset === offset && page.limit === 20 &&
    Number.isSafeInteger(page.total) && Number(page.total) >= 0 && page.items.length <= 20;
  const rows = valid ? (page.items as RecordData[]).filter((row) => row && row.owner_id === userId && typeof row.run_id === 'string') : [];
  return <>
    <button className="text-button" onClick={() => setRefresh((n) => n + 1)}>刷新结果列表</button>
    {state.loading ? <p>正在读取我的研究结果…</p> : state.error ? <ErrorBox error={state.error} /> : !valid ? <Notice>暂时无法读取这一页结果，请刷新后重试。</Notice> : <>
      <ServerRows value={rows} columns={[
        { key: 'created_at', label: '保存时间 / UTC' },
        { key: 'run_id', label: '研究记录', render: (row) => <span title={String(row.run_id)}>记录 {String(row.run_id).slice(-10)}</span> },
        { key: 'open', label: '操作', render: (row) => <ResultPreviewLink resultId={`research:${String(row.run_id)}`}
          title="已保存的研究" href={`#my-results?run=${encodeURIComponent(String(row.run_id))}`}>预览研究报告</ResultPreviewLink> },
      ]} empty={Number(page.total) === 0 ? '还没有已保存的研究结果。可在任务记录查看未完成的任务。' : '本页没有你的结果，可继续翻页。'} />
      <div className="pagination"><small>当前页本人记录 {rows.length} 条</small><button className="secondary" disabled={offset === 0} onClick={() => setOffset((n) => n - 20)}>上一页</button><button className="secondary" disabled={offset + 20 >= Number(page.total)} onClick={() => setOffset((n) => n + 20)}>下一页</button></div>
    </>}
  </>;
}
