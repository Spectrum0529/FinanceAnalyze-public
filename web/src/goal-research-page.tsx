import { useEffect, useRef, useState } from 'react';
import { ApiError } from './api';
import { useResource } from './components';
import { observeResource } from './resource-reader';
import { useAutonomousIntent } from './autonomous-request-intent';
import { goalCommand, goalDefaults, goalPath, goalProviders, goalReadiness, goalSettings, goalTask, goalTaskLink, goalTaskPath, sendGoal } from './goal-research-api';
import type { GoalCommand } from './goal-research-api';
import { GoalResearchView } from './goal-research-workspace';
import type { GoalReadiness, GoalTaskView } from './goal-research-workspace';
import { GoalSettingsForm } from './goal-research-settings';

const pageSize = 20;
const currentTask = () => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('goal_task') ?? '';
const currentTeam = () => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('goal_team') ?? undefined;
const unavailable: GoalReadiness = { status: 'unavailable', missing: [] };

function TaskList({ refresh }: { refresh: number }) {
  const [offset, setOffset] = useState(0);
  const state = useResource<{ items?: unknown }>(goalPath + '?limit=' + pageSize + '&offset=' + offset, refresh);
  const items = Array.isArray(state.data?.items) ? state.data.items : [];
  const tasks = items.flatMap((item) => {
    try { return [goalTask(item)]; } catch { return []; }
  });
  return (
    <details className="panel">
      <summary>已保存的目标研究</summary>
      {state.error != null ? <p>暂时读不到任务列表，请刷新后再试。</p> : state.loading ? <p>正在读取任务列表…</p>
        : tasks.length ? <ul>{tasks.map((task) => <li key={task.id}>
          <a href={goalTaskLink(task.id)}>{task.goal}</a> · {task.status}
        </li>)}</ul> : <p>这一页没有已保存的任务。</p>}
      <div className="form-actions">
        <button type="button" disabled={offset === 0 || state.loading} onClick={() => setOffset(offset - pageSize)}>上一页</button>
        <button type="button" disabled={items.length !== pageSize || offset >= 10000 || state.loading}
          onClick={() => setOffset(offset + pageSize)}>下一页</button>
      </div>
    </details>
  );
}

