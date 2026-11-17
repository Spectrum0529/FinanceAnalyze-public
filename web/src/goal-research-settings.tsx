import { useRef, useState } from 'react';
import { ApiError } from './api';
import type { RecordData } from './api';
import { useResearchInputs, ResearchInputPages, researchTeamMembers } from './autonomous-inputs';
import { useResource } from './components';
import { record } from './native-research-api';
import { useAutonomousIntent } from './autonomous-request-intent';
import { goalResearchRoles, saveGoalSettings, settingsCommand } from './goal-research-api';
import type { GoalDefaults, ProfileReference, SettingsCommand } from './goal-research-api';

const profileKey = (ref: ProfileReference | null) => ref ? JSON.stringify([ref.profile_id, ref.revision]) : '';
const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
const roleLabels = { hypothesis: '提出思路', coder: '编写策略或因子', critic: '检查结果', reporter: '整理报告' };
const presetLabels: Record<string, string> = { balanced: '均衡', deep: '深度研究', extended: '长时研究' };
const positiveCount = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value > 0;

function budgetDuration(seconds: unknown) {
  if (!positiveCount(seconds)) return '';
  if (seconds % 3600 === 0) return ` · 最长 ${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return ` · 最长 ${seconds / 60} 分钟`;
  return ` · 最长 ${seconds} 秒`;
}

function TeamRoleSettings({ snapshotId, refresh, mapping, change }: {
  snapshotId: string; refresh: number; mapping: NonNullable<GoalDefaults['team_role_map']>;
  change: (mapping: NonNullable<GoalDefaults['team_role_map']>) => void;
}) {
  const snapshot = useResource<RecordData>(`/agent-teams/snapshots/${encodeURIComponent(snapshotId)}`, refresh);
  const members = researchTeamMembers(snapshot.data?.snapshot_id === snapshotId ? snapshot.data : undefined)
    .filter((member) => member.slot !== 'default');
  return <details open={Object.keys(mapping).length > 0}>
    <summary>给团队成员分配任务</summary>
    <p>留空时用团队默认模型；选了成员后，用该成员保存的模型版本。</p>
    {snapshot.error != null && <p>暂时读不到所选团队版本。</p>}
    {!snapshot.loading && !members.length && <p>这个团队版本没有可选的成员模型。</p>}
    {goalResearchRoles.map((role) => <label key={role}>{roleLabels[role]}
      <select value={mapping[role] ?? ''} disabled={snapshot.loading || snapshot.error != null} onChange={(event) => {
        const next = { ...mapping };
        if (event.target.value) next[role] = event.target.value;
        else delete next[role];
        change(next);
      }}>
        <option value="">使用团队默认模型</option>
        {mapping[role] && !members.some((member) => member.slot === mapping[role]) &&
          <option value={mapping[role]}>当前保存的成员</option>}
        {members.map((member) => <option key={member.slot} value={member.slot}>{member.label} · 版本 {member.revision}</option>)}
      </select>
    </label>)}
  </details>;
}

export function GoalSettingsForm({ userId, revision, defaults, readiness, refresh, selectedTeam, changed }: {
  userId: string; revision: number; defaults: GoalDefaults; readiness: unknown; refresh: number;
  selectedTeam?: string; changed: () => void;
}) {
  const profiles = useResearchInputs('/model-profiles', 50, refresh);
  const connections = useResource<RecordData>('/data/library/connections', refresh);
  const [draft, setDraft] = useState<GoalDefaults>(() => selectedTeam && id(selectedTeam)
    ? { ...defaults, team_snapshot_id: selectedTeam, model_profile: null,
        team_role_map: selectedTeam === defaults.team_snapshot_id ? defaults.team_role_map : {} } : defaults);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const gate = useRef(false);
  const journal = useAutonomousIntent(userId, 'goal-defaults', settingsCommand);
  const pending = journal.current.current;
  const config = record(readiness);
  const presets = Array.isArray(config.budget_presets) ? config.budget_presets.map(record).filter((row) => id(row.id)) : [];
  const budget = presets.find((preset) => preset.id === draft.budget_preset_id);
  const roundLimits = [
    positiveCount(budget?.compute_seconds_per_round) && '每轮计算上限 ' + budget.compute_seconds_per_round + ' 秒',
    positiveCount(budget?.max_prefix_workload) && '每轮最多处理 ' + budget.max_prefix_workload + ' 行交易日数据',
  ].filter(Boolean);
  const sources = Array.isArray(connections.data?.items) ? connections.data.items.map(record)
    .filter((row) => id(row.connection_id) && row.provider !== 'csv') : [];
  const refs = profiles.items.flatMap((profile) => id(profile.profile_id) && Number.isSafeInteger(profile.revision)
    ? [{ profile_id: profile.profile_id, revision: Number(profile.revision), name: String(profile.name ?? profile.profile_id) }]
    : []);
  const choice = (key: string) => {
    const selected = refs.find((ref) => profileKey(ref) === key);
    return selected ? { profile_id: selected.profile_id, revision: selected.revision } : null;
  };

  async function save(command: SettingsCommand, replay = false) {
    if (gate.current || journal.error || journal.current.current && !replay) return;
    gate.current = true;
    setBusy(true);
    setError('');
    try {
      if (!replay) journal.save(command);
      const saved = await saveGoalSettings(command);
      if (saved.revision !== command.expected_revision + 1) throw Error('暂时无法确认设置是否已保存，请刷新后查看。');
      journal.clear();
      changed();
    } catch (error) {
      if (error instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(error.status)
          && !['IDEMPOTENCY_CONFLICT', 'INVALID_RESPONSE'].includes(error.code)) {
        journal.clear();
        setError('保存未完成，请核对当前配置。');
      } else {
        setError('还不能确认设置是否已保存。请点击下方按钮查看。');
      }
    } finally {
      gate.current = false;
      setBusy(false);
    }
  }

  const locked = busy || !!pending || !!journal.error;
  return (
    <form onSubmit={(event) => {
      event.preventDefault();
      void save({ defaults: draft, expected_revision: revision, idempotency_key: crypto.randomUUID() });
    }}>
      <fieldset disabled={locked}>
        <legend>保存目标研究默认设置</legend>
        <label>研究模型<select value={profileKey(draft.model_profile)} onChange={(event) => {
          const model = choice(event.target.value);
          setDraft({ ...draft, model_profile: model, team_snapshot_id: model ? null : draft.team_snapshot_id,
            team_role_map: model ? {} : draft.team_role_map });
        }}>
          <option value="">{draft.team_snapshot_id ? '使用团队默认模型' : '请选择保存的模型'}</option>
          {draft.model_profile && !refs.some((row) => profileKey(row) === profileKey(draft.model_profile)) &&
            <option value={profileKey(draft.model_profile)}>当前保存模型 · 版本 {draft.model_profile.revision}</option>}
          {refs.map((ref) => <option key={profileKey(ref)} value={profileKey(ref)}>{ref.name} · 版本 {ref.revision}</option>)}
        </select></label>
        {draft.team_snapshot_id && <>
          <p>已选定一个团队版本。<button type="button" onClick={() => setDraft({ ...draft, team_snapshot_id: null, team_role_map: {} })}>移除团队</button></p>
          <TeamRoleSettings snapshotId={draft.team_snapshot_id} refresh={refresh} mapping={draft.team_role_map ?? {}}
            change={(team_role_map) => setDraft({ ...draft, team_role_map })}/>
        </>}
        <p><a href="#intelligent?research_tab=teams">去“我的团队”选择一个已保存的团队</a></p>
        <label>制定研究计划的模型<select value={profileKey(draft.planner)} onChange={(event) => setDraft({ ...draft, planner: choice(event.target.value) })}>
          <option value="">沿用研究模型或团队默认模型</option>
          {draft.planner && !refs.some((row) => profileKey(row) === profileKey(draft.planner)) &&
            <option value={profileKey(draft.planner)}>当前规划模型 · 版本 {draft.planner.revision}</option>}
          {refs.map((ref) => <option key={profileKey(ref)} value={profileKey(ref)}>{ref.name} · 版本 {ref.revision}</option>)}
        </select></label>
        <ResearchInputPages state={profiles} noun="模型配置" />
        <div>
          <p>可选最多 8 个数据来源，按勾选顺序尝试</p>
          {connections.error != null && <p>暂时读不到已保存的数据来源。</p>}
          {sources.length === 0 && <p>请先在数据管理添加来源。</p>}
          {sources.map((row) => {
            const source = String(row.connection_id);
            const order = draft.connection_ids.indexOf(source);
            return <label key={source}><input type="checkbox" checked={order !== -1}
              disabled={order === -1 && draft.connection_ids.length >= 8} onChange={(event) => setDraft({
                ...draft, connection_ids: event.target.checked ? [...draft.connection_ids, source] : draft.connection_ids.filter((value) => value !== source),
              })} />{String(row.name ?? row.provider)}{order !== -1 && ' · 优先顺序 ' + (order + 1)}</label>;
          })}
          {draft.connection_ids.filter((source) => !sources.some((row) => row.connection_id === source)).map((source) =>
            <button key={source} type="button" onClick={() => setDraft({ ...draft, connection_ids: draft.connection_ids.filter((id) => id !== source) })}>移除暂时读不到的来源</button>)}
        </div>
        <label>研究额度<select value={draft.budget_preset_id ?? ''} onChange={(event) => setDraft({ ...draft, budget_preset_id: event.target.value || null })}>
          <option value="">请选择预算</option>
          {draft.budget_preset_id && !presets.some((row) => row.id === draft.budget_preset_id) &&
            <option value={draft.budget_preset_id}>已保存的预设</option>}
          {presets.map((preset) => <option key={String(preset.id)} value={String(preset.id)}>
            {Object.hasOwn(presetLabels, String(preset.id)) ? presetLabels[String(preset.id)] : String(preset.id)}
            {budgetDuration(preset.deadline_seconds)}
          </option>)}
        </select></label>
        {positiveCount(budget?.research_attempts_per_generation) && <p>每个数据版本最多研究 {budget.research_attempts_per_generation} 轮，最多准备两个版本。</p>}
        {roundLimits.length > 0 && <p>{roundLimits.join('，')}。</p>}
        {budget?.cost_policy === 'COUNTERS_ONLY' && <p>可以限制模型用量，实际费用由厂商收取。</p>}
        <button type="submit">保存默认设置</button>
      </fieldset>
      {pending && <button type="button" disabled={busy || !!journal.error} onClick={() => void save(pending, true)}>确认上次设置是否已保存</button>}
      {(journal.error || error) && <p role="alert">{journal.error?.message || error}</p>}
    </form>
  );
}
