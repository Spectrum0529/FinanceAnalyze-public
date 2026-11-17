import { SavedModelUsage } from './model-usage-panel';
import { ComputationCacheProvenance } from './computation-cache-provenance';
import { ModelObservationUsage } from './model-observation-usage';
import { useEffect, useMemo, useRef, useState } from 'react';
import { use } from 'echarts/core';
import { HeatmapChart } from 'echarts/charts';
import { CalendarComponent, VisualMapComponent, GridComponent, TooltipComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
import type { EChartsOption } from 'echarts';
import type { RecordData } from './api';
import { formatAmount, formatReturn } from './display-amount';
import { portfolioReportName, returnRuleLabel } from './backtest-presentation';
import { amountWithCurrency, cnDailySimulation, currencyLabel, reportCurrency } from './report-currency';
import type { ReportCurrency } from './report-currency';
import { ErrorBox, JsonView, Notice } from './components';
import { record } from './native-research-api';
import { AreaHelp } from './area-help';
import { ServerRows } from './research-visuals';
import { bindEngineChart, projectEngineChart } from './engine-chart';
import { benchmarkId, indexEtfs } from './index-benchmarks';
import type { EngineChartSeries } from './engine-chart';
use([HeatmapChart, CalendarComponent, VisualMapComponent, GridComponent, TooltipComponent, SVGRenderer]);
const rows = (value: unknown) => Array.isArray(value) ? value.map(record) : [];
const amount = (value: unknown) => value == null ? '无数据' : String(value);
const money = (value: unknown) => <span title={amount(value)}>{formatAmount(value)}</span>;
type LoadBenchmarkCurve = (signal: AbortSignal) => Promise<RecordData[]>;

function useBenchmarkCurve(load: LoadBenchmarkCurve | undefined, requested: boolean) {
  const [rows, setRows] = useState<RecordData[]>();
  const [error, setError] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!load || !requested || rows) return;
    const controller = new AbortController();
    setError(null);
    void load(controller.signal).then(result => {
      if (!controller.signal.aborted) setRows(result);
    }).catch(error => {
      if (!controller.signal.aborted) setError(error);
    });
    return () => controller.abort();
  }, [load, requested, retry, rows]);
  return { rows, error, retry: () => setRetry(value => value + 1) };
}

