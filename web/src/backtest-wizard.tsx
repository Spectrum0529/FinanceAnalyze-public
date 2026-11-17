import { useState } from 'react';
import { ApiError, post } from './api';
import type { RecordData } from './api';
import {
  ActionForm,
  ErrorBox,
  JsonView,
  Notice,
  Panel,
  ResourceSelect,
  useResource,
  isTerminalJob,
} from './components';
import { useWorkspaceSelection } from './workspace-selection';

type Draft = { instance_id: string; backend: string; model_id: string };
type Attempt = { sample: RecordData; label: string; draft: Draft; key: string };
export function BacktestWizard({ canWrite, changed }: { canWrite: boolean; changed: () => void }) {
  const { reference, label, select } = useWorkspaceSelection();
  const [step, setStep] = useState(1);
  const [draft, setDraft] = useState<Draft>({ instance_id: '', backend: 'event', model_id: '' });
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [submitted, setSubmitted] = useState<{ attempt: Attempt; result: RecordData } | null>(null);
  // Synchronous guard covers repeated callbacks before a React rerender.
  const [flight] = useState(() => ({ busy: false, uncertain: false, completed: false }));
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [source, setSource] = useState('saved');
  const choose = (r: RecordData, text: string) => {
    const ref = r.reference;
    if (!ref || typeof ref !== 'object' || Array.isArray(ref) || !ref.content_hash)
      throw new Error('服务未返回有效的数据引用，请检查返回记录后重试。');
    select(ref as RecordData, text);
    setAttempt(null);
  };
  const back = (next: number) => {
    if (flight.busy || flight.uncertain || flight.completed) return;
    setAttempt(null);
    setStep(next);
  };
  return (
    <section className="wizard" aria-label="开始回测">
      <ol className="step-list" aria-label="回测步骤">
        {['选数据', '选策略', '确认开始', '进度与结果'].map((name, i) => (
          <li key={name} aria-current={step === i + 1 ? 'step' : undefined}>
            <span>{i + 1}</span>
            {name}
          </li>
        ))}
      </ol>
      {!canWrite ? (
        <Notice>
          你可以查看结果。开始回测需要研究者或管理员权限。<a href="#results">查看已保存结果 →</a>
        </Notice>
      ) : (
        <>
          {step === 1 && (
            <>
              <Panel title="1. 这次用什么数据？">
                <p>选择已经保存的数据，或用明确标记的模拟数据练习。</p>
                <label className="field">
                  <span>数据方式</span>
                  <select value={source} onChange={(e) => setSource(e.target.value)}>
                    <option value="saved">使用已保存数据</option>
                    <option value="practice">模拟数据练习（不是市场行情）</option>
                  </select>
                </label>
                <p>
                  还没有数据？
                  <a className="text-button" href="#data">
                    去管理数据准备 →
                  </a>{' '}
                  返回后重新选择。
                </p>
              </Panel>
              {source === 'saved' ? (
                <ActionForm
                  title="选择已保存数据"
                  fields={[
                    {
                      key: 'snapshot_id',
                      label: '已保存的数据快照',
                      type: 'select',
                      help: '选择后由服务检查用途、质量与可用时间。',
                      resource: {
                        path: '/data/snapshots',
                        idKey: 'snapshot_id',
                        labelKeys: ['scope', 'as_of', 'snapshot_id'],
                      },
                    },
                  ]}
                  submitLabel="使用这份数据"
                  onSubmit={async (v) => {
                    const r = await post<RecordData>('/research/samples', v);
                    choose(
                      r,
                      `已保存数据 · ${v.snapshot_id.slice(-12)}（来源与用途以服务记录为准）`,
                    );
                    return r;
                  }}
                />
              ) : (
                <ActionForm
                  title="准备模拟练习数据"
                  fields={[
                    {
                      key: 'frequency',
                      label: '数据频率',
                      type: 'select',
                      options: [
                        { value: '1d', label: '日线' },
                        { value: '1m', label: '分钟线' },
                      ],
                      help: '模拟生成，不是实际市场行情。',
                    },
                    {
                      key: 'count',
                      label: '样本块数',
                      type: 'number',
                      initial: '8',
                      help: '填写 2 到 8；少量样本不适合评价投资表现。',
                    },
                    {
                      key: 'two_assets',
                      label: '资产数量',
                      type: 'select',
                      options: [
                        { value: 'false', label: '一个模拟资产' },
                        { value: 'true', label: '两个模拟资产' },
                      ],
                      help: '仅用于练习操作。',
                    },
                  ]}
                  submitLabel="生成并选择模拟数据"
                  onSubmit={async (v) => {
                    const r = await post<RecordData>('/research/samples', {
                      frequency: v.frequency,
                      count: Number(v.count),
                      two_assets: v.two_assets === 'true',
                    });
                    choose(
                      r,
                      `模拟练习数据 · ${v.frequency === '1d' ? '日线' : '分钟线'} · ${v.count} 块（非市场行情）`,
                    );
                    return r;
                  }}
                />
              )}
              <Notice>
                {reference ? (
                  <>
                    已选：{label}。
                    <details>
                      <summary>数据来源与引用详情</summary>
                      <JsonView value={reference} />
                    </details>
                  </>
                ) : (
                  '尚未选择数据；只有服务确认后才能继续。'
                )}
              </Notice>
              <button disabled={!reference} onClick={() => setStep(2)}>
                下一步：选策略 →
              </button>
            </>
          )}
          {step === 2 && (
            <Panel title="2. 选择要测试的策略">
              <p>已选数据：{label}</p>
              <label className="field">
                <span>已保存策略</span>
                <ResourceSelect
                  field={{
                    key: 'instance_id',
                    label: '已保存策略',
                    type: 'select',
                    initial: draft.instance_id,
                    help: '',
                    resource: {
                      path: '/strategies/instances',
                      idKey: 'instance_id',
                      labelKeys: ['status', 'created_at', 'instance_id'],
                    },
                  }}
                  onChange={(id) => setDraft((v) => ({ ...v, instance_id: id }))}
                />
              </label>
              <p>
                列表为空或策略未准备好？
                <a className="text-button" href="#strategies">
                  去策略工具准备 →
                </a>{' '}
                服务仍会检查策略是否可信、是否支持回测。
              </p>
              <details>
                <summary>高级选项：计算方式与模型</summary>
                <label className="field">
                  <span>计算方式</span>
                  <select
                    value={draft.backend}
                    onChange={(e) => setDraft((v) => ({ ...v, backend: e.target.value }))}
                  >
                    <option value="event">逐事件计算（默认）</option>
                    <option value="batch">批量计算（需要兼容策略）</option>
                  </select>
                </label>
                <label className="field">
                  <span>已保存模型（可选）</span>
                  <ResourceSelect
                    field={{
                      key: 'model_id',
                      label: '模型',
                      required: false,
                      type: 'select',
                      initial: draft.model_id,
                      help: '',
                      resource: {
                        path: '/models',
                        idKey: 'model_id',
                        labelKeys: ['version', 'train_cutoff', 'model_id'],
                      },
                    }}
                    onChange={(id) => setDraft((v) => ({ ...v, model_id: id }))}
                  />
                </label>
                <p>
                  只有使用模型的策略才需要绑定。<a href="#models">准备模型 →</a>
                </p>
              </details>
              <div className="form-actions">
                <button className="secondary" onClick={() => back(1)}>
                  ← 上一步：选数据
                </button>
                <button
                  disabled={!reference || !draft.instance_id}
                  onClick={() => {
                    if (!reference || !draft.instance_id) return;
                    setAttempt({
                      sample: structuredClone(reference),
                      label,
                      draft: { ...draft },
                      key: crypto.randomUUID(),
                    });
                    setStep(3);
                  }}
                >
                  下一步：确认 →
                </button>
              </div>
            </Panel>
          )}
          {step === 3 && attempt && (
            <>
              <Panel title="3. 核对后开始">
                <dl className="key-value">
                  <dt>数据</dt>
                  <dd>{attempt.label}</dd>
                  <dt>策略</dt>
                  <dd>策略 · 编号 {attempt.draft.instance_id.slice(-8)}</dd>
                  <dt>计算方式</dt>
                  <dd>{attempt.draft.backend === 'event' ? '逐事件计算' : '批量计算'}</dd>
                  <dt>模型</dt>
                  <dd>
                    {attempt.draft.model_id
                      ? `模型 · 编号 ${attempt.draft.model_id.slice(-8)}`
                      : '不绑定'}
                  </dd>
                </dl>
                <details>
                  <summary>完整策略与数据引用</summary>
                  <JsonView
                    value={{
                      sample_reference: attempt.sample,
                      instance_id: attempt.draft.instance_id,
                      model_id: attempt.draft.model_id || null,
                    }}
                  />
                </details>
                <p>
                  点击下方按钮才会创建回测任务。这里只做历史回放，不下真实订单。数据是否可用、策略是否可信，由服务最后检查。
                </p>
                {uncertain && (
                  <Notice>
                    请求未确认，任务可能已经创建。请先<a href="#jobs">查询任务记录</a>
                    ；如需再次确认，只能使用下方同一次请求，不能修改数据后另行提交。
                  </Notice>
                )}
                <button className="secondary" disabled={busy || uncertain} onClick={() => back(2)}>
                  ← 返回修改策略
                </button>
              </Panel>
              <ActionForm
                title="确认提交"
                fields={[]}
                submitLabel={uncertain ? '用同一次请求重新确认' : '确认，开始回测'}
                disabled={busy || !canWrite}
                onSubmit={async () => {
                  if (flight.busy || flight.completed)
                    throw new Error('这次提交正在处理或已返回，请查看任务记录。');
                  flight.busy = true;
                  setBusy(true);
                  try {
                    const r = await post<RecordData>('/research/backtests', {
                      instance_id: attempt.draft.instance_id,
                      sample_reference: attempt.sample,
                      backend: attempt.draft.backend,
                      model_id: attempt.draft.model_id || null,
                      idempotency_key: attempt.key,
                    });
                    if (typeof r.job_id !== 'string' || !r.job_id)
                      throw new Error('服务未返回任务编号，请先查询任务记录，勿重复创建。');
                    flight.completed = true;
                    flight.uncertain = false;
                    setUncertain(false);
                    setSubmitted({ attempt, result: r });
                    setStep(4);
                    changed();
                    return r;
                  } catch (e) {
                    const unknownOutcome =
                      flight.uncertain ||
                      !(
                        e instanceof ApiError &&
                        e.status >= 400 &&
                        e.status < 500 &&
                        e.status !== 408
                      );
                    flight.uncertain = unknownOutcome;
                    setUncertain(unknownOutcome);
                    throw e;
                  } finally {
                    flight.busy = false;
                    setBusy(false);
                  }
                }}
              />
            </>
          )}
          {step === 4 && submitted && (
            <Panel title="4. 查看这次回测">
              <p>本次已提交数据：{submitted.attempt.label}</p>
              <p>本次策略：编号 {submitted.attempt.draft.instance_id.slice(-8)}</p>
              <details>
                <summary>本次提交的完整引用</summary>
                <JsonView
                  value={{
                    sample_reference: submitted.attempt.sample,
                    instance_id: submitted.attempt.draft.instance_id,
                    model_id: submitted.attempt.draft.model_id || null,
                  }}
                />
              </details>
              <Notice>任务已由服务返回。排队不代表完成；完成后在下方结果列表打开对应报告。</Notice>
              <WizardProgress result={submitted.result} />
              {typeof submitted.result.run_id === 'string' && (
                <p>
                  <a href={`#results?run=${encodeURIComponent(submitted.result.run_id)}`}>
                    读取本次报告（任务完成后可用） →
                  </a>
                </p>
              )}
              <div className="form-actions">
                <a className="primary-link" href="#results">
                  查看已保存结果 →
                </a>
                <button
                  className="secondary"
                  onClick={() => {
                    flight.completed = false;
                    flight.uncertain = false;
                    setAttempt(null);
                    setSubmitted(null);
                    setStep(1);
                  }}
                >
                  再做一次回测
                </button>
              </div>
            </Panel>
          )}
        </>
      )}
      <p className="muted">
        离开或刷新页面不会停止已提交任务。页面草稿不保存；回来后请在“更多工具 →
        任务记录”查询，避免重复提交。
      </p>
    </section>
  );
}

