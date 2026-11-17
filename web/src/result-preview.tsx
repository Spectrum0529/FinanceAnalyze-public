import { createContext, useContext, useId, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { RecordData } from './api';
import { keepDialogFocus } from './area-help';
import { ErrorBox, Notice, useResource } from './components';
import { formatAmount } from './display-amount';
import { portfolioReportName } from './backtest-presentation';
import { record } from './native-research-api';
import { amountWithCurrency, reportCurrency } from './report-currency';
import { createDialogMotion } from './v434/assets/dialog-motion.js';

type ResultLink = { resultId: string; title: string; href: string };
type Selection = ResultLink & { trigger: HTMLAnchorElement; x: number; y: number; hash: string };
const OpenPreview = createContext<((selection: Selection) => void) | null>(null);

export function ResultPreviewProvider({ children, motion }: { children: ReactNode; motion: () => boolean }) {
  const [selection, setSelection] = useState<Selection | null>(null);
  return <OpenPreview.Provider value={setSelection}>
    {children}
    {selection && <ResultDialog selection={selection} motion={motion} closed={() => setSelection(null)} />}
  </OpenPreview.Provider>;
}

export function ResultPreviewLink({ resultId, title, children, href, className }: {
  resultId: string; title: string; children: ReactNode; href?: string; className?: string;
}) {
  const open = useContext(OpenPreview);
  const fullReport = href ?? '#my-results?result_id=' + encodeURIComponent(resultId);
  return <a href={fullReport} className={className} aria-haspopup={open ? 'dialog' : undefined} onClick={event => {
    if (!open || event.defaultPrevented || event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey)
      return;
    event.preventDefault();
    open({ resultId, title, href: fullReport, trigger: event.currentTarget,
      x: window.scrollX, y: window.scrollY, hash: window.location.hash });
  }}>{children}</a>;
}

function ResultDialog({ selection, motion, closed }: { selection: Selection; motion: () => boolean; closed: () => void }) {
  const heading = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const animation = useRef<ReturnType<typeof createDialogMotion>>(null);
  const restoreFocus = useRef(true);
  const onClosed = useRef(closed);
  const { resultId, title, href, trigger, x, y, hash } = selection;
  useLayoutEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const events = new AbortController();
    const controller = createDialogMotion(element, { motion });
    animation.current = controller;
    element.addEventListener('close', () => {
      if (restoreFocus.current && window.location.hash === hash && trigger.isConnected) {
        trigger.focus({ preventScroll: true });
        window.scrollTo({ left: x, top: y, behavior: 'instant' });
      }
      onClosed.current();
    }, { signal: events.signal });
    window.addEventListener('hashchange', () => {
      restoreFocus.current = false;
      controller.close(undefined, { immediate: true });
    }, { signal: events.signal });
    document.addEventListener('account-motion-change', controller.refreshMotion, { signal: events.signal });
    controller.open();
    closeButton.current?.focus({ preventScroll: true });
    return () => {
      events.abort();
      controller.destroy();
      animation.current = null;
    };
  }, [hash, motion, trigger, x, y]);
  return <dialog className="help-dialog result-preview-dialog" ref={dialog} aria-labelledby={heading} onKeyDown={keepDialogFocus}>
    <header className="result-preview-header">
      <h2 id={heading}>结果预览</h2>
      <button ref={closeButton} autoFocus type="button" onClick={() => animation.current?.close()}>关闭预览</button>
    </header>
    <div className="result-preview-content">
      <h3>{title}</h3>
      <p><a href={href} onClick={event => {
        if (event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
        restoreFocus.current = false;
        animation.current?.close(undefined, { immediate: true });
      }}>打开完整报告</a></p>
      <p><a href={'#my-results?manage_result=' + encodeURIComponent(resultId)}>改名、回收或恢复这份报告 →</a></p>
      <ResultSummary resultId={resultId} />
    </div>
  </dialog>;
}

export function useSavedResultSummary(resultId: string, retry = 0) {
  return useResource<RecordData>(`/results/${encodeURIComponent(resultId)}/summary`, retry);
}

