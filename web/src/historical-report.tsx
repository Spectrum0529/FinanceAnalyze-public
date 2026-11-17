import type { RecordData } from './api';
import { portfolioReportName } from './backtest-presentation';
import { JsonView } from './components';
import { record } from './native-research-api';
import { PortfolioAnalytics } from './portfolio-analytics';
import { amountWithCurrency, reportCurrency } from './report-currency';

export function HistoricalReport({ report, technical = report }: { report: RecordData; technical?: RecordData }) {
  const summary = record(report.report);
  const dates = record(report.coverage);
  const reconciliation = record(report.reconciliation);
  const title = (typeof report.title === 'string' ? report.title : typeof report.name === 'string' ? report.name
    : typeof report.headline === 'string' ? report.headline : portfolioReportName(report)) || '原电脑保存的研究报告';
  const symbols = Array.isArray(report.securities) ? report.securities.filter(value => typeof value === 'string').join('、')
    : typeof report.ticker === 'string' ? report.ticker : '';
  const first = dates.actual_first ?? dates.first;
  const last = dates.actual_last ?? dates.last;
  const conclusion = typeof summary.conclusion === 'string' ? summary.conclusion
    : typeof report.conclusion === 'string' ? report.conclusion : '';
  return <>
    <h3>{title}</h3><p>这份材料来自原电脑的已保存报告。</p>
    <dl className="key-value">
      {symbols && <><dt>证券</dt><dd>{symbols}</dd></>}
      {typeof first === 'string' && typeof last === 'string' && <><dt>实际使用日期</dt><dd>{first} 至 {last}</dd></>}
      {reconciliation.net_profit != null && <><dt>扣费后赚亏金额</dt><dd>{amountWithCurrency(reconciliation.net_profit, reportCurrency(report))}</dd></>}
    </dl>
    {conclusion && <><h4>报告结论</h4><p>{conclusion}</p></>}
    {report.family === 'dataset-portfolio-result-1' && <PortfolioAnalytics value={report} />}
    <details><summary>查看报告原文及校验值</summary><JsonView value={technical} /></details>
  </>;
}
