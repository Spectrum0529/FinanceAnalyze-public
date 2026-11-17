import { SavedModelUsage } from './model-usage-panel';
import { display } from './api';
import type { RecordData } from './api';
import { JsonView, Notice } from './components';
import { PagedServerRows } from './research-visuals';
import { record, qlibBasicProfile } from './native-research-api';

const text = (value: unknown) => typeof value === 'string' && value ? value : '原记录未提供。';
function NativeText({ value }: { value: unknown }) { return <pre className="json-view">{text(value)}</pre>; }

export function NativeResearchReport({ value, runId, userId }: { value: RecordData; runId: string; userId: string }) {
  const owner = value.owner_id ?? record(value.lineage).owner_id;
  if (value.run_id !== runId || (typeof owner === 'string' && owner !== userId))
    return <Notice>无法确认这是所选报告，请回到结果列表重新打开。</Notice>;
  if (value.family === 'qlib-workflow-result-1') return <QlibReport value={value} />;
  if (value.family === 'tradingagents-native-report-1') return <DebateReport value={value} />;
  return <Notice>这条记录沿用原回测报告。<a href={`#results?run=${encodeURIComponent(runId)}`}>打开原报告 →</a></Notice>;
}

function QlibReport({ value }: { value: RecordData }) {
  const engine = record(value.engine), metrics = record(value.metrics), audit = record(value.effective_sample_audit);
  if (engine.name !== 'Qlib' || engine.version !== '0.9.8' || engine.commit !== '54355232463878d2eebb91fe0ee5fa7fa1f5976c' ||
      value.publication_gate !== 'CORE_JOB_SUCCEEDED_REQUIRED')
    return <Notice>保存的模型报告资料不全，请重新打开；如仍如此，请查看任务记录。</Notice>;
  const basic = engine.workflow === qlibBasicProfile;
  const metricRows = Object.entries(metrics).map(([name, number]) => ({ name, value: number }));
  const predictions = Array.isArray(value.predictions) ? value.predictions : [];
  const portfolio = Array.isArray(value.portfolio_report) ? value.portfolio_report : [];
  const portfolioLabels: Record<string, string> = { datetime: '日期', account: '账户价值', return: '收益（按报告保存的小数）', cost: '成本（按报告保存的小数）', bench: '基准（按报告保存的小数）', turnover: '换手率' };
  const portfolioColumns = Object.entries(portfolioLabels).filter(([key]) => portfolio.some((r) => Object.hasOwn(record(r), key))).map(([key, label]) => ({ key, label }));
  return <>
    <h3>{basic ? 'Qlib 使用三项行情数据 · LightGBM 训练与预测' : engine.workflow === 'alpha158-lgb-us-1' ? 'Qlib 历史 Alpha158 / LightGBM 报告' : 'Qlib 已保存结果 · 无法确认使用了哪些行情字段'}</h3>
    {typeof value.job_id === 'string' && <a href={`#intelligent?qlib_job=${encodeURIComponent(value.job_id)}`}>查看本次任务与重试记录 →</a>}
    <Notice>{value.scope === 'SYNTHETIC' ? '使用合成练习数据。' : '数据范围以保存记录为准。'}指标沿用 Qlib 保存的结果。空白表示没有可用的数值，不能按零计算。</Notice>
    {basic ? <Notice>本次使用收盘价、开盘价和成交量做训练和预测。没有做组合回测，因此没有组合收益或回撤。</Notice> : <PagedServerRows value={metricRows} columns={[{ key: 'name', label: '模型保存的指标' }, { key: 'value', label: '保存值', render: (r) => r.value == null ? '未定义' : display(r.value) }]} empty="原记录未提供指标。" />}
    <details><summary>训练、验证与测试样本</summary><dl className="key-value">{(['train', 'valid', 'test'] as const).map((role) => <div key={role}><dt>{{ train: '训练', valid: '验证', test: '测试' }[role]}</dt><dd>{display(record(audit[role]).rows)} 条；有标签 {display(record(audit[role]).nonmissing_label_rows)} 条</dd></div>)}</dl></details>
    <details><summary>模型预测结果</summary><PagedServerRows value={predictions} columns={[{ key: 'datetime', label: '日期' }, { key: 'instrument', label: '股票' }, { key: 'score', label: '模型预测值' }]} empty="原记录未提供预测。" /></details>
    {!basic && <details><summary>Qlib 的组合回测</summary><PagedServerRows value={portfolio} columns={portfolioColumns} empty="原记录未提供组合回测。" /></details>}
    <details><summary>数据、实验与版本引用</summary><JsonView value={{ engine: value.engine, recorder: value.recorder, export_reference: value.export_reference, source_reference: value.source_reference, undefined_metrics: value.undefined_metrics }} /></details>
  </>;
}

