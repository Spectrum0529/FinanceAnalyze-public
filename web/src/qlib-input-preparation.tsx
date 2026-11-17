import { useState } from 'react';
import type { ReactNode } from 'react';
import { post, request } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, useResource } from './components';
import { record, terminalResearchJob, validateResearchJob } from './native-research-api';

export const inputProfile = 'FA014-QLIB-US-OFFLINE-V2';
type InputAction = 'prepare' | 'register';
type InputIntent = { status: 'UNKNOWN' | 'ACKNOWLEDGED'; jobId?: string; runId?: string };
const handler = (action: InputAction) => `data.qlib-${action}`;
const slot = (owner: string, action: InputAction, source?: RecordData) =>
  `fa-qlib-input-v1:${JSON.stringify([owner, inputProfile, action, source?.job_id ?? null, source?.run_id ?? null])}`;

export function inputPreparationAvailable(capability: unknown) {
  const value = record(record(capability).input_preparation);
  return value.profile_id === inputProfile && value.scope === 'SYNTHETIC_ONLY' &&
    value.arbitrary_history_supported === false && value.can_queue === true;
}
export function inputIntent(owner: string, action: InputAction, source?: RecordData): InputIntent | null {
  try {
    const raw = sessionStorage.getItem(slot(owner, action, source));
    if (raw === null) return null;
    const value = record(JSON.parse(raw));
    if (value.status === 'ACKNOWLEDGED' && typeof value.jobId === 'string' && value.jobId && typeof value.runId === 'string' && value.runId)
      return { status: 'ACKNOWLEDGED', jobId: value.jobId, runId: value.runId };
  } catch { /* Unreadable intent cannot establish whether work was submitted. */ }
  return { status: 'UNKNOWN' };
}
export async function submitInputAction(owner: string, canWrite: boolean, capability: unknown, action: InputAction, source?: RecordData) {
  if (!owner || !canWrite || !inputPreparationAvailable(capability)) throw new Error('样本准备服务尚未就绪或当前为只读，未发送任务。');
  if (action === 'register') {
    validateResearchJob(source ?? {}, owner, undefined, handler('prepare'));
    if (source?.status !== 'SUCCEEDED') throw new Error('准备任务尚未真实完成，不能登记。');
    const fresh = await request<RecordData>(`/jobs/${encodeURIComponent(String(source.job_id))}`);
    validateResearchJob(fresh, owner, String(source.job_id), handler('prepare'));
    if (fresh.run_id !== source.run_id || fresh.status !== 'SUCCEEDED') throw new Error('准备任务回读不匹配或尚未完成，不能登记。');
  }
  if (inputIntent(owner, action, source)) throw new Error('已有待核对提交或已确认任务，未重复发送。请查看任务记录。');
  const idempotency_key = crypto.randomUUID();
  // Same session persistence as normal research submit/retry. No automatic replay or manual unlock.
  sessionStorage.setItem(slot(owner, action, source), JSON.stringify({ status: 'UNKNOWN', idempotency_key }));
  const path = action === 'prepare' ? '/research/qlib/inputs/prepare' : `/research/qlib/inputs/${encodeURIComponent(String(source!.job_id))}/register`;
  const body = action === 'prepare' ? { profile_id: inputProfile, idempotency_key } : { idempotency_key };
  const job = await post<RecordData>(path, body);
  validateResearchJob(job, owner, undefined, handler(action));
  if (source && (job.job_id === source.job_id || job.run_id === source.run_id)) throw new Error('未收到新任务编号。请先查看任务记录。');
  sessionStorage.setItem(slot(owner, action, source), JSON.stringify({ status: 'ACKNOWLEDGED', jobId: job.job_id, runId: job.run_id, idempotency_key }));
  return job;
}

