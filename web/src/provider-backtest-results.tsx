import { SINA_ORIGIN, SINA_INTERFACE, OBSERVATION_FAMILY } from './provider-observation-contract';
import { useEffect, useState } from 'react';
import type { RecordData } from './api';
import { display } from './api';
import { DataTable, ErrorBox, JsonView, Notice, Panel, Resource, useResource } from './components';
import { EquityChart, ServerRows } from './research-visuals';
import { DAILY_RESULTS, dailyResultLink } from './provider-backtest-api';
export function fractionPercent(value: unknown): string {
  if (typeof value !== 'string' || !/^-?\d+(\.\d+)?$/.test(value)) return '服务未提供';
  const negative = value.startsWith('-');
  const [whole, fraction = ''] = value.replace(/^-/, '').split('.');
  let scaled = BigInt(whole) * 10000n + BigInt((fraction + '0000').slice(0, 4));
  if ((fraction[4] ?? '0') >= '5') scaled += 1n;
  return `${negative && scaled !== 0n ? '-' : ''}${scaled / 100n}.${String(scaled % 100n).padStart(2, '0')}%`;
}
const strategyLabel = (id: unknown) =>
  ({ buy_hold: '买入持有', sma20: '20日均线', sma_cross_5_20: '5/20日均线' })[
    String(id) as 'buy_hold'
  ] ?? display(id as string);
function record(value: unknown): RecordData {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as RecordData) : {};
}

type FrozenObservation = {
  resultId: string;
  hash: string;
  sourceId: string;
  version: number;
  sourceHash: string;
};
function frozenObservation(value: RecordData): FrozenObservation | null {
  const observation = record(value.observation_reference),
    source = record(observation.source_ref);
  const sha = (v: unknown) => typeof v === 'string' && /^[a-f0-9]{64}$/i.test(v);
  if (
    typeof observation.result_id !== 'string' ||
    !observation.result_id ||
    !sha(observation.content_hash) ||
    typeof source.source_id !== 'string' ||
    !source.source_id ||
    !Number.isSafeInteger(source.version) ||
    Number(source.version) <= 0 ||
    !sha(source.content_hash)
  )
    return null;
  return {
    resultId: observation.result_id,
    hash: String(observation.content_hash),
    sourceId: source.source_id,
    version: Number(source.version),
    sourceHash: String(source.content_hash),
  };
}
function matchesSource(source: RecordData, expected: FrozenObservation) {
  return (
    source.provider === 'akshare' &&
    source.source_id === expected.sourceId &&
    source.version === expected.version &&
    source.content_hash === expected.sourceHash &&
    record(source.connection).endpoint === SINA_ORIGIN
  );
}
export function verifiedDailyProviderLabel(
  value: RecordData,
  source?: RecordData,
  observation?: RecordData,
) {
  if (value.provider !== 'akshare') return display(value.provider);
  const expected = frozenObservation(value);
  if (!expected || !source || !observation || !matchesSource(source, expected)) return 'AKShare';
  const ref = record(observation.source_ref);
  return observation.provider === 'akshare' &&
    observation.data_family === OBSERVATION_FAMILY &&
    observation.result_id === expected.resultId &&
    observation.content_hash === expected.hash &&
    ref.source_id === expected.sourceId &&
    ref.version === expected.version &&
    ref.content_hash === expected.sourceHash &&
    record(observation.use_capabilities).display === true &&
    record(observation.sdk_ref).interface === SINA_INTERFACE
    ? 'AKShare（新浪）· 仅未复权'
    : 'AKShare';
}
// Labels resolve only frozen source/observation metadata through existing authenticated read APIs.
// The current catalog or a source ID name can never relabel an older result.
export function DailyProviderLabel({ value }: { value: RecordData }) {
  const expected = frozenObservation(value);
  return value.provider === 'akshare' && expected ? (
    <FrozenProviderLabel
      key={`${expected.resultId}:${expected.hash}`}
      value={value}
      expected={expected}
    />
  ) : (
    <>{value.provider === 'akshare' ? 'AKShare' : display(value.provider)}</>
  );
}
export function FrozenProviderLabel({
  value,
  expected,
}: {
  value: RecordData;
  expected: FrozenObservation;
}) {
  const state = useResource<RecordData>(
    `/data/sources/${encodeURIComponent(expected.sourceId)}?version=${expected.version}`,
  );
  return !state.loading && !state.error && state.data && matchesSource(state.data, expected) ? (
    <ObservedProviderLabel value={value} source={state.data} expected={expected} />
  ) : (
    <>AKShare</>
  );
}
export function ObservedProviderLabel({
  value,
  source,
  expected,
}: {
  value: RecordData;
  source: RecordData;
  expected: FrozenObservation;
}) {
  const state = useResource<RecordData>(
    `/data/observations/${encodeURIComponent(expected.resultId)}?limit=1&offset=0`,
  );
  return (
    <>
      {!state.loading && !state.error && state.data
        ? verifiedDailyProviderLabel(value, source, state.data)
        : 'AKShare'}
    </>
  );
}

