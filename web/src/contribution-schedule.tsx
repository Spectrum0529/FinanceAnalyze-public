import { NativeDateInput } from './native-date-input';
import { useState } from 'react';
import { AreaHelp } from './area-help';
export type Contribution = { date: string; amount: string };
function validDate(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
}
function positiveAmount(value: string) { return /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) && /[1-9]/.test(value); }
export function contributionRows(rows: Contribution[]): Contribution[] {
  if (rows.some((row) => !validDate(row.date) || !positiveAmount(row.amount.trim()))) throw new Error('请为每笔定投选择有效日期，并填写大于零的金额。');
  return rows.map((row) => ({ date: row.date, amount: row.amount.trim() }));
}
export function monthlyContributions(start: string, end: string, amount: string): Contribution[] {
  if (!validDate(start) || !validDate(end) || start > end || !positiveAmount(amount.trim())) throw new Error('请填写有效起止日期和大于零的每月金额。');
  const [year, month, day] = start.split('-').map(Number), result: Contribution[] = [];
  for (let index = year * 12 + month - 1; index <= Number(end.slice(0, 4)) * 12 + Number(end.slice(5, 7)) - 1; index++) {
    
    const y = Math.floor(index / 12), m = index % 12, last = [31, y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m];
    const date = `${String(y).padStart(4, '0')}-${String(m + 1).padStart(2, '0')}-${String(Math.min(day, last)).padStart(2, '0')}`;
    if (date <= end) { if (result.length >= 600) throw new Error('按月快捷生成最多 600 期，请缩短日期范围。'); result.push({ date, amount: amount.trim() }); }
  }
  return result;
}
export function ContributionSchedule({ value, changed, disabled = false, reserved = false }: { value: Contribution[]; changed: (value: Contribution[]) => void; disabled?: boolean; reserved?: boolean }) {
  const [start, setStart] = useState(''), [end, setEnd] = useState(''), [amount, setAmount] = useState(''), [error, setError] = useState('');
  return <fieldset><legend>{reserved ? '分批买入预算（已含在初始本金内）' : '追加本金定投日期与金额'}</legend><AreaHelp topic={reserved ? 'reserved-contributions' : 'contributions'} title={reserved ? '分批买入预算' : '追加本金日期和金额'} />
    <p>{reserved ? '基准起始资金全部放在现金中，按下列日期从这笔钱分批买入。每笔预算含买入费用，总预算不能超过起始资金，其余资金留在现金中；始终使用同一笔本金。' : '策略仍只使用起始资金；以下追加金额仅投入所选基准，结果另列参考。'}</p>
    <details><summary>按月快捷填写</summary><label>定投起始日期<NativeDateInput value={start} disabled={disabled} onChange={(e) => setStart(e.target.value)} /></label><label>定投截止日期<NativeDateInput value={end} disabled={disabled} onChange={(e) => setEnd(e.target.value)} /></label><label>{reserved ? '每月买入预算（含费用）' : '每月投入金额'}<input inputMode="decimal" value={amount} disabled={disabled} onChange={(e) => setAmount(e.target.value)} /></label><button type="button" disabled={disabled} onClick={() => { try { const rows = monthlyContributions(start, end, amount); changed(rows); setError(''); } catch (e) { setError(e instanceof Error ? e.message : '请核对定投日期与金额。'); } }}>{reserved ? '生成并替换分批预算' : '生成并替换定投日期'}</button><p>按起始日的日号逐月生成；该月没有此日时用月末。生成后可逐笔修改，非交易日由后台基准规则处理。</p></details>
    {value.map((row, index) => <div className="form-grid" key={index}><label>第 {index + 1} 笔日期<NativeDateInput value={row.date} disabled={disabled} onChange={(e) => changed(value.map((item, i) => i === index ? { ...item, date: e.target.value } : item))} /></label><label>第 {index + 1} 笔{reserved ? '预算（含费用）' : '金额'}<input inputMode="decimal" value={row.amount} disabled={disabled} onChange={(e) => changed(value.map((item, i) => i === index ? { ...item, amount: e.target.value } : item))} /></label><button type="button" disabled={disabled} onClick={() => changed(value.filter((_, i) => i !== index))}>移除第 {index + 1} 笔</button></div>)}
    <button type="button" disabled={disabled} onClick={() => changed([...value, { date: '', amount: '' }])}>{reserved ? '添加一笔分批买入' : '添加一笔定投'}</button>{value.length === 0 && <p>{reserved ? '未安排分批预算时，将保持全现金，不安排买入；可作为现金参考，或按月生成、添加一笔预算。' : '还没有追加计划，可按月生成或添加一笔。'}</p>}{error && <p role="alert">{error}</p>}
  </fieldset>;
}
