import type { RecordData } from './api';
import { fractionPercent } from './provider-backtest-results';
import { FullRangeEquity } from './history-full-range-equity';
import type { FullRangeOverview } from './history-full-range-equity';
import { HistoryPricePolicyNote } from './history-price-policy-note';
import { ErrorBox } from './components';
import { display } from './api';
// Display formatting only, including upstream scientific notation; no metric calculation.
export function upstreamPercent(value: unknown): string {
  if (
    typeof value !== 'string' ||
    !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value) ||
    !Number.isFinite(Number(value))
  )
    return '暂无';
  return new Intl.NumberFormat('zh-CN', {
    style: 'percent',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value));
}
export function projectResearchOverview(summary: RecordData, maxRows = 9000): FullRangeOverview {
  const overview = summary.overview as RecordData | undefined,
    range = overview?.full_range as RecordData | undefined;
  if (
    !overview ||
    !Array.isArray(overview.items) ||
    overview.metrics_basis !== 'FULL_SERIES' ||
    overview.method !== 'TIME_BUCKET_MIN_MAX_EQUITY_WITH_ENDPOINTS' ||
    !Number.isSafeInteger(overview.source_total) ||
    Number(overview.source_total) < 2 ||
    Number(overview.source_total) > maxRows ||
    !Number.isSafeInteger(overview.maximum_points) ||
    Number(overview.maximum_points) < 2 ||
    Number(overview.maximum_points) > 500 ||
    typeof overview.sampled !== 'boolean' ||
    overview.items.length > Number(overview.maximum_points) ||
    overview.items.length > Number(overview.source_total) ||
    overview.items.length > 500
  )
    throw new Error('完整走势图暂未提供。可查看下方原始记录。');
  const points = overview.items as RecordData[];
  const first = points[0],
    last = points.at(-1);
  if (
    !first ||
    !last ||
    first.ordinal !== 0 ||
    last.ordinal !== Number(overview.source_total) - 1 ||
    typeof range?.first !== 'string' ||
    typeof range.last !== 'string' ||
    first.at !== range.first ||
    last.at !== range.last ||
    points.some(
      (p, i) =>
        !Number.isSafeInteger(p.ordinal) ||
        (i > 0 && Number(p.ordinal) <= Number(points[i - 1].ordinal)),
    )
  )
    throw new Error('无法确认走势覆盖全部记录。请查看原始记录。');
  return {
    points,
    total: Number(overview.source_total),
    first: range.first,
    last: range.last,
    fullRange: true,
    method: String(overview.method),
  };
}
export function HistoryResultOverview({
  summary,
  policyLabel,
  policyMessage,
  quantityBasis,
  policyEvidence,
}: {
  summary: RecordData;
  policyLabel: string | null;
  policyMessage: string;
  quantityBasis: string | null;
  policyEvidence: RecordData | null;
}) {
  const report = summary.report as RecordData | undefined;
  const native = summary.stored_family === 'daily-simulation-result-3';
  let overview: FullRangeOverview;
  try {
    overview = projectResearchOverview(summary);
  } catch (error) {
    return <ErrorBox error={error} />;
  }
  return (
    <>
      {native ? (
        <>
          <p className="muted">
            价格如何处理：{policyLabel ?? '暂无'}；按报告记录的手续费和买卖价差计算。
          </p>
          {policyMessage && <p className="muted">{policyMessage}</p>}
        </>
      ) : (
        <HistoryPricePolicyNote
          label={policyLabel}
          message={policyMessage}
          quantityBasis={quantityBasis}
          evidence={policyEvidence}
        />
      )}
      <div className="report-metrics">
        <div>
          <small>{native ? '累计赚亏百分比' : '累计赚亏百分比（完整报告）'}</small>
          <strong title={display(report?.net_return)}>
            {native ? upstreamPercent(report?.net_return) : fractionPercent(report?.net_return)}
          </strong>
        </div>
        <div>
          <small>{native ? '从高点跌得最多的比例' : '从高点跌得最多的比例（完整报告）'}</small>
          <strong title={display(report?.max_drawdown)}>
            {native ? upstreamPercent(report?.max_drawdown) : fractionPercent(report?.max_drawdown)}
          </strong>
        </div>
        <div>
          <small>{native
            ? report?.fees_basis === 'ALL_EXECUTED_ENTRIES_AND_EXITS'
              ? '全部已成交手续费（美元）'
              : '已卖出交易的手续费（美元）'
            : '交易费用（美元）'}</small>
          <strong>{report?.fees == null ? '未提供' : display(report.fees)}</strong>
        </div>
        {native && (
          <>
            <div>
              <small>已卖出的交易</small>
              <strong>{display(report?.trade_count)}</strong>
            </div>
            <div>
              <small>仍持有的资产</small>
              <strong>{display(report?.open_trade_count)}</strong>
            </div>
          </>
        )}
      </div>
      <FullRangeEquity overview={overview} run={String(summary.run_id)} />
    </>
  );
}
