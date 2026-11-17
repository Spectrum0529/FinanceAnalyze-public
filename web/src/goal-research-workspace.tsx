import { useId, useState } from 'react';
import type { FormEvent } from 'react';
import type { ReactNode } from 'react';
import { AreaHelp } from './area-help';
import { GoalModelUsage } from './goal-model-usage';
import { GoalProviderSettings } from './goal-research-provider-settings';
import type { GoalProvider } from './goal-research-provider-settings';
import { ResultPreviewLink } from './result-preview';
import './goal-research.css';

const stages = {
  understand: '理解目标',
  find: '查找已有数据',
  fill: '准备数据',
  start: '开始研究',
  review: '检查研究结果',
  results: '查看结果',
};
const stageLabels = { active: '进行中', done: '已完成', ended: '已结束', blocked: '待处理', skipped: '已跳过', failed: '未完成', queued: '已排队' };
const actionLabels = { pause: '暂停任务', cancel: '取消任务', resume: '继续任务' };
const settingsLinks = { models: '#intelligent?research_tab=models', teams: '#intelligent?research_tab=teams', data: '#data' };

type Missing = { label: string; target?: keyof typeof settingsLinks; code?: string };
type Stage = { id: keyof typeof stages; state: keyof typeof stageLabels; detail?: string };
type Budget = { label: string; unit?: string; limit?: number; spent?: number; remaining?: number };
type Dataset = {
  id: string;
  name: string;
  round?: string;
  version?: string;
  source?: string;
  market?: string;
  kind?: string;
  frequency?: string;
  adjustment?: string;
  currency?: string;
  symbols?: string[];
  start?: string;
  end?: string;
  hash?: string;
};
type SavedReport = { sessionId: string; attemptId: string };
export type GoalAction = keyof typeof actionLabels;
export type GoalTaskView = {
  id: string;
  goal: string;
  status: string;
  revision: number;
  terminal: boolean;
  updatedAt?: string;
  stages: Stage[];
  allowedActions: GoalAction[];
  failure?: string;
  reasonCode?: string;
  deadline?: number;
  billingUnknown?: boolean;
  missing?: Missing[];
  budgets?: Budget[];
  datasets?: Dataset[];
  requirements?: Omit<Dataset, 'id' | 'name'>[];
  downloadJobId?: string;
  childSessionId?: string;
  previousSessionId?: string;
  additionalDataRequested?: boolean;
  extensionReason?: string;
  noNewData?: boolean;
  previousResult?: SavedReport;
  result?: SavedReport;
};
export type GoalReadiness = { status: 'loading' | 'ready' | 'blocked' | 'unavailable'; missing: Missing[]; canCreate?: boolean };

function MissingItems({ items }: { items: Missing[] }) {
  if (items.length === 0) return null;
  return (
    <ul>
      {items.map((item) => (
        <li key={item.code ?? item.label}>
          {item.target ? <a href={settingsLinks[item.target]}>{item.label}</a> : item.label}
        </li>
      ))}
    </ul>
  );
}

const childLink = (id: string) => '#intelligent?autonomous_session=' + encodeURIComponent(id);

function SavedResult({ report, label }: { report: SavedReport; label: string }) {
  return <div><ResultPreviewLink resultId={`autonomous:${report.attemptId}`} title={label}>预览{label}</ResultPreviewLink></div>;
}

