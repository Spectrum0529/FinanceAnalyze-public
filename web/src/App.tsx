import { lazy, Suspense, useEffect, useState } from 'react';
import { PageBoundary } from './page-boundary';
import { ApiError, post, request, setCsrf } from './api';
import { ActionForm, ErrorBox, Notice } from './components';
import { WorkspaceSelection } from './workspace-selection';
import { StartPage } from './start-page';
import { localSessionFor } from './local-personal-session';
const DataLibraryWorkspace = lazy(() => import('./data-library-workspace').then((m) => ({ default: m.DataLibraryWorkspace }))); 
const DatasetResearchWorkspace = lazy(() => import('./dataset-research-workspace').then((m) => ({ default: m.DatasetResearchWorkspace })));
const Strategies = lazy(() => import('./strategy-page').then((m) => ({ default: m.Strategies })));
const Research = lazy(() => import('./research-page').then((m) => ({ default: m.Research })));
const AiResearchWorkspace = lazy(() => import('./ai-research-workspace').then((m) => ({ default: m.AiResearchWorkspace }))); 
const UnifiedResultsWorkspace = lazy(() => import('./unified-results-workspace').then((m) => ({ default: m.UnifiedResultsWorkspace }))); 
const Models = lazy(() => import('./research-page').then((m) => ({ default: m.Models })));
const Accounts = lazy(() => import('./trading-page').then((m) => ({ default: m.Accounts })));
const Simulation = lazy(() => import('./trading-page').then((m) => ({ default: m.Simulation })));
const Jobs = lazy(() => import('./pages').then((m) => ({ default: m.Jobs })));

