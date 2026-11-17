export { ServerRows } from './research-visuals';
import { EquityChart, ServerRows } from './research-visuals';
import { HistoryWorkspace } from './history-workspace';
import { NativeResearchReport } from './native-research-report';
import { BacktestWizard } from './backtest-wizard';
import { useEffect, useState } from 'react';
import { display, post, download } from './api';
import type { RecordData } from './api';
import {
  ActionForm,
  DataTable,
  JsonView,
  Notice,
  Panel,
  Resource,
  ResourceSelect,
  Status,
  HashValue,
  ErrorBox,
} from './components';
import type { Field } from './components';
import { useWorkspaceSelection } from './workspace-selection';
const input = (key: string, label: string, help: string, initial?: string): Field => ({
  key,
  label,
  help,
  initial,
});
const metric = (value: unknown) =>
  typeof value === 'string' && value.length > 16 && /^-?\d+(\.\d+)?$/.test(value)
    ? `≈ ${Number(value).toPrecision(6)}`
    : display(value as RecordData[string]);
export function Research({
  canWrite,
  resultsOnly = false,
  userId = '',
}: {
  canWrite: boolean;
  resultsOnly?: boolean;
  userId?: string;
}) {
  const { reference } = useWorkspaceSelection();
  const [refresh, setRefresh] = useState(0),
    [run, setRun] = useState(
      () => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('run') ?? '',
    ),
    [searchJob, setSearchJob] = useState(''),
    [exportError, setExportError] = useState<unknown>(null);
  useEffect(() => {
    const update = () =>
      setRun(new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('run') ?? '');
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  return (
    <>
      <HistoryWorkspace canWrite={canWrite} userId={userId} resultsOnly={resultsOnly} />
      <details key={`legacy-${run}`} open={!!run}>
        <summary>高级回测与模拟练习 · 原有数据引用和结果</summary>
        {!resultsOnly && (
          <BacktestWizard canWrite={canWrite} changed={() => setRefresh((n) => n + 1)} />
        )}
        <Panel
          title="研究结果与报告"
          action={
            <a className="text-button" href="#jobs">
              队列、日志与取消 →
            </a>
          }
        >
          <DataTable
            path="/research/results"
            columns={[
              {
                key: 'run_id',
                label: '回测记录',
                render: (r) => <span title={String(r.run_id)}>{String(r.run_id).slice(-12)}</span>,
              },
              {
                key: 'sample_hash',
                label: '样本摘要',
                render: (r) => (
                  <details>
                    <summary>数据引用</summary>
                    <HashValue value={r.sample_hash} />
                  </details>
                ),
              },
              {
                key: 'strategy_hash',
                label: '策略摘要',
                render: (r) => (
                  <details>
                    <summary>策略引用</summary>
                    <HashValue value={r.strategy_hash} />
                  </details>
                ),
              },
              { key: 'created_at', label: '时间 / UTC' },
            ]}
            refresh={refresh}
            polling
            empty="还没有已保存结果。先开始一次回测；未完成或失败的任务可在任务记录中查询。"
            onSelect={(r) => {
              window.location.hash = `results?run=${encodeURIComponent(String(r.run_id))}`;
            }}
          />
        </Panel>
        {run && (
          <Panel title="报告、交易、成本与时点">
            <button
              className="secondary"
              onClick={async () => {
                setExportError(null);
                try {
                  await download(
                    `/research/results/${encodeURIComponent(run)}/export`,
                    'research-result.json',
                  );
                } catch (e) {
                  setExportError(e);
                }
              }}
            >
              导出获准报告
            </button>
            {exportError != null && <ErrorBox error={exportError} />}
            <Resource path={`/research/results/${encodeURIComponent(run)}`}>
              {(r) => {
                if (r.family === 'qlib-workflow-result-1' || r.family === 'tradingagents-native-report-1')
                  return <NativeResearchReport value={r} runId={run} userId={userId} />;
                const report = r.report as RecordData;
                return (
                  <>
                    <Status value={report?.status} />
                    <div className="report-metrics">
                      {[
                        ['净收益率（比率）', report?.net_return],
                        ['最大回撤（比率）', report?.max_drawdown],
                        ['费用 / USD', report?.fees],
                        ['换手（比率）', report?.turnover],
                        ['订单数', report?.order_count],
                        ['成交数', report?.trade_count],
                      ].map(([label, value], i) => (
                        <div key={i}>
                          <small>{String(label)}</small>
                          <strong title={display(value)}>{metric(value)}</strong>
                        </div>
                      ))}
                    </div>
                    <EquityChart value={report?.equity_curve} />
                    <Notice>
                      {display(report?.decision)} 年化波动、Sharpe 与 Alpha 尚无获准统计评价。
                    </Notice>
                    <h3>覆盖、成本与基准</h3>
                    <dl className="key-value">
                      <dt>样本交易日</dt>
                      <dd>{display((report?.coverage as RecordData)?.sessions)}</dd>
                      <dt>估值点 / 未决估值</dt>
                      <dd>
                        {display((report?.coverage as RecordData)?.valuation_points)} /{' '}
                        {display((report?.coverage as RecordData)?.missing_valuations)}
                      </dd>
                      <dt>风险拒绝 / 待执行输出</dt>
                      <dd>
                        {display((report?.coverage as RecordData)?.rejections)} /{' '}
                        {display((report?.coverage as RecordData)?.unsubmitted_outputs)}
                      </dd>
                      <dt>费用假设</dt>
                      <dd>{display((report?.assumptions as RecordData)?.cost_status)}</dd>
                      <dt>参与率 / 滑点 bps</dt>
                      <dd>
                        {display((report?.assumptions as RecordData)?.participation)} /{' '}
                        {display((report?.assumptions as RecordData)?.slippage_bps)}
                      </dd>
                      <dt>基准净收益 / 费用 USD</dt>
                      <dd>
                        {report?.benchmark == null
                          ? '服务未提供可比基准'
                          : `${display((report.benchmark as RecordData)?.net_return)} / ${display((report.benchmark as RecordData)?.fees)}`}
                      </dd>
                    </dl>
                    <h3>逐笔成交与费用</h3>
                    <ServerRows
                      value={report?.trades}
                      columns={[
                        { key: 'instrument_id', label: '永久证券标识' },
                        { key: 'side', label: '方向' },
                        { key: 'quantity', label: '数量' },
                        { key: 'price', label: '价格 / USD' },
                        { key: 'fee', label: '费用 / USD' },
                        { key: 'order_id', label: '订单' },
                      ]}
                      empty="没有合法成交。请查看风险拒绝或可执行事件缺口。"
                    />
                    <details>
                      <summary>覆盖、基准与运行假设</summary>
                      <JsonView
                        value={{
                          coverage: report?.coverage,
                          benchmark: report?.benchmark,
                          assumptions: report?.assumptions,
                          risk: report?.risk,
                        }}
                      />
                    </details>
                    <details>
                      <summary>信号、订单、账本与完整血缘</summary>
                      <JsonView value={r} />
                    </details>
                  </>
                );
              }}
            </Resource>
          </Panel>
        )}
        {canWrite && (
          <details>
            <summary>高级：限定范围内试用参数，查看每次结果</summary>
            <label className="field">
              <span>查看已保存搜索任务</span>
              <ResourceSelect
                field={{
                  key: 'search_lookup',
                  label: '查看已保存搜索任务',
                  help: '从后台任务列表选取 research.search 父任务；翻页只筛选当前页。',
                  required: false,
                  type: 'select',
                  initial: searchJob,
                  resource: {
                    path: '/jobs',
                    idKey: 'job_id',
                    labelKeys: ['status', 'created_at', 'job_id'],
                    filter: { key: 'handler', value: 'research.search' },
                  },
                }}
                onChange={setSearchJob}
              />
              <small>从后台任务列表选取搜索父任务；每页显示匹配项，关闭或刷新页面后仍可查询。</small>
            </label>
            <ActionForm
              title="预先声明参数搜索范围"
              disabled={!reference}
              fields={[
                {
                  key: 'instance_id',
                  label: '已保存基础实例',
                  type: 'select',
                  help: '必须是兼容批量后端的 BACKTEST 实例；当前绑定的冻结样本保持不变。',
                  resource: {
                    path: '/strategies/instances',
                    idKey: 'instance_id',
                    labelKeys: ['status', 'created_at', 'instance_id'],
                  },
                },
                input('budget', '最大尝试数', '1–16，不能只保留较好的尝试。', '2'),
                {
                  key: 'space',
                  label: '明确参数集合',
                  type: 'textarea',
                  initial: '[{"threshold":"10"},{"threshold":"11"}]',
                  help: '冻结参数对象数组；数值仅为合成工程示例。所有失败、子 run 和阶段保留。',
                },
              ]}
              submitLabel="冻结范围并创建搜索任务"
              onSubmit={async (v) => {
                let space;
                try {
                  space = JSON.parse(v.space);
                } catch {
                  throw new Error('参数集合 JSON 格式不正确。');
                }
                const r = await post<RecordData>('/research/searches', {
                  instance_id: v.instance_id,
                  sample_reference: reference,
                  space,
                  budget: Number(v.budget),
                });
                setSearchJob(String(r.job_id));
                setRefresh((n) => n + 1);
                return r;
              }}
            />
            {searchJob && (
              <Resource path={`/research/searches/${encodeURIComponent(searchJob)}`} polling>
                {(r) => (
                  <>
                    <Status value={r.status} />
                    <ServerRows
                      value={r.attempts}
                      columns={[
                        { key: 'attempt_id', label: '尝试' },
                        { key: 'status', label: '状态' },
                        { key: 'stage', label: '阶段' },
                        { key: 'error_code', label: '失败代码' },
                        {
                          key: 'result',
                          label: '子 run',
                          render: (x) => display((x.result as RecordData)?.run_id),
                        },
                      ]}
                      empty="父任务尚未完成提交；子任务结果不会被预先判为成功。"
                    />
                    <details>
                      <summary>全部搜索血缘</summary>
                      <JsonView value={r} />
                    </details>
                  </>
                )}
              </Resource>
            )}
          </details>
        )}
        {canWrite && (
          <details>
            <summary>高级：比较两次回测结果</summary>
            <ActionForm
              title="跨后端语义比较"
              fields={[
                {
                  key: 'left_run_id',
                  label: '左侧已完成 run',
                  type: 'select',
                  help: '选择已保存研究结果。',
                  resource: {
                    path: '/research/results',
                    idKey: 'run_id',
                    labelKeys: ['created_at', 'run_id'],
                  },
                },
                {
                  key: 'right_run_id',
                  label: '右侧已完成 run',
                  type: 'select',
                  help: '必须相同样本、兼容策略与成本；不可比将被拒绝。',
                  resource: {
                    path: '/research/results',
                    idKey: 'run_id',
                    labelKeys: ['created_at', 'run_id'],
                  },
                },
              ]}
              submitLabel="检查可比性并比较"
              onSubmit={(v) => post('/research/compare', v)}
            />
          </details>
        )}
        <Notice>
          历史回放没有批准正式 S03。合成净值与情景费用不证明有效 Alpha、真实历史净收益或实盘就绪。
        </Notice>
      </details>
    </>
  );
}
export function Models({ canWrite = false }: { canWrite?: boolean }) {
  const [refresh, setRefresh] = useState(0),
    [selected, setSelected] = useState('');
  return (
    <>
      {canWrite && (
        <ActionForm
          title="确定性开发训练与推理"
          fields={[
            {
              key: 'kind',
              label: '训练窗',
              type: 'select',
              options: [
                { value: 'expanding', label: '扩展训练窗' },
                { value: 'rolling', label: '滚动训练窗' },
              ],
              help: '预处理只在合法训练区 fit；推理样本无标签，封存区不可访问。',
            },
            input('seed', '随机种子', '0 到 2³²−1 整数，用于可复现工程示例。', '0'),
          ]}
          submitLabel="提交合成训练任务"
          onSubmit={async (v) => {
            const r = await post('/models/train-example', {
              kind: v.kind,
              seed: Number(v.seed),
            });
            setRefresh((n) => n + 1);
            return r;
          }}
        />
      )}
      <Panel title="模型文件与训练截止">
        <DataTable
          path="/models"
          columns={[
            { key: 'model_id', label: '标识' },
            { key: 'version', label: '版本' },
            { key: 'run_id', label: '来源实验' },
            { key: 'train_cutoff', label: '训练截止 / UTC' },
            { key: 'model_hash', label: '摘要' },
          ]}
          refresh={refresh}
          polling
          empty="尚无模型。开发训练示例仅验证时间边界、冻结版本与推理管线。"
          onSelect={(r) => setSelected(String(r.model_id))}
        />
      </Panel>
      {selected && (
        <Panel title="模型血缘与适用范围">
          <Resource path={`/models/${encodeURIComponent(selected)}`} />
        </Panel>
      )}
      <Notice>模型来源、训练截止、代码和依赖需可追溯。正式封存数据不得参与开发调参。</Notice>
    </>
  );
}
