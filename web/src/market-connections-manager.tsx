import { useState } from 'react';
import { ApiError, post } from './api';
import type { RecordData } from './api';
import { ErrorBox, Panel, useResource } from './components';
import { record } from './native-research-api';
import { sourceDescription } from './source-labels';

const path = '/data/library/connections';
const captions = { edit: '编辑', archive: '停用', restore: '恢复' };
export function MarketConnectionsManager({ canWrite, revision, changed }: {
  canWrite: boolean; revision: number; changed: () => void;
}) {
  const [state, setState] = useState('active');
  const [input, setInput] = useState(''), [q, setQuery] = useState('');
  const [offset, setOffset] = useState(0), [refresh, setRefresh] = useState(0);
  const [action, setAction] = useState<{ kind: keyof typeof captions; row: RecordData; key: string } | null>(null);
  const [name, setName] = useState(''), [credential, setCredential] = useState('');
  const [changeCredential, setChangeCredential] = useState(false);
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null);
  const params = new URLSearchParams({ state, q, limit: '50', offset: String(offset) });
  const data = useResource<RecordData>(path + '/manage?' + params, revision + refresh);
  const rows = Array.isArray(data.data?.items) ? data.data.items.map(record) : [];
  function prepare(kind: keyof typeof captions, row: RecordData) {
    if (action || busy || !canWrite) return;
    setName(String(row.name ?? ''));
    setCredential(typeof row.credential_ref === 'string' ? row.credential_ref : '');
    setChangeCredential(false); setSent(false); setError(null);
    setAction({ kind, row, key: crypto.randomUUID() });
  }
  async function apply() {
    if (!action || busy || !canWrite) return;
    const { kind, row, key } = action;
    if (kind === 'edit' && !name.trim()) return;
    setSent(true); setBusy(true); setError(null);
    try {
      const value = await post<RecordData>(path + '/' + encodeURIComponent(String(row.connection_id)) + '/manage', {
        action: kind, expected_revision: row.revision, idempotency_key: key,
        ...(kind === 'edit' ? { name: name.trim(), ...(changeCredential ? { credential_ref: credential.trim() || null } : {}) } : {}),
      });
      if (value.connection_id !== row.connection_id || value.archived !== (kind === 'archive') ||
          value.revision !== Number(row.revision) + 1) throw Error('还不能确认连接是否更新。请点“核对上次操作”。');
      setAction(null); changed();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setAction(null); setRefresh(value => value + 1);
      }
      setError(cause);
    } finally { setBusy(false); }
  }
  return <Panel title="已保存来源连接">
    <div className="form-actions">
      <button type="button" aria-pressed={state === 'active'} onClick={() => { setState('active'); setOffset(0); }}>可用连接</button>
      <button type="button" aria-pressed={state === 'archived'} onClick={() => { setState('archived'); setOffset(0); }}>已停用的来源</button>
      <button type="button" onClick={() => setRefresh(n => n + 1)}>刷新连接</button>
    </div>
    <form onSubmit={event => { event.preventDefault(); setQuery(input.trim()); setOffset(0); }}>
      <label>搜索来源连接<input value={input} onChange={event => setInput(event.target.value)} /></label>
      <button>搜索连接</button>
    </form>
    {data.error != null ? <ErrorBox error={data.error} /> : data.loading
      ? <p role="status">正在读取来源连接…</p> : !rows.length && <p>没有找到连接。可清空搜索，或切换查看可用和已停用的来源。</p>}
    <ul>{rows.map(row => <li key={String(row.connection_id)}>
      <strong>{String(row.name)}</strong> · {sourceDescription(String(row.provider)).name}
      <p>{String(row.reference_count ?? '未知')} 份行情、{String(row.job_reference_count ?? '未知')} 项任务用过此连接</p>
      {canWrite && typeof row.revision === 'number' && <div className="form-actions">
        {row.archived !== true && <button type="button" disabled={!!action} onClick={() => prepare('edit', row)}>编辑连接</button>}
        <button type="button" disabled={!!action} onClick={() => prepare(row.archived === true ? 'restore' : 'archive', row)}>
          {row.archived === true ? '恢复使用此来源' : '停用此数据来源'}
        </button>
      </div>}
    </li>)}</ul>
    {action && <fieldset><legend>{captions[action.kind]}来源连接 · {String(action.row.name)}</legend>
      {action.kind === 'edit' ? <>
        <label>新连接名称<input value={name} maxLength={80} disabled={busy || sent} onChange={event => setName(event.target.value)} /></label>
        <label><input type="checkbox" checked={changeCredential} disabled={busy || sent} onChange={event => setChangeCredential(event.target.checked)} />选择其他已保存的登录凭据</label>
        {changeCredential && <label>已保存的凭据编号（留空可解除关联）<input value={credential} disabled={busy || sent} onChange={event => setCredential(event.target.value)} /></label>}
      </> : <p>已下载数据会保留，原报告仍可查看。{action.kind === 'archive' ? '停用后，新的下载任务无法选择这个来源。' : '恢复后，新下载可以选择此连接。'}</p>}
      <button type="button" disabled={busy || action.kind === 'edit' && !name.trim()} onClick={() => void apply()}>
        {sent ? '核对上次操作' : '确认' + captions[action.kind]}
      </button>
      <button type="button" disabled={busy} onClick={() => setAction(null)}>返回</button>
    </fieldset>}
    {error != null && <ErrorBox error={error} />}
    <div className="pagination">
      <button type="button" disabled={!offset || data.loading} onClick={() => setOffset(n => Math.max(0, n - 50))}>上一页连接</button>
      <button type="button" disabled={data.loading || offset + 50 >= Number(data.data?.total ?? 0)} onClick={() => setOffset(n => n + 50)}>下一页连接</button>
    </div>
  </Panel>;
}