const Settings = lazy(() => import('./settings-page').then((m) => ({ default: m.Settings })));
const Operations = lazy(() => import('./settings-page').then((m) => ({ default: m.Operations })));
type User = { user_id: string; username: string; role: string };
type Session = { user: User; csrf_token: string };
const navigation = [
  ['overview', '开始', '◈', '一步一步，完成一次回测'],
  ['results', '查看结果', '▤', '已保存的回测报告'],
  ['intelligent', 'AI研究', '◇', '模板研究与多方讨论'],
  ['my-results', '我的结果', '▤', '已保存研究、数据与回测'],
  ['data', '管理数据', '▤', '准备回测所需数据'],
  ['strategies', '策略中心', '◇', '包、参数与版本'],
  ['research', '开始回测', '⌁', '选择数据集、策略和资金成本'],
  ['models', '模型与产物', '⬡', '训练截止与血缘'],
  ['accounts', '组合与账户', '▣', '现金、风险与对账'],
  ['simulation', '模拟交易', '▷', '运行与恢复'],
  ['jobs', '任务与调度', '≡', '任务排队与重试'],
  ['agent-teams', '模型与团队', '◇', '连接、Agent 与 Prompt 模板'],
  ['settings', '系统设置', '⚙', '连接、角色与诊断'],
  ['operations', '审计与运维', '◎', '健康、备份与告警'],
] as const;
export function App() {
  const [localPersonal, setLocalPersonal] = useState(false);
  const [session, setSession] = useState<Session | null>(null),
    [initialized, setInitialized] = useState<boolean | null>(null),
    [error, setError] = useState<unknown>(null),
    [loading, setLoading] = useState(true);
  const [page, setPage] = useState(window.location.hash.slice(1).split('?')[0] || 'research');
  async function discover() {
    setLoading(true);
    setError(null);
    try {
      const state = await request<{ initialized: boolean; access_mode?: string; interactive_login_required?: boolean }>('/auth/status');
      setLocalPersonal(state.access_mode === 'local-personal');
      if (state.access_mode === 'local-personal' && state.interactive_login_required === false) {
        const current = (await localSessionFor(state))!;
        setSession(current); setCsrf(current.csrf_token); setInitialized(true); return;
      }
      setInitialized(state.initialized);
      if (state.initialized) {
        try {
          const s = await request<Session>('/auth/me');
          setSession(s);
          setCsrf(s.csrf_token);
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 401)) throw e;
          setSession(null);
        }
      }
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void discover();
    const onHash = () => setPage(window.location.hash.slice(1).split('?')[0] || 'research');
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  async function authenticate(path: string, values: Record<string, string>) {
    if (path === '/auth/initialize') {
      await post(path, values);
      const s = await post<Session>('/auth/login', {
        username: values.username,
        password: values.password,
      });
      setSession(s);
      setInitialized(true);
      setCsrf(s.csrf_token);
      return { username: s.user.username, role: s.user.role };
    }
    const s = await post<Session>(path, values);
    setSession(s);
    setInitialized(true);
    setCsrf(s.csrf_token);
    return { username: s.user.username, role: s.user.role };
  }
  if (loading) return <Notice>正在打开研究工作区…</Notice>;
  if (!session && localPersonal) return <Notice>个人本地会话未就绪。{error != null && <ErrorBox error={error} retry={() => void discover()} />}</Notice>;
  if (!session)
    return (
      <div className="auth-layout">
        <div className="auth-intro">
          <div className="brand">
            <span className="brand-mark">FA</span>
            <span>
              FinanceAnalyze<small>研究控制台</small>
            </span>
          </div>
          <div>
            <span className="eyebrow">RESEARCH WORKSPACE</span>
            <h1>
              让研究过程
              <br />
              可复现，可追溯。
            </h1>
            <p>
              数据、策略、实验与模拟账户，
              <br />
              在同一处管理。
            </p>
            <div className="auth-chips">
              <span>Windows native</span>
              <span>后台任务</span>
              <span>中文控制台</span>
            </div>
          </div>
          <small>仅本地开发与模拟 · 真实交易关闭</small>
        </div>
        <main className="auth-main">
          <div className="auth-card">
            <span className="eyebrow">安全访问</span>
            <h2>{initialized === false ? '建立首个管理员' : '登录研究工作区'}</h2>
            <p className="muted">
              {initialized === false
                ? '账户信息由服务保存。请设置自己的用户名与强密码。'
                : '使用本地平台账户登录，权限由后端验证。'}
            </p>
            {loading ? (
              <Notice>正在检查平台初始化状态…</Notice>
            ) : error ? (
              <ErrorBox error={error} retry={() => void discover()} />
            ) : (
              <ActionForm
                title={initialized ? '账户登录' : '初次设置'}
                fields={[
                  ...(!initialized
                    ? [
                        {
                          key: 'setup_token',
                          label: '一次性初始化令牌',
                          type: 'password' as const,
                          help: '由本地 CLI 生成，有效期内仅使用一次。敏感信息，不进入日志或截图。',
                        },
                      ]
                    : []),
                  {
                    key: 'username',
                    label: '用户名',
                    help: '本地平台账号。无需第三方账号，不使用通用默认密码。',
                  },
                  {
                    key: 'password',
                    label: '密码',
                    type: 'password',
                    help: '用于平台认证；提交后不回显，不保存到浏览器存储。建议至少 12 位。',
                  },
                ]}
                submitLabel={initialized ? '登录' : '创建管理员并进入'}
                onSubmit={(values) =>
                  authenticate(initialized ? '/auth/login' : '/auth/initialize', values)
                }
              />
            )}
            <Notice>模拟练习数据不是真实市场行情。回测供本地研究使用，不代表投资收益。</Notice>
          </div>
        </main>
      </div>
    );
  const navPage = page === 'agent-teams' ? 'intelligent' : page === 'overview' ? 'research' : page;
  const active = navigation.find((n) => n[0] === navPage) || navigation[0];
  const canWrite = session.user.role !== 'viewer',
    isAdmin = session.user.role === 'admin';
  const screens: Record<string, React.ReactNode> = {
    overview: <StartPage canWrite={canWrite} userId={session.user.user_id} />,
    results: <Research canWrite={canWrite} userId={session.user.user_id} resultsOnly />,
    intelligent: <AiResearchWorkspace canWrite={canWrite} userId={session.user.user_id} />,
    'my-results': <UnifiedResultsWorkspace userId={session.user.user_id} />,
    data: <DataLibraryWorkspace canWrite={canWrite} userId={session.user.user_id} />,
    strategies: <Strategies canWrite={canWrite} />,
    research: <DatasetResearchWorkspace canWrite={canWrite} userId={session.user.user_id} backtest />,
    models: <Models canWrite={canWrite} />,
    accounts: <Accounts canWrite={canWrite} />,
    simulation: <Simulation canWrite={canWrite} />,
    jobs: <Jobs canWrite={canWrite} />,
    'agent-teams': <AiResearchWorkspace initialTab="teams" canWrite={canWrite} userId={session.user.user_id} />,
    settings: <Settings isAdmin={isAdmin} storageCanWrite={localPersonal||isAdmin} cacheCanWrite={canWrite} storageOwner={session.user.user_id} />,
    operations: <Operations isAdmin={isAdmin} />,
  };
  return (
    <div className="workspace">
      <aside className="sidebar">
        <a className="brand" href="#research">
          <span className="brand-mark">FA</span>
          <span>
            FinanceAnalyze<small>研究控制台</small>
          </span>
        </a>
        <div className="workspace-label">个人研究工作区</div>
        <nav aria-label="主导航">
          {['research', 'intelligent', 'data', 'my-results'].map((id) => {
            const n = navigation.find((item) => item[0] === id)!;
            return (
              <a
                key={id}
                aria-current={navPage === id ? 'page' : undefined}
                className={navPage === id ? 'active' : ''}
                href={`#${id}`}
              >
                <span aria-hidden="true">{n[2]}</span>
                {n[1]}
              </a>
            );
          })}
          <details
            className="more-tools"
            key={page}
            open={!['overview', 'research', 'intelligent', 'my-results', 'data', 'agent-teams'].includes(page)}
          >
            <summary>更多工具</summary>
            {navigation
              .filter((n) => !['overview', 'research', 'intelligent', 'my-results', 'data', 'agent-teams'].includes(n[0]))
              .map((n) => (
                <a
                  key={n[0]}
                  aria-current={page === n[0] ? 'page' : undefined}
                  className={page === n[0] ? 'active' : ''}
                  href={`#${n[0]}`}
                >
                  {n[1]}
                </a>
              ))}
          </details>
        </nav>
        <div className="sidebar-footer">
          <StatusMode />
          <small>
            开发研究 · USD 现金账户
            <br />
            实盘通道保持关闭
          </small>
        </div>
      </aside>
      <div className="main-column">
        <header className="topbar">
          <div>
            <span className="workspace-name">研究 / {active[1]}</span>
            <span className="topbar-context">本地平台</span>
          </div>
          <div className="user-menu">
            <span>
              {session.user.username} <small>{session.user.role}</small>
            </span>
            {!localPersonal && <button
              className="text-button"
              onClick={async () => {
                try {
                  await post('/auth/logout');
                  setSession(null);
                  setCsrf('');
                } catch (e) {
                  setError(e);
                }
              }}
            >
              退出
            </button>}
          </div>
        </header>
        <main className="content">
          <div className="page-heading">
            <div>
              <span className="eyebrow">{active[3]}</span>
              <h1>{active[1]}</h1>
            </div>
            <StatusMode />
          </div>
          <div className="boundary-strip">
            <strong>本地研究 · 真实交易关闭</strong>
            <span>数据来源以具体记录为准；模拟数据会单独标记，回测结果不代表未来收益。</span>
          </div>
          {error != null && <ErrorBox error={error} retry={() => setError(null)} />}
          {!canWrite && (
            <Notice>当前为只读角色。可浏览获准记录；创建与运行操作需研究者或管理员权限。</Notice>
          )}
          <WorkspaceSelection key={session.user.user_id}>
            <PageBoundary key={active[0]}>
              <Suspense fallback={<Notice>正在加载页面…</Notice>}>{screens[active[0]]}</Suspense>
            </PageBoundary>
          </WorkspaceSelection>
        </main>
        <footer className="footer">
          FinanceAnalyze · 本地研究工作区{' '}
          <details>
            <summary>研究与验证范围</summary>S02.06 blocked / UNVERIFIED；正式 S03 与 LIVE
            未授权。模拟演示仅验证工程能力。
          </details>
        </footer>
      </div>
    </div>
  );
}
function StatusMode() {
  return (
    <span className="mode-badge">
      <span />
      本地工作区 · 非实盘
    </span>
  );
}
