import { useEffect, useState } from 'react';
import { display, idOf, post, request } from './api';
import type { Json, RecordData } from './api';
import {
  ActionForm,
  DataTable,
  ErrorBox,
  JsonView,
  Notice,
  Panel,
  Resource,
  Status,
  ResultSummary,
  HashValue,
  useResource,
} from './components';
import type { Column, Field } from './components';
import { definitelyRejected } from './ux-mutation';
import { record } from './native-research-api';
type WriteProps = { canWrite: boolean };
const keyColumn: Column = {
  key: 'id',
  label: '标识',
  render: (row) => <span className="highlight">{idOf(row)}</span>,
};
const statusColumn: Column = {
  key: 'status',
  label: '状态',
  render: (row) => <Status value={row.status} />,
};
const modeColumn: Column = { key: 'mode', label: '模式' };
const timeColumn: Column = { key: 'created_at', label: '创建时间 / UTC' };
const jsonField = (key: string, label: string, help: string, initial = '{}'): Field => ({
  key,
  label,
  help,
  type: 'textarea',
  initial,
});
const textField = (key: string, label: string, help: string, initial?: string): Field => ({
  key,
  label,
  help,
  initial,
});
const modeField: Field = {
  key: 'mode',
  label: '运行模式',
  type: 'select',
  options: [
    { value: 'BACKTEST', label: '用历史行情试算' },
    { value: 'LOCAL_SIMULATION', label: '本机模拟交易' },
  ],
  help: '实盘交易未开放。外部模拟账户需单独连接并核对。',
};
const parseJson = (s: string) => {
  try {
    return JSON.parse(s);
  } catch {
    throw new Error('JSON 格式不正确，请检查引号、逗号与括号。');
  }
};
const encoded = (s: string) => encodeURIComponent(s);