const curveMedia = [
  { query: { maxWidth: 430 }, option: { grid: { left: 16, right: 12 }, xAxis: { splitNumber: 3 }, yAxis: { nameGap: 9 } } },
  { option: { grid: { left: 70, right: 24 }, xAxis: { splitNumber: 5 }, yAxis: { nameGap: 15 } } },
];
export function calendarOption(values: RecordData[], period: string, yearly: boolean, currency?: ReportCurrency): EChartsOption {
  const filtered = values.filter((row) => String(row.at).startsWith(period) && (yearly ? row.net_pnl != null && row.complete === true : row.daily_pnl != null));
  const data = filtered.map((row) => { const raw = row[yearly ? 'net_pnl' : 'daily_pnl']; if (typeof raw !== 'string' && typeof raw !== 'number' || !Number.isFinite(Number(raw))) throw new Error('盈亏金额异常，未绘制为零。'); return { value: yearly ? [Number(String(row.at).slice(5, 7)) - 1, 0, Number(raw)] : [String(row.at), Number(raw)], rawAt: row.at, rawPnl: raw }; });
  const max = Math.max(1, ...data.map((row) => Math.abs(Number(row.value.at(-1)))));
  const common = { animation: false, tooltip: { renderMode: 'richText' as const, formatter: (param: unknown) => { const datum = record(record(param).data); return `${String(datum.rawAt)}\n净盈亏 ${currency ? amountWithCurrency(datum.rawPnl, currency) : formatAmount(datum.rawPnl) + '（原币种金额）'}`; } }, visualMap: { min: -max, max, calculable: true, orient: 'horizontal' as const, left: 'center', bottom: 0, formatter: formatAmount, inRange: { color: ['#ba4b45', '#f7f8f6', '#377d61'] } } };
  return yearly ? { ...common, grid: { top: 30, bottom: 65, left: 45, right: 15 }, xAxis: { type: 'category', data: Array.from({ length: 12 }, (_, i) => `${i + 1}月`) }, yAxis: { type: 'category', data: ['净盈亏'] }, series: [{ type: 'heatmap', data, label: { show: true, formatter: (param) => formatAmount(record(param.data).rawPnl) } }] } : { ...common, calendar: { range: period, top: 35, left: 50, right: 20, bottom: 65, cellSize: ['auto', 25], dayLabel: { firstDay: 1, nameMap: 'ZH' }, monthLabel: { nameMap: 'ZH' }, yearLabel: { show: false }, itemStyle: { borderColor: '#e4e8e2' } }, series: [{ type: 'heatmap', coordinateSystem: 'calendar', data }] };
}
function NativeView({ option, label }: { option: EChartsOption; label: string }) {
  const element = useRef<HTMLDivElement>(null), [error, setError] = useState(false);
  const binding = useRef<ReturnType<typeof bindEngineChart> | null>(null);
  useEffect(() => {
    if (!element.current) return;
    const current = bindEngineChart(element.current, () => setError(true));
    binding.current = current;
    return () => { current.dispose(); binding.current = null; };
  }, []);
  useEffect(() => binding.current?.update(option), [option]);
  return <>{error && <Notice>暂时无法绘图，可查看下方表格。</Notice>}<div ref={element} role="img" aria-label={label} style={{ width: '100%', height: 320 }} /></>;
}
const comparisonReasons: Record<string, string> = {
  MISSING_START_SESSION: '缺少策略开始日的行情，未模拟买入。',
  MISSING_END_SESSION: '缺少策略结束日的行情，收益率仅展示已覆盖的日期。',
  CURRENCY_MISMATCH: '策略和基金币种不同，未换算起始资金。',
  RETURN_BASIS_MISMATCH: '策略和基金的价格调整或收益口径不同，收益率仅供参考。',
  OBSERVATION_CALENDAR_MISMATCH: '策略和基金的实际交易日不同，收益率仅供参考。',
  NO_AFFORDABLE_UNIT: '起始资金不足以买入一个单位，账户保留现金。',
};
const comparisonViews = {
  return_fraction: '累计赚亏百分比',
  equity: '账户总额',
  net_profit: '扣费后赚亏金额',
  contributed_capital: '累计投入本金',
} as const;
type ComparisonView = keyof typeof comparisonViews;

