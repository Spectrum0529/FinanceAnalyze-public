import type { RecordData } from './api';
import { display } from './api';
import { ErrorBox, JsonView } from './components';
import { ServerRows } from './research-visuals';
import { HistoryResultOverview, upstreamPercent } from './history-result-overview';
import { HistorySeriesTable } from './history-series-table';
const record = (value: unknown): RecordData =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as RecordData) : {};
const rows = (value: unknown): RecordData[] => {
  if (
    !Array.isArray(value) ||
    value.length > 1 ||
    value.some((row) => !row || typeof row !== 'object' || Array.isArray(row))
  )
    throw new Error('未平仓或待执行明细不完整，请重新加载结果。');
  return value as RecordData[];
};
export function projectBacktestingReport(value: RecordData) {
  const engine = record(value.engine),
    report = record(value.report),
    manifest = record(value.series_manifest),
    cost = record(value.cost_model),
    final = record(value.final);
  const adapter = value.calculation_version;
  const currentEngine = engine.name === 'financeanalyze.daily' && engine.version === '1.0.0' &&
    adapter === 'financeanalyze-daily-calculation-1' && value.engine_version === 'financeanalyze-daily-1' &&
    cost.version === 'financeanalyze-daily-costs-1';
  const legacyEngine = engine.name === 'backtesting.py' && engine.version === '0.6.6' &&
    cost.version === 'backtesting-py-costs-1';
  const allExecutedFees = currentEngine || adapter === 'backtesting-py-adapter-3';
  const legacyFees = (adapter === 'backtesting-py-adapter-1' || adapter === 'backtesting-py-adapter-2') &&
    value.engine_version === 'backtesting-py-0.6.6-daily-1' &&
    report.fees_basis === 'UPSTREAM_CLOSED_TRADE_COMMISSIONS_ONLY';
  const fullFees = allExecutedFees &&
    (currentEngine || value.engine_version === 'backtesting-py-0.6.6-daily-2') &&
    report.fees_basis === 'ALL_EXECUTED_ENTRIES_AND_EXITS' &&
    cost.fee_report_basis === report.fees_basis &&
    final.fees_basis === report.fees_basis;
  if (
    value.family !== 'daily-simulation-result-3' ||
    value.stored_family !== 'daily-simulation-result-3' ||
    !(currentEngine || legacyEngine) ||
    value.strategy_version !== 'daily-long-cash-3' ||
    engine.adapter_version !== adapter ||
    !(legacyFees || fullFees) ||
    report.trade_count_basis !== 'CLOSED_ROUND_TRIPS' ||
    report.order_count_basis !== 'ADAPTER_STRATEGY_SUBMISSION_INTENTS' ||
    !Number.isSafeInteger(report.trade_count) ||
    Number(report.trade_count) < 0 ||
    record(manifest.trades).total !== report.trade_count ||
    !Number.isSafeInteger(report.open_trade_count) ||
    Number(report.open_trade_count) < 0
  )
    throw new Error('回测记录缺少计算方法或模拟交易规则，请重新打开。');
  if (allExecutedFees) {
    const accounting = record(value.full_accounting), request = record(value.request);
    if (accounting.basis !== 'ALL_EXECUTED_ENTRY_EXIT_FEES_NO_DOUBLE_DEDUCTION' ||
      typeof report.fees !== 'string' || !report.fees ||
      accounting.fees !== report.fees || final.fees !== report.fees ||
      typeof request.spread_bps !== 'string' || !request.spread_bps ||
      cost.spread_bps !== request.spread_bps || Object.hasOwn(request, 'slippage_bps'))
      throw new Error('回测结果的价差或佣金记录不一致，请重新加载。');
  }
  const opened = rows(value.open_trades),
    pending = rows(value.pending_orders);
  if (opened.length !== report.open_trade_count)
    throw new Error('未平仓数量与明细不一致，请重新加载。');
  return { engine, report, cost, final, opened, pending, allExecutedFees };
}
export function BacktestingReport({ value, run }: { value: RecordData; run: string }) {
  let view: ReturnType<typeof projectBacktestingReport>;
  try {
    view = projectBacktestingReport(value);
  } catch (error) {
    return <ErrorBox error={error} />;
  }
  const { engine, report, final, opened, pending, allExecutedFees } = view,
    request = record(value.request),
    coverage = record(value.coverage),
    policy = record(value.price_policy),
    warmup = record(report.warmup);
  return (
    <>
      <h3>
        {display(value.name)} · {display(value.ticker)}
      </h3>
      <p>
        <strong>计算工具：{engine.name === 'financeanalyze.daily' ? 'FinanceAnalyze' : 'Backtesting.py'} {display(engine.version)}</strong>
      </p>
      <dl className="key-value">
        <dt>请求日期</dt>
        <dd>
          {display(request.start_date)} 至 {display(request.end_date)}
        </dd>
        <dt>实际行情范围 / 条数</dt>
        <dd>
          {display(coverage.actual_first)} 至 {display(coverage.actual_last)} /{' '}
          {display(coverage.used_rows)}
        </dd>
        <dt>期末账户总额（USD）</dt>
        <dd>{display(final.equity)}</dd>
      </dl>
      <HistoryResultOverview
        summary={value}
        policyLabel={typeof policy.display_name === 'string' ? policy.display_name : null}
        policyMessage={
          policy.mode === 'AKSHARE_STOCK_US_DAILY_NATIVE_QFQ_ROUND4_V1'
            ? '新浪提供的是换算后的价格，部分空值可能用相邻数据补过。行情覆盖、缺口和成交量单位尚未核实。回测用换算后的数量模拟买卖，未另计分红、拆股或分红再投资。'
            : ''
        }
        quantityBasis={null}
        policyEvidence={null}
      />
      {policy.mode === 'AKSHARE_STOCK_US_DAILY_NATIVE_QFQ_ROUND4_V1' &&
        <details>
          <summary>查看价格算法和数据限制</summary>
          <p>来源对完整表前向和后向填充，使用浮点计算并四舍五入到四位小数。实际覆盖、缺口、成交量单位和公司行为数量均未认证。原始价格和复权因子的来源链尚未独立核实。报告采用调整价对应的模拟单位，不能当成实际股数或股东总回报。</p>
        </details>}
      <h3>已买入并卖出的交易 · {display(report.trade_count)} 笔</h3>
      <p className="muted">
        每笔手续费是买入和卖出时收取的费用合计。
        {allExecutedFees
          ? '上方手续费总额已计入尚未卖出持仓的买入费。剩余持仓按最后的价格估算。'
          : '尚未卖出持仓的买入费已从账户总额扣除。这些费用没有计入本节已买入并卖出的交易。'}
      </p>
      <HistorySeriesTable
        key={`${run}:trades`}
        run={run}
        kind="trades"
        columns={[
          { key: 'entry_session_date', label: '买入日期' },
          { key: 'exit_session_date', label: '卖出日期' },
          { key: 'quantity', label: '数量' },
          { key: 'entry_price', label: '买入价 / USD' },
          { key: 'exit_price', label: '卖出价 / USD' },
          { key: 'pnl', label: '盈亏 / USD' },
          { key: 'fee', label: '买卖手续费合计（USD）' },
          {
            key: 'return_fraction',
            label: '收益率',
            render: (row) => (
              <span title={display(row.return_fraction)}>
                {upstreamPercent(row.return_fraction)}
              </span>
            ),
          },
        ]}
        empty="这次没有完成买入和卖出的交易。"
      />
      <h3>结束时仍持有 · {display(report.open_trade_count)} 项</h3>
      <ServerRows
        value={opened}
        columns={[
          { key: 'entry_session_date', label: '买入日期' },
          { key: 'quantity', label: '数量' },
          { key: 'entry_price', label: '买入价 / USD' },
          { key: 'mark_pnl_before_commissions', label: '剩余持仓估算赚亏（未扣手续费，USD）' },
        ]}
        empty="结束时没有剩余持仓。"
      />
      {pending.length > 0 && (
        <>
          <h3>期末待执行订单</h3>
          <ServerRows
            value={pending}
            columns={[
              { key: 'size', label: '订单规模（按引擎记录）' },
              {
                key: 'reason',
                label: '状态',
                render: (row) =>
                  row.reason === 'END_OF_DATA_NO_FOLLOWING_OBSERVED_BAR'
                    ? '期末无后续行情，尚未执行'
                    : '暂无可解释状态',
              },
            ]}
            empty="没有待执行订单。"
          />
        </>
      )}
      <details>
        <summary>每天的账户总额和策略记录</summary>
        <h4>每天的账户总额</h4>
        <HistorySeriesTable
          key={`${run}:equity_curve`}
          run={run}
          kind="equity_curve"
          columns={[
            { key: 'at', label: '日期' },
            { key: 'equity', label: '账户总额（USD）' },
          ]}
          empty="没有保存每天的账户总额。"
        />
        <p className="muted">引擎未提供逐日现金和持仓明细。</p>
        <h4>策略订单意图</h4>
        <p className="muted">这是策略提交的意图，不是实际成交记录。</p>
        <HistorySeriesTable
          key={`${run}:orders`}
          run={run}
          kind="orders"
          columns={[
            { key: 'decision_session_date', label: '决策日期' },
            {
              key: 'side',
              label: '意图',
              render: (row) =>
                row.side === 'buy'
                  ? '申请买入'
                  : row.side === 'close'
                    ? '申请平仓'
                    : display(row.side),
            },
            { key: 'order_id', label: '记录编号' },
          ]}
          empty="没有订单意图。"
        />
        <h4>引擎提示</h4>
        <HistorySeriesTable
          key={`${run}:rejections`}
          run={run}
          kind="rejections"
          columns={[{ key: 'reason', label: '提示' }]}
          empty="没有引擎提示。"
        />
        <p>
          首次策略决策：
          {warmup.first_decision_session == null ? '暂无' : display(warmup.first_decision_session)}
          。均线先积累所需有效行情，再按引擎默认时机决策。
        </p>
      </details>
      <details>
        <summary>费用设置与保存信息</summary>
        <dl className="key-value">
          <dt>模拟买卖价格差（基点）</dt>
          <dd>{display(request.spread_bps)}</dd>
          <dt>每单位手续费（USD）</dt>
          <dd>{display(request.fee_per_share)}</dd>
          <dt>每笔最低手续费（USD）</dt>
          <dd>{display(request.minimum_fee)}</dd>
          <dt>期末处理</dt>
          <dd>结束时剩余持仓继续计入账户，价值按最后的价格估算。</dd>
        </dl>
        <JsonView
          value={{
            engine: value.engine,
            engine_version: value.engine_version,
            strategy_version: value.strategy_version,
            calculation_version: value.calculation_version,
            stored_result_hash: value.stored_result_hash,
            history_reference: value.history_reference,
            observation_reference: value.observation_reference,
          }}
        />
      </details>
    </>
  );
}
