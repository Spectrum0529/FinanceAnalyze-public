import { AreaHelp } from './area-help';
import { record } from './native-research-api';

const count=(value:unknown)=>typeof value==='number'&&Number.isSafeInteger(value)&&value>=0?value:null;
const number=(value:number|null)=>value===null?'未提供':value.toLocaleString('zh-CN');
const speedBasis='CONFIRMED_OUTPUT_TOKENS_OVER_FULL_REQUEST_ELAPSED_INCLUDING_WAIT';

export function modelCallStatus(status:unknown){
 switch(status){
  case 'DONE':
  case 'SUCCEEDED':return '调用已返回';
  case 'FAILED':return '调用未成功';
  case 'CANCELLED':return '调用已取消';
  case 'UNKNOWN':return '调用结果尚不确定';
  default:return '状态未提供';
 }
}

// Presentation props only. Core owns the final transport DTO, persistence,
// aggregate scope/completeness and per-model/role identities. No UI accumulator.
export function ModelCallUsage({metrics,scope='call',missingUsageCalls,latestCallMetrics,speedSummary,title='模型用量'}:{metrics:unknown;scope?:'call'|'known_subtotal';missingUsageCalls?:unknown;latestCallMetrics?:unknown;speedSummary?:unknown;title?:string}){
 const values=record(metrics),latest=record(scope==='call'?metrics:latestCallMetrics);
 const input=count(values.input_tokens),output=count(values.output_tokens),total=count(values.total_tokens);
 const cached=count(values.cached_input_tokens),reasoning=count(values.reasoning_output_tokens);
 const context=count(latest.input_tokens),limit=count(latest.configured_input_limit);
 const window=count(latest.known_model_context_window),windowKnown=window!==null&&window>0&&typeof latest.known_model_context_window_source==='string'&&latest.known_model_context_window_source.trim().length>0;
 const elapsed=latest.elapsed_ms,rate=latest.output_tokens_per_second;
 const aggregate=record(speedSummary),aggregateRate=aggregate.output_tokens_per_second;
 const callSpeed=latest.speed_basis===speedBasis&&count(latest.output_tokens)!==null&&typeof elapsed==='number'&&Number.isFinite(elapsed)&&elapsed>0&&typeof rate==='number'&&Number.isFinite(rate)&&rate>=0?rate:null;
 const speed=speedSummary!==undefined?(aggregate.speed_basis===speedBasis&&count(aggregate.speed_pair_count)!==null&&Number(aggregate.speed_pair_count)>0&&typeof aggregateRate==='number'&&Number.isFinite(aggregateRate)&&aggregateRate>=0?aggregateRate:null):callSpeed;
 return <section aria-label="模型实际用量"><div className="section-heading"><h3>{title}</h3><AreaHelp topic="model-usage" title="模型用量"/></div>
  <dl>
   <div><dt>模型读了多少／写了多少（Token）</dt><dd>{number(input)}／{number(output)}</dd></div>
   <div><dt>{scope==='known_subtotal'?'已确认用量小计（Token）':'本次合计用量（Token）'}</dt><dd>{number(total)}</dd></div>
   <div><dt>{scope==='call'?'本次输入量／你设置的上限':'上次输入量／你设置的上限'}</dt><dd>{number(context)}／{number(limit)}</dd></div>
   <div><dt>{speedSummary!==undefined?'平均输出速度（含等待）':scope==='call'?'本次平均输出速度':'最后保存调用的平均输出速度'}</dt><dd>{speed===null?'未提供':`${speed.toLocaleString('zh-CN',{maximumFractionDigits:2})} Token/秒`}</dd></div>
  </dl>
  <details><summary>查看重复内容、推理用量和输入上限</summary>
  <p>{scope==='known_subtotal'?'以下是已确认的小计，不一定包含所有调用。':'以下是这次调用已确认的用量。'}</p>
  {scope==='known_subtotal'&&<p>未返回用量的调用：{count(missingUsageCalls)===null?'未提供':`${number(count(missingUsageCalls))}次`}。</p>}
  <dl>
   <div><dt>其中缓存命中输入Token</dt><dd>{number(cached)}</dd></div>
   <div><dt>其中推理输出Token</dt><dd>{number(reasoning)}</dd></div>
   <div><dt>配置输出上限</dt><dd>{number(count(latest.configured_output_limit))}</dd></div>
   <div><dt>配置历史消息上限</dt><dd>{number(count(latest.configured_history_messages))}</dd></div>
   <div><dt>输入太长时怎么办</dt><dd>{latest.context_policy==='reject'?'停止并提示':latest.context_policy==='truncate_oldest'?'移除最早的历史消息':'未提供'}</dd></div>
   <div><dt>模型最多能处理的用量（已核实）</dt><dd>{windowKnown?number(window):'未提供'}</dd></div>
  </dl><p>模型用 Token 来计量文字，每个 Token 可能是一部分或几个字符。重复输入和推理用量已包含在总数里。你设置的输入上限不代表实际用量或模型自身上限。</p></details>
 </section>;
}
