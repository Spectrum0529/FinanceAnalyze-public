import { ApiError, post } from './api';
import type { RecordData } from './api';
import type { DailyBacktestFormConfig, DailyBacktestInput } from './provider-backtest-form';
export const DAILY_CATALOG = '/research/daily/catalog';
export const DAILY_RESULTS = '/research/daily/results';
const object = (v: unknown): RecordData => {
  if (!v || typeof v !== 'object' || Array.isArray(v))
    throw new Error('服务返回的数据格式不完整。');
  return v as RecordData;
};
const text = (v: unknown) => {
  if (typeof v !== 'string' || !v) throw new Error('服务返回的选项或默认值不完整。');
  return v;
};
export function normalizeDailyCatalog(raw: RecordData): DailyBacktestFormConfig {
  const d = object(raw.defaults),
    limits = object(raw.limits);
  if (
    raw.result_family !== 'daily-simulation-result-1' ||
    raw.data_family !== 'daily-observation-series-1' ||
    !Number.isInteger(limits.max_calendar_days) ||
    Number(limits.max_calendar_days) < 1 ||
    Number(limits.max_calendar_days) > 366
  )
    throw new Error('服务的数据类型或日期范围规则不兼容。');
  if (
    !Array.isArray(raw.items) ||
    !Array.isArray(raw.strategies) ||
    !Array.isArray(raw.providers) ||
    limits.end_inclusive !== true
  )
    throw new Error('服务未提供完整股票、策略或包含结束日的日期规则。');
  const symbols = raw.items.map((item) => {
    const x = object(item);
    const id = text(x.ticker),
      name = text(x.name);
    const labels: Record<string, string> = {
      AAPL: '苹果（AAPL）',
      MSFT: '微软（MSFT）',
      NVDA: '英伟达（NVDA）',
      AMD: 'AMD',
      GOOG: '谷歌（GOOG）',
      AMZN: '亚马逊（AMZN）',
    };
    return { id, label: labels[id] || `${name}（${id}）` };
  });
  const strategies = raw.strategies.map((item) => {
    const x = object(item);
    if (
      !Number.isInteger(x.warmup_bars) ||
      Number(x.warmup_bars) < 0 ||
      Number(x.warmup_bars) >= 400
    )
      throw new Error('服务返回的策略所需日线条数不完整。');
    return {
      id: text(x.id),
      label: text(x.name),
      description: text(x.description),
      minimumRows: Number(x.warmup_bars) + 1,
    };
  });
  const providers = raw.providers.map((item) => {
    const id = text(item);
    if (!['yfinance', 'akshare'].includes(id)) throw new Error('服务返回了尚未支持的数据来源。');
    const labels =
      raw.provider_labels &&
      typeof raw.provider_labels === 'object' &&
      !Array.isArray(raw.provider_labels)
        ? (raw.provider_labels as RecordData)
        : {};
    return {
      id,
      label:
        typeof labels[id] === 'string' && labels[id]
          ? String(labels[id])
          : id === 'akshare'
            ? 'AKShare'
            : 'yfinance',
    };
  });
  const end = text(limits.last_allowed_end_date);
  const parsed = new Date(`${end}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== end)
    throw new Error('服务返回的最晚日期无效。');
  const originalDay = parsed.getUTCDate();
  parsed.setUTCDate(1);
  parsed.setUTCMonth(parsed.getUTCMonth() - 3);
  const lastDay = new Date(
    Date.UTC(parsed.getUTCFullYear(), parsed.getUTCMonth() + 1, 0),
  ).getUTCDate();
  parsed.setUTCDate(Math.min(originalDay, lastDay));
  if (!symbols.length || !strategies.length || !providers.some((p) => p.id === 'yfinance'))
    throw new Error('暂时没有可用股票、策略或默认数据来源。');
  return {
    symbols,
    strategies,
    providers,
    maxCalendarDays: Number(limits.max_calendar_days),
    lastAllowedEnd: end,
    earliestStart: text(limits.earliest_start),
    defaults: {
      symbol: symbols[0].id,
      start_date: parsed.toISOString().slice(0, 10),
      end_date: end,
      strategy: strategies.find((s) => s.id === 'buy_hold')?.id ?? strategies[0].id,
      provider: 'yfinance',
      initial_cash: text(d.initial_cash),
      fee_per_share: text(d.fee_per_share),
      minimum_fee: text(d.minimum_fee),
      slippage_bps: text(d.slippage_bps),
    },
  };
}
export type DailyRequest = Omit<DailyBacktestInput, 'symbol'> & {
  ticker: string;
  idempotency_key: string;
};
export function dailyRequest(input: DailyBacktestInput, key: string): DailyRequest {
  const { symbol, ...values } = input;
  return { ...values, ticker: symbol, idempotency_key: key };
}
export function submitDaily(request: DailyRequest) {
  return post<RecordData>('/research/daily/backtests', request);
}
export function isCertainDailyRejection(error: unknown) {
  return (
    error instanceof ApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    ![408, 409, 429].includes(error.status)
  );
}
export const dailyResultLink = (run: string) => `#results?daily_run=${encodeURIComponent(run)}`;
export const legacyResultLink = (run: string) => `#results?run=${encodeURIComponent(run)}`;

export function dailyFailureHint(code: unknown) {
  const messages: Record<string, string> = {
    DAILY_LEGACY_SLIPPAGE_UNSUPPORTED:
      '这次请求使用旧版滑点，不能按新引擎运行。请新建选择并使用高级设置的买卖价差；旧滑点不会自动换算。',
    DAILY_INSUFFICIENT_SESSIONS:
      '实际日线条数不足：买入持有至少2条，均线策略至少21条。请扩大日期范围；不会额外抓取预热行情。',
    PROVIDER_EMPTY_OR_FIELDS_MISSING:
      '数据来源没有返回可用行情或缺少所需字段。请核对股票、日期与数据来源。',
    DAILY_DATE_RANGE_INVALID:
      '日期范围不受支持。请选择已结束的日线，日期两端包含；允许长度以当前服务规则为准。',
    DAILY_PRICE_POLICY_UNSUPPORTED:
      '这个回测入口无法使用该价格类型。请改选受支持的数据，并保留错误代码以便排查。',
    DAILY_KNOWN_FACTOR_QUALITY_RESTRICTION:
      '这只证券的来源复权因子存在已知质量限制，本次未继续，也未退回未复权价格。',
    DAILY_ADJUSTED_PRICE_BELOW_SIMULATION_TICK:
      '调整后价格低于本次模拟的美分刻度，本次未继续；这不代表该证券实际不可交易。',
    DAILY_FACTOR_BINDING_REQUIRED: '缺少本次复权因子的不可变证据，未使用未复权价格代替。',
    SECURITY_MARKET_OR_IDENTITY_UNSUPPORTED:
      '服务无法核实为当前支持的美元美股或ETF。请检查准确代码；不会猜别名或切换来源。',
    HISTORY_WINDOW_UNAVAILABLE:
      '指定保存版本不覆盖所选区间，本次未自动下载或更换版本。请修改日期或明确重新获取。',
    DAILY_CATALOG_WINDOW_UNSUPPORTED: '所选日期不在当前本地开发目录允许范围内，请调整日期。',
    DAILY_SOURCE_CHANGED: '数据来源记录已变化。本次没有继续，请检查来源记录后再开始。',
    DAILY_SOURCE_CONFLICT: '已有来源记录发生冲突。本次未覆盖原有声明，请检查数据来源。',
    DAILY_SOURCE_LIMIT: '可用数据来源数量达到限制，请检查数据来源管理。',
    DAILY_OBSERVATION_BINDING_MISMATCH: '下载结果与请求的数据身份不匹配，本次已阻止继续。',
    DAILY_SESSION_ORDER_INVALID: '行情日期重复或顺序异常，本次没有自动排序或补齐后继续。',
    DAILY_COST_CONFIGURATION_INVALID: '资金或成本设置不受支持，请检查高级设置。',
  };
  return (
    messages[String(code)] ||
    '任务未完成。请保留错误代码并在任务详情核对原因；不会自动重试或切换来源。'
  );
}
