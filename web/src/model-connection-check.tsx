import { useState } from 'react';
import type { RecordData } from './api';
import { ErrorBox, Notice, Status, useResource } from './components';
import { useDataSubmit } from './data-development-tools';
import { record, terminalResearchJob } from './native-research-api';

export function ModelConnectionCheck({ owner, profile, canWrite }: { owner: string; profile: RecordData; canWrite: boolean }) {
  const [open, setOpen] = useState(false);
  return <details onToggle={event => setOpen(event.currentTarget.open)}><summary>检查此模型连接</summary>
    {open && <ModelTest owner={owner} profile={profile} canWrite={canWrite} />}
  </details>;
}
const credentials = { SAVED: '已在本机保存', NOT_CONFIGURED: '尚未保存', NOT_REQUIRED: '此模型无需密钥' };
function ModelTest({ owner, profile, canWrite }: { owner: string; profile: RecordData; canWrite: boolean }) {
  const profileId = typeof profile.profile_id === 'string' ? profile.profile_id : '';
  const path = '/model-profiles/' + encodeURIComponent(profileId) + '/test';
  const version = profile.revision;
  const model = record(record(profile.configuration).model);
  const handler = 'model_profile.connection_test';
  const [allowed, setAllowed] = useState(false);
  const [lastJob, setLastJob] = useState(() => {
    try { return sessionStorage.getItem('fa-model-connection-job:' + owner + ':' + profileId + ':' + version) ?? ''; }
    catch { return ''; }
  });
  const capability = useResource<RecordData>(profileId ? path : null);
  const write = useDataSubmit(owner, 'model-test:' + profileId + ':' + version, path, handler);
  const id = write.job || lastJob;
  const state = useResource<RecordData>(id ? '/jobs/' + encodeURIComponent(id) : null, 0, 1500, terminalResearchJob);
  const job = state.data;
  const matchesJob = job?.job_id === id && job.owner_id === owner && job.handler === handler;
  const result = record(matchesJob ? job.result : null);
  const matchesResult = result.profile_id === profileId && result.revision === version &&
    result.provider === model.provider && result.model === model.model;
  const ready = capability.data?.profile_id === profileId && capability.data?.revision === version &&
    capability.data?.provider === model.provider && capability.data?.model === model.model && capability.data?.available === true;
  const saved = String(capability.data?.credential_state ?? '');
  const liveCheck = matchesJob && job.status === 'SUCCEEDED' && matchesResult;
  const active = !!id && (!matchesJob || !terminalResearchJob(job));
  async function test() {
    if (!ready || !canWrite || (!allowed && !write.pending) || write.busy) return;
    setAllowed(false);
    setLastJob('');
    const accepted = await write.submit({ expected_revision: version, allow_billable_call: true });
    if (accepted) {
      try { sessionStorage.setItem('fa-model-connection-job:' + owner + ':' + profileId + ':' + version, accepted); }
      catch { /* The task can still be reopened from task records. */ }
    }
  }
  return <>
    <p>本机密钥：{Object.hasOwn(credentials, saved) ? credentials[saved as keyof typeof credentials] : '状态待核对'}。</p>
    <p>模型调用：{liveCheck ? '本次提供方调用成功。' : matchesJob && job.status === 'FAILED' ? '本次测试失败。' : '尚未确认可用。'}</p>
    {!capability.loading && capability.data && !ready && <Notice>当前保存的配置不能发起此测试。请核对本机密钥和支持的连接地址。</Notice>}
    {ready && canWrite && <>
      <p>测试会向 {String(model.provider)} 的 {String(model.model)} 发送一条固定短消息，最多生成 64 个词元。可能收费，以供应商账单为准。</p>
      <label><input type="checkbox" checked={allowed} disabled={active || write.busy || write.pending}
        onChange={event => setAllowed(event.target.checked)} />我同意发送这一次测试请求，费用可能计入我的模型账号</label>
      <button type="button" disabled={active || write.busy || write.disabled || (!allowed && !write.pending)}
        onClick={() => void test()}>{write.pending ? '核对上次模型测试' : '明确开始一次模型测试'}</button>
    </>}
    {id && <p><a href={'#jobs?job=' + encodeURIComponent(id)}>查看模型测试进度</a>{matchesJob && <> · <Status value={job.status} /></>}</p>}
    {matchesJob && job.status === 'SUCCEEDED' && !matchesResult && <Notice>测试记录与当前模型配置不匹配。请从任务详情核对。</Notice>}
    {matchesJob && job.status === 'FAILED' && <Notice>请在任务详情查看失败原因，核对密钥或模型后再决定是否测试。</Notice>}
    {(capability.error ?? write.error ?? state.error) != null && <ErrorBox error={capability.error ?? write.error ?? state.error} />}
  </>;
}
