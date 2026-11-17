import { useEffect, useId, useRef, useState } from 'react';
import { ApiError } from './api';
import { useResource } from './components';
import { accountWrite, hasCode } from './platform-account-session';
import { easyConnectionStatus, mcpPath, mcpScopeNames } from './mcp-account-contract';
import type { EasyConnectRequest, EasyConnectionStatus, EasyMcpClient } from './mcp-account-contract';

type Proof = (signal: AbortSignal, resume: () => void, opener: HTMLElement | null) => Promise<boolean>;
const names = { codex: 'Codex', deepseek: 'DeepSeek' };
const reasons: Record<string, string> = {
  MCP_SCOPE_REQUIRED: '当前账户无法授予所选权限。可尝试只查看。',
  MCP_INSTALLATION_INCOMPLETE: '本机连接组件尚未就绪。请重新打开工作台后重试。',
  MCP_LISTENER_MISMATCH: '无法核对本机服务地址。请从 FinanceAnalyze 本机工作台重新打开此页。',
  MCP_CLIENT_CONFIGURATION_REQUIRED: '还需要准备此客户端的本机配置。请查看高级设置。',
  MCP_CLIENT_CONFIGURATION_CONFLICT: '此客户端已有不同的 FinanceAnalyze 配置。请在高级设置核对。',
  MCP_CLIENT_CONFIG_CONFLICT: '此客户端已有不同的 FinanceAnalyze 配置。请在高级设置核对。',
  MCP_CLIENT_CONFIG_UNAVAILABLE: '暂时无法准备客户端的配置。请重新打开工作台后重试，或到高级设置检查。',
  MCP_CONNECTION_IN_PROGRESS: '这个客户端正在处理另一次连接。请稍后检查状态。',
  MCP_CONNECTION_SLOT_CONFLICT: '这个客户端已有需要核对的本机连接。请打开高级设置检查。',
  MCP_SERVICE_UNAVAILABLE: '暂时无法连接本机服务。请稍后重试。',
  REAUTH_REQUIRED: '需要重新验证账户身份。请重新点击连接后重试。',
};
const stopWatching = (result: EasyConnectionStatus) => result?.state !== 'paired_waiting_for_client';
const scopeText = (value: EasyConnectionStatus['scope']) => value === 'browse' ? '只查看'
  : value === 'workbench' ? '帮我操作'
    : value === 'external' ? '帮我操作，并允许用 AI 模型做研究'
      : value && Object.hasOwn(mcpScopeNames, value) ? mcpScopeNames[value] : '';
const confirmedTime = (value: string | null) => value
  ? new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }) : '';

