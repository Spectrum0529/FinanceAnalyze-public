import type { RecordData } from './api';
import type { HistoryUiConfig, SecurityChoice } from './history-view-model';
import { presetStart, typedSecurity, validateHistoryDates } from './history-view-model';
const record = (value: unknown): RecordData => {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('服务选项格式不完整。');
  return value as RecordData;
};
const text = (value: unknown) => {
  if (typeof value !== 'string' || !value) throw new Error('服务选项或默认值缺失。');
  return value;
};
// Catalog items are suggestions only. Identity is resolved by HistoryGateway in the worker.
export function catalogSuggestions(raw: RecordData, provider: string): SecurityChoice[] {
  if (!Array.isArray(raw.items) || raw.items.length > 20) throw new Error('股票建议格式不完整。');
  return raw.items.flatMap((value) => {
    const row = record(value),
      symbols = row.provider_symbols;
    if (
      symbols &&
      typeof symbols === 'object' &&
      !Array.isArray(symbols) &&
      symbols[provider] === null
    )
      return [];
    return [
      {
        ...typedSecurity(text(row.ticker), provider),
        name: text(row.name),
        identityState: 'candidate' as const,
      },
    ];
  });
}
export function normalizeHistoryCatalog(raw: RecordData): HistoryUiConfig {
  const limits = record(raw.limits),
    defaults = record(raw.defaults);
  if (
    raw.result_family !== 'daily-simulation-result-3' ||
    raw.data_family !== 'daily-history-series-2' ||
    limits.start_inclusive !== true ||
    limits.end_inclusive !== true ||
    !Number.isSafeInteger(limits.max_calendar_days) ||
    Number(limits.max_calendar_days) < 10958 ||
    Number(limits.max_calendar_days) > 11346 ||
    !Number.isSafeInteger(limits.max_rows) ||
    Number(limits.max_rows) < 1 ||
    Number(limits.max_rows) > 9000 ||
    !Array.isArray(raw.providers) ||
    !raw.providers.length ||
    !Array.isArray(raw.strategies) ||
    !raw.strategies.length
  )
    throw new Error('服务尚未提供兼容的长期历史类型和日期规则。');
  const engine = record(raw.engine),
    costModel = record(raw.cost_model);
  const adapter = engine.adapter_version;
  const currentEngine = engine.name === 'financeanalyze.daily' && engine.version === '1.0.0' &&
    adapter === 'financeanalyze-daily-calculation-1';
  const legacyEngine = engine.name === 'backtesting.py' && engine.version === '0.6.6' &&
    ['backtesting-py-adapter-1', 'backtesting-py-adapter-2', 'backtesting-py-adapter-3'].includes(String(adapter));
  if (
    !(currentEngine || legacyEngine) ||
    engine.strategy_version !== 'daily-long-cash-3' ||
    raw.strategy_version !== 'daily-long-cash-3' ||
    raw.calculation_version !== engine.adapter_version ||
    costModel.spread_bps !== (currentEngine ? 'LONG_ENTRY_PRICE_MARKUP_RAW_OPEN_EXIT' : 'UPSTREAM_CONSTANT_RELATIVE_SPREAD_NOT_LEGACY_SLIPPAGE') ||
    costModel.legacy_slippage !== 'REJECTED' ||
    Object.hasOwn(defaults, 'slippage_bps')
  )
    throw new Error('服务的回测引擎或费用选项不兼容，请重新加载。');
  if ((currentEngine || adapter === 'backtesting-py-adapter-3') &&
    (typeof defaults.spread_bps !== 'string' ||
      !/^\d+(?:\.\d+)?$/.test(defaults.spread_bps) ||
      Number(defaults.spread_bps) > 1000 ||
      !Array.isArray(raw.assumptions) ||
      !raw.assumptions.includes('ALL_EXECUTED_ENTRIES_AND_EXITS_OPEN_ENTRY_FEES_INCLUDED_ONCE')))
    throw new Error('当前回测未提供完整的买卖价差或手续费规则，请重新加载。');
  const policies = raw.provider_price_policies == null ? {} : record(raw.provider_price_policies);
  const providers = raw.providers.map((value) => {
    const id = text(value);
    if (!['yfinance', 'akshare'].includes(id)) throw new Error('服务返回了尚未支持的来源。');
    const policy = policies[id] == null ? {} : record(policies[id]);
    const nativeQfq =
      id === 'akshare' && policy.mode === 'AKSHARE_STOCK_US_DAILY_NATIVE_QFQ_ROUND4_V1';
    if (
      nativeQfq &&
      (policy.source_id !== 'history-v4-akshare-stock-us-daily-native-qfq' ||
        policy.interface !== 'stock_us_daily_native_qfq_v1' ||
        policy.quantity_basis !== 'ADJUSTED_SIMULATION_UNITS')
    )
      throw new Error('服务的原生前复权来源或单位不匹配，请重新加载。');
    if (
      id === 'akshare' && Object.keys(policy).length && !nativeQfq &&
      policy.mode !== 'SINA_QFQ_AFFINE_OBSERVED_V1'
    )
      throw new Error('这个版本暂不支持服务返回的价格类型，请改选其他数据。');
    return {
      id,
      label: nativeQfq
        ? text(record(raw.provider_labels).akshare)
        : id === 'akshare' ? 'AKShare / 新浪' : 'Yahoo / yfinance',
      help:
        nativeQfq
          ? text(policy.description)
          : id === 'akshare'
          ? '前复权价格路径；严格因子校验，不回退未复权。'
          : '使用已按拆股调整的价格，不计现金分红。',
      pricePolicyLabel:
        nativeQfq
          ? text(policy.display_name)
          : id === 'akshare' ? '前复权价格路径模拟（含源端分红调整）' : '拆股调整价格模拟（不含分红）',
    };
  });
  const strategies = raw.strategies.map((value) => {
    const row = record(value);
    const help: Record<string, string> = {
      buy_hold: '买入后持续持有，期末不自动卖出。',
      sma20: '以20条有效行情的均线判断持有或现金，信号在之后的有效开盘执行。',
      sma_cross_5_20: '以5/20条有效行情的均线判断持有或现金，信号在之后的有效开盘执行。',
    };
    return {
      id: text(row.id),
      label: text(row.name),
      help: help[text(row.id)] ?? text(row.description),
    };
  });
  const defaultProvider = text(defaults.provider),
    defaultEnd = text(limits.last_allowed_end_date),
    earliestStart = text(limits.earliest_start);
  if (!providers.some((p) => p.id === defaultProvider))
    throw new Error('默认来源不在当前服务选项中。');
  const config: HistoryUiConfig = {
    providers,
    strategies,
    defaultProvider,
    defaultStrategy: strategies.find((s) => s.id === 'buy_hold')?.id ?? strategies[0].id,
    defaultEnd,
    earliestStart,
    latestEnd: defaultEnd,
    defaultStart: presetStart(defaultEnd, 1),
    maxCalendarDays: Number(limits.max_calendar_days),
    initialCash: text(defaults.initial_cash),
    feePerShare: text(defaults.fee_per_share),
    minimumFee: text(defaults.minimum_fee),
    spreadBps: text(defaults.spread_bps ?? '0'),
    engine,
    costModel,
    strategyVersion: text(raw.strategy_version),
    calculationVersion: text(raw.calculation_version),
  };
  if (config.defaultStart < earliestStart) config.defaultStart = earliestStart;
  validateHistoryDates(config.defaultStart, config.defaultEnd, config);
  config.commonSecurities = providers.flatMap((p) => catalogSuggestions(raw, p.id));
  return config;
}