function DebateReport({ value }: { value: RecordData }) {
  const report = record(value.report);
  const offline = value.model_status === 'OFFLINE_FIXTURE' || value.scope === 'OFFLINE_FIXTURE' ||
    value.model_status === 'OFFLINE_TEAM_CONFIGURED' || value.execution_scope === 'TEST_OFFLINE';
  const analystNames: Record<string, string> = { market_report: '市场分析', sentiment_report: '情绪分析', news_report: '新闻分析', fundamentals_report: '基本面分析' };
  return <>
    <h3>多角色研究结论</h3>
    <SavedModelUsage jobId={value.job_id} runId={value.run_id} source="research" trace/>
    {typeof value.job_id === 'string' && <a href={`#intelligent?debate_job=${encodeURIComponent(value.job_id)}`}>查看本次任务、停止与恢复状态 →</a>}
    {offline && <Notice>这是离线练习，未调用在线模型。请勿用作投资依据。</Notice>}
    <p>报告评级：{({ Buy: '买入', Overweight: '提高持仓比例', Hold: '持有', Underweight: '降低持仓比例', Sell: '卖出', REVIEW: '暂缓判断' } as Record<string, string>)[String(report.rating)] ?? '请查看报告结论'}</p>
    <details><summary>模型状态和评级代码（技术信息）</summary><p>{display(value.model_status)} · {display(report.rating)}</p></details>
    {report.rating === 'REVIEW' && <Notice>报告暂缓判断，未给出买入或卖出结论。</Notice>}
    <h4>结论与适用条件</h4><NativeText value={report.conclusion} />
    <h4>未解决分歧</h4><Notice>报告没有单列分歧。可展开下方各方观点，了解他们在哪些地方意见不同。</Notice>
    <details><summary>看涨观点与讨论</summary><NativeText value={report.supporting_argument} /></details>
    <details><summary>看跌观点与讨论</summary><NativeText value={report.opposing_argument} /></details>
    <details><summary>风险讨论记录</summary><NativeText value={report.risk_debate} /></details>
    {Object.entries(record(report.analyst_reports)).map(([key, content]) => <details key={key}><summary>{analystNames[key] ?? '分析团队报告'}</summary><NativeText value={content} /></details>)}
    <details><summary>讨论轮数</summary>{report.rounds == null ? <p>报告没有记录实际讨论轮数。</p> : <JsonView value={report.rounds} />}</details>
    <details><summary>查阅使用的工具及关联报告</summary>{value.tool_references == null ? <p>原记录未提供工具引用。</p> : <>
      {Array.isArray(value.tool_references) && value.tool_references.map((item, i) => {
        const ref = record(item);
        if (typeof ref.run_id !== 'string' || !ref.run_id || !['backtesting', 'qlib'].includes(String(ref.kind))) return null;
        return <p key={i}><a href={ref.kind === 'backtesting' ? `#results?daily_run=${encodeURIComponent(ref.run_id)}` : `#my-results?run=${encodeURIComponent(ref.run_id)}`}>打开已保存{ref.kind === 'backtesting' ? '回测' : '模型'}结果 →</a></p>;
      })}
      <JsonView value={value.tool_references} />
    </>}</details>
    <details><summary>预算与本次使用</summary>{value.budget == null ? <p>报告未记录实际用量。</p> : <JsonView value={value.budget} />}</details>
    <details><summary>数据与引擎引用</summary><JsonView value={{ engine: value.engine, data_binding: value.data_binding, scope: value.scope }} /></details>
  </>;
}
