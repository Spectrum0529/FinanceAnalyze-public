import { useState, useEffect } from 'react';
import type { FormEvent } from 'react';
import { ErrorBox, Notice } from './components';

// View-model boundary only. The final server contract supplies catalog/defaults;
// this form neither guesses endpoints nor creates packages/models/source approvals.
export type DailyBacktestInput = {
  symbol: string;
  start_date: string;
  end_date: string;
  strategy: string;
  provider: string;
  initial_cash: string;
  fee_per_share: string;
  minimum_fee: string;
  slippage_bps: string;
};
export type DailyBacktestFormConfig = {
  symbols: { id: string; label: string }[];
  strategies: { id: string; label: string; description: string; minimumRows: number }[];
  providers: { id: string; label: string }[];
  defaults: DailyBacktestInput;
  maxCalendarDays: number;
  lastAllowedEnd: string;
  earliestStart: string;
};
export function validateDailyInput(value: DailyBacktestInput, config: DailyBacktestFormConfig) {
  const next = { ...value, symbol: value.symbol.trim().toUpperCase() };
  if (!config.symbols.some((s) => s.id === next.symbol))
    throw new Error('请从列表中选股票。目前此处只支持列出的六只美股。');
  for (const [name, date] of [
    ['开始日期', next.start_date],
    ['结束日期', next.end_date],
  ]) {
    const parsed = new Date(`${date}T00:00:00Z`);
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(date) ||
      !Number.isFinite(parsed.getTime()) ||
      parsed.toISOString().slice(0, 10) !== date
    )
      throw new Error(`请填写有效的${name}。`);
  }
  if (next.start_date < config.earliestStart)
    throw new Error(`开始日期不能早于 ${config.earliestStart}。`);
  if (next.start_date > next.end_date) throw new Error('开始日期不能晚于结束日期。');
  if (next.end_date > config.lastAllowedEnd)
    throw new Error(`结束日期最晚为 ${config.lastAllowedEnd}，不使用尚未结束的日线。`);
  if (
    (Date.parse(next.end_date) - Date.parse(next.start_date)) / 86400000 + 1 >
    config.maxCalendarDays
  )
    throw new Error(`本次最多选择 ${config.maxCalendarDays} 个日历日。`);
  if (!config.strategies.some((s) => s.id === next.strategy))
    throw new Error('请从列表中选择策略。');
  if (!config.providers.some((p) => p.id === next.provider))
    throw new Error('请从列表中选择数据来源。');
  for (const key of ['initial_cash', 'fee_per_share', 'minimum_fee', 'slippage_bps'] as const) {
    if (
      !/^\d+(\.\d+)?$/.test(next[key]) ||
      !Number.isFinite(Number(next[key])) ||
      (key === 'initial_cash' && Number(next[key]) <= 0)
    )
      throw new Error(
        key === 'initial_cash' ? '起始资金需要大于零。' : '手续费和滑点需要填写非负数。',
      );
  }
  return next;
}
export function ProviderBacktestForm({
  config,
  canWrite,
  locked = false,
  confirmedJobId = '',
  initialValues,
  onStart,
}: {
  config: DailyBacktestFormConfig;
  canWrite: boolean;
  locked?: boolean;
  confirmedJobId?: string;
  initialValues?: DailyBacktestInput;
  onStart: (input: DailyBacktestInput) => Promise<void>;
}) {
  const [value, setValue] = useState<DailyBacktestInput>(() => ({
    ...(initialValues ?? config.defaults),
  }));
  const [busy, setBusy] = useState(false);
  const [guard] = useState(() => ({ busy: false }));
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    // A parent-validated receipt supersedes the local submission error; keep all choices intact.
    if (confirmedJobId) setError(null);
  }, [confirmedJobId]);
  const change = (key: keyof DailyBacktestInput, text: string) =>
    setValue((v) => ({ ...v, [key]: text }));
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canWrite || locked || guard.busy) return;
    guard.busy = true;
    setBusy(true);
    setError(null);
    try {
      await onStart(validateDailyInput(value, config));
    } catch (e) {
      setError(e);
    } finally {
      guard.busy = false;
      setBusy(false);
    }
  }
  return (
    <section className="panel provider-backtest" aria-label="股票日线回测">
      <h2>选股票、日期和策略，开始回测</h2>
      <p>开始后会下载行情并运行回测。直接用列表里的策略即可。</p>
      {!canWrite && <Notice>当前账号只可查看结果。开始回测需要研究者或管理员权限。</Notice>}
      <form onSubmit={submit}>
        <fieldset disabled={!canWrite || busy || locked}>
          <legend className="sr-only">这次要回测什么？</legend>
          <div className="provider-inputs">
            <label>
              <span>股票</span>
              <select
                name="symbol"
                value={value.symbol}
                onChange={(e) => change('symbol', e.target.value)}
                required
              >
                {config.symbols.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>开始日期</span>
              <input
                name="start_date"
                type="date"
                min={config.earliestStart}
                max={config.lastAllowedEnd}
                value={value.start_date}
                onChange={(e) => change('start_date', e.target.value)}
                required
              />
            </label>
            <label>
              <span>结束日期（包含当天）</span>
              <input
                name="end_date"
                type="date"
                max={config.lastAllowedEnd}
                value={value.end_date}
                onChange={(e) => change('end_date', e.target.value)}
                required
              />
            </label>
            <label>
              <span>策略</span>
              <select
                name="strategy"
                value={value.strategy}
                onChange={(e) => change('strategy', e.target.value)}
                required
              >
                {config.strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <p className="muted">
            {config.strategies.find((s) => s.id === value.strategy)?.description}
          </p>
          <p className="defaults-summary">
            来源：{config.providers.find((p) => p.id === value.provider)?.label || '未选择'} ·
            起始资金 {value.initial_cash} USD · 手续费每股 {value.fee_per_share} USD，单笔最低{' '}
            {value.minimum_fee} USD · 滑点 {value.slippage_bps} 基点
          </p>
          <details>
            <summary>调整数据来源、资金和成本</summary>
            <div className="form-grid">
              <label>
                <span>数据来源</span>
                <select
                  name="provider"
                  value={value.provider}
                  onChange={(e) => change('provider', e.target.value)}
                >
                  {config.providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}
                    </option>
                  ))}
                </select>
                <small>若所选来源下载失败，回测会提示失败，仍会保留你选择的来源。</small>
              </label>
              <label>
                <span>起始资金（美元）</span>
                <input
                  name="initial_cash"
                  type="number"
                  min="0.01"
                  step="any"
                  value={value.initial_cash}
                  onChange={(e) => change('initial_cash', e.target.value)}
                  required
                />
              </label>
              <label>
                <span>每股手续费（美元）</span>
                <input
                  name="fee_per_share"
                  type="number"
                  min="0"
                  step="any"
                  value={value.fee_per_share}
                  onChange={(e) => change('fee_per_share', e.target.value)}
                  required
                />
              </label>
              <label>
                <span>每笔最低手续费（美元）</span>
                <input
                  name="minimum_fee"
                  type="number"
                  min="0"
                  step="any"
                  value={value.minimum_fee}
                  onChange={(e) => change('minimum_fee', e.target.value)}
                  required
                />
              </label>
              <label>
                <span>滑点（成交价格偏差，1 基点 = 0.01%）</span>
                <input
                  name="slippage_bps"
                  type="number"
                  min="0"
                  step="any"
                  value={value.slippage_bps}
                  onChange={(e) => change('slippage_bps', e.target.value)}
                  required
                />
              </label>
            </div>
          </details>
          <p className="muted">
            模拟在有行情的交易日产生信号，到下一条行情的开盘价成交。结束日也在所选范围内。实际有数据的日期及价格调整请看下载结果；调整后价格可能与真实成交价不同，还需核实回测当天能否取得资料。
          </p>
          <button type="submit" disabled={!canWrite || busy || locked || !config.strategies.length}>
            {busy ? '正在提交，请稍候…' : '开始回测'}
          </button>
        </fieldset>
      </form>
      {error != null && <ErrorBox error={error} />}
    </section>
  );
}
