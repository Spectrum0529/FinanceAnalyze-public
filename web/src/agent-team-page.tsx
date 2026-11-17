import { teamWrite, teamWritePending } from './team-write';
import { TeamWriteRecovery } from './team-write-recovery';
import { useRef, useState } from 'react';
import { post, request } from './api';
import type { RecordData } from './api';
import { ErrorBox, JsonView, Notice, Panel, useResource } from './components';
import { record } from './native-research-api';
import { AdvancedModelOptions, ModelBindingFields, PromptText } from './model-configuration-controls';
import { AgentTeamPreview } from './agent-team-preview';
import { ResearchContextBoundary } from './research-context-boundary';
import type { BindingDraft, ModelChoice } from './model-configuration-controls';

const roles = { market_analyst: '市场分析', social_analyst: '情绪分析', news_analyst: '新闻分析', fundamentals_analyst: '基本面分析', bull_researcher: '多方研究', bear_researcher: '空方研究', research_manager: '研究经理', trader: '交易决策', risky_analyst: '积极风险', safe_analyst: '保守风险', neutral_analyst: '中性风险', risk_manager: '风险经理' };
const initial = () => ({ schema_version: 1, defaults: { provider: 'offline', model: 'deterministic-v1', effort: null, secret_ref: null }, roles: {}, prompt: { system: '', task: '', output: '' }, shared_context: '', budgets: { history_messages: 32, input_tokens: 8192, output_tokens: 1024, input_chars: 32768, context_policy: 'reject' }, rounds: { debate: 1, risk: 1, tool: 1 }, execution: { timeout_seconds: 120, retry: 0 } }) as RecordData;
export function modelChoices(raw: RecordData): ModelChoice[] {
  if (raw.contract !== 'finance-agent-teams/v1' || !Array.isArray(raw.models) || !Array.isArray(raw.roles) || raw.roles.some((role) => typeof role !== 'string' || !Object.hasOwn(roles, role))) throw new Error('模型能力合同不匹配，未生成模型选项。');
  return raw.models.map((entry) => {
    const row = record(entry);
    if (typeof row.provider !== 'string' || typeof row.model !== 'string' || !Array.isArray(row.efforts) || row.efforts.some((v) => typeof v !== 'string')) throw new Error('模型目录不完整。');
    return { provider: row.provider, id: row.model, label: row.model, efforts: row.efforts as string[] };
  });
}
const draftBinding = (value: RecordData): BindingDraft => ({ provider: String(value.provider ?? ''), model: String(value.model ?? ''), effort: String(value.effort ?? '') });
export function bindingWithReference(previous: RecordData, value: BindingDraft): RecordData {
  const standard: Record<string, string> = { openai: 'env:FA_OPENAI_API_KEY', deepseek: 'env:FA_DEEPSEEK_API_KEY' };
  const sameProvider = previous.provider === value.provider;
  const secret = value.provider === 'offline' || !value.provider ? null :
    sameProvider && typeof previous.secret_ref === 'string' && previous.secret_ref ? previous.secret_ref : standard[value.provider] ?? null;
  return { provider: value.provider, model: value.model, effort: value.effort || null, secret_ref: secret, endpoint_profile: sameProvider ? previous.endpoint_profile ?? null : null, native_parameters: sameProvider ? previous.native_parameters ?? {} : {} };
}
export function modelValue(value: BindingDraft, secret: unknown): RecordData {
  if (!value.provider || !value.model) throw new Error('请选择完整的厂商与模型。');
  if (value.provider === 'offline' && secret != null && secret !== '') throw new Error('离线模型不接受密钥或密钥引用。');
  if (!['offline','ollama'].includes(value.provider) && (typeof secret !== 'string' || !/^(?:env:FA_[A-Z0-9_]{1,80}|credential:FA_[0-9a-f]{64})$/.test(secret))) throw new Error('真实模型仅接受 env:FA_ 环境变量引用，不能填写密钥。');
  return { provider: value.provider, model: value.model, effort: value.effort || null, secret_ref: value.provider === 'offline' ? null : secret as string };
}
export function validateTeamDraft(configuration: RecordData, capability: RecordData) {
  const choices = modelChoices(capability);
  const validate = (raw: unknown) => {
    const value = record(raw), binding = draftBinding(value);
    const selected = choices.find((c) => c.provider === binding.provider && c.id === binding.model);
    if (binding.effort && (!selected || !selected.efforts.includes(binding.effort))) throw new Error('模型或推理强度不在当前能力目录中。');
    modelValue(binding, value.secret_ref);
  };
  validate(configuration.defaults);
  for (const [id, role] of Object.entries(record(configuration.roles))) {
    if (!Object.hasOwn(roles, id)) throw new Error('未知团队角色。');
    if (record(role).model != null) validate(record(role).model);
  }
  return configuration;
}
export function AgentTeams({ canWrite, userId, initialConfiguration }: { canWrite: boolean; userId: string; initialConfiguration?: RecordData }) {
  const [refresh, setRefresh] = useState(0), [offset, setOffset] = useState(0);
  const caps = useResource<RecordData>('/agent-teams/capabilities', refresh);
  const list = useResource<RecordData>(`/agent-teams/presets?limit=50&offset=${offset}`, refresh);
  const [tab, setTab] = useState('connections'), [name, setName] = useState('我的离线研究团队'), [configuration, setConfiguration] = useState<RecordData>(() => initialConfiguration ?? initial());
  const [selected, setSelected] = useState<RecordData | null>(null), [role, setRole] = useState('bull_researcher'), [preview, setPreview] = useState<RecordData | null>(null), [snapshot, setSnapshot] = useState<RecordData | null>(null), [error, setError] = useState<unknown>(null), [busy, setBusy] = useState(false), [pending, setPending] = useState(() => teamWritePending(userId));
  const gate = useRef(false);
  let choices: ModelChoice[] = [], contractError: unknown = null;
  try { if (caps.data) choices = modelChoices(caps.data); } catch (e) { contractError = e; }
  const defaults = record(configuration.defaults), overrides = record(configuration.roles), currentRole = record(overrides[role]), roleModel = record(currentRole.model);
  const savedMatches = !!selected && JSON.stringify(selected.configuration) === JSON.stringify(configuration);
  function change(next: RecordData) { setConfiguration(next); setPreview(null); setSnapshot(null); }
  function roleChange(next: RecordData) { change({ ...configuration, roles: { ...overrides, [role]: next } }); }
  async function act(kind: 'save' | 'copy' | 'preview' | 'snapshot') {
    if (gate.current || busy || contractError || !caps.data) return;
    if (kind !== 'preview' && (!canWrite || pending)) return;
    gate.current = true; setBusy(true); setError(null);
    try {
      validateTeamDraft(configuration, caps.data);
      if (kind === 'preview') { setPreview(await post<RecordData>('/agent-teams/preview', { configuration })); setSnapshot(null); }
      else if (kind === 'snapshot') {
        if (!selected || !savedMatches || !preview || !Number.isSafeInteger(selected.revision)) throw new Error('请先保存当前配置并完成检查并预览设置。');
        setSnapshot(await teamWrite(userId, '/agent-teams/snapshots', { preset_id: selected.preset_id, revision: selected.revision }));
      } else {
        const updating = kind === 'save' && selected !== null;
        const result = await teamWrite(userId, updating ? `/agent-teams/presets/${encodeURIComponent(String(selected!.preset_id))}` : '/agent-teams/presets', { name: kind === 'copy' ? `${name} 副本`.slice(0, 80) : name, configuration, ...(updating ? { expected_revision: selected!.revision } : {}) }, updating ? 'PUT' : 'POST');
        if (typeof result.preset_id !== 'string' || !Number.isSafeInteger(result.revision)) throw new Error('暂时无法确认配置是否已保存，请刷新列表查看。');
        setSelected(result); setConfiguration(record(result.configuration)); setName(String(result.name)); setPreview(null); setSnapshot(null); setRefresh((n) => n + 1);
      }
    } catch (e) { setError(e); }
    finally { setPending(teamWritePending(userId)); gate.current = false; setBusy(false); }
  }
  async function open(id: string) {
    if (busy) return; setBusy(true); setError(null);
    try { const saved = await request<RecordData>(`/agent-teams/presets/${encodeURIComponent(id)}`); if (saved.preset_id !== id || !Number.isSafeInteger(saved.revision) || !saved.configuration) throw new Error('配置记录不匹配。'); setSelected(saved); setName(String(saved.name)); change(record(saved.configuration)); }
    catch (e) { setError(e); } finally { setBusy(false); }
  }
  const prompt = tab === 'prompts' ? record(configuration.prompt) : record(currentRole.prompt);
  return <div className="v434-team-layout">
    <Panel title="我的研究团队" helpTopic="teams">
      <Notice>默认使用离线练习模型。保存团队设置不会调用在线模型。</Notice>
      <ResearchContextBoundary />
      <div className="form-actions">{[['connections', '模型连接'], ['team', '团队分工'], ['prompts', '给模型的指令']].map(([id, title]) => <button type="button" key={id} disabled={busy} onClick={() => setTab(id)} className={tab === id ? 'primary' : 'secondary'}>{title}</button>)}</div>
      {caps.error || contractError ? <ErrorBox error={caps.error || contractError} /> : null}
      <label>配置名称<input value={name} maxLength={80} disabled={busy || !canWrite} onChange={(e) => { setName(e.target.value); setSnapshot(null); }} /></label>
      {tab === 'connections' ? <>
        <h3>团队默认使用的模型</h3><ModelBindingFields value={draftBinding(defaults)} choices={choices} disabled={busy || !canWrite} changed={(v) => { change({ ...configuration, defaults: bindingWithReference(defaults, v) }); }} />
        {defaults.provider !== 'offline' && <p>这里保存模型选择；密钥请在“模型配置”中设置。</p>}
        <details><summary>高级连接选项</summary><label>已有密钥的引用名称<input value={String(defaults.secret_ref ?? '')} disabled={busy || !canWrite || defaults.provider === 'offline'} placeholder="env:FA_OPENAI_API_KEY" onChange={(e) => change({ ...configuration, defaults: { ...defaults, secret_ref: e.target.value || null } })} /></label><p>已有自定义环境变量时才需修改。请勿在这里粘贴密钥。</p></details>
      </> : tab === 'team' ? <>
        <label>角色<select value={role} disabled={busy} onChange={(e) => setRole(e.target.value)}>{Object.entries(roles).map(([id, title]) => <option key={id} value={id}>{title}</option>)}</select></label>
        <label><input type="checkbox" checked={currentRole.model == null} disabled={busy || !canWrite} onChange={(e) => roleChange({ ...currentRole, model: e.target.checked ? null : { ...defaults } })} />此角色使用团队默认模型</label>
        {currentRole.model != null && <><ModelBindingFields value={draftBinding(roleModel)} choices={choices} disabled={busy || !canWrite} changed={(v) => roleChange({ ...currentRole, model: bindingWithReference(roleModel, v) })} />{roleModel.provider !== 'offline' && <p>可以先保存角色设置，之后再配置密钥。</p>}<details><summary>高级角色连接选项</summary><label>此角色密钥的引用名称<input value={String(roleModel.secret_ref ?? '')} disabled={busy || !canWrite || roleModel.provider === 'offline'} onChange={(e) => roleChange({ ...currentRole, model: { ...roleModel, secret_ref: e.target.value || null } })} /></label></details></>}
        <PromptText label="单个角色能参考的资料" value={String(currentRole.private_context ?? '')} disabled={busy || !canWrite} changed={(v) => roleChange({ ...currentRole, private_context: v })} help="资料起初只发给这个角色；其他角色可能看到讨论内容和输出，请勿填写密钥或不宜共享的资料。" />
      </> : <>
        <div className="form-actions"><button disabled={busy || !canWrite} onClick={() => change({ ...configuration, prompt: { system: '仅依据已授权证据开展研究，明确区分事实与不确定性。', task: '比较多空与风险观点，保留证据不足和未解决分歧。', output: '用中文给出结论、支持证据、反对证据与适用限制。' } })}>填入示例研究指令</button><button disabled={busy || !canWrite} onClick={() => change({ ...configuration, prompt: { system: '', task: '', output: '' } })}>使用服务默认指令</button></div>
      </>}
      {tab !== 'connections' && (['system', 'task', 'output'] as const).map((key) => <PromptText key={key} label={`${tab === 'prompts' ? '团队' : '角色'} ${{ system: '通用指令', task: '任务要求', output: '输出格式' }[key]}`} value={String(prompt[key] ?? '')} disabled={busy || !canWrite} changed={(v) => tab === 'prompts' ? change({ ...configuration, prompt: { ...prompt, [key]: v } }) : roleChange({ ...currentRole, prompt: { ...prompt, [key]: v } })} />)}
      <AdvancedModelOptions>
        <PromptText label="团队共同参考的资料" value={String(configuration.shared_context ?? '')} disabled={busy || !canWrite} changed={(v) => change({ ...configuration, shared_context: v })} />
        {(['history_messages', 'input_tokens', 'output_tokens', 'input_chars'] as const).map((key) => <label key={key}>{{ history_messages: '历史消息条数', input_tokens: '模型输入用量上限（Token）', output_tokens: '模型输出用量上限（Token）', input_chars: '输入字符上限（不等于 token）' }[key]}<input type="number" value={String(record(configuration.budgets)[key])} disabled={busy || !canWrite} onChange={(e) => change({ ...configuration, budgets: { ...record(configuration.budgets), [key]: Number(e.target.value) } })} /></label>)}
        <label>资料太长时怎么办<select value={String(record(configuration.budgets).context_policy)} disabled={busy || !canWrite} onChange={(e) => change({ ...configuration, budgets: { ...record(configuration.budgets), context_policy: e.target.value } })}><option value="reject">拒绝并报告</option><option value="truncate_oldest">先省去最早的消息，保留必要指令及完整工具记录</option></select></label>
        {['debate', 'risk', 'tool'].map((key) => <label key={key}>{{ debate: '多空轮数', risk: '风险轮数', tool: '工具轮数' }[key]}<input type="number" min={1} max={2} value={String(record(configuration.rounds)[key])} disabled={busy || !canWrite} onChange={(e) => change({ ...configuration, rounds: { ...record(configuration.rounds), [key]: Number(e.target.value) } })} /></label>)}
        {['timeout_seconds', 'retry'].map((key) => <label key={key}>{key === 'timeout_seconds' ? '最长运行时间（秒）' : '允许的执行重试次数'}<input type="number" min={key === 'retry' ? 0 : 1} max={key === 'retry' ? 2 : 120} value={String(record(configuration.execution)[key])} disabled={busy || !canWrite} onChange={(e) => change({ ...configuration, execution: { ...record(configuration.execution), [key]: Number(e.target.value) } })} /></label>)}
      </AdvancedModelOptions>
      <div className="form-actions"><button disabled={busy || !canWrite || pending || !caps.data || !!contractError} onClick={() => void act('save')}>保存配置</button><button disabled={busy || !canWrite || pending} onClick={() => void act('copy')}>保存为副本</button><button disabled={busy || !caps.data || !!contractError} onClick={() => void act('preview')}>检查并预览设置</button><button disabled={busy || !canWrite || pending || !savedMatches || !preview} onClick={() => void act('snapshot')}>选定本次使用的版本</button></div>
      {pending && <TeamWriteRecovery key={userId} owner={userId} disabled={!canWrite || busy} recovered={(receipt) => {
        setPending(false); setError(null);
        if (typeof receipt.snapshot_id === 'string') setSnapshot(receipt);
        else {
          setSelected(receipt); setConfiguration(record(receipt.configuration)); setName(String(receipt.name));
          setPreview(null); setSnapshot(null); setRefresh(n => n + 1);
        }
      }} />}
      {error != null && <ErrorBox error={error} />}
      {preview && <AgentTeamPreview value={preview} roleNames={roles} />}
      {snapshot && <Notice>已固定本次使用的配置。<a href={`#intelligent?task_team_snapshot_id=${encodeURIComponent(String(snapshot.snapshot_id))}`}>使用此团队开始研究 →</a> <a href={`#intelligent?agent_team_snapshot_id=${encodeURIComponent(String(snapshot.snapshot_id))}`}>用于多角色讨论（TradingAgents） →</a> <a href={`#intelligent?goal_team=${encodeURIComponent(String(snapshot.snapshot_id))}`}>用于按目标研究</a> 打开研究表单后，还要点击开始才会运行。<details><summary>选定版本的完整配置（技术信息）</summary><JsonView value={snapshot} /></details></Notice>}
    </Panel>
    <Panel title="已保存的团队和指令">
      <button className="text-button" onClick={() => setRefresh((n) => n + 1)}>刷新已保存配置</button>
      {list.error ? <ErrorBox error={list.error} /> : Array.isArray(list.data?.items) ? <ul>{list.data.items.map((raw) => { const value = record(raw); return <li key={String(value.preset_id)}><button className="text-button" disabled={busy} onClick={() => void open(String(value.preset_id))}>{String(value.name)} · 版本 {String(value.revision)}</button></li>; })}</ul> : <p>正在读取配置…</p>}
      <div className="pagination"><button disabled={busy || offset === 0} onClick={() => setOffset((n) => Math.max(0, n - 50))}>上一页</button><button disabled={busy || !list.data || offset + 50 >= Number(list.data.total)} onClick={() => setOffset((n) => n + 50)}>下一页</button></div>
    </Panel>
  </div>;
}
