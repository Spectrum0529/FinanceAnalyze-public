import { useState, useSyncExternalStore } from 'react';
import { ErrorBox, Notice } from './components';
import { canRepeatUxMutation, recoverUxMutation, uxPending, uxPendingChanged } from './ux-mutation';
import type { PendingWriteResource } from './ux-mutation';

function observe(listener: () => void) {
  window.addEventListener(uxPendingChanged, listener);
  return () => window.removeEventListener(uxPendingChanged, listener);
}

export function useUxPending(owner: string, area: string) {
  return useSyncExternalStore(observe, () => uxPending(owner, area));
}

export function UxWriteRecovery({ owner, area, busy, canWrite, recovered, allowSameRequestRetry = true }: {
  owner: string; area: string; busy: boolean; canWrite: boolean;
  recovered: (value: PendingWriteResource, path: string) => void;
  allowSameRequestRetry?: boolean;
}) {
  const pending = useUxPending(owner, area);
  const [checking, setChecking] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [closing, setClosing] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState<unknown>(null);
  async function check(choice: 'lookup' | 'repeat' | 'close' = 'lookup') {
    setChecking(true);
    setError(null);
    setMessage('');
    try {
      const result = await recoverUxMutation(owner, area, choice);
      setUnknown(result.status === 'UNKNOWN');
      if (result.status === 'CLOSED') setMessage('上次提交已关闭。可以修改内容后再提交。');
      if (result.status === 'COMMITTED') {
        setMessage(result.resource.kind === 'upload' ? '已找到原上传。请重新预览所选文件以继续。' : '已找到原操作的保存记录。');
        recovered(result.resource, result.path);
      }
    } catch (cause) { setError(cause); }
    finally { setChecking(false); setClosing(false); }
  }
  if (!pending) return message ? <p role="status">{message}</p> : null;
  return <Notice>
    <p>{unknown ? '暂时找不到上次提交的记录。你可以再查一次。' : '上次提交还没有确认结果。'}</p>
    <button type="button" disabled={busy || checking} onClick={() => void check()}>查看上次提交结果</button>
    {unknown && canWrite && allowSameRequestRetry && canRepeatUxMutation(owner, area) &&
      <button type="button" disabled={busy || checking} onClick={() => void check('repeat')}>按原内容重试</button>}
    {unknown && canWrite && !closing &&
      <button type="button" disabled={busy || checking} onClick={() => setClosing(true)}>关闭上次提交</button>}
    {closing && <div>
      <p>系统会先检查上次提交是否已经保存。已保存时会打开原记录。</p>
      <button type="button" disabled={busy || checking} onClick={() => void check('close')}>确认关闭上次提交</button>
      <button type="button" disabled={checking} onClick={() => setClosing(false)}>返回结果查询</button>
    </div>}
    {error != null && <ErrorBox error={error} />}
  </Notice>;
}
