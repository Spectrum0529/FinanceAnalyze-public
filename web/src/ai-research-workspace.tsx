import { Activity, lazy, Suspense } from 'react';
const AutonomousResearchWorkspace=lazy(()=>import('./autonomous-research-workspace').then(m=>({default:m.AutonomousResearchWorkspace})));
const GoalResearchPage=lazy(()=>import('./goal-research-page').then(m=>({default:m.GoalResearchPage})));
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { Panel, Notice } from './components';
import { IntelligentResearch } from './native-research-page';
import type { RecordData } from './api';
import { DatasetResearchWorkspace } from './dataset-research-workspace';
import { McpConnections } from './mcp-connections';
import { ModelProfileLibrary } from './model-profile-library';
import { AgentTeams } from './agent-team-page';
import { AreaHelp } from './area-help';
import type { AccountReauthentication } from './platform-account-session';

const researchTabs = ['goal', 'start', 'teams', 'models', 'external', 'autonomous'];
function requestedTab(query: URLSearchParams) {
  if (query.has('autonomous_session')) return 'autonomous';
  if (query.has('goal_task') || query.has('goal_team')) return 'goal';
  const requested = query.get('research_tab');
  if (requested && researchTabs.includes(requested)) return requested;
  if (['task_team_snapshot_id', 'agent_team_snapshot_id', 'debate_job', 'qlib_job'].some((key) => query.has(key))) return 'start';
}

function researchLocation(hash: string, initialTab: string) {
  const [route, search] = hash.split('?');
  const query = new URLSearchParams(search);
  return {
    tab: requestedTab(query) ?? (route === '#agent-teams' ? 'teams' : initialTab === 'start' ? 'goal' : initialTab),
    snapshot: query.get('task_team_snapshot_id') ?? '',
    team: ['agent_team_snapshot_id', 'debate_job', 'qlib_job'].some((key) => query.has(key)),
  };
}

export function AiResearchWorkspace({ canWrite, userId, initialTab = 'start', accountMode = false, reauthenticate, motion }: {
  canWrite: boolean; userId: string; initialTab?: string; accountMode?: boolean;
  reauthenticate?: AccountReauthentication; motion?: () => boolean;
}) {
  const [linked, setLinked] = useState(() => researchLocation(window.location.hash, initialTab));
  const [mode, setMode] = useState(linked.team ? 'team' : 'data'), [composed, setComposed] = useState<RecordData | null>(null);
  const [tab, setTab] = useState(linked.tab);
  const [openedGoal, setOpenedGoal] = useState(linked.tab === 'goal');
  const departure = useRef<{ from: string; to: string; link: HTMLAnchorElement } | null>(null);
  const returnFocus = useRef<HTMLAnchorElement | null>(null);

  function rememberGoalLink(event: MouseEvent<HTMLDivElement>) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target instanceof Element
      ? event.target.closest<HTMLAnchorElement>('.goal-research-task a[href^="#intelligent?autonomous_session="]') : null;
    if (link) departure.current = { from: window.location.hash, to: link.hash, link };
  }

  const selectTab = (next: string) => {
    departure.current = null;
    if (next === 'goal') setOpenedGoal(true);
    setTab(next);
  };
  useEffect(() => {
    const changed = (event: HashChangeEvent) => {
      const here = window.location.hash;
      const previous = departure.current;
      if (previous && here !== previous.to) {
        if (here === previous.from && new URL(event.oldURL).hash === previous.to) {
          returnFocus.current = previous.link;
        }
        departure.current = null;
      }
      const next = researchLocation(window.location.hash, initialTab);
      setLinked(next);
      if (next.tab === 'goal') setOpenedGoal(true);
      setTab(next.tab);
      if (next.tab === 'start') setMode(next.team && !next.snapshot ? 'team' : 'data');
    };
    window.addEventListener('hashchange', changed); return () => window.removeEventListener('hashchange', changed);
  }, [initialTab]);
  useLayoutEffect(() => {
    if (tab !== 'goal') return;
    const link = returnFocus.current;
    returnFocus.current = null;
    if (link?.isConnected && document.activeElement === document.body) {
      link.focus({ preventScroll: true });
    }
  }, [tab, linked]);
  return <>
    <div className="v434-ai-toolbar"><div role="tablist" aria-label="AI 研究方式">{[['goal', '按目标研究'], ['start', '自己选研究方式'], ['teams', '我的团队'], ['models', '模型配置'], ['external', '连接外部 AI'], ['autonomous', '自主研究']].map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => selectTab(id)} className={tab === id ? 'primary' : 'secondary'}>{label}</button>)}</div><AreaHelp title="AI 研究" topic="ai-research-choices" /></div>
    {openedGoal && <Activity mode={tab === 'goal' ? 'visible' : 'hidden'}>
      <Suspense fallback={<Notice>正在读取目标研究页面…</Notice>}>
        <div onClick={rememberGoalLink}><GoalResearchPage key={userId} userId={userId} canWrite={canWrite}/></div>
      </Suspense>
    </Activity>}
    {tab === 'goal' ? null : tab === 'autonomous' ? <Suspense fallback={<Notice>正在读取自主研究页面…</Notice>}><AutonomousResearchWorkspace userId={userId} canWrite={canWrite}/></Suspense> : tab === 'external' ? <McpConnections accountMode={accountMode} canWrite={canWrite} reauthenticate={reauthenticate} motion={motion} /> : tab === 'start' ? <>
      <div className="v434-workflow-heading"><span>{mode === 'data' ? '用已保存的数据做研究' : '多角色讨论与模型训练'}</span>{mode !== 'data' && <button className="text-button" onClick={() => setMode('data')}>返回数据研究</button>}<details><summary>更多研究工具</summary><button onClick={() => setMode('team')}>打开多角色讨论与模型训练</button></details></div>
      {mode === 'data' ? <DatasetResearchWorkspace key={linked.snapshot || 'task'} initialTeamSnapshotId={linked.snapshot || undefined} canWrite={canWrite} userId={userId} /> : <IntelligentResearch canWrite={canWrite} userId={userId} />}
    </> : tab === 'models' ? <ModelProfileLibrary canWrite={canWrite} userId={userId} onComposed={(configuration) => { setComposed(configuration); setTab('teams'); }} /> : <>
      <Panel title="配置研究团队" helpTopic="teams"><Notice>给团队角色选择模型，可以让同一个模型承担多个角色。</Notice></Panel>
      <AgentTeams key={composed ? JSON.stringify(composed.profile_refs) : tab} canWrite={canWrite} userId={userId} initialConfiguration={composed ?? undefined} />
    </>}
  </>;
}
