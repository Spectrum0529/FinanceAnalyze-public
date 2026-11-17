import type { RecordData } from './api';
import { ComputationCacheProvenance } from './computation-cache-provenance';
import { display } from './api';
import { ErrorBox, JsonView, Notice, Panel, Resource } from './components';
import { HistoryResultOverview } from './history-result-overview';
import { HistorySeriesTable } from './history-series-table';
import { BacktestingReport } from './backtesting-report';
const record = (value: unknown): RecordData =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as RecordData) : {};
export function HistoryReportBody({ value, run }: { value: RecordData; run: string }) {
  if (
    value.run_id !== run ||
    value.view_schema !== 'daily-result-summary-2' ||
    ![
      'daily-simulation-result-1',
      'daily-simulation-result-2',
      'daily-simulation-result-3',
    ].includes(String(value.stored_family)) ||
    value.family !== value.stored_family ||
    !Number.isSafeInteger(value.revision) ||
    Number(value.revision) < 0 ||
    value.deleted !== false
  )
    return <ErrorBox error={new Error('无法确认读到的是所选报告。请从结果列表重新打开。')} />;
  if (value.stored_family === 'daily-simulation-result-3')
    return <><ComputationCacheProvenance value={value.cache_disposition} /><BacktestingReport value={value} run={run} /></>;
  const policy = record(value.price_policy),
    cost = record(policy.cost_semantics),
    request = record(value.request),
    coverage = record(value.coverage),
    manifest = record(value.series_manifest);
  const label = typeof policy.display_name === 'string' ? policy.display_name : null;
  return (
    <>
      <ComputationCacheProvenance value={value.cache_disposition} />
      <h3>
        {display(value.name)} · {display(value.ticker)}
      </h3>
      <dl className="key-value">
        <dt>所选日期（含首尾）</dt>
        <dd>
          {display(request.start_date)} 至 {display(request.end_date)}
        </dd>
        <dt>实际用到的日期和记录数</dt>
        <dd>
          {display(coverage.actual_first)} 至 {display(coverage.actual_last)} /{' '}
          {display(coverage.used_rows)}
        </dd>
      </dl>
      <Notice>
        实际用到的日期可能少于所选日期。缺少的记录保持为空。这是用保存的价格模拟的表现，实际成交和计入分红的总收益可能不同。
      </Notice>
      <HistoryResultOverview
        summary={value}
        policyLabel={label}
        policyMessage={
          label
            ? '这里显示回测报告原来使用的价格处理方式。'
            : '这份旧报告没有可确认的价格算法。可展开下方原始记录核对。'
        }
        quantityBasis={typeof cost.quantity_basis === 'string' ? cost.quantity_basis : null}
        policyEvidence={Object.keys(policy).length ? policy : null}
      />
      <h3>逐笔成交 · 共 {display(record(manifest.trades).total)} 条</h3>
      <HistorySeriesTable
        key={`${run}:trades`}
        run={run}
        kind="trades"
        columns={[
          { key: 'decision_session_date', label: '信号日期' },
          { key: 'session_date', label: '假设成交日期' },
          { key: 'side', label: '方向' },
          { key: 'quantity', label: '数量（按当时保存的单位）' },
          { key: 'price', label: '价格 / USD' },
          { key: 'fee', label: '费用 / USD' },
        ]}
        empty="这次没有成交。"
      />
      <details>
        <summary>查看每日估算、未成交和原始记录</summary>
        <h4>原始估值</h4>
        <HistorySeriesTable
          key={`${run}:equity_curve`}
          run={run}
          kind="equity_curve"
          columns={[
            { key: 'at', label: '日期' },
            { key: 'equity', label: '账户总额（美元）' },
            { key: 'cash', label: '现金 / USD' },
            { key: 'quantity', label: '模拟单位数' },
          ]}
          empty="没有估值记录。"
        />
        <h4>未成交的记录</h4>
        <HistorySeriesTable
          key={`${run}:rejections`}
          run={run}
          kind="rejections"
          columns={[
            { key: 'at', label: '时间' },
            { key: 'code', label: '代码' },
            { key: 'reason', label: '原因' },
          ]}
          empty="没有未成交的记录。"
        />
        <JsonView
          value={{
            stored_family: value.stored_family,
            stored_result_hash: value.stored_result_hash,
            request: value.request,
            coverage: value.coverage,
            price_policy: value.price_policy,
            history_reference: value.history_reference,
            observation_reference: value.observation_reference,
            final: value.final,
            report: value.report,
          }}
        />
      </details>
    </>
  );
}
export function HistoryReport({ run }: { run: string }) {
  return (
    <Panel title="已保存回测结果">
      <Resource path={`/research/daily/results/${encodeURIComponent(run)}`}>
        {(value) => <HistoryReportBody value={value} run={run} />}
      </Resource>
    </Panel>
  );
}
