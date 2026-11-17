import { SavedModelUsage } from './model-usage-panel';
import { ModelObservationUsage } from './model-observation-usage';
import type { RecordData } from './api';
import { record } from './native-research-api';
import { Notice, JsonView } from './components';
import { AreaHelp } from './area-help';
import { PortfolioAnalytics } from './portfolio-analytics';
import { formatAmount } from './display-amount';
const rows=(value:unknown):RecordData[]=>Array.isArray(value)?value.map(record):[];
const search=(value:RecordData)=>value.family==='strategy-search-result-1'?value:record(value.tool_result);
export const isStrategySearch=(value:RecordData)=>search(value).family==='strategy-search-result-1';
function candidate(value:unknown){const row=record(value);return row.template==='buy_hold'?'买入持有':row.template==='sma'?`${row.slow}日均线`:row.template==='sma_cross'?`${row.fast}/${row.slow}日均线交叉`:'未提供方案';}
export function StrategySearchReport({value}:{value:RecordData}){
 const report=search(value),comparison=rows(report.training_comparison),validation=record(report.selected_validation),steps=rows(report.steps),split=record(report.split),training=Array.isArray(split.training)?split.training:[],later=Array.isArray(split.validation)?split.validation:[];
 return <section aria-label="保存的策略试验报告"><div className="section-heading"><h2>策略试验结果</h2><AreaHelp title="策略试验结果" topic="strategy-search" /></div>
  {(value.model_observations!=null||report.model_observations!=null||rows(value.model_calls).length>0)&&<SavedModelUsage jobId={value.job_id??report.job_id} runId={value.run_id??report.run_id} source="tasks" fallback={<ModelObservationUsage observations={[...rows(value.model_observations),...rows(report.model_observations)]} coverage={value.model_observations_status??report.model_observations_status}/>}/>}
  {report.status==='INSUFFICIENT_DATA'?<Notice>数据不足，未能比较策略。请选更长的数据：每只证券至少需要 80 个用来选策略的交易日和 40 个留作检查的交易日。</Notice>:<Notice>{report.status==='COMPLETED_EXPLORATORY_NOT_MARKET_VALIDATED'?'已保存较早一段的对比结果，并用较晚一段数据检查了一次选中的策略。':'试验已停止。下方显示已保存的对比结果，较晚一段的检查可能还没完成。'}历史试验不能保证未来收益。</Notice>}
  <p>用来比较策略的数据：{String(training[0] ?? '未提供')} 至 {String(training.at(-1) ?? '未提供')}；留作检查的数据：{String(later[0] ?? '未提供')} 至 {String(later.at(-1) ?? '未提供')}。</p>
  <p>每个方案用相同的起始资金单独计算。较晚一段的数据不参与排名。</p>
  {!!comparison.length&&<div className="table-scroll"><table><caption>用较早数据比较方案</caption><thead><tr><th>方案</th><th>较早一段的净收益</th><th>费用</th></tr></thead><tbody>{comparison.map(row=><tr key={String(row.result_id)}><td>{candidate(row.candidate)}</td><td>{formatAmount(row.training_net_profit)}</td><td>{formatAmount(row.fees)}</td></tr>)}</tbody></table></div>}
  {!!steps.length&&<details><summary>查看实际执行步骤（{steps.length}步）</summary><ol>{steps.map(row=><li key={String(row.step_id)}>第{String(row.step)}步 · {row.tool==='strategy_trial'?'回测候选方案':row.tool==='strategy_compare'?'对比训练结果':row.tool==='strategy_finalize'?'固定选中的方案并检查':'保存步骤'}</li>)}</ol></details>}
  {validation.result!=null&&<><h3>用较晚一段数据检查选中的方案 · {candidate(validation.candidate)}</h3><PortfolioAnalytics value={record(validation.result)} /></>}
  {rows(report.training_trials).map(row=><details key={String(row.result_id)}><summary>较早一段的结果 · {candidate(row.candidate)}</summary><PortfolioAnalytics value={record(row.result)} /></details>)}
  <details><summary>技术信息（原始记录）</summary><JsonView value={report} /></details>
 </section>;
}