export function matchesSavedResultSummary(receipt: RecordData, resultId: string) {
  return receipt.result_id === resultId && receipt.view === 'summary' &&
    typeof receipt.source_view_sha256 === 'string' && /^[a-f0-9]{64}$/.test(receipt.source_view_sha256);
}

const excerptLimit = 600;
const isAmount = (value: unknown) => formatAmount(value) !== '无数据';
const valueText = (value: unknown) => typeof value === 'string' || typeof value === 'number' ? String(value) : '';

function ResultExcerpt({ value, more = false }: { value: string; more?: boolean }) {
  const characters = Array.from(value.trim());
  if (!characters.length) return <p>这份报告没有可预览的文字。可打开完整报告查看其他记录。</p>;
  return <div>
    <h4>报告摘录</h4>
    <p className="result-preview-excerpt">{characters.slice(0, excerptLimit).join('')}{(more || characters.length > excerptLimit) && '…'}</p>
  </div>;
}

function SavedExcerpt({ resultId, digest, section }: { resultId: string; digest: string; section: string }) {
  const query = new URLSearchParams({ section, limit: String(excerptLimit), offset: '0', expected_sha256: digest });
  const state = useResource<RecordData>(`/results/${encodeURIComponent(resultId)}/series?${query}`);
  if (state.loading) return <p role="status">正在读取报告摘录…</p>;
  if (state.error) return <ErrorBox error={state.error} />;
  const page = state.data;
  if (page?.result_id !== resultId || page.source_view_sha256 !== digest ||
      page.kind !== 'text' || page.section !== section || page.offset !== 0 ||
      typeof page.text !== 'string' || Array.from(page.text).length > excerptLimit)
    return <Notice>无法确认这段文字属于所选报告。请打开完整报告。</Notice>;
  return <ResultExcerpt value={page.text} more={page.next_offset !== null} />;
}

function TextPreview({ body, resultId, digest }: { body: RecordData; resultId: string; digest: string }) {
  if (body.family === 'tradingagents-native-report-1') {
    const report = record(body.report);
    const offline = ['OFFLINE_FIXTURE', 'OFFLINE_TEAM_CONFIGURED'].includes(String(body.model_status)) ||
      body.scope === 'OFFLINE_FIXTURE' || body.execution_scope === 'TEST_OFFLINE';
    return <>
      {offline && <Notice>离线练习结果，没有调用真实模型。</Notice>}
      {report.rating === 'REVIEW' && <p>报告暂时没有作出判断。</p>}
      {typeof report.conclusion === 'string' ? <ResultExcerpt value={report.conclusion} />
        : record(report.conclusion).section === '/report/conclusion'
          ? <SavedExcerpt resultId={resultId} digest={digest} section="/report/conclusion" />
          : <p>没有可预览的结论。请打开完整报告。</p>}
    </>;
  }
  if (body.family === 'model-tool-research-result-1') {
    const reports = record(body.reports);
    const tools = record(body.tool_result);
    const needsData = tools.status === 'NEEDS_DATA' || body.status === 'EVIDENCE_GAP_REPORTED' ||
      body.status === 'INSUFFICIENT_DATA' || tools.family === 'strategy-search-result-1' && tools.status === 'INSUFFICIENT_DATA';
    const hasExcerpt = reports._paged === true && reports.section === '/reports' && Number(reports.total) > 0;
    return <>
      {needsData && <Notice>这次研究还需要补充资料。打开完整报告可查看缺少什么。</Notice>}
      {hasExcerpt && <SavedExcerpt resultId={resultId} digest={digest} section="/reports/0/text" />}
      {!needsData && !hasExcerpt && body.status === 'MODEL_PLAN_AND_LOCAL_LICENSED_TOOLS_COMPLETED' &&
        <p>模型选了分析方法，计算由本机完成。请打开完整报告查看计算结果。</p>}
    </>;
  }
  return null;
}