function IndexBenchmarkComparison({ value, currency }: { value: RecordData; currency?: ReportCurrency }) {
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set());
  const [requestedView, setView] = useState<ComparisonView>('return_fraction');
  const strategyCurve = useMemo(() => rows(value.equity_curve), [value.equity_curve]);
  const benchmarks = useMemo(() => rows(value.benchmarks).flatMap(row => {
    const id = benchmarkId(row.id);
    return id ? [{ ...indexEtfs[id], id, row, curve: rows(row.curve) }] : [];
  }), [value.benchmarks]);
  const hasAmount = (row: RecordData) => Boolean(currency && row.currency === currency &&
    row.comparable === true && row.amount_comparable === true);
  const canPlot = (view: ComparisonView) => view === 'return_fraction' ||
    Boolean(currency && strategyCurve.some(point => point[view] != null));
  const measure = canPlot(requestedView) ? requestedView : 'return_fraction';
  const showAmounts = measure !== 'return_fraction';
  const strategyOnly = showAmounts && !benchmarks.some(({ row, curve }) =>
    hasAmount(row) && curve.some(point => point[measure] != null));
  const chartLabel = (strategyOnly ? '策略账户 ' : '策略与指数 ETF ') + comparisonViews[measure] + '曲线';
  const projection = useMemo(() => {
    try {
      const pointsFor = (curve: RecordData[]) => curve.map(row => ({ at: row.at, equity: row[measure] }));
      const sources: EngineChartSeries[] = [
        { id: 'strategy', label: '策略账户', color: '#377d61', points: pointsFor(strategyCurve) },
        ...benchmarks.filter(({ row, curve }) =>
          (!showAmounts || hasAmount(row)) && curve.some(point => point[measure] != null))
          .map(({ id, label, color, curve }) => ({ id, label, color, points: pointsFor(curve) })),
      ];
      return { result: projectEngineChart(sources, currency ?? null), error: null };
    } catch (error) { return { result: null, error }; }
  }, [strategyCurve, benchmarks, measure, showAmounts, currency]);
  const graph = useMemo<EChartsOption | null>(() => {
    const projected = projection.result;
    if (!projected?.finitePoints) return null;
    const { option } = projected;
    const format = showAmounts ? formatAmount : formatReturn;
    const unit = showAmounts ? comparisonViews[measure] + ' / ' + currency : '累计赚亏百分比（%）';
    const selected = Object.fromEntries(option.series.map(series => [series.name, !hidden.has(series.id)]));
    return {
      ...option, media: curveMedia, legend: { ...option.legend, selectedMode: false, selected },
      aria: { enabled: true, label: { description: chartLabel } },
      yAxis: { ...option.yAxis, name: unit, axisLabel: { ...option.yAxis.axisLabel, formatter: format } },
      tooltip: { ...option.tooltip, formatter: (params: unknown) => (Array.isArray(params) ? params : [params]).map(raw => {
        const item = record(raw), point = record(item.data);
        return `${String(item.seriesName)}\n${String(point.rawAt)}\n${showAmounts
          ? amountWithCurrency(point.rawEquity, currency) : formatReturn(point.rawEquity)}`;
      }).join('\n\n') },
    };
  }, [projection, measure, showAmounts, currency, chartLabel, hidden]);
  const hasAshr = benchmarks.some(({ id }) => id === 'csi300');
  return <section aria-label="三只指数基金的对照">
    <div className="section-heading"><h3>三只指数基金的对照</h3><AreaHelp topic="index-etf-benchmarks" title="三只指数基金的对照" /></div>
    <p>三只基金各用相同的起始资金，在策略首个交易日开盘各买一次并持有。未另计现金分红。</p>
    {hasAshr && <p>沪深300基金（美股 ASHR）以美元交易，表现受汇率、基金费用和跟踪差异影响。</p>}
    <label>对比单位<select value={measure} onChange={event => setView(event.target.value as ComparisonView)}>
      {(Object.keys(comparisonViews) as ComparisonView[]).map(view => <option key={view} value={view} disabled={!canPlot(view)}>
        {comparisonViews[view]}{view === 'return_fraction' ? '（%）' : currency ? '（' + currency + '）' : ''}
      </option>)}
    </select></label>
    {strategyOnly && <p>这个金额视图只显示策略。切换到“累计赚亏百分比”可以查看基金走势。</p>}
    <fieldset><legend>显示对比曲线</legend>{benchmarks.map(({ id, color, label, row, curve }) => {
      const available = (!showAmounts || hasAmount(row)) && curve.some(point => point[measure] != null);
      return <label key={id}>
        <input type="checkbox" checked={available && !hidden.has(id)} disabled={!available} onChange={event => {
          const checked = event.target.checked;
          setHidden(previous => { const next = new Set(previous); if (checked) next.delete(id); else next.add(id); return next; });
        }} />
        <span aria-hidden="true" style={{ display: 'inline-block', width: 16, borderTop: '3px solid ' + color, marginInline: 6, verticalAlign: 'middle' }} />
        {label}
      </label>;
    })}</fieldset>
    {projection.error != null ? <ErrorBox error={projection.error} />
      : graph ? <NativeView option={graph} label={chartLabel} />
      : <Notice>此报告没有可显示的对比曲线。</Notice>}
    <ServerRows value={benchmarks.map(({ label, row, curve }) => ({ ...row, label, last_return: curve.at(-1)?.return_fraction }))} columns={[
      { key: 'label', label: '对比基金' },
      { key: 'entry_session', label: '实际买入日' },
      { key: 'last_session', label: '最后估值日' },
      { key: 'initial_capital', label: '起始资金', render: row => amountWithCurrency(row.initial_capital, reportCurrency(row)) },
      { key: 'fees', label: '费用', render: row => amountWithCurrency(row.fees, reportCurrency(row)) },
      { key: 'cash_remaining', label: '保留现金', render: row => amountWithCurrency(row.cash_remaining, reportCurrency(row)) },
      { key: 'last_return', label: '截至最后估值日收益率', render: row => formatReturn(row.last_return) },
      { key: 'status', label: '能否直接比较', render: row => comparisonReasons[String(row.status)] ??
          comparisonReasons[String(row.comparability_reason)] ??
          (hasAmount(row) ? '可直接比较：币种、本金、首末日期和收益算法一致' : '账户总额无法直接比较') },
    ]} empty="这份报告没有三只指数基金的结果。" />
  </section>;
}
export function PortfolioAnalytics({ value, loadBenchmarkCurve }: { value: RecordData; loadBenchmarkCurve?: LoadBenchmarkCurve }) {
  const autonomous = value.family === 'autonomous-backtest-result-1';
  const currency = reportCurrency(value);
  const showMoney = (amount: unknown) => currency || autonomous
    ? amountWithCurrency(amount, currency) : formatAmount(amount);
  const [measure, setMeasure] = useState('equity'), [compare, setCompare] = useState(false), [yearly, setYearly] = useState(false), [period, setPeriod] = useState(''), [expanded, setExpanded] = useState(false), [worst, setWorst] = useState(false);
  const [reconciliationOpen, setReconciliationOpen] = useState(false);
  const curve = rows(value.equity_curve), benchmark = record(value.benchmark), calendar = record(value.calendar);
  const dca = record(value.request).benchmark_mode === 'dca';
  const reserved = record(value.request).benchmark_mode === 'dca_reserved';
  const overlayAllowed = !dca && benchmark.comparable === true && benchmark.coverage_complete === true;
  const compareRequested = compare && overlayAllowed;
  const benchmarkRequest = useBenchmarkCurve(loadBenchmarkCurve, compareRequested || reconciliationOpen);
  const deferredCurve = benchmarkRequest.rows;
  const showBenchmark = compareRequested && (!loadBenchmarkCurve || deferredCurve !== undefined);
  const overlayCurve = showBenchmark ? deferredCurve : undefined;
  const benchmarkPending = Boolean(loadBenchmarkCurve && !deferredCurve);
  const benchmarkStatus = benchmarkRequest.error != null
    ? <><ErrorBox error={benchmarkRequest.error} /><button type="button" onClick={benchmarkRequest.retry}>重试读取保存基准</button></>
    : <p role="status">正在读取并核对保存基准…</p>;
  const periods = [...new Set(rows(calendar[yearly ? 'monthly' : 'daily']).map((row) => String(row.at).slice(0, yearly ? 4 : 7)))];
  const chosen = periods.includes(period) ? period : periods[0] ?? '';
  const chart = useMemo(() => {
    try {
      if (!curve.length || autonomous && !currency && measure !== 'return_fraction') return null;
      const sources = [{ id: 'account', label: '策略账户', points: curve.map((row) => ({ at: row.at, equity: row[measure] })) }];
      if (showBenchmark) sources.push({ id: 'benchmark', label: String(record(benchmark.metadata).instrument_label ?? '保存基准'), points: (overlayCurve ?? rows(benchmark.curve)).map((row) => ({ at: row.at, equity: row[measure] })) });
      const projected = projectEngineChart(sources);
      const percent = measure === 'return_fraction';
      const unit = percent ? '收益率（%）' : currency ? '金额 / ' + currencyLabel(currency) : '金额（币种未提供）';
      const format = percent ? formatReturn : formatAmount;
      return { ...projected.option, media: curveMedia, yAxis: { ...projected.option.yAxis, name: unit, axisLabel: { ...projected.option.yAxis.axisLabel, formatter: format } }, tooltip: { ...projected.option.tooltip, formatter: (params: unknown) => (Array.isArray(params) ? params : [params]).map((raw) => { const row = record(raw), point = record(row.data); return `${String(row.seriesName)}\n${String(point.rawAt)}\n${point.rawEquity == null ? '无估值' : format(point.rawEquity)}${!percent && point.rawEquity != null ? ' · ' + (currency ?? '币种未提供') : ''}`; }).join('\n\n') } } as EChartsOption;
    } catch (error) { return error instanceof Error ? error : new Error('曲线未能核对。'); }
  }, [value, measure, showBenchmark, overlayCurve, autonomous, currency]);
  const { heatmap, calendarError } = useMemo(() => {
    try {
      const values = record(value.calendar);
      const option = chosen && (!autonomous || currency)
        ? calendarOption(rows(values[yearly ? 'monthly' : 'daily']), chosen, yearly, currency) : null;
      return { heatmap: option, calendarError: null };
    } catch (error) {
      return { heatmap: null, calendarError: error };
    }
  }, [value, chosen, yearly, autonomous, currency]);
  const security = record(value.security_pnl), pnl = rows(security[worst ? 'worst' : expanded ? 'all' : 'top5']);
  if (value.family === 'model-tool-research-result-1') { const tool = record(value.tool_result), gap = tool.status === 'NEEDS_DATA' || value.status === 'EVIDENCE_GAP_REPORTED'; return <><SavedModelUsage jobId={value.job_id} runId={value.run_id} source="tasks" fallback={<ModelObservationUsage observations={value.model_observations} coverage={value.model_observations_status}/>}/><Notice>{gap ? '资料不足，请按下方提示补充。当前没有完整结论。' : value.status === 'MODEL_PLAN_AND_LOCAL_LICENSED_TOOLS_COMPLETED' ? '模型根据字段说明选择分析工具。下方结果在本机计算，未发送给模型。' : '模型报告依据下面实际工具输出生成；不代表预测或投资结论已经验证。'}</Notice><p>执行方式：{({ single_assistant:'单模型助手',model_panel:'多个模型分别报告',role_team:'固定角色团队' } as Record<string,string>)[String(value.execution_mode)] ?? '保存模式'}</p>{gap ? <><p>{String(tool.reason ?? '缺少任务所需证据。')}</p><ServerRows value={Array.isArray(tool.missing_evidence) ? tool.missing_evidence.map((item) => ({ item })) : []} columns={[{key:'item',label:'需要补充'}]} empty="请补充任务所需数据后新建研究。" /></> : <PortfolioAnalytics value={tool} />}{rows(value.reports).map((report, index) => <details key={index} open><summary>{String(report.role ?? report.profile_id ?? '选定模型')} · 版本 {String(report.revision)}</summary><p style={{ whiteSpace:'pre-wrap' }}>{String(report.text)}</p></details>)}<details><summary>实际工具与调用记录</summary><JsonView value={{plan:value.plan,model_calls:value.model_calls,budget:value.budget}} /></details></>; }
  if (value.family === 'factor-exploration-result-1') return <><Notice>试验比较了三个选股线索。多试几个更容易碰巧出现好结果，不能证明在线索未见过的数据上有效，也不能据此下单。</Notice><ServerRows value={value.results} columns={[{key:'factor',label:'选股线索'},{key:'mean_rank_ic',label:'排名一致程度（平均）'},{key:'excluded_rows',label:'排除行数'},{key:'status',label:'实际状态'}]} empty="没有实际探索输出。" /><details><summary>探索范围与原始记录</summary><JsonView value={value} /></details></>;
  if (value.family === 'document-evidence-analysis-1') return <><Notice>来源和时间由提供资料的人填写。回测那天能否看到这些资料尚未核实；之后才出现的资料只能用于讨论假设。</Notice>{rows(value.documents).map((doc) => <details key={String(doc.id)}><summary>{String(doc.source)} · {String(doc.id)}</summary><p>发布时间：{amount(doc.published_at)}；能够取得的时间：{amount(doc.available_at)}</p><p style={{whiteSpace:'pre-wrap'}}>{String(doc.text)}</p></details>)}<ServerRows value={value.excluded} columns={[{key:'id',label:'排除资料'},{key:'reason',label:'原因'}]} empty="没有被截止时间排除的材料。" /></>;
  if (value.family !== 'dataset-portfolio-result-1' && value.family !== 'autonomous-backtest-result-1') return <><ComputationCacheProvenance value={value.cache_disposition} /><Notice>下面是这批数据算出的结果。能否预测、回测当天能否取得这些资料，都要另行核实。</Notice>{value.family === 'factor-validation-result-1' ? <><p>选股线索：{String(value.factor)}；排名一致程度（平均）：{amount(value.mean_rank_ic)}；排除行数：{String(value.excluded_rows)}。</p><ServerRows value={value.rank_ic} columns={[{ key: 'at', label: '日期' }, { key: 'rank_ic', label: '排名一致程度' }, { key: 'securities', label: '证券数' }]} empty="样本不足，无法计算排名一致程度。" /></> : <ServerRows value={value.items} columns={[{ key: 'ticker', label: '证券' }, { key: 'rows', label: '实际记录' }, { key: 'first', label: '首日' }, { key: 'last', label: '末日' }, { key: 'observed_close_return', label: '观察区间价格变化（小数）' }]} empty="报告未提供统计行。" />}<details><summary>保存报告详情</summary><JsonView value={value} /></details></>;
  const title = portfolioReportName(value);
  const hasIndices = Array.isArray(value.benchmarks) && value.benchmarks.length > 0;
  return <>
    {title && <h3>{title}</h3>}
    <ComputationCacheProvenance value={value.cache_disposition} />
    <p>报告币种 {currencyLabel(currency)}。</p>
    {value.market_semantics === cnDailySimulation && <Notice>简化日线模拟。使用下一开盘成交假设、整数股及保存的佣金和价差。未模拟整手规则、T+1 卖出限制、涨跌停、税费和分红。</Notice>}
    <Notice>{value.family==='autonomous-backtest-result-1'?'这是用所选历史数据做的模拟。总资金分给各证券，账户总额已扣费；缺行情时不补价格。拆股与分红的处理见报告。':'这是用所选历史数据做的模拟。总资金分给各证券，账户总额已扣费；缺行情时不补价格。当前名单不能代表历史名单。是否计入分红见报告。'}</Notice>
    {value.family==='autonomous-backtest-result-1'?<p>扣费后赚亏金额：{showMoney(record(value.reconciliation).net_profit)}；各证券的扣费后赚亏合计：{showMoney(record(value.reconciliation).security_net_pnl)}；两项相差：{showMoney(record(value.reconciliation).residual)}。账户总额已扣费。</p>:<p>扣费后赚亏金额：{showMoney(record(value.reconciliation).net_profit)}；费用：{showMoney(record(value.reconciliation).fees)}；现金等剩余项：{showMoney(record(value.reconciliation).cash_residual)}。币种以保存数据为准。</p>}
    {hasIndices ? <IndexBenchmarkComparison value={value} currency={currency} /> : <>
    <label>折线内容<select value={measure} onChange={(e) => setMeasure(e.target.value)}><option value="equity">账户总额</option><option value="net_profit">扣费后赚亏金额</option><option value="return_fraction">累计赚亏百分比（%）</option><option value="contributed_capital">累计投入本金</option></select></label>
    {value.benchmark && <label><input type="checkbox" checked={compare} disabled={!overlayAllowed} onChange={(e) => setCompare(e.target.checked)} />比较已保存基准：{String(record(benchmark.metadata).instrument_label ?? '未命名基准')}（{reserved ? '同起始资金分批定投 · ' : ''}{benchmark.return_basis === 'price' ? '价格收益' : '报告记录的算法'}）</label>}
    {compareRequested && benchmarkPending && benchmarkStatus}
    {value.benchmark && !overlayAllowed && <p>{dca ? '指数定投有额外本金，作为独立参考展示在下方。策略仍只使用起始资金，账户总额不能直接比较。' : '基准的日期或收益算法不同，下面单独展示作参考。'}</p>}
    {chart instanceof Error ? <ErrorBox error={chart} /> : chart ? <NativeView option={chart} label="账户与保存基准曲线" /> : <Notice>没有足够数据绘图。</Notice>}
    {reserved && overlayAllowed && <ReservedBenchmarkSummary value={benchmark} />}
    {value.benchmark && !overlayAllowed && <IndependentBenchmark value={benchmark} dca={dca} />}
    </>}
    <div className="section-heading"><h3>盈亏日历</h3><AreaHelp topic="calendar" title="盈亏日历" /></div><label>视图<select value={yearly ? 'year' : 'month'} onChange={(e) => setYearly(e.target.value === 'year')}><option value="month">月视图 · 按天</option><option value="year">年视图 · 按月</option></select></label><label>日期范围<select value={chosen} onChange={(e) => setPeriod(e.target.value)}>{periods.map((value) => <option key={value}>{value}</option>)}</select></label><p>红色为亏损，绿色为盈利。无数据或休市留空；存入本金不算利润。</p>
    {calendarError != null ? <ErrorBox error={calendarError} /> : heatmap ? <NativeView option={heatmap} label={yearly ? '每月净盈亏日历' : '每日净盈亏日历'} /> : <p>未提供此范围的盈亏。</p>}
    <details><summary>查看每个日期的金额</summary><ServerRows value={rows(calendar[yearly ? 'monthly' : 'daily']).filter((row) => String(row.at).startsWith(chosen))} columns={[{ key: 'at', label: '日期' }, { key: yearly ? 'net_pnl' : 'daily_pnl', label: '扣费后赚亏金额', render: (row) => showMoney(row[yearly ? 'net_pnl' : 'daily_pnl']) }, { key: yearly ? 'return_fraction' : 'daily_return', label: '保存收益率（%）', render: (row) => formatReturn(row[yearly ? 'return_fraction' : 'daily_return']) }]} empty="此范围没有观察记录。" /></details>
    <div className="section-heading"><h3>成交过的证券各赚亏多少</h3><AreaHelp topic="security-pnl" title="逐股盈亏" /></div><label><input type="checkbox" checked={worst} onChange={(e) => setWorst(e.target.checked)} />亏损最多优先</label><button onClick={() => setExpanded((value) => !value)}>{expanded ? '只看前五' : '展开全部'}</button><ServerRows value={expanded ? pnl : pnl.slice(0, 5)} columns={[{ key: 'ticker', label: '证券' }, { key: 'net_pnl', label: '扣费后赚亏金额', render: (row) => showMoney(row.net_pnl) }, { key: 'realized_pnl', label: '已卖出赚亏', render: (row) => showMoney(row.realized_pnl) }, { key: 'unrealized_pnl', label: '还持有的估算赚亏', render: (row) => showMoney(row.unrealized_pnl) }, { key: 'fees', label: '相关费用（已计入）', render: (row) => showMoney(row.fees) }, { key: 'trade_count', label: '交易次数' }]} empty="这次没有模拟成交。" />
    <details onToggle={event => setReconciliationOpen(event.currentTarget.open)}><summary>对账与报告记录的算法</summary>{reconciliationOpen && <><p>收益怎么算：{returnRuleLabel(value.return_basis)}；若与基准的资金流入或流出不同，请分开看。</p>{benchmarkPending
      ? compareRequested ? <p>等读取完上方的基准数据，再显示完整计算说明。</p> : benchmarkStatus
      : <JsonView value={{ reconciliation: value.reconciliation, benchmark: deferredCurve ? { ...benchmark, curve: deferredCurve } : value.benchmark, benchmarks: value.benchmarks, dataset_reference: value.dataset_reference, coverage: value.coverage, engine: value.engine }} />}</>}</details>
  </>;
}