// Local presentation only: keep the existing guarded reader and terminal polling policy.
export function WizardProgress({ result }: { result: RecordData }) {
  const jobId = String(result.job_id);
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useResource<RecordData>(
    `/jobs/${encodeURIComponent(jobId)}`,
    refresh,
    true,
    isTerminalJob,
  );
  const labels: Record<string, string> = {
    QUEUED: '等待执行',
    RUNNING: '正在回测',
    SUCCEEDED: '回测已完成',
    FAILED: '回测失败',
    CANCELLED: '已取消',
  };
  return (
    <div className="operation-result">
      <div role="status">
        {loading ? (
          <p>正在读取任务进度…</p>
        ) : error ? (
          <>
            <p>暂时无法读取任务进度；请刷新查询，勿重复创建。</p>
            <ErrorBox error={error} />
          </>
        ) : data ? (
          <>
            <strong>{labels[String(data.status)] || '任务状态待确认，请查看详情'}</strong>
            <p>进度：{data.progress == null ? '服务未提供' : `${data.progress}%`}</p>
            {data.cancel_requested === true && <p>取消请求已收到，以服务最终状态为准。</p>}
            {data.status === 'FAILED' && (
              <Notice>
                失败原因：
                {data.error_code == null ? '服务未提供，请查看任务记录。' : String(data.error_code)}
              </Notice>
            )}
            <small>
              {isTerminalJob(data) ? '任务已结束，已停止自动刷新。' : '每 4 秒更新任务进度。'}
            </small>
          </>
        ) : (
          <p>服务尚未返回任务状态。</p>
        )}
      </div>
      <div className="form-actions">
        <button type="button" className="secondary" onClick={() => setRefresh((n) => n + 1)}>
          刷新当前任务状态
        </button>
        <a className="text-button" href={`#jobs?job=${encodeURIComponent(jobId)}`}>
          查看任务、日志与取消 →
        </a>
      </div>
      <details>
        <summary>任务技术详情与完整编号</summary>
        <p>当前任务记录</p>
        <JsonView value={data} />
        <p>本次提交返回（提交时快照）</p>
        <JsonView value={result} />
      </details>
    </div>
  );
}
