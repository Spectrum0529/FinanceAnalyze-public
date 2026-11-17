import { useEffect, useId, useRef, useState } from 'react';
import { ApiError, request } from './api';
import { AreaHelp } from './area-help';
import { useResource } from './components';
import { accountWrite, hasCode } from './platform-account-session';
import type { AccountReauthentication } from './platform-account-session';
import { RecentProof } from './recent-proof';
import {
  clientConfigurationText, clientIdPattern, localPairingCommand, mcpPath, mcpScopeNames, pairingReceipt, publicClientConfiguration,
} from './mcp-account-contract';
import type { McpConfigurationReceipt, McpGrantPage, McpScope, PairingReceipt, PairingRequest } from './mcp-account-contract';
import { McpEasyConnect } from './mcp-easy-connect';

type Client = 'codex' | 'deepseek' | 'other';
type Challenge = { resume: () => void; needsOtp: boolean; opener: HTMLElement | null; easy: boolean };
const clients = { codex: 'Codex', deepseek: 'DeepSeek Harness', other: '其他本机 MCP 客户端' };
const scopeDescriptions: Record<McpScope, string> = {
  read: '查看获准共享的固定数据、任务和报告。',
  research: '增加内置策略回测与相关取消。',
  browse: '查看本人可共享的行情、结果、任务、策略、模型和团队。',
  manage: '可管理本人的数据、结果、模型与团队，包含回收和恢复。',
  workbench: '可下载公开行情、运行获准的本机回测，并管理相关任务。',
  external: '也可在每次明确指定来源、模型和预算后发起外部研究。',
};
const diagnosticsText: Record<string, string> = {
  MCP_INSTALLATION_INCOMPLETE: '本机 MCP 组件尚未装齐。',
  MCP_AUTHORIZATION_REQUIRED: '此客户端需要为当前登录配对。',
  MCP_LISTENER_MISMATCH: '客户端指向的本机监听地址不一致。',
};
const statuses = { active: '已授权', revoked: '已撤销', expired: '已过期' };
const pageSize = 20;
const formatTime = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : '—';