function TaskProgress({ task, busy, canWrite, onAction }: {
  task: GoalTaskView; busy: boolean; canWrite: boolean; onAction: (action: GoalAction) => void;
}) {
  const heading = useId();
  const [usageOpen, setUsageOpen] = useState(false);
  const observed = new Map(task.stages.map((stage) => [stage.id, stage]));
  return (
    <section className="panel goal-research-task" aria-labelledby={heading}>
      <div className="panel-heading">
        <h2 id={heading}>{task.goal}</h2>
        <AreaHelp topic="goal-research-progress" title="任务进度" />
      </div>
      <p role="status">{task.status}{task.updatedAt && <> · <time dateTime={task.updatedAt}>{task.updatedAt}</time></>}</p>
      {task.deadline != null && <p>截止时间 {new Date(task.deadline * 1000).toLocaleString('zh-CN')}。</p>}
      {task.failure && <p role="alert">{task.failure}</p>}
      {task.reasonCode && <details><summary>查看原因代码（技术信息）</summary><code>{task.reasonCode}</code></details>}
      <MissingItems items={task.missing ?? []} />
      {task.noNewData ? <p>没有补到新的数据，首轮研究结果仍可查看。</p> : task.additionalDataRequested && (
        <p>已请求补充数据。{task.extensionReason}如果补充成功，将用新数据继续，预算和截止时间不变。</p>
      )}
      <ol className="goal-detail-list" aria-label="研究步骤">
        {(Object.keys(stages) as (keyof typeof stages)[]).map((id) => {
          const stage = observed.get(id);
          if (id === 'review' && !stage) return null;
          return (
            <li key={id}>
              <strong>{stages[id]}</strong>
              {stage && <> · {stageLabels[stage.state]}{stage.detail && <p>{stage.detail}</p>}</>}
            </li>
          );
        })}
      </ol>
      {canWrite && task.allowedActions.length > 0 && (
        <div className="form-actions">
          {task.allowedActions.map((action) => (
            <button key={action} type="button" disabled={busy} onClick={() => onAction(action)}>
              {actionLabels[action]}
            </button>
          ))}
        </div>
      )}
      {task.datasets && task.datasets.length > 0 && (
        <section aria-label="数据与版本">
          <h3>数据与版本</h3>
          <ul className="goal-detail-list">{task.datasets.map((dataset) => <DatasetVersion key={dataset.id} dataset={dataset} />)}</ul>
        </section>
      )}
      {task.requirements && task.requirements.length > 0 && (
        <section aria-label="数据需求">
          <h3>数据需求</h3>
          <ul className="goal-detail-list">{task.requirements.map((need, index) => (
            <DatasetVersion key={index} dataset={{ ...need, id: String(index), name: '所需范围 ' + (index + 1) }} />
          ))}</ul>
        </section>
      )}
      {task.budgets && task.budgets.length > 0 && (
        <section aria-label="研究预算">
          <h3>研究预算</h3>
          <p>下方额度包括已经用掉和为当前工作预留的部分。{task.billingUnknown && '实际费用金额未提供。'}</p>
          <dl className="goal-budget">{task.budgets.map((budget) => (
            <div key={budget.label}>
              <dt>{budget.label}</dt>
              <dd>
                {[['上限', budget.limit], ['已占用额度', budget.spent], ['剩余额度', budget.remaining]]
                  .map(([label, value]) => value == null ? null : label + ' ' + value + (budget.unit ?? ''))
                  .filter(Boolean).join(' · ') || '未提供'}
              </dd>
            </div>
          ))}</dl>
        </section>
      )}
      <details onToggle={(event) => setUsageOpen(event.currentTarget.open)}>
        <summary>制定计划和检查结果用了多少模型额度</summary>
        {usageOpen && <GoalModelUsage taskId={task.id} revision={task.revision} />}
        {task.childSessionId && <p>自主研究本身的用量在下方“打开自主研究”里查看。</p>}
      </details>
      {(task.childSessionId || task.previousSessionId || task.downloadJobId) && (
        <div className="form-actions">
          {task.childSessionId && <a href={childLink(task.childSessionId)}>打开自主研究</a>}
          {task.previousSessionId && <a href={childLink(task.previousSessionId)}>打开原研究</a>}
          {task.downloadJobId && <a href={'#data?goal_download_job=' + encodeURIComponent(task.downloadJobId)}>打开补数据任务</a>}
        </div>
      )}
      {[{ report: task.previousResult, label: '首轮已保存结果' },
        { report: task.result, label: task.previousResult ? '本轮已保存结果' : '已保存结果' }].map(({ report, label }) => (
        report && <SavedResult key={report.sessionId + ':' + report.attemptId} report={report} label={label} />
      ))}
    </section>
  );
}

