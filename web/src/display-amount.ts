// Display formatting only: saved numeric strings and chart points remain unchanged.
// Reuses the existing zh-CN Intl.NumberFormat convention used for report percentages.
const money = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 2 });
function finiteNumber(value: unknown) {
  if ((typeof value !== 'number' && (typeof value !== 'string' || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value))) || !Number.isFinite(Number(value))) return '无数据';
  return Number(value);
}
export function formatAmount(value: unknown): string {
  const parsed = finiteNumber(value);
  if (typeof parsed !== 'number') return parsed;
  return money.format(Math.abs(parsed) < 0.005 ? 0 : parsed);
}
export function formatReturn(value: unknown): string {
  const parsed = finiteNumber(value);
  return typeof parsed === 'number' ? percent.format(Math.abs(parsed) < 0.00005 ? 0 : parsed) : parsed;
}
