import type { RecordData } from './api';
import { record } from './native-research-api';

export const builtInStrategyNames: Record<string, string> = {
  buy_hold: '买入并持有', sma20: '20日均线', sma_cross_5_20: '5/20日均线交叉',
};

export function portfolioReportName(report: RecordData): string | undefined {
  if (typeof report.title === 'string' && report.title.trim()) return report.title;
  const saved = record(report.strategy_summary);
  const strategy = record(report.request).strategy;
  const strategyName = typeof saved.label === 'string' && saved.label.trim() ? saved.label
    : typeof strategy === 'string' && Object.hasOwn(builtInStrategyNames, strategy) ? builtInStrategyNames[strategy] : '';
  const securities = Array.isArray(report.securities)
    ? report.securities.filter((value): value is string => typeof value === 'string' && Boolean(value.trim()))
    : Object.keys(record(report.allocations));
  const names = securities.slice(0, 3).join('、') + (securities.length > 3 ? ` 等 ${securities.length} 只` : '');
  return [names, strategyName].filter(Boolean).join(' · ') || undefined;
}

const returnRules: Record<string, string> = {
  PRICE_AND_PROVIDED_CASH_DIVIDENDS: '计入价格变化和数据中提供的现金分红。',
  PRICE_ONLY_DIVIDENDS_NOT_PROVIDED: '按价格变化计算。数据未提供现金分红。',
  ADJUSTED_PRICE_PATH_NOT_SHAREHOLDER_TOTAL_RETURN: '使用调整后的价格模拟，未另加现金分红。',
  SOURCE_PRICE_UNITS_AS_RECORDED_NO_ADDED_CORPORATE_ACTIONS: '使用来源保存的价格单位，未另加拆股或分红处理。',
  PRICE_SERIES_SIMULATION_NOT_TOTAL_RETURN: '用保存的价格模拟收益，未提供股票总回报。',
  price: '只看价格变化。',
};

export const returnRuleLabel = (value: unknown) =>
  typeof value === 'string' && Object.hasOwn(returnRules, value) ? returnRules[value] : '原报告未说明收益算法。';

export function savedResultTitle(row: RecordData) {
  const title = typeof row.title === 'string' ? row.title : '已保存报告';
  if (row.source !== 'daily') return title;
  const split = title.lastIndexOf(' · ');
  const strategy = title.slice(split + 3);
  return split >= 0 && Object.hasOwn(builtInStrategyNames, strategy)
    ? title.slice(0, split + 3) + builtInStrategyNames[strategy] : title;
}
