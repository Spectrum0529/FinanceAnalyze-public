import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError } from './api';
import { ErrorBox, Notice, JsonView } from './components';
import { assertUsSecurity, presetStart, validateHistoryDates } from './history-view-model';
import type {
  HistoryUiConfig,
  HistoryUiPort,
  HistoryIntent,
  SecurityChoice,
  HistoryCoverage,
} from './history-view-model';

export function HistoryQuickForm({
  config,
  port,
  canWrite,
  locked = false,
  onReceipt,
  initialChoice,
  initialHistory,
  searchDelay = 250,
}: {
  config: HistoryUiConfig;
  port: HistoryUiPort;
  canWrite: boolean;
  locked?: boolean;
  onReceipt: (
    receipt: { jobId: string },
    intent: HistoryIntent,
    requestId: string,
    kind: 'start' | 'save',
  ) => void;
  initialChoice?: SecurityChoice;
  initialHistory?: HistoryCoverage;
  searchDelay?: number;
}) {
  const [query, setQuery] = useState(initialChoice?.symbol ?? ''),
    [choice, setChoice] = useState<SecurityChoice | null>(initialChoice ?? null),
    [suggestions, setSuggestions] = useState<SecurityChoice[]>([]);
  const [provider, setProvider] = useState(initialChoice?.provider ?? config.defaultProvider),
    [start, setStart] = useState(config.defaultStart),
    [end, setEnd] = useState(config.defaultEnd),
    [strategy, setStrategy] = useState(config.defaultStrategy);
  const [pinnedHistory, setPinnedHistory] = useState(initialHistory ?? null);
  const [bypassCache, setBypassCache] = useState(false);
  const [referenceNotice, setReferenceNotice] = useState('');
  const [historyPolicy, setHistoryPolicy] = useState<'prefer_local' | 'refresh'>('prefer_local');
  const [costs, setCosts] = useState({
    initialCash: config.initialCash,
    feePerShare: config.feePerShare,
    minimumFee: config.minimumFee,
    spreadBps: config.spreadBps,
  });
  const [coverage, setCoverage] = useState<HistoryCoverage | null>(null),
    [coverageError, setCoverageError] = useState<unknown>(null),
    [searchError, setSearchError] = useState<unknown>(null),
    [error, setError] = useState<unknown>(null);
  const [searching, setSearching] = useState(false),
    [busy, setBusy] = useState(false),
    [blocked, setBlocked] = useState(false),
    [uncertain, setUncertain] = useState(false),
    [active, setActive] = useState(-1),
    [open, setOpen] = useState(false);
  const [pendingRequest, setPendingRequest] = useState<{
    requestId: string;
    intent: HistoryIntent;
    kind: 'start' | 'save';
  } | null>(null);
  const [guard] = useState(() => ({
    busy: false,
    blocked: false,
    alive: true,
    resolve: null as AbortController | null,
  }));
  const [menuId] = useState(() => `security-${crypto.randomUUID()}`);
  useEffect(() => {
    guard.alive = true;
    return () => {
      guard.alive = false;
      guard.resolve?.abort();
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    let timer: number | undefined;
    const local = (config.commonSecurities ?? [])
      .filter(
        (s) =>
          s.provider === provider &&
          (!query.trim() ||
            `${s.symbol} ${s.name}`.toLowerCase().includes(query.trim().toLowerCase())),
      )
      .slice(0, 8);
    setSuggestions(local);
    setSearchError(null);
    setActive(-1);
    if (!query.trim() || choice?.symbol === query.trim()) {
      setSearching(false);
      return () => controller.abort();
    }
    const load = async () => {
      setSearching(true);
      try {
        const found = await port.search(query.trim(), provider, controller.signal);
        if (alive) {
          setSuggestions(
            [...found, ...local]
              .filter((s, i, all) => all.findIndex((x) => x.key === s.key) === i)
              .slice(0, 20),
          );
          setOpen(true);
        }
      } catch (e) {
        if (alive && !controller.signal.aborted) setSearchError(e);
      } finally {
        if (alive) setSearching(false);
      }
    };
    if (searchDelay === 0) void load();
    else timer = window.setTimeout(() => void load(), searchDelay);
    return () => {
      alive = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [query, provider, choice, port, searchDelay, config]);
  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    setCoverage(null);
    setCoverageError(null);
    if (choice) {
      try {
        validateHistoryDates(start, end, config);
        void port.coverage({ security: choice, start, end, provider }, controller.signal).then(
          (value) => {
            if (alive) {
              if (value.symbol !== choice.symbol || value.provider !== provider) {
                setCoverageError(new Error('无法确认日期范围属于所选证券和来源。请刷新再试。'));
              } else setCoverage(value);
            }
          },
          (e) => {
            if (alive && !controller.signal.aborted) setCoverageError(e);
          },
        );
      } catch {
        /* Invalid dates remain editable; validation appears on explicit submit. */
      }
    }
    return () => {
      alive = false;
      controller.abort();
    };
  }, [choice, start, end, provider, port, config]);
  const choose = (item: SecurityChoice) => {
    try {
      assertUsSecurity(item, provider);
      setChoice(item);
      setQuery(item.symbol);
      setOpen(false);
      setError(null);
    } catch (e) {
      setError(e);
    }
  };
  async function run(kind: 'start' | 'save') {
    if (!canWrite || locked || guard.busy || guard.blocked) return;
    guard.busy = true;
    setBusy(true);
    setError(null);
    let sent = false;
    try {
      validateHistoryDates(start, end, config);
      if (!query.trim()) throw new Error('请输入股票或ETF代码，也可以按名称搜索。');
      if (!config.providers.some((p) => p.id === provider))
        throw new Error('请从列表中选择数据来源。');
      if (!config.strategies.some((s) => s.id === strategy))
        throw new Error('请从列表中选择策略。');
      const controller = new AbortController();
      guard.resolve = controller;
      const security = assertUsSecurity(
        await port.resolve(query.trim(), provider, controller.signal),
        provider,
      );
      if (!guard.alive) return;
      setChoice(security);
      setQuery(security.symbol);
      setOpen(false);
      if (
        pinnedHistory &&
        (!Number.isSafeInteger(pinnedHistory.version) || Number(pinnedHistory.version) <= 0)
      )
        throw new Error('所选行情缺少有效的版本编号。请刷新已保存行情后重新选择。');
      const intent: HistoryIntent = {
        security,
        start,
        end,
        strategy,
        provider,
        historyPolicy,
        history: pinnedHistory
          ? {
              id: pinnedHistory.id,
              version: pinnedHistory.version!,
              observationHash: pinnedHistory.observationHash,
            }
          : null,
        ...(kind === 'start' && config.canBypassCache && pinnedHistory && historyPolicy !== 'refresh' && bypassCache
          ? { cachePolicy: 'bypass' as const } : {}),
        ...costs,
      };
      for (const value of Object.values(costs))
        if (!/^\d+(\.\d+)?$/.test(value) || !Number.isFinite(Number(value)))
          throw new Error('资金和成本请填写有效非负数。');
      if (Number(costs.initialCash) <= 0) throw new Error('起始资金需要大于零。');
      if (Number(costs.spreadBps) > 1000) throw new Error('买卖价差最多为1000个基点。');
      const requestId = crypto.randomUUID();
      setPendingRequest({ requestId, intent, kind });
      sent = true;
      const receipt = await port[kind](intent, requestId);
      if (!receipt.jobId) throw new Error('还不能确认任务是否提交。请先查这次进度，以免重复提交。');
      guard.blocked = true;
      setBlocked(true);
      if (guard.alive) onReceipt(receipt, intent, requestId, kind);
    } catch (e) {
      if (guard.alive) {
        setError(e);
        if (
          sent &&
          !(
            e instanceof ApiError &&
            e.status >= 400 &&
            e.status < 500 &&
            ![408, 409, 429].includes(e.status)
          )
        ) {
          guard.blocked = true;
          setBlocked(true);
          setUncertain(true);
        }
      }
    } finally {
      guard.busy = false;
      guard.resolve = null;
      if (guard.alive) setBusy(false);
    }
  }
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    void run('start');
  };
  return (
    <section className="panel history-quick" aria-label="长期历史回测">
      <h2>选股票、日期和策略，开始回测</h2>
      <p>优先用本机已保存的行情。如果没有合适的行情，会下载并保存。</p>
      {!canWrite && (
        <Notice>只读账号可查看获准的行情和报告。回测、下载和修改需要编辑权限。</Notice>
      )}
      <form onSubmit={submit}>
        <fieldset disabled={!canWrite || locked || busy || blocked}>
          <legend className="sr-only">股票、日期和策略</legend>
          <div className="provider-inputs">
            <div className="security-search">
              <label htmlFor={`${menuId}-input`}>股票 / ETF</label>
              <input
                id={`${menuId}-input`}
                role="combobox"
                aria-autocomplete="list"
                aria-expanded={open && suggestions.length > 0}
                aria-controls={menuId}
                aria-activedescendant={open && active >= 0 ? `${menuId}-${active}` : undefined}
                value={query}
                maxLength={64}
                placeholder="输入代码或名称，例如 SPY"
                autoComplete="off"
                onFocus={() => setOpen(true)}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setChoice(null);
                  setOpen(true);
                  if (pinnedHistory) {
                    setPinnedHistory(null);
                    setReferenceNotice(
                      '已改代码。系统会重新查找本机已保存的行情。',
                    );
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    setOpen(false);
                    return;
                  }
                  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                    e.preventDefault();
                    setOpen(true);
                    setActive((i) =>
                      suggestions.length
                        ? i < 0
                          ? e.key === 'ArrowDown'
                            ? 0
                            : suggestions.length - 1
                          : (i + (e.key === 'ArrowDown' ? 1 : -1) + suggestions.length) %
                            suggestions.length
                        : -1,
                    );
                  } else if (e.key === 'Enter' && open && active >= 0 && suggestions[active]) {
                    e.preventDefault();
                    choose(suggestions[active]);
                  }
                }}
              />
              {searching && <small role="status">正在查找证券…</small>}
              {open && suggestions.length > 0 && (
                <ul id={menuId} role="listbox" aria-label="证券建议">
                  {suggestions.map((s, i) => (
                    <li
                      key={s.key}
                      id={`${menuId}-${i}`}
                      role="option"
                      aria-selected={active === i}
                    >
                      <button type="button" onClick={() => choose(s)}>
                        {s.symbol} · {s.name}
                        <small>
                          {s.market ?? '市场待核实'} · {s.currency ?? '币种待核实'} ·{' '}
                          {s.identityState === 'verified' ? '已核实当前代码' : '候选'}
                        </small>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <small>
                {choice
                  ? `${choice.name} · ${choice.identityState === 'verified' ? '已核实当前代码' : '待核实的代码'}`
                  : '可以输入列表外的代码。开始前会向来源核实。'}
              </small>
            </div>
            <label>
              <span>开始日期</span>
              <input
                name="history_start"
                type="date"
                min={config.earliestStart}
                max={config.latestEnd}
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label>
              <span>结束日期（包含当天）</span>
              <input
                name="history_end"
                type="date"
                min={config.earliestStart}
                max={config.latestEnd}
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
            <label>
              <span>策略</span>
              <select
                name="history_strategy"
                value={strategy}
                onChange={(e) => setStrategy(e.target.value)}
              >
                {config.strategies.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="year-presets" aria-label="快速选择历史年限">
            {[1, 5, 10, 20, 30].map((year) => (
              <button
                type="button"
                className="secondary"
                key={year}
                onClick={() => {
                  try {
                    setStart(presetStart(end, year));
                    setError(null);
                  } catch (e) {
                    setError(e);
                  }
                }}
              >
                {year}年
              </button>
            ))}
          </div>
          <p className="muted">
            {config.strategies.find((s) => s.id === strategy)?.help}{' '}
            实际数据可能少于所选日期，例如股票较晚上市或来源缺少部分记录。
          </p>
          <details>
            <summary>数据来源、更新和成本</summary>
            <div className="form-grid">
              <label>
                <span>数据来源</span>
                <select
                  name="history_provider"
                  value={provider}
                  onChange={(e) => {
                    setProvider(e.target.value);
                    setChoice(null);
                    setCoverage(null);
                    if (pinnedHistory) {
                      setPinnedHistory(null);
                      setReferenceNotice('已改数据来源，取消选中的旧行情版本。');
                    }
                  }}
                >
                  {config.providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}
                    </option>
                  ))}
                </select>
                <small>{config.providers.find((p) => p.id === provider)?.help}</small>
              </label>
              <label>
                <span>怎样获取行情</span>
                <select
                  value={historyPolicy}
                  onChange={(e) => {
                    setHistoryPolicy(e.target.value as 'prefer_local' | 'refresh');
                    if (e.target.value === 'refresh' && pinnedHistory) {
                      setPinnedHistory(null);
                      setReferenceNotice('本次会重新下载，取消选中的旧版本。');
                    }
                  }}
                >
                  <option value="prefer_local">优先用已保存的行情</option>
                  <option value="refresh">重新下载并保存</option>
                </select>
              </label>
              {(
                [
                  ['initialCash', '起始资金（美元）'],
                  ['feePerShare', '每单位手续费（美元）'],
                  ['minimumFee', '每笔最低手续费（美元）'],
                  ['spreadBps', '模拟买卖价差（1 基点 = 0.01%）'],
                ] as const
              ).map(([key, label]) => (
                <label key={key}>
                  <span>{label}</span>
                  <input
                    type="number"
                    min="0"
                    max={key === 'spreadBps' ? '1000' : undefined}
                    step="any"
                    value={costs[key]}
                    onChange={(e) => setCosts((c) => ({ ...c, [key]: e.target.value }))}
                  />
                </label>
              ))}
            </div>
          </details>
          <p className="defaults-summary">
            来源：{config.providers.find((p) => p.id === provider)?.label} · 起始资金{' '}
            {costs.initialCash} USD · 价格如何处理：
            {config.providers.find((p) => p.id === provider)?.pricePolicyLabel ?? '待服务确认'} ·
            自动保存行情 · {historyPolicy === 'prefer_local' ? '优先用已保存行情' : '本次重新下载'}
          </p>
          {config.canBypassCache && pinnedHistory && historyPolicy !== 'refresh' && <label>
            <input type="checkbox" checked={bypassCache} onChange={event => setBypassCache(event.target.checked)} />
            重新计算这次回测
          </label>}
          <div className="form-actions">
            <button type="submit">{busy ? '正在核实并提交…' : '开始回测'}</button>
            <button type="button" className="secondary" onClick={() => void run('save')}>
              只下载或补齐行情
            </button>
          </div>
        </fieldset>
      </form>
      {pinnedHistory && (
        <Notice>
          这次只用选中的已保存版本，不会自动更换或下载。
          <p>
            保存版本：
            {Number.isSafeInteger(pinnedHistory.version) && Number(pinnedHistory.version) > 0
              ? pinnedHistory.version
              : '未确认'}
            ；实际有数据的日期：{pinnedHistory.start ?? '服务未提供'} 至{' '}
            {pinnedHistory.end ?? '服务未提供'}，{pinnedHistory.rows} 条。{pinnedHistory.message}
            {' 还没确认所选日期内是否有缺少的记录。'}
          </p>
          <button
            type="button"
            className="text-button"
            disabled={busy || blocked || locked || !canWrite}
            onClick={() => {
              setPinnedHistory(null);
              setReferenceNotice('已取消指定版本，将优先查找本机已保存的行情。');
            }}
          >
            取消指定版本
          </button>
        </Notice>
      )}
      {referenceNotice && <Notice>{referenceNotice}</Notice>}
      {!pinnedHistory &&
        (coverage?.status === 'unknown' ? (
          <Notice>还未确认本机已保存哪些日期。{coverage.message}</Notice>
        ) : coverage?.status === 'none' ? (
          <Notice>还没有适用于这次选择的已保存行情。{coverage.message}</Notice>
        ) : coverage ? (
          <Notice>
            已保存日期：{coverage.start ?? '未取得'} 至 {coverage.end ?? '未取得'}，{coverage.rows}{' '}
            条。{coverage.message}
            {coverage.status !== 'complete' && ' 还没确认所选日期内是否有缺少的记录。'}
          </Notice>
        ) : choice ? (
          <p className="muted">正在查看本机已保存哪些日期…</p>
        ) : null)}
      {searchError != null && <ErrorBox error={searchError} />}{' '}
      {coverageError != null && <ErrorBox error={coverageError} />}
      {error != null && <ErrorBox error={error} />}{' '}
      {uncertain && (
        <Notice>
          还不能确认任务是否提交。请先查这次进度；系统不会自动重发。
        </Notice>
      )}
      {uncertain && pendingRequest && (
        <details>
          <summary>原提交编号与选择</summary>
          <JsonView value={pendingRequest} />
        </details>
      )}
    </section>
  );
}