function UnknownInput() {
  return <Notice>还不能确认上次提交是否成功，请先查看任务记录。离开或刷新页面都不会重新提交。<a href="#jobs">查看任务记录 →</a></Notice>;
}
function InputJob({ intent, action, owner, onSuccess }: { intent: InputIntent; action: InputAction; owner: string; onSuccess: (job: RecordData) => ReactNode }) {
  const [refresh, setRefresh] = useState(0);
  const state = useResource<RecordData>(`/jobs/${encodeURIComponent(intent.jobId!)}`, refresh, 1500, terminalResearchJob);
  let job: RecordData | null = null, identityError: unknown = null;
  try {
    if (state.data) {
      job = validateResearchJob(state.data, owner, intent.jobId, handler(action));
      if (job.run_id !== intent.runId) throw new Error('服务返回的运行编号与提交记录不一致。');
    }
  } catch (error) { identityError = error; job = null; }
  const names: Record<string, string> = { QUEUED: '排队中', RUNNING: '执行中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消' };
  return <>
    <button type="button" className="text-button" onClick={() => setRefresh((n) => n + 1)}>刷新{action === 'prepare' ? '准备' : '登记'}任务</button>
    {state.loading ? <p>正在读取任务…</p> : state.error || identityError ? <ErrorBox error={state.error || identityError} /> : job ? <>
      <p>{action === 'prepare' ? '准备' : '登记'}任务：{names[String(job.status)]}。<a href="#jobs">查看任务记录 →</a></p>
      {job.status === 'SUCCEEDED' ? onSuccess(job) : terminalResearchJob(job) ? <Notice>任务未成功。请查看任务记录定位原因；这批数据还不能用于训练。</Notice> : <p>请等待任务结束，再操作下一步。</p>}
    </> : null}
  </>;
}
function RegisterInput({ source, owner, canWrite, capability, onRefresh }: { source: RecordData; owner: string; canWrite: boolean; capability: unknown; onRefresh: () => void }) {
  const [revision, setRevision] = useState(0), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null);
  void revision;
  const intent = inputIntent(owner, 'register', source);
  async function submit() {
    if (busy) return;
    setBusy(true); setError(null);
    try { await submitInputAction(owner, canWrite, capability, 'register', source); }
    catch (error) { setError(error); }
    finally { setBusy(false); setRevision((n) => n + 1); }
  }
  return <>
    <p>数据已准备好，请再点击登记。</p>
    <button type="button" disabled={busy || !canWrite || !inputPreparationAvailable(capability) || intent !== null} onClick={() => void submit()}>2. 登记合成离线样本</button>
    {error != null && <ErrorBox error={error} />}
    {intent?.status === 'UNKNOWN' ? <UnknownInput /> : intent?.status === 'ACKNOWLEDGED' ? <InputJob intent={intent} action="register" owner={owner} onSuccess={() => <>
      <Notice>数据已登记。请刷新模板列表查看能否选择它，选好后还需点击开始训练。</Notice>
      <button type="button" onClick={onRefresh}>3. 刷新可用样本</button>
    </>} /> : null}
  </>;
}
export function QlibInputPreparation({ owner, canWrite, capability, onRefresh }: { owner: string; canWrite: boolean; capability: unknown; onRefresh: () => void }) {
  const [revision, setRevision] = useState(0), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null);
  void revision;
  const intent = inputIntent(owner, 'prepare');
  async function submit() {
    if (busy) return;
    setBusy(true); setError(null);
    try { await submitInputAction(owner, canWrite, capability, 'prepare'); }
    catch (error) { setError(error); }
    finally { setBusy(false); setRevision((n) => n + 1); }
  }
  return <section aria-label="合成离线样本准备">
    <h3>准备合成练习数据</h3>
    <Notice>这里只用合成的美股练习数据，不能代表真实市场或正式研究。先准备，再登记；完成后才可选择模板训练。</Notice>
    {!inputPreparationAvailable(capability) && <p>暂时不能开始准备数据，请稍后刷新重试。</p>}
    <button type="button" disabled={busy || !canWrite || !inputPreparationAvailable(capability) || intent !== null} onClick={() => void submit()}>1. 准备合成练习数据</button>
    {error != null && <ErrorBox error={error} />}
    {intent?.status === 'UNKNOWN' ? <UnknownInput /> : intent?.status === 'ACKNOWLEDGED' ? <InputJob intent={intent} action="prepare" owner={owner} onSuccess={(source) => <RegisterInput key={String(source.job_id)} source={source} owner={owner} canWrite={canWrite} capability={capability} onRefresh={onRefresh} />} /> : null}
  </section>;
}