export function McpEasyConnect({ canWrite, blocked, verifying, statusRevision, askProof, onBusy, onAuthorized }: {
  canWrite: boolean; blocked: boolean; verifying: boolean; statusRevision: number;
  askProof: Proof; onBusy: (busy: boolean) => void; onAuthorized: () => void;
}) {
  const permissions = useId();
  const clientField = useId();
  const [client, setClient] = useState<EasyMcpClient>('codex');
  const [access, setAccess] = useState<EasyConnectRequest['access']>(canWrite ? 'workbench' : 'browse');
  const [allowModel, setAllowModel] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0), [watching, setWatching] = useState(false);
  const [receipt, setReceipt] = useState<EasyConnectionStatus | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const life = useRef<AbortController | null>(null);
  const pending = useRef(false);
  const status = useResource<EasyConnectionStatus>(mcpPath + '/status?' + new URLSearchParams({ client_id: client }),
    refresh + statusRevision, watching ? 1800 : false, stopWatching);
  const confirmed = easyConnectionStatus(receipt, client);
  const selectedAccess = canWrite ? access : 'browse';
  const modelAllowed = selectedAccess === 'workbench' && allowModel;

  useEffect(() => {
    const controller = new AbortController();
    life.current = controller;
    return () => { controller.abort(); life.current = null; };
  }, []);
  useEffect(() => {
    if (!watching) return;
    const timer = window.setTimeout(() => setWatching(false), 18000);
    return () => window.clearTimeout(timer);
  }, [watching, refresh]);
  useEffect(() => {
    const current = easyConnectionStatus(status.data, client);
    if (current) setReceipt(current);
  }, [status.data, client]);

  function refreshStatus(watch: boolean) {
    setWatching(watch);
    setRefresh(value => value + 1);
  }

  async function connect(body: EasyConnectRequest, permitProof = true) {
    const signal = life.current?.signal;
    if (!signal || signal.aborted || pending.current) return;
    pending.current = true;
    setBusy(true); onBusy(true); setError('');
    try {
      const reply = await accountWrite<EasyConnectionStatus>(mcpPath + '/connect', 'POST', body, signal);
      if (signal.aborted) return;
      const value = easyConnectionStatus(reply, body.client_id);
      const wanted = body.allow_model_budget ? 'external' : body.access;
      if (!value) throw new Error('invalid connection response');
      if (value.state !== 'connected' && value.state !== 'paired_waiting_for_client') {
        setReceipt(value);
        setError(value.reason_code && reasons[value.reason_code] || '连接未完成，请核对账户和客户端状态后再试。');
        return;
      }
      if (value.scope !== wanted) throw new Error('unexpected grant scope');
      setReceipt(value);
      onAuthorized();
      refreshStatus(value.state === 'paired_waiting_for_client');
    } catch (cause) {
      if (signal.aborted) return;
      if (permitProof && hasCode(cause, 'REAUTH_REQUIRED') &&
          await askProof(signal, () => void connect(body, false), trigger.current)) return;
      if (!signal.aborted) setError(cause instanceof ApiError && reasons[cause.code] ||
        '未能确认连接结果。可先检查连接状态，再重试。');
    } finally {
      pending.current = false;
      if (!signal.aborted) { setBusy(false); onBusy(false); }
    }
  }

  const waiting = confirmed?.state === 'paired_waiting_for_client';
  const connected = confirmed?.state === 'connected';
  const unavailable = confirmed?.state === 'unavailable' || status.error != null;

  return <>
    <form onSubmit={event => {
      event.preventDefault();
      if (busy || blocked) return;
      void connect({ client_id: client, access: selectedAccess, allow_model_budget: modelAllowed });
    }}>
      <fieldset disabled={busy || blocked}>
        <legend>连接哪个 AI 软件</legend>
        <label htmlFor={clientField}>AI 软件</label>
        <select id={clientField} value={client} onChange={event => {
          setClient(event.target.value as EasyMcpClient);
          setReceipt(null); setWatching(false); setError(''); setAllowModel(false);
        }}>
          <option value="codex">Codex</option>
          <option value="deepseek">DeepSeek Harness</option>
        </select>
      </fieldset>
      <fieldset disabled={busy || blocked}>
        <legend>希望 AI 帮你做什么</legend>
        <label><input type="radio" name={permissions} checked={selectedAccess === 'browse'}
          onChange={() => { setAccess('browse'); setAllowModel(false); }} />只查看</label>
        <p>查看你有权共享的行情、任务和报告。</p>
        {canWrite && <>
          <label><input type="radio" name={permissions} checked={selectedAccess === 'workbench'}
            onChange={() => setAccess('workbench')} />帮我操作（推荐）</label>
          <p>下载和整理行情、准备并运行策略、查看与管理结果。</p>
          {selectedAccess === 'workbench' && <label>
            <input type="checkbox" checked={allowModel} onChange={event => setAllowModel(event.target.checked)} />
            允许软件调用我配置的 AI 模型（可能收费）
          </label>}
          {selectedAccess === 'workbench' && <p>每次使用模型做研究都需明确选择来源、模型和预算。</p>}
        </>}
      </fieldset>
      <div className="form-actions">
        <button className="primary-action" ref={trigger} disabled={busy || blocked}
          type="submit">连接 {names[client]}</button>
      </div>
    </form>
    <div aria-live="polite">
      {verifying ? <p>等待身份验证…</p> : busy ? <p>正在检查并准备连接…</p>
        : unavailable ? <p>{confirmed?.reason_code && reasons[confirmed.reason_code] || '暂时无法检查本机连接。'}</p>
          : connected ? <p>{names[client]} 已连接，最近确认 {confirmedTime(confirmed.client_confirmed_at)}。当前授权：{scopeText(confirmed.scope)}</p>
            : waiting ? <p>已授权 · {scopeText(confirmed.scope)}。等待 {names[client]} 确认连接。</p>
              : error ? null : status.loading ? <p>正在检查现有连接…</p> : <p>还没有连接此 AI 软件</p>}
    </div>
    {waiting && (!watching || ['MCP_CLIENT_RELOAD_REQUIRED', 'MCP_CLIENT_REFRESH_REQUIRED'].includes(confirmed.reason_code ?? '')) &&
      <p>如果 {names[client]} 还没显示 FinanceAnalyze 工具，请在客户端刷新一次工具列表，再检查连接状态。</p>}
    {!busy && !verifying && (waiting || unavailable || error) &&
      <button type="button" className="text-button" onClick={() => refreshStatus(Boolean(waiting))}>检查连接状态</button>}
    {error && <p className="message error" role="alert">{error}</p>}
  </>;
}
