import { useRef, useState } from 'react';
import { post } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, useResource } from './components';
import { beginResearchAttempt, clearResearchAttempt, pendingResearchAttempt, record, validateResearchJob, versionReference } from './native-research-api';

export function validateTeamSnapshot(value: RecordData, id: string, owner: string) {
  if (value.snapshot_id !== id || value.owner_id !== owner || !/^[a-f0-9]{64}$/.test(String(value.sha256)) || !value.configuration || !value.resolved_roles)
    throw new Error('团队快照身份或版本信息不匹配，未使用其他配置。');
  return value;
}
export function offlineTeam(snapshot: RecordData) {
  const config = record(snapshot.configuration), defaults = record(config.defaults);
  const offline = (model: RecordData) => model.provider === 'offline' && model.model === 'deterministic-v1' && model.secret_ref == null;
  return offline(defaults) && Object.values(record(config.roles)).every((role) => record(role).model == null || offline(record(record(role).model)));
}
export function teamRunAvailable(capability: RecordData) {
  const team = record(capability.agent_teams);
  return team.contract === 'finance-agent-teams/v1' && team.snapshot_field === 'agent_team_snapshot_id' &&
    team.snapshot_required === true && team.offline_model === 'deterministic-v1' && team.can_submit === true;
}
export function teamRunBody(capability: RecordData, snapshot: RecordData, owner: string, input: { reference: unknown; ticker: string; date: string }, key: string) {
  validateTeamSnapshot(snapshot, String(snapshot.snapshot_id), owner);
  if (!teamRunAvailable(capability) || !offlineTeam(snapshot)) throw new Error('当前未就绪或不是获准的离线团队，未发起模型调用。');
  const ticker = input.ticker.trim().toUpperCase(), date = input.date;
  if (!/^[A-Z][A-Z0-9.-]{0,17}$/.test(ticker) || !/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(Date.parse(date)) || new Date(date).toISOString().slice(0, 10) !== date)
    throw new Error('请核对股票代码和准确研究日期。');
  return { agent_team_snapshot_id: snapshot.snapshot_id, data_reference: versionReference(input.reference), ticker, trade_date: date, as_of: date, evidence_results: [], idempotency_key: key };
}
export function TeamResearchRun({ snapshotId, owner, canWrite, reference, label, onJob }: { snapshotId: string; owner: string; canWrite: boolean; reference: unknown; label: string; onJob: (job: RecordData) => void }) {
  const [refresh, setRefresh] = useState(0);
  const saved = useResource<RecordData>(`/agent-teams/snapshots/${encodeURIComponent(snapshotId)}`, refresh);
  const capability = useResource<RecordData>('/research/debate-capabilities', refresh);
  const [ticker, setTicker] = useState(''), [date, setDate] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null), [uncertain, setUncertain] = useState(() => pendingResearchAttempt(owner));
  const gate = useRef(false);
  let snapshot: RecordData | null = null, identityError: unknown = null, hasData = false;
  try { if (saved.data) snapshot = validateTeamSnapshot(saved.data, snapshotId, owner); } catch (e) { identityError = e; }
  try { versionReference(reference); hasData = true; } catch { /* No fabricated reference. */ }
  const enabled = canWrite && !busy && !uncertain && snapshot !== null && offlineTeam(snapshot) && hasData && capability.data !== null && teamRunAvailable(capability.data);
  async function submit(event: React.FormEvent) {
    event.preventDefault(); if (!enabled || gate.current) return;
    gate.current = true; setBusy(true); setError(null); let attempted = false;
    try {
      const body = teamRunBody(capability.data!, snapshot!, owner, { reference, ticker, date }, crypto.randomUUID());
      beginResearchAttempt(owner, body); attempted = true;
      const receipt = validateResearchJob(await post<RecordData>('/research/debates', body), owner, undefined, 'research.debate');
      clearResearchAttempt(owner); onJob(receipt);
    } catch (e) { setError(e); if (attempted) setUncertain(true); }
    finally { gate.current = false; setBusy(false); }
  }
  return <section aria-label="用保存的团队设置研究">
    <h3>用所选团队做离线练习</h3>
    <Notice>这项练习使用所选团队版本和讨论轮数，只支持离线模型，不会调用在线模型。</Notice>
    <p>已选数据：{hasData ? label || '已选保存版本' : '尚未选择'}。<a href="#data">管理数据 →</a> <a href="#agent-teams">返回模型与团队 →</a></p>
    {snapshot && <p>保存版本：{String(snapshot.preset_revision)}；{offlineTeam(snapshot) ? '离线团队' : '包含在线模型，不能用于这项离线练习'}。</p>}
    {capability.data && !teamRunAvailable(capability.data) && <Notice>暂时无法开始离线团队研究。<details><summary>查看原因代码（技术信息）</summary><code>{String(record(capability.data.agent_teams).reason_code ?? '能力未确认')}</code></details></Notice>}
    <button type="button" className="text-button" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>刷新团队设置与任务状态</button>
    <form onSubmit={submit}><label>股票代码<input value={ticker} disabled={busy} placeholder="与所选数据一致，例如 AAPL" onChange={(e) => setTicker(e.target.value)} /></label><label>研究日期<input type="date" value={date} disabled={busy} onChange={(e) => setDate(e.target.value)} /></label><button type="submit" disabled={!enabled}>{busy ? '正在提交…' : '开始离线练习'}</button></form>
    {uncertain && <Notice>还不能确认研究已提交。到任务记录查看；退出或刷新都不会重新提交。<a href="#jobs">核对任务记录 →</a></Notice>}
    {(error || identityError || saved.error || capability.error) != null && <ErrorBox error={error || identityError || saved.error || capability.error} />}
  </section>;
}