export function DailyResultBody({ value, run }: { value: RecordData; run: string }) {
  if (value.family !== 'daily-simulation-result-1' || value.run_id !== run)
    return (
      <ErrorBox error={new Error('结果编号或结果类型不匹配；未将旧回测结果当作日线结果显示。')} />
    );
  const request = record(value.request),
    report = record(value.report),
    coverage = record(report.coverage);
  const curve = Array.isArray(value.equity_curve) ? (value.equity_curve as RecordData[]) : [];
  return (
    <>
      <p>
        <strong>
          {display(value.ticker)} · <DailyProviderLabel value={value} /> ·{' '}
          {strategyLabel(value.strategy)}
        </strong>
      </p>
      <dl className="key-value">
        <dt>请求日期（两端包含）</dt>
        <dd>
          {display(request.start_date)} 至 {display(request.end_date)}
        </dd>
        <dt>实际日线范围</dt>
        <dd>
          {curve.length ? `${display(curve[0].at)} 至 ${display(curve.at(-1)?.at)}` : '服务未提供'}
        </dd>
        <dt>实际参与模拟条数</dt>
        <dd>{display(coverage.sessions)} 条日线</dd>
      </dl>
      <Notice>
        日线假设模拟：前一观测交易日收盘信号，下一观测交易日开盘成交；不是实际成交。使用供应商观察价格，非原始价或时点认证；不含完整拆股、现金分红与总回报处理；期末持仓按观察收盘价估值，不强制卖出。
      </Notice>
      <div className="report-metrics">
        {[
          ['净收益率', fractionPercent(report.net_return), report.net_return],
          ['从高点跌得最多的比例', fractionPercent(report.max_drawdown), report.max_drawdown],
          ['交易费用（美元）', report.fees == null ? '未提供' : display(report.fees), report.fees],
          ['成交笔数', display(report.trade_count), report.trade_count],
        ].map(([label, shown, raw]) => (
          <div key={String(label)}>
            <small>{String(label)}</small>
            <strong title={display(raw)}>{String(shown)}</strong>
          </div>
        ))}
      </div>
      <small>收益与回撤按比例转为百分比显示并四舍五入至两位；完整精度在结果详情保留。</small>
      <EquityChart value={value.equity_curve} />
      <h3>成交记录（日期为交易日标签）</h3>
      <ServerRows
        value={value.trades}
        columns={[
          { key: 'decision_session_date', label: '产生信号的日期' },
          { key: 'session_date', label: '假设成交日期' },
          {
            key: 'side',
            label: '方向',
            render: (r) =>
              r.side === 'buy' ? '买入' : r.side === 'sell' ? '卖出' : display(r.side),
          },
          { key: 'quantity', label: '股数' },
          { key: 'observed_open', label: '观察开盘价 / USD' },
          { key: 'price', label: '含滑点价格 / USD' },
          { key: 'fee', label: '手续费 / USD' },
        ]}
        empty="没有成交。请查看资金不足、预热期或结果中的拒绝原因；空成交不代表零风险。"
      />
      <details>
        <summary>资金、成本与模拟假设</summary>
        <JsonView
          value={{
            request,
            assumptions: value.assumptions,
            final: value.final,
            rejections: value.rejections,
            unsubmitted_outputs: value.unsubmitted_outputs,
          }}
        />
      </details>
      <details>
        <summary>数据来源、接收时间与完整结果</summary>
        <JsonView value={value} />
      </details>
    </>
  );
}
export function DailyReport({ run }: { run: string }) {
  return (
    <Panel title="股票日线回测结果">
      <Resource path={`${DAILY_RESULTS}/${encodeURIComponent(run)}`}>
        {(value) => <DailyResultBody value={value} run={run} />}
      </Resource>
    </Panel>
  );
}
export function DailySavedResults() {
  const [run, setRun] = useState(
    () => new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('daily_run') ?? '',
  );
  useEffect(() => {
    const change = () =>
      setRun(new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('daily_run') ?? '');
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, []);
  return (
    <>
      <Panel title="股票日线回测 · 已保存结果">
        <DataTable
          path={DAILY_RESULTS}
          polling
          columns={[
            { key: 'ticker', label: '股票' },
            {
              key: 'provider',
              label: '数据来源',
              render: (r) => (r.provider === 'akshare' ? 'AKShare' : display(r.provider)),
            },
            { key: 'strategy', label: '策略', render: (r) => strategyLabel(r.strategy) },
            { key: 'created_at', label: '保存时间 / UTC' },
          ]}
          empty="还没有完成的股票日线回测。选择股票、日期和策略后开始；失败或尚在运行的任务不会显示为结果。"
          onSelect={(r) => {
            window.location.hash = dailyResultLink(String(r.run_id)).slice(1);
          }}
        />
      </Panel>
      {run && <DailyReport key={run} run={run} />}
    </>
  );
}
