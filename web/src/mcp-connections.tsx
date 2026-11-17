import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, Panel, useResource } from './components';
import { McpAccountConnections } from './mcp-account-connections';
import type { AccountReauthentication } from './platform-account-session';
export function CredentialEditor({ provider, endpointId = null, canWrite = true, expandedInitially = false, onReference }: { provider: string; endpointId?: string | null; canWrite?: boolean; expandedInitially?: boolean; onReference: (reference: string) => void }) {
  const [expanded, setExpanded] = useState(expandedInitially);
  const [value, setValue] = useState(''), [status, setStatus] = useState<RecordData | null>(null), [error, setError] = useState<unknown>(null), [busy, setBusy] = useState(false), [unknown, setUnknown] = useState(false);
  const lifetime = useRef({ active: true });
  useEffect(() => () => { lifetime.current.active = false; }, []);
  async function act(method: string) {
    if (busy || (!canWrite || unknown) && method !== 'GET') return;
    setBusy(true); setError(null);
    const supplied = value; setValue('');
    try {
      const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([provider, endpointId])));
      if (!lifetime.current.active) return;
      const slot = 'key-' + Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2,'0')).join('');
      const result = await request<RecordData>(`/operations/provider-credentials/${slot}`, { method, ...(method === 'PUT' ? { body: JSON.stringify({ provider, endpoint_id: endpointId, value: supplied }) } : {}) });
      if (!lifetime.current.active) return;
      if (result.slot !== slot || typeof result.configured !== 'boolean' || typeof result.secret_ref !== 'string') throw new Error('无法确认这个密钥的保存状态，请重新查看。');
      if (result.configured && (result.binding_verified !== true || result.provider !== provider || result.endpoint_id !== endpointId)) throw new Error('返回状态属于另一个密钥，请重新选择当前目标。');
      setStatus(result);
      if (method === 'PUT' && result.configured && result.binding_verified === true) onReference(result.secret_ref);
      if (method === 'GET') setUnknown(false);
      if (method === 'DELETE') onReference('');
    } catch { setStatus(null); setError(new Error('无法确认这个密钥的操作结果。请先查看所选密钥状态，再决定是否重试。')); if (method !== 'GET') setUnknown(true); }
    finally { setBusy(false); }
  }
  const matched = status?.configured === true && status.binding_verified === true && status.provider === provider && status.endpoint_id === endpointId;
  return <details open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}><summary>在本机保存密钥（可稍后）</summary><Notice>密钥保存在本机，供这家模型服务的当前连接使用。导出设置时不会包含密钥。</Notice><p>所选服务：{provider} · {endpointId ?? '默认连接地址'}</p><label>新密钥<input type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} disabled={busy || unknown || !canWrite} /></label><button disabled={busy || unknown || !canWrite || !value} onClick={() => void act('PUT')}>保存新密钥</button><button disabled={busy} onClick={() => void act('GET')}>查看所选密钥状态</button><button disabled={busy || !canWrite || !matched} onClick={() => onReference(String(status!.secret_ref))}>使用这把密钥</button><button disabled={busy || unknown || !canWrite || !matched} onClick={() => void act('DELETE')}>移除所选密钥</button>{status && <p>{matched ? '密钥已保存在本机。能否调用模型需要向供应商确认。' : '这项连接还没有保存密钥。'}</p>}{error != null && <ErrorBox error={error} />}</details>;
}
export function McpConnections({ accountMode = false, canWrite = false, reauthenticate, motion }: {
  accountMode?: boolean; canWrite?: boolean; reauthenticate?: AccountReauthentication; motion?: () => boolean;
}) {
  if (accountMode) return reauthenticate
    ? <McpAccountConnections canWrite={canWrite} reauthenticate={reauthenticate} motion={motion ?? (() => false)} />
    : <Panel title="连接外部 AI"><Notice>请重新打开工作台，以使用账户配对。</Notice></Panel>;
  return <PersonalMcpConnections />;
}
function PersonalMcpConnections() {
  const [refresh, setRefresh] = useState(0), [copied, setCopied] = useState(false), [error, setError] = useState<unknown>(null);
  const [advanced, setAdvanced] = useState(false);
  const connection = useResource<RecordData>(advanced ? '/operations/mcp-connection' : null, refresh);
  const ready = connection.data?.available === true && connection.data.client_config != null && typeof connection.data.client_config === 'object';
  const config = ready ? JSON.stringify(connection.data!.client_config, null, 2) : '';
  async function copyConfiguration() {
    try {
      await navigator.clipboard.writeText(config);
      setCopied(true);
      setError(null);
    } catch {
      setCopied(false);
      setError(new Error('无法自动复制。请打开客户端配置，选中文字后手动复制。'));
    }
  }
  return <Panel title="连接外部 AI" helpTopic="mcp">
    <p>当前使用本机个人模式。登录工作台账户后，可直接连接 Codex 或 DeepSeek。</p>
    <details onToggle={event => setAdvanced(event.currentTarget.open)}>
      <summary>高级设置</summary>
      {advanced && <>
        <p>保持工作台运行。复制配置到支持本地 MCP 的客户端，再在客户端检查工具列表。</p>
        <button onClick={() => { setCopied(false); setRefresh(value => value + 1); }}>刷新连接配置</button>
        {connection.error != null && <ErrorBox error={connection.error} />}
        {ready ? <>
          <details><summary>查看客户端配置</summary><textarea aria-label="MCP 客户端配置" rows={10} readOnly value={config} /></details>
          <button className="primary-action" onClick={() => void copyConfiguration()}>复制 MCP 配置</button>
        </> : <p>{connection.loading ? '正在检查本机配置…' : '暂时没有可复制的配置。请从本机启动器打开工作台，再刷新。'}</p>}
        {typeof connection.data?.message === 'string' &&
          <details><summary>查看连接诊断</summary><p>{connection.data.message}</p></details>}
        {copied && <p role="status">已复制。请到客户端添加配置并核对工具列表。</p>}
        {error != null && <ErrorBox error={error} />}
      </>}
    </details>
  </Panel>;
}