export function IndependentBenchmark({ value, dca = false }: { value: RecordData; dca?: boolean }) {
  const [measure, setMeasure] = useState('equity');
  const reserved = value.benchmark_mode === 'dca_reserved';
  const curve = rows(value.curve), last = curve.at(-1), label = String(record(value.metadata).instrument_label ?? '保存基准');
  let option: EChartsOption | null = null, error: unknown = null;
  try {
    if (curve.length) {
      const projected = projectEngineChart([{ id: 'independent-benchmark', label, points: curve.map((row) => ({ at: row.at, equity: row[measure] })) }]);
      const unit = measure === 'return_fraction' ? '净收益 / 累计投入（小数）' : '金额（保存币种）';
      option = { ...projected.option, yAxis: { ...projected.option.yAxis, name: unit, axisLabel: { ...projected.option.yAxis.axisLabel, formatter: measure === 'return_fraction' ? undefined : formatAmount } }, tooltip: { ...projected.option.tooltip, formatter: (params: unknown) => (Array.isArray(params) ? params : [params]).map((raw) => { const row = record(raw), point = record(row.data); return `${label}\n${String(point.rawAt)}\n${point.rawEquity == null ? '无估值' : measure === 'return_fraction' ? String(point.rawEquity) : formatAmount(point.rawEquity)} ${unit}`; }).join('\n') } } as EChartsOption;
    }
  } catch (caught) { error = caught; }
  return <section aria-label={reserved ? '同起始资金分批定投保存参考' : dca ? '指数定投独立参考' : '保存基准独立参考'}><div className="section-heading"><h3>{reserved ? '用原本金分批买（另列参考）' : dca ? '再加钱分批买（另列参考）' : '保存基准（另列参考）'} · {label}</h3><AreaHelp topic="benchmark" title="独立基准曲线与本金" /></div><p>{reserved ? '本参考使用同一笔起始资金，分批预算含费用，剩余资金留在现金中。日期范围或收益算法不同，下面按原报告单独显示。' : '策略只用起始资金；本参考的追加资金进入基准账户，资金不同的账户总额不能直接比较。追加本金单列，净收益不包含新增本金。'}</p>
    {reserved && <ReservedBenchmarkSummary value={value} />}
    <dl><dt>保存末日</dt><dd>{amount(last?.at)}</dd><dt>基准账户总额</dt><dd>{money(last?.equity)}</dd><dt>基准累计投入本金</dt><dd>{money(last?.contributed_capital)}</dd><dt>基准累计净收益</dt><dd>{money(last?.net_profit)}</dd><dt>相关费用（已计入净收益）</dt><dd>{money(value.fees)}</dd></dl>
    {value.coverage_complete !== true && <Notice>保存基准覆盖不完整；下图只显示已有记录，请核对日期范围。</Notice>}
    <label>独立参考折线<select value={measure} onChange={(e) => setMeasure(e.target.value)}><option value="equity">基准账户总额</option><option value="contributed_capital">累计投入本金</option><option value="net_profit">扣费后赚亏金额</option><option value="return_fraction">净收益 / 累计投入（非年化）</option></select></label><p>这里的收益率按保存定义为净收益除以累计投入，不是年化收益或内部收益率。</p>
    {error != null ? <ErrorBox error={error} /> : option ? <NativeView option={option} label="独立基准保存曲线" /> : <p>没有保存曲线，请核对基准数据；此处仍保留已提供的金额信息。</p>}
    <details><summary>查看基准日期金额明细</summary><ServerRows value={curve} columns={[{ key: 'at', label: '日期' }, { key: 'equity', label: '账户总额', render: (row) => money(row.equity) }, { key: 'contributed_capital', label: '累计本金', render: (row) => money(row.contributed_capital) }, { key: 'external_flow', label: '当日新增本金', render: (row) => money(row.external_flow) }, { key: 'net_profit', label: '累计净收益', render: (row) => money(row.net_profit) }]} empty="没有保存的基准金额记录。" /></details>
    {rows(value.uninvested_contributions).length > 0 && <><p>以下追加计划尚未进入保存曲线，请核对覆盖范围：</p><ServerRows value={value.uninvested_contributions} columns={[{ key: 'date', label: '计划日期' }, { key: 'amount', label: '金额', render: (row) => money(row.amount) }]} empty="" /></>}
  </section>;
}