export function GoalResearchPage({ userId, canWrite }: { userId: string; canWrite: boolean }) {
  const [id, setId] = useState(currentTask);
  const [team, setTeam] = useState(currentTeam);
  const [task, setTask] = useState<GoalTaskView>();
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [commandError, setCommandError] = useState('');
  const [readError, setReadError] = useState('');
  const confirmedTerminal = useRef<{ id: string; refresh: number } | null>(null);
  const gate = useRef(false);
  const journal = useAutonomousIntent(userId, 'goal-research', goalCommand);
  const config = useResource<unknown>(goalPath + '/readiness', refresh);
  const providers = useResource<unknown>('/data/library/providers', refresh);
  let readiness = unavailable;
  let settings: ReturnType<typeof goalSettings> | undefined;
  if (config.loading) readiness = { status: 'loading', missing: [] };
  if (config.data) {
    try { readiness = goalReadiness(config.data); settings = goalSettings(config.data); } catch { readiness = unavailable; }
  }

  useEffect(() => {
    const changed = () => {
      const query = new URLSearchParams(window.location.hash.split('?')[1] ?? '');
      const tab = query.get('research_tab');
      const route = window.location.hash.split('?')[0];
      const otherTab = route === '#agent-teams' || tab && tab !== 'goal' ||
        ['task_team_snapshot_id', 'agent_team_snapshot_id', 'debate_job', 'qlib_job'].some((key) => query.has(key));
      if (query.has('autonomous_session') || otherTab && !query.has('goal_task') && !query.has('goal_team')) return;
      setId(query.get('goal_task') ?? '');
      setTeam(query.get('goal_team') ?? undefined);
    };
    changed();
    window.addEventListener('hashchange', changed);
    return () => window.removeEventListener('hashchange', changed);
  }, []);

  useEffect(() => {
    const previous = confirmedTerminal.current;
    if (!id || (previous?.id === id && previous.refresh === refresh)) return;
    confirmedTerminal.current = null;
    function publish(value: unknown) {
      try {
        const next = goalTask(value, id);
        if (next.terminal) confirmedTerminal.current = { id, refresh };
        setTask((previous) => previous?.id === id && previous.revision > next.revision ? previous : next);
        setReadError('');
      } catch {
        setReadError('无法确认返回的是本次任务。请刷新后查看。');
      }
    }
    return observeResource(goalTaskPath(id), {
      begin: () => setReadError(''),
      data: publish,
      error: () => setReadError('暂时读不到任务状态，请刷新后再试。'),
      settled: () => {},
    }, 3000, (value) => {
      try { return goalTask(value, id).terminal; } catch { return false; }
    });
  }, [id, refresh]);

  function open(next: GoalTaskView) {
    setTask((previous) => previous?.id === next.id && previous.revision > next.revision ? previous : next);
    setId(next.id);
    window.location.hash = goalTaskLink(next.id);
    setRefresh((value) => value + 1);
  }

  async function submit(command: GoalCommand, replay = false) {
    if (!canWrite || gate.current || journal.error || (!replay && journal.current.current)) return;
    gate.current = true;
    setBusy(true);
    setCommandError('');
    try {
      if (!replay) journal.save(command);
      const next = await sendGoal(command);
      journal.clear();
      open(next);
    } catch (error) {
      if (error instanceof ApiError && [400, 401, 403, 404, 409, 422].includes(error.status)
        && error.code !== 'IDEMPOTENCY_CONFLICT' && error.code !== 'INVALID_RESPONSE') {
        journal.clear();
        setRefresh((value) => value + 1);
        setCommandError('服务未接受本次操作，请核对当前任务或配置。' +
          (/^[A-Z][A-Z0-9_]{0,63}$/.test(error.code) ? ' ' + error.code : ''));
      } else {
        setCommandError('还不能确认上次操作结果，请点击下方按钮核对。');
      }
    } finally {
      gate.current = false;
      setBusy(false);
    }
  }

  const selected = task?.id === id ? task : undefined;
  const pending = journal.current.current;
  return (
    <>
      {pending && <div className="message notice">
        <p>还不能确认上一次操作是否成功。</p>
        <button type="button" disabled={!canWrite || busy || !!journal.error}
          onClick={() => void submit(pending, true)}>确认上次操作结果</button>
      </div>}
      <GoalResearchView canWrite={canWrite} readiness={readiness} defaults={goalDefaults(config.data)} settingsOpen={!!team}
        providers={providers.data ? goalProviders(providers.data) : undefined} task={selected}
        settings={settings && canWrite && <GoalSettingsForm key={settings.revision + ':' + (team ?? '')}
          userId={userId} revision={settings.revision} defaults={settings.defaults} readiness={config.data}
          refresh={refresh} selectedTeam={team} changed={() => {
            if (team) {
              setTeam(undefined);
              window.history.replaceState(null, '', id ? goalTaskLink(id) : '#intelligent?research_tab=goal');
            }
            setRefresh((value) => value + 1);
          }} />}
        error={journal.error?.message || commandError || (id ? readError : '')}
        busy={busy || !!pending || !!journal.error}
        onStart={(goal) => void submit({ kind: 'create', goal, idempotency_key: crypto.randomUUID() })}
        onAction={(action) => {
          if (selected?.allowedActions.includes(action)) {
            void submit({ kind: 'control', id: selected.id, action, expected_revision: selected.revision, idempotency_key: crypto.randomUUID() });
          }
        }}
        onRefresh={() => setRefresh((value) => value + 1)} />
      <TaskList refresh={refresh} />
    </>
  );
}