export function Overview() {
  return (
    <>
      <Resource path="/overview">
        {(data) => (
          <>
            <div className="stats">
              {[
                [
                  '后台任务',
                  data.jobs == null
                    ? '未接通'
                    : Object.values(data.jobs as RecordData).reduce<number>(
                        (sum, count) => sum + (typeof count === 'number' ? count : 0),
                        0,
                      ),
                ],
                ['排队中', data.jobs == null ? '未接通' : ((data.jobs as RecordData)?.QUEUED ?? 0)],
                [
                  '运行中',
                  data.jobs == null ? '未接通' : ((data.jobs as RecordData)?.RUNNING ?? 0),
                ],
                [
                  '失败记录',
                  data.jobs == null ? '未接通' : ((data.jobs as RecordData)?.FAILED ?? 0),
                ],
              ].map(([label, value], i) => (
                <div className="stat" key={i}>
                  <small>{String(label)}</small>
                  <strong>{display(value as Json)}</strong>
                  <p>来自服务保存的记录</p>
                </div>
              ))}
            </div>
            <div className="grid-2">
              <Panel title="数据版本与质量状态">
                <OverviewCounts
                  value={(data.summary as RecordData)?.data}
                  fields={[
                    ['sources', '登记来源'],
                    ['datasets', '数据版本'],
                    ['snapshots', '不可变快照'],
                    ['quality_allowed_snapshots', '质量通过'],
                    ['quality_blocked_snapshots', '质量阻断'],
                    ['quality_unknown_snapshots', '质量未知'],
                    ['quality_state', '质量状态'],
                  ]}
                />
              </Panel>
              <Panel title="策略运行状态">
                <OverviewCounts
                  value={(data.summary as RecordData)?.strategies}
                  fields={[
                    ['instances', '实例总数'],
                    ['running', '运行中'],
                  ]}
                />
              </Panel>
              <Panel title="模拟账户与异常">
                <OverviewCounts
                  value={(data.summary as RecordData)?.accounts}
                  fields={[
                    ['total', '模拟账户'],
                    ['frozen', '冻结账户'],
                    ['emergency_stopped', '紧停账户'],
                    ['unknown_orders', '未决订单'],
                    ['sending_orders', '发送中订单'],
                    ['accounts_with_uncertain_orders', '存在未决发送的账户'],
                  ]}
                />
                <small>内部模拟与外部 paper 分账；这些计数不证明线上 paper 已验收。</small>
              </Panel>
              <Panel title="摘要查询范围">
                <dl className="key-value">
                  <dt>可见范围</dt>
                  <dd>
                    {(data.summary as RecordData)?.scope === 'OWNED'
                      ? '当前用户自有资源'
                      : (data.summary as RecordData)?.scope === 'ALL_OWNERS'
                        ? '管理员获准的平台资源'
                        : '服务未提供'}
                  </dd>
                  <dt>生成时间 / UTC</dt>
                  <dd>{display((data.summary as RecordData)?.generated_at ?? '服务未提供')}</dd>
                </dl>
                <Notice>
                  模块缺失显示不可用。可用模块的零条记录才显示 0；不把空响应当成健康或无异常。
                </Notice>
              </Panel>
            </div>
            <div className="grid-2">
              <Panel title="工作区状态">
                <dl className="key-value">
                  <dt>平台数据库</dt>
                  <dd>
                    <Status
                      value={
                        (data.database as RecordData)?.database === 'ok'
                          ? '可用'
                          : '不可用 / 未接通'
                      }
                    />
                  </dd>
                  <dt>数据库版本</dt>
                  <dd>{display((data.database as RecordData)?.schema_revision)}</dd>
                  <dt>队列中任务</dt>
                  <dd>
                    {display(
                      (data.jobs as RecordData)?.QUEUED ?? (data.jobs == null ? '未接通' : 0),
                    )}
                  </dd>
                  <dt>已完成任务</dt>
                  <dd>
                    {display(
                      (data.jobs as RecordData)?.SUCCEEDED ?? (data.jobs == null ? '未接通' : 0),
                    )}
                  </dd>
                  <dt>本地运行范围</dt>
                  <dd>开发研究、历史回放与内部模拟</dd>
                  <dt>真实资金通道</dt>
                  <dd>{data.live_trading_enabled === false ? '关闭' : '状态不可用'}</dd>
                </dl>
                <details>
                  <summary>诊断详情</summary>
                  <JsonView value={data} />
                </details>
              </Panel>
              <Panel title="研究边界">
                <dl className="key-value">
                  <dt>正式数据认证</dt>
                  <dd>
                    <Status value="UNVERIFIED" />
                  </dd>
                  <dt>研究 S02.06</dt>
                  <dd>blocked · 许可与覆盖证据未获批准</dd>
                  <dt>正式 S03</dt>
                  <dd>未授权</dd>
                  <dt>开发数据</dt>
                  <dd>仅允许合成样本和已授权开发数据</dd>
                  <dt>真实交易</dt>
                  <dd>LIVE 关闭</dd>
                </dl>
                <Notice>
                  模型或回测指标用于工程检查。不能将本地运行结果称为可靠收益或真实市场验证。
                </Notice>
              </Panel>
            </div>
          </>
        )}
      </Resource>
      <Panel
        title="最近任务"
        action={
          <a className="text-button" href="#jobs">
            查看全部 →
          </a>
        }
      >
        <DataTable
          path="/jobs"
          columns={[
            { key: 'job_id', label: '任务' },
            { key: 'handler', label: '任务类型' },
            statusColumn,
            modeColumn,
            timeColumn,
          ]}
          polling
          empty="提交一个无真实数据的练习任务，检查是否能排队、运行并保存记录。"
        />
      </Panel>
      <Panel title="已实现能力与验证维度">
        <Resource path="/capabilities">
          {(data) => (
            <>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>模块</th>
                      <th>实现</th>
                      <th>验证</th>
                      <th>外部依赖</th>
                      <th>验证范围与环境</th>
                      <th>接口</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries((data.modules || {}) as RecordData).map(([name, module]) => {
                      const m = module as RecordData;
                      const names: Record<string, string> = {
                        data: '数据中心',
                        strategies: '策略中心',
                        research: '研究与模型',
                        trading: '账户与交易',
                        operations: '审计与运维',
                      };
                      return (
                        <tr key={name}>
                          <td>{names[name] || name}</td>
                          <td>
                            <Status value={m.implementation} />
                          </td>
                          <td>
                            <Status value={m.verification ?? 'NOT_RUN'} />
                          </td>
                          <td>{display(m.external_dependency ?? '未声明 / 未核验')}</td>
                          <td>
                            <details>
                              <summary>
                                {m.acceptance_status === 'UNVERIFIED' || !m.declaration_id
                                  ? '未验证 · 查看原因'
                                  : '查看受控验收范围'}
                              </summary>
                              <dl className="key-value">
                                <dt>声明状态</dt>
                                <dd>{display(m.acceptance_status ?? 'UNVERIFIED')}</dd>
                                <dt>原因</dt>
                                <dd>
                                  {display(
                                    m.acceptance_reason ??
                                      (m.acceptance_status === 'MATCHED'
                                        ? '无阻断'
                                        : 'NO_DECLARATION'),
                                  )}
                                </dd>
                                <dt>验证范围</dt>
                                <dd>{display(m.verified_scope ?? '未声明')}</dd>
                                <dt>环境</dt>
                                <dd>
                                  {display(
                                    (m.verification_environment as RecordData)?.os ?? '未声明',
                                  )}{' '}
                                  /{' '}
                                  {display(
                                    (m.verification_environment as RecordData)?.python ?? '未声明',
                                  )}
                                </dd>
                                <dt>架构 / 目标</dt>
                                <dd>
                                  {display(
                                    (m.verification_environment as RecordData)?.architecture ??
                                      '未声明',
                                  )}{' '}
                                  /{' '}
                                  {display(
                                    (m.verification_environment as RecordData)?.target_id ??
                                      '无目标绑定',
                                  )}
                                </dd>
                                <dt>声明标识</dt>
                                <dd>{display(m.declaration_id ?? '未声明')}</dd>
                              </dl>
                              <small>Windows 本机验证不代表供应商或目标生产环境验证。</small>
                            </details>
                          </td>
                          <td>{m.router === true ? '已接通' : '待接通'}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <details>
                <summary>可用任务类型与运行模式</summary>
                <JsonView value={data.handlers} />
              </details>
            </>
          )}
        </Resource>
      </Panel>
    </>
  );
}
function OverviewCounts({
  value,
  fields,
}: {
  value: Json | undefined;
  fields: [string, string][];
}) {
  const record =
    value && typeof value === 'object' && !Array.isArray(value) ? (value as RecordData) : null;
  return (
    <>
      <Status value={record?.status ?? 'UNAVAILABLE'} />
      <dl className="key-value">
        {fields.map(([key, label]) => (
          <div className="detail-pair" key={key}>
            <dt>{label}</dt>
            <dd>{record?.[key] == null ? '服务未提供 / 不可用' : display(record[key])}</dd>
          </div>
        ))}
      </dl>
    </>
  );
}
function terminalJob(value: RecordData) {
  return ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(value.status));
}

const jobReason: Record<string, string> = {
  ALREADY_TERMINAL: '任务已结束。',
  JOB_ALREADY_TERMINAL: '任务已结束。',
  JOB_CANCELLATION_REQUESTED: '正在等待取消结果。',
  INTERNAL_HANDLER_REQUIRED: '请回到创建任务的页面重试。',
  CHECKPOINT_UNAVAILABLE: '还没有可用于继续任务的进度。',
  VERIFIED_CHECKPOINT_REQUIRED: '有已确认的进度后才能继续。',
  ACTIVE_SUCCESSOR: '已有一个重试或继续任务正在运行。',
  FORBIDDEN: '当前账户无此操作权限。',
};

function JobActions({ job, refresh, changed }: {
  job: RecordData; refresh: number; changed: (id?: string) => void;
}) {
  const id = String(job.job_id);
  const capabilities = useResource<RecordData>(`/jobs/${encoded(id)}/capabilities`, refresh);
  const [error, setError] = useState<unknown>(null), [busy, setBusy] = useState(false);
  const [originalUnknown, setOriginalUnknown] = useState(false);
  const [pending, setPending] = useState(() => {
    try { return sessionStorage.getItem(`fa-job-action:${job.owner_id}:${id}`) ?? ''; }
    catch { return ''; }
  });
  const storage = `fa-job-action:${job.owner_id}:${id}`;
  const pendingAction = pending.split(':', 1)[0];
  const valid = capabilities.data?.job_id === id && capabilities.data.status === job.status;
  const actions = [
    ['cancel', '请求取消'], ['retry', '新建任务重试'], ['resume', '从已确认进度继续'],
  ] as const;
  async function action(name: typeof actions[number][0], repeat = false) {
    if (busy) return;
    setBusy(true); setError(null);
    const original = name === pendingAction;
    try {
      let child: RecordData;
      if (name === 'cancel') {
        child = await post<RecordData>(`/jobs/${encoded(id)}/cancel`);
        if (child.job_id !== id || child.owner_id !== job.owner_id) throw Error('暂时无法确认取消结果。');
        changed();
        return;
      }
      const key = original ? pending.slice(name.length + 1) : crypto.randomUUID();
      const path = `/jobs/${encoded(id)}/${name}`;
      if (original && !repeat) {
        const receipt = await post<RecordData>('/operations/pending-write/lookup', { method: 'POST', path, idempotency_key: key });
        if (receipt.status === 'UNKNOWN') { setOriginalUnknown(true); return; }
        if (receipt.status === 'CLOSED' && receipt.resource === null) {
          sessionStorage.removeItem(storage); setPending(''); setOriginalUnknown(false); return;
        }
        const resource = record(receipt.resource);
        if (receipt.status !== 'COMMITTED' || resource.kind !== 'job' || resource.owner_id !== job.owner_id ||
            typeof resource.job_id !== 'string' || resource.job_id !== resource.id) {
          throw Error('暂时无法确认上次创建的新任务。');
        }
        child = await request<RecordData>('/jobs/' + encoded(resource.job_id));
      } else {
        sessionStorage.setItem(storage, name + ':' + key);
        setPending(name + ':' + key);
        child = await post<RecordData>(path, { idempotency_key: key });
      }
      if (typeof child.job_id !== 'string' || child.job_id === id ||
          child.owner_id !== job.owner_id || child.parent_run_id !== job.run_id) {
        throw Error('暂时无法确认新任务是否已创建。请点“确认原操作结果”。');
      }
      sessionStorage.removeItem(storage);
      setPending('');
      setOriginalUnknown(false);
      changed(child.job_id);
    } catch (cause) {
      if (name !== 'cancel' && !original && definitelyRejected(cause)) {
        sessionStorage.removeItem(storage);
        setPending('');
      }
      setError(cause);
    } finally { setBusy(false); }
  }
  return <>
    {actions.map(([name, label]) => {
      const ability = record(capabilities.data?.[name]);
      return pendingAction === name || (!pending && valid && ability.available === true)
        ? <button key={name} className="secondary" disabled={busy} onClick={() => void action(name)}>
            {pendingAction === name ? '确认原操作结果' : label}
          </button>
        : valid && typeof ability.reason_code === 'string' && jobReason[ability.reason_code]
          ? <small key={name}>{label} · {jobReason[ability.reason_code]}</small> : null;
    })}
    {originalUnknown && (pendingAction === 'retry' || pendingAction === 'resume') &&
      <button type="button" disabled={busy} onClick={() => void action(pendingAction, true)}>重新提交同一次操作</button>}
    {capabilities.error != null && <ErrorBox error={capabilities.error} />}
    {error != null && <ErrorBox error={error} />}
  </>;
}

export function JobDetail({ id, canWrite, refresh, changed }: {
  id: string; canWrite: boolean; refresh: number; changed: (newJobId?: string) => void;
}) {
  const state = useResource<RecordData>(`/jobs/${encoded(id)}`, refresh, true, terminalJob);
  const job = state.data?.job_id === id ? state.data : null;
  return <Panel title={`任务详情 · ${id}`}>
    {state.error != null ? <ErrorBox error={state.error} /> : job ? <>
      <div className="detail-actions">
        <Status value={job.status} /><span className="status">{display(job.mode)}</span>
        {canWrite && <JobActions key={id + ':' + String(job.status)} job={job} refresh={refresh} changed={changed} />}
      </div>
      <dl className="key-value">
        <dt>任务类型</dt><dd>{display(job.handler)}</dd>
        <dt>进度</dt><dd>{display(job.progress)}%</dd>
        <dt>本次运行编号</dt><dd><HashValue value={job.run_id} /></dd>
        <dt>上一次运行</dt><dd>{job.parent_run_id == null ? '首次运行' : <HashValue value={job.parent_run_id} />}</dd>
        <dt>创建 / UTC</dt><dd>{display(job.created_at)}</dd>
        <dt>更新时间 / UTC</dt><dd>{display(job.updated_at)}</dd>
        <dt>错误代码</dt><dd>{display(job.error_code ?? '无错误代码')}</dd>
      </dl>
      <details><summary>高级｜原始任务详情</summary><JsonView value={job} /></details>
      {job.result && <details open><summary>结果和相关文件</summary><ResultSummary value={job.result} /></details>}
      <h3>任务进度与日志</h3>
      <DataTable path={`/jobs/${encoded(id)}/events`} refresh={refresh} polling={!terminalJob(job)}
        columns={[
          { key: 'kind', label: '事件' }, { key: 'recorded_at', label: '时间 / UTC' },
          { key: 'metadata', label: '详情' },
        ]}
        empty="尚无任务日志。" />
    </> : <p role="status">正在读取任务详情…</p>}
  </Panel>;
}
export function Jobs({ canWrite }: WriteProps) {
  const [refresh, setRefresh] = useState(0),
    [selected, setSelected] = useState(
      () => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('job') ?? '',
    );
  useEffect(() => {
    const onHash = () => {
      const [route, query = ''] = window.location.hash.slice(1).split('?');
      if (route === 'jobs') setSelected(new URLSearchParams(query).get('job') ?? '');
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const openJob = (id: string) => {
    if (!id) return;
    setSelected(id);
    history.replaceState(null, '', '#jobs?' + new URLSearchParams({ job: id }));
  };
  const change = (newJobId?: string) => {
    setRefresh((n) => n + 1);
    if (newJobId) openJob(newJobId);
  };
  return (
    <>
      {canWrite && (
        <ActionForm
          title="提交后台测试任务"
          fields={[
            textField(
              'handler',
              '任务类型',
              '填写已注册的任务类型。检查后台是否正常时，可用 synthetic.check。',
              'synthetic.check',
            ),
            modeField,
            jsonField(
              'parameters',
              '任务参数',
              'synthetic.check 的 steps 填正整数。请勿填入密码、密钥或受限路径。',
              '{"steps": 5}',
            ),
          ]}
          submitLabel="提交任务"
          onSubmit={async (v) => {
            const result = await post<RecordData>('/jobs', {
              handler: v.handler,
              mode: v.mode,
              parameters: parseJson(v.parameters),
              idempotency_key: crypto.randomUUID(),
              versions: null,
            });
            change();
            openJob(String(result.job_id || ''));
            return result;
          }}
        />
      )}
      <Panel
        title="后台任务记录"
        action={
          <button className="secondary" onClick={() => change()}>
            刷新
          </button>
        }
      >
        <DataTable
          path="/jobs"
          columns={[
            { key: 'job_id', label: '任务' },
            { key: 'handler', label: '任务类型' },
            statusColumn,
            { key: 'progress', label: '进度' },
            modeColumn,
            timeColumn,
          ]}
          refresh={refresh}
          polling
          empty="这里还没有任务。关闭浏览器后，已提交的任务会继续运行。"
          onSelect={(row) => openJob(String(row.job_id))}
        />
      </Panel>
      {selected && (
        <JobDetail id={selected} canWrite={canWrite} refresh={refresh} changed={change} />
      )}
      <Schedules canWrite={canWrite} />
    </>
  );
}
function Schedules({ canWrite }: WriteProps) {
  const [refresh, setRefresh] = useState(0),
    [selected, setSelected] = useState('');
  return (
    <>
      <Panel title="定时任务">
        <DataTable
          path="/schedules"
          columns={[
            keyColumn,
            { key: 'name', label: '名称' },
            { key: 'handler', label: '任务类型' },
            {
              key: 'paused',
              label: '状态',
              render: (row) => (
                <Status
                  value={
                    row.archived === true ? '已归档' : row.paused === true ? '已暂停' : '已启用'
                  }
                />
              ),
            },
            { key: 'next_due_at', label: '下次执行 / UTC' },
          ]}
          refresh={refresh}
          empty="还没有定时任务。"
          onSelect={(r) => setSelected(String(r.schedule_id))}
        />
      </Panel>
      {canWrite && (
        <ActionForm
          title="新建定时任务"
          fields={[
            textField('name', '计划名称', '起一个容易在任务记录中找到的名称。'),
            textField('handler', '任务类型', '填写支持定时运行的任务类型。'),
            {
              key: 'rule',
              label: '规则',
              type: 'select',
              options: [
                { value: 'INTERVAL', label: '固定间隔' },
                { value: 'ONCE', label: '运行一次' },
              ],
              help: '错过多个执行时间时，只补跑一次。',
            },
            textField('start_at', '首次运行时间（UTC）', '填写包含时区的时间，例如 2026-10-12T00:00:00Z。'),
            {
              ...textField(
                'interval_seconds',
                '间隔（秒）',
                '填写大于零的整数。',
                '3600',
              ),
              showWhen: { key: 'rule', values: ['INTERVAL'] },
            },
            jsonField('parameters', '参数', '填写这一类任务需要的 JSON 参数。'),
          ]}
          submitLabel="保存计划"
          onSubmit={async (v) => {
            const r = await post('/schedules', {
              name: v.name,
              handler: v.handler,
              rule: v.rule,
              start_at: v.start_at,
              interval_seconds: v.rule === 'ONCE' ? null : Number(v.interval_seconds),
              parameters: parseJson(v.parameters),
              mode: 'BACKTEST',
            });
            setRefresh((n) => n + 1);
            return r;
          }}
        />
      )}
      {selected && (
        <Panel title="定时任务详情">
          <Resource path={`/schedules/${encoded(selected)}`} refresh={refresh}>
            {(r) => (
              <>
                <ResultSummary value={r} />
                {canWrite && r.archived !== true && (
                  <details>
                    <summary>编辑执行计划</summary>
                    <ActionForm
                      key={`${selected}-${r.revision}`}
                      title="修改定时任务"
                      fields={[
                        textField('name', '计划名称', '保存后仍可查看以前的执行记录。', String(r.name)),
                        textField(
                          'handler',
                          '任务类型',
                          '填写支持定时运行的任务类型。',
                          String(r.handler),
                        ),
                        {
                          key: 'rule',
                          label: '规则',
                          type: 'select',
                          initial: String(r.rule),
                          options: [
                            { value: 'INTERVAL', label: '固定间隔' },
                            { value: 'ONCE', label: '运行一次' },
                          ],
                          help: '修改后按新的触发时间计算。',
                        },
                        textField(
                          'start_at',
                          '新的运行时间（UTC）',
                          '这里先显示上次保存的下次运行时间。请核对后再保存。',
                          String(r.next_due_at ?? ''),
                        ),
                        {
                          ...textField(
                            'interval_seconds',
                            '间隔（秒）',
                            '固定间隔需要正整数。',
                            String(r.interval_seconds ?? 3600),
                          ),
                          showWhen: { key: 'rule', values: ['INTERVAL'] },
                        },
                        jsonField(
                          'parameters',
                          '参数',
                          '填写这一类任务需要的完整 JSON 参数。',
                          JSON.stringify(r.parameters, null, 2),
                        ),
                      ]}
                      submitLabel="保存修改"
                      onSubmit={async (v) => {
                        const updated = await request(`/schedules/${encoded(selected)}`, {
                          method: 'PUT',
                          body: JSON.stringify({
                            name: v.name,
                            handler: v.handler,
                            parameters: parseJson(v.parameters),
                            mode: r.mode,
                            rule: v.rule,
                            interval_seconds:
                              v.rule === 'INTERVAL' ? Number(v.interval_seconds) : null,
                            start_at: v.start_at,
                          }),
                        });
                        setRefresh((n) => n + 1);
                        return updated;
                      }}
                    />
                  </details>
                )}
              </>
            )}
          </Resource>
          <DataTable
            path={`/schedules/${encoded(selected)}/history`}
            columns={[
              { key: 'occurrence_key', label: '执行标识' },
              { key: 'due_at', label: '计划时间 / UTC' },
              { key: 'job_id', label: '后台任务' },
              statusColumn,
              { key: 'error_code', label: '失败原因' },
            ]}
            refresh={refresh}
            polling
            empty="计划还没运行。"
          />
          {canWrite && (
            <ActionForm
              title="管理执行计划"
              fields={[
                {
                  key: 'action',
                  label: '动作',
                  type: 'select',
                  options: [
                    { value: 'pause', label: '暂停' },
                    { value: 'resume', label: '恢复' },
                    { value: 'trigger', label: '立即运行一次' },
                    { value: 'archive', label: '归档，保留执行记录' },
                  ],
                  help: '暂停后，过去的执行记录仍可查看。',
                },
              ]}
              submitLabel="执行"
              onSubmit={async (v) => {
                const r =
                  v.action === 'archive'
                    ? await request(`/schedules/${encoded(selected)}`, { method: 'DELETE' })
                    : await post(
                        `/schedules/${encoded(selected)}/${v.action}`,
                        v.action === 'trigger' ? { idempotency_key: crypto.randomUUID() } : {},
                      );
                setRefresh((n) => n + 1);
                return r;
              }}
            />
          )}
        </Panel>
      )}
    </>
  );
}