export function ReservedBenchmarkSummary({ value }: { value: RecordData }) {
  const reconciliation = record(value.reconciliation), empty = /^0(?:\.0+)?$/.test(String(reconciliation.scheduled_purchase_budget));
  const reason = String(value.comparability_reason ?? '');
  const explanation = reason === 'SAME_INITIAL_CAPITAL_RESERVED_PURCHASE_BUDGETS_NO_EXTERNAL_FLOWS' ? '同一笔起始资金、无追加本金；日期完整、收益算法相同时，可画在同一张图上。' : reason === 'BENCHMARK_COVERAGE_INCOMPLETE' ? '基准首末日期覆盖不完整，按保存记录另列参考。' : reason === 'RETURN_BASIS_MISMATCH' ? '基准与策略的收益算法不同，所以单独显示作参考。' : '请核对下方保存的比较原因与收益定义。';
  return <section aria-label="分批买入资金与预算"><h4>分批买入 · 资金与预算</h4>{empty && <Notice>没有安排买入，参考账户全部保留现金。</Notice>}<dl><dt>起始本金</dt><dd>{money(reconciliation.initial_capital)}</dd><dt>追加本金</dt><dd>{money(reconciliation.external_contributions)}</dd><dt>计划买入总预算（含费用）</dt><dd>{money(reconciliation.scheduled_purchase_budget)}</dd><dt>已处理日期的预算</dt><dd>{money(reconciliation.processed_purchase_budget)}</dd><dt>实际支出（含费用）</dt><dd>{money(reconciliation.spent_including_fees)}</dd><dt>剩余现金</dt><dd>{money(reconciliation.cash)}</dd><dt>费用（已计入支出与净收益）</dt><dd>{money(value.fees)}</dd></dl><p>每笔预算含买入费用，未使用部分留在现金中，不追加到下一笔预算。收益率沿用保存定义，按净收益与原始本金计算。</p><p>{explanation}</p><ServerRows value={value.unexecuted_purchase_budgets} columns={[{ key: 'date', label: '未执行预算日期' }, { key: 'amount', label: '预算金额（含费用）', render: (row) => money(row.amount) }]} empty="没有保存的未执行分批预算。" /><details><summary>保存的比较原因与原始收益定义</summary><p>{reason || '未提供比较原因'}</p><p>{amount(value.return_definition)}</p></details></section>;
}
