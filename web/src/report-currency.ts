import type { RecordData } from './api';
import { formatAmount } from './display-amount';

export type ReportCurrency = 'USD' | 'CNY';
export const cnDailySimulation = 'CN_EQUITY_RAW_CNY_DAILY_SIMULATION_1';

export function reportCurrency(value: RecordData): ReportCurrency | undefined {
  const currency = value.currency;
  if (value.market === 'CN' || value.market_semantics === cnDailySimulation)
    return currency === 'CNY' ? 'CNY' : undefined;
  if (currency === 'USD' || currency === 'CNY') return currency;
  const explicitMarket = value.market !== undefined || value.market_semantics !== undefined;
  return currency === undefined && !explicitMarket && value.schema === 'autonomous-backtest-visuals-1'
    ? 'USD' : undefined;
}

export const currencyLabel = (currency?: ReportCurrency) =>
  currency === 'CNY' ? '人民币（CNY）' : currency === 'USD' ? '美元（USD）' : '币种未提供';

export function amountWithCurrency(value: unknown, currency: ReportCurrency | undefined) {
  if (!currency) return '未提供';
  const formatted = formatAmount(value);
  return formatted === '无数据' ? formatted : formatted + ' ' + currency;
}