function DatasetVersion({ dataset }: { dataset: Dataset }) {
  const scope = [dataset.source, dataset.market, dataset.kind, dataset.frequency, dataset.adjustment, dataset.currency]
    .filter(Boolean).join(' · ');
  return (
    <li>
      <strong>{dataset.round && <>{dataset.round} · </>}{dataset.name}{dataset.version && <> · 版本 {dataset.version}</>}</strong>
      {scope && <p>{scope}</p>}
      {(dataset.start || dataset.end) && <p>{dataset.start ?? '起始日期未提供'} 至 {dataset.end ?? '结束日期未提供'}</p>}
      {dataset.symbols && dataset.symbols.length > 0 && <p>证券 {dataset.symbols.join('、')}</p>}
      {dataset.hash && <details><summary>数据校验值（技术信息）</summary><code>{dataset.hash}</code></details>}
    </li>
  );
}

export function GoalResearchView({
  canWrite, readiness, defaults = [], providers, task, busy = false, error, onStart, onAction, onRefresh, settings, settingsOpen,
}: {
  canWrite: boolean;
  readiness: GoalReadiness;
  defaults?: string[];
  providers?: GoalProvider[];
  task?: GoalTaskView;
  busy?: boolean;
  error?: string;
  onStart: (goal: string) => void;
  onAction: (action: GoalAction) => void;
  onRefresh?: () => void;
  settings?: ReactNode;
  settingsOpen?: boolean;
}) {
  const heading = useId();
  const canStart = canWrite && (readiness.canCreate ?? readiness.status === 'ready') && !busy;
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canStart) return;
    const data = new FormData(event.currentTarget);
    const goal = String(data.get('goal') ?? '').trim();
    if (!goal || goal.length > 2000) return;
    onStart(goal);
  }
  return (
    <div className="goal-research">
      <section className="panel" aria-labelledby={heading}>
        <div className="panel-heading">
          <h2 id={heading}>按目标研究</h2>
          <AreaHelp topic="goal-research" title="按目标研究" />
        </div>
        <form onSubmit={submit}>
          <label>研究目标
            <textarea name="goal" rows={4} required maxLength={2000}
              placeholder="例如：比较苹果（AAPL）和微软（MSFT）从 2020 到 2024 年的每月股价变化。" />
          </label>
          <div className="form-actions">
            <button className="primary" type="submit" disabled={!canStart}>开始研究</button>
          </div>
          {readiness.status === 'blocked' && readiness.canCreate && <p>可以先保存目标。按下方提示补齐设置后，回来继续这项任务。</p>}
          {readiness.status === 'loading' && <p role="status">正在检查研究配置…</p>}
          {readiness.status === 'unavailable' && <p role="status">暂时无法开始按目标研究。</p>}
          <MissingItems items={readiness.missing} />
          {error && <p role="alert">{error}</p>}
        </form>
          <details open={settingsOpen || undefined}>
            <summary>研究设置</summary>
            {defaults.length > 0 && <ul>{defaults.map((value) => <li key={value}>{value}</li>)}</ul>}
            <div className="form-actions">
              <a href={settingsLinks.models}>配置模型</a>
              <a href={settingsLinks.teams}>配置团队</a>
              <a href={settingsLinks.data}>配置数据源</a>
              {onRefresh && <button type="button" onClick={onRefresh}>重新核对配置</button>}
            </div>
            {providers && <GoalProviderSettings providers={providers} />}
            {settings}
          </details>
      </section>
      {task && <TaskProgress key={task.id} task={task} busy={busy} canWrite={canWrite} onAction={onAction} />}
    </div>
  );
}