function ResultSummary({ resultId }: { resultId: string }) {
  const [retry, setRetry] = useState(0);
  const state = useSavedResultSummary(resultId, retry);
  if (state.loading) return <p role="status">正在读取报告摘要…</p>;
  if (state.error) return <ErrorBox error={state.error} retry={() => setRetry(n => n + 1)} />;
  if (!state.data || !matchesSavedResultSummary(state.data, resultId))
    return <ErrorBox error={new Error('无法确认摘要属于所选报告。请重试。')} retry={() => setRetry(n => n + 1)} />;

  const body = record(state.data.summary);
  const report = record(body.report);
  const coverage = record(body.coverage);
  const currency = reportCurrency(body);
  const amountData = record(body.reconciliation);
  const amount = (value: unknown) => currency ? amountWithCurrency(value, currency) : `${formatAmount(value)}（币种未提供）`;
  const first = valueText(coverage.actual_first ?? coverage.first), last = valueText(coverage.actual_last ?? coverage.last);
  const savedTitle = body.family === 'dataset-portfolio-result-1' ? portfolioReportName(body)
    : typeof body.title === 'string' ? body.title : typeof body.name === 'string' ? body.name : null;
  const hasText = body.family === 'tradingagents-native-report-1' || body.family === 'model-tool-research-result-1';
  const strategyNeedsData = body.family === 'strategy-search-result-1' && body.status === 'INSUFFICIENT_DATA';
  const isAutonomousBacktest = body.family === 'autonomous-backtest-result-1';
  const figures: [string, string][] = [];
  if (isAmount(amountData.net_profit)) figures.push(['扣费后赚亏金额', amount(amountData.net_profit)]);
  if (body.family === 'dataset-portfolio-result-1' || isAmount(amountData.fees)) {
    figures.push(['交易费用', isAmount(amountData.fees) ? amount(amountData.fees) : '未提供']);
  }
  if (/^daily-simulation-result-[123]$/.test(String(body.family))) {
    if (isAmount(report.net_return)) figures.push(['累计赚亏百分比', formatAmount(Number(report.net_return) * 100) + '%']);
    if (isAmount(report.max_drawdown)) figures.push(['从高点跌得最多的比例', formatAmount(Number(report.max_drawdown) * 100) + '%']);
    figures.push(['交易费用', isAmount(report.fees) ? formatAmount(report.fees) + ' USD' : '未提供']);
  }
  const evidence = body.family === 'model-tool-research-result-1' ? record(body.tool_result) : body;
  if (isAmount(evidence.mean_rank_ic)) figures.push(['选股线索与后续收益的排名有多一致（平均）', valueText(evidence.mean_rank_ic)]);
  if (evidence.family === 'dataset-research-result-1') {
    const items = record(evidence.items);
    if (items.kind === 'list' && typeof items.total === 'number' && items.total > 0)
      figures.push(['报告统计的证券数量', String(items.total)]);
  }
  if (body.family === 'qlib-workflow-result-1') {
    const metrics = record(body.metrics);
    if (isAmount(metrics['Rank IC'])) figures.push(['预测排名与实际收益排名的相关性', valueText(metrics['Rank IC'])]);
    if (isAmount(metrics.IC)) figures.push(['预测值与实际收益的相关性', valueText(metrics.IC)]);
  }
  return <>
    {savedTitle && <p>{savedTitle.slice(0, 200)}</p>}
    {strategyNeedsData && <Notice>数据不足，未能比较策略。打开完整报告可查看所需数据。</Notice>}
    {isAutonomousBacktest && <>
      {body.evaluation_scope === 'DEVELOPMENT_ONLY' && <Notice>这份数据已经用来调整过方案。要检验方案，还需用一份没参与调整的数据。</Notice>}
      {body.evaluation_scope === 'SEALED_FINAL' && <Notice>已用事先留出的数据检查方案。这次结果不会用来修改方案。</Notice>}
    </>}
    {(figures.length > 0 || first && last) && <dl className="key-value">
      {figures.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
      {first && last && <div className="result-preview-range"><dt>{isAutonomousBacktest ? '参与评分的日期' : '实际使用日期'}</dt><dd>{first} 至 {last}</dd></div>}
    </dl>}
    {hasText && <TextPreview body={body} resultId={resultId} digest={String(state.data.source_view_sha256)} />}
    {!figures.length && !(first && last) && !hasText && !strategyNeedsData &&
      <Notice>这份保存记录没有可预览的摘要。请打开完整报告查看内容。</Notice>}
  </>;
}