export function McpAccountConnections({ canWrite, reauthenticate, motion }: {
  canWrite: boolean; reauthenticate: AccountReauthentication; motion: () => boolean;
}) {
  const heading = useId(), permissions = useId();
  const [advanced, setAdvanced] = useState(false);
  const [easyBusy, setEasyBusy] = useState(false);
  const [client, setClient] = useState<Client>('codex');
  const [customId, setCustomId] = useState('other');
  const [committed, setCommitted] = useState('codex');
  const [scope, setScope] = useState<McpScope | ''>('');
  const [configurationRefresh, setConfigurationRefresh] = useState(0);
  const [grantRefresh, setGrantRefresh] = useState(0);
  const [offset, setOffset] = useState(0);
  const [pairing, setPairing] = useState<PairingReceipt | null>(null);
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const life = useRef<AbortController | null>(null);
  const locked = useRef(false);
  const manualTrigger = useRef<HTMLButtonElement | null>(null);
  const selected = client === 'other' ? customId : client;
  const config = useResource<McpConfigurationReceipt>(
    advanced ? mcpPath + '?client_id=' + encodeURIComponent(committed) : null, configurationRefresh,
  );
  const grants = useResource<McpGrantPage>(advanced ? mcpPath + '/grants?limit=' + pageSize + '&offset=' + offset : null, grantRefresh);
  const readiness = selected === committed && !config.loading && !config.error
    ? publicClientConfiguration(config.data, committed) : null;
  const approvedScopes = (readiness?.scopes ?? []).filter(value => canWrite || value === 'browse' || value === 'read');
  const blocked = busy || easyBusy || challenge !== null;
  const exportText = readiness ? clientConfigurationText(readiness.server, client) : '';
  const pairCommand = readiness ? localPairingCommand(readiness.pairing) : '';

  useEffect(() => {
    const controller = new AbortController();
    life.current = controller;
    return () => {
      controller.abort();
      life.current = null;
    };
  }, []);
  useEffect(() => {
    if (!pairing) return;
    const expire = window.setTimeout(() => {
      setPairing(null);
      setMessage('配对码已过期，可以生成新码。');
    }, Math.min(120000, Math.max(0, Date.parse(pairing.expires_at) - Date.now())));
    return () => window.clearTimeout(expire);
  }, [pairing]);

  function clearSelection() {
    setPairing(null);
    setScope('');
    setError('');
    setMessage('');
  }
  async function run(operation: (signal: AbortSignal) => Promise<void>) {
    const signal = life.current?.signal;
    if (!signal || signal.aborted || locked.current) return;
    locked.current = true;
    setBusy(true);
    setError('');
    setMessage('');
    try { await operation(signal); }
    finally {
      locked.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  async function askProof(signal: AbortSignal, resume: () => void, opener: HTMLElement | null, easy: boolean) {
    try {
      const factors = await request<{ enabled?: boolean; available?: boolean }>('/auth/mfa', { signal });
      if (!signal.aborted && typeof factors?.enabled === 'boolean' && (!factors.enabled || factors.available === true)) {
        setChallenge({ resume, needsOtp: factors.enabled, opener, easy });
        return true;
      }
    } catch { /* Present the same retryable state on all identity-probe failures. */ }
    return false;
  }
  function issuePairing(approval: PairingRequest, allowProof: boolean) {
    void run(async signal => {
      try {
        const received = await accountWrite<PairingReceipt>(mcpPath + '/pairings', 'POST', approval, signal);
        const confirmed = pairingReceipt(received, approval);
        if (!confirmed) throw Error('Invalid pairing receipt');
        if (signal.aborted) return;
        setPairing(confirmed);
        setMessage('已生成一次性配对码，有效期 120 秒。');
      } catch (requestError) {
        if (signal.aborted) return;
        if (allowProof && hasCode(requestError, 'REAUTH_REQUIRED') &&
            await askProof(signal, () => issuePairing(approval, false), manualTrigger.current, false)) return;
        if (!signal.aborted) setError(
          hasCode(requestError, 'MCP_SCOPE_REQUIRED') ? '当前账户没有所选权限。请检查配置并选择只读。'
            : '未能生成配对码。请核对客户端配置、权限和账户状态，再手动重试。',
        );
      }
    });
  }
  function finishProof(verified: boolean) {
    const resume = challenge?.resume;
    setChallenge(null);
    if (verified) resume?.();
  }
  function revoke(id: string) {
    void run(async signal => {
      try {
        const receipt = await accountWrite<{ revoked?: true }>(mcpPath + '/grants/' + encodeURIComponent(id), 'DELETE', undefined, signal);
        if (receipt?.revoked !== true) throw Error('Revocation not confirmed');
        if (!signal.aborted) {
          setGrantRefresh(value => value + 1);
          setMessage('已撤销此客户端授权。已提交的任务继续按原流程运行。');
        }
      } catch (error) {
        if (signal.aborted) return;
        setError(error instanceof ApiError && error.status === 404
          ? '这项授权已不可用，请刷新列表。' : '未能确认撤销，请刷新列表后检查状态。');
      }
    });
  }
  async function copy(value: string, success: string) {
    if (!value) return;
    const signal = life.current?.signal;
    try {
      await navigator.clipboard.writeText(value);
      if (!signal?.aborted) { setError(''); setMessage(success); }
    } catch {
      if (!signal?.aborted) setError('无法自动复制。可选中页面上显示的文字，手动复制。');
    }
  }
  const items = Array.isArray(grants.data?.items) ? grants.data.items : [];
  const selectedGrantScope = scope && approvedScopes.includes(scope) ? scope
    : approvedScopes.includes('browse') ? 'browse' : approvedScopes[0];

  return <section className="panel" aria-labelledby={heading}>
    <div className="panel-heading"><h2 id={heading}>连接外部 AI</h2><AreaHelp title="连接外部 AI" topic="mcp-easy-connect" /></div>
    <McpEasyConnect canWrite={canWrite} verifying={challenge?.easy === true} blocked={busy || challenge !== null}
      statusRevision={grantRefresh} onBusy={setEasyBusy} onAuthorized={() => setGrantRefresh(value => value + 1)}
      askProof={(signal, resume, opener) => askProof(signal, resume, opener, true)} />
    <details onToggle={event => setAdvanced(event.currentTarget.open)}>
    <summary>高级设置</summary>
    <p>授权管理、其他本机客户端和故障排查。</p>
    {advanced && <>
    <form onSubmit={event => {
      event.preventDefault();
      if (!clientIdPattern.test(selected) || blocked) return;
      clearSelection();
      setCommitted(selected);
      setConfigurationRefresh(value => value + 1);
    }}>
      <fieldset disabled={blocked}>
        <legend>客户端</legend>
        <label>客户端<select value={client} onChange={event => {
          clearSelection();
          setClient(event.target.value as Client);
        }}>
          {Object.entries(clients).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select></label>
        {client === 'other' && <label>客户端标识<input value={customId} maxLength={32} pattern="[a-z][a-z0-9_\-]{0,31}"
          onChange={event => { clearSelection(); setCustomId(event.target.value); }} required /></label>}
        {client === 'other' && <small>每个客户端使用独立标识。使用小写字母开头，最多 32 位，可含数字、短横线或下划线。</small>}
        <div className="form-actions"><button type="submit" disabled={!clientIdPattern.test(selected)}>读取客户端配置</button></div>
      </fieldset>
    </form>
    {selected === committed && config.loading && <p role="status">正在读取本机配置…</p>}
    {selected === committed && !config.loading && !readiness && <p role="status">
      {config.data?.reason_codes?.map(code => diagnosticsText[code]).filter(Boolean).join(' ') ||
        '当前无法提供本机配置。请在服务所在电脑打开工作台，检查本地安装后重试。'}
    </p>}
    {readiness && <>
      <p>{clients[client]} 的本机配置已就绪。配对后，在客户端运行 <code>connection_diagnostics</code>，查看实际授权范围、工具列表和该进程最近一次调用状态。</p>
      <details>
        <summary>查看本机命令与客户端配置</summary>
        <p>在本机 PowerShell 运行配对命令。把一次性配对码粘贴到随后提示输入配对码的位置（字符不会显示）。</p>
        <textarea aria-label="本机配对命令" value={pairCommand} readOnly rows={3} />
        <p>{client === 'codex' ? '将这段 TOML 加到 Codex 的 MCP 配置。'
          : client === 'deepseek' ? '在 DeepSeek Harness 的插件设置（Cordis）里添加这一段 YAML 配置。'
            : '使用下方的标准 stdio 配置。若客户端要求分别填入启动程序和参数，可从配置复制对应字段。'}</p>
        <textarea aria-label="MCP 客户端配置" value={exportText} readOnly rows={7} />
      </details>
      <div className="form-actions">
        <button type="button" onClick={() => void copy(pairCommand, '已复制本机配对命令。')}>复制配对命令</button>
        <button type="button" onClick={() => void copy(exportText, '已复制客户端配置。')}>复制客户端配置</button>
      </div>
      <fieldset disabled={blocked || !selectedGrantScope}>
        <legend>配对权限</legend>
        {approvedScopes.map(value => <label key={value}>
          <input type="radio" name={permissions} value={value} checked={selectedGrantScope === value} onChange={() => setScope(value)} />
          {readiness.scopeLabels[value] ?? mcpScopeNames[value]} · {scopeDescriptions[value]}
        </label>)}
        <button className="primary-action" type="button" ref={manualTrigger} onClick={() => {
          if (!selectedGrantScope) return;
          setPairing(null);
          issuePairing({ client_id: committed, scope: selectedGrantScope }, true);
        }}>生成配对码</button>
      </fieldset>
    </>}
    {pairing && <section aria-label="一次性配对码">
      <h3>一次性配对码</h3>
      <p><code>{pairing.pairing_code}</code>（{mcpScopeNames[pairing.scope]}，在 <time dateTime={pairing.expires_at}>{formatTime(pairing.expires_at)}</time> 到期）</p>
      <p>只将此码粘贴到本机配对命令提示输入配对码的位置。客户端配置和对话无需此码。</p>
      <div className="form-actions">
        <button type="button" onClick={() => {
          if (Date.parse(pairing.expires_at) > Date.now()) void copy(pairing.pairing_code, '已复制一次性配对码。');
        }}>复制配对码</button>
        <button type="button" onClick={() => {
          setPairing(null);
          setMessage('已隐藏配对码。已复制的码仍在原到期时间内有效。');
        }}>隐藏配对码</button>
      </div>
    </section>}
    <p role="status">{busy ? '正在处理…' : message}</p>
    {error && <p role="alert" className="message error">{error}</p>}
    <section aria-label="当前账户的客户端授权">
      <h3>客户端授权</h3>
      <p>授权最多 8 小时，到本次登录结束时也会失效。退出、身份重新验证导致登录更新，或账号权限变化时，相关授权会结束。</p>
      <button type="button" disabled={blocked} onClick={() => setGrantRefresh(value => value + 1)}>刷新授权</button>
      {grants.loading ? <p role="status">正在读取授权…</p>
        : grants.error ? <p role="alert">无法读取授权。请刷新重试。</p>
        : items.length === 0 ? <p>当前页没有客户端授权。完成本机配对后可在这里查看。</p>
        : <ul>{items.map(grant => <li key={grant.grant_id}>
          <strong>{grant.client_name}</strong> <small>（{grant.client_id}）</small>
          <p>{statuses[grant.status]} · {mcpScopeNames[grant.scope]} · 到期 <time dateTime={grant.expires_at}>{formatTime(grant.expires_at)}</time></p>
          {grant.status === 'active' && <button type="button" disabled={blocked} aria-label={'撤销' + grant.client_name}
            onClick={() => revoke(grant.grant_id)}>撤销授权</button>}
        </li>)}</ul>}
      <div className="pagination">
        {offset > 0 && <button type="button" disabled={blocked} onClick={() => setOffset(Math.max(0, offset - pageSize))}>上一页</button>}
        {grants.data && offset + pageSize < grants.data.total && <button type="button" disabled={blocked}
          onClick={() => setOffset(offset + pageSize)}>下一页</button>}
      </div>
    </section>
    </>}
    </details>
    {challenge && <RecentProof motion={motion} needsOtp={challenge.needsOtp} opener={challenge.opener} verify={reauthenticate} done={finishProof} />}
  </section>;
}
