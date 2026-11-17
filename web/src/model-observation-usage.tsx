import { useMemo, useState } from 'react';
import { record } from './native-research-api';
import { ModelCallUsage, modelCallStatus } from './model-call-usage';

const metricKeys=['input_tokens','output_tokens','total_tokens','cached_input_tokens','reasoning_output_tokens','usage_source','elapsed_ms','output_tokens_per_second','speed_basis','configured_input_limit','configured_output_limit','configured_history_messages','context_policy','known_model_context_window','known_model_context_window_source'];
const tokenKeys=['input_tokens','output_tokens','total_tokens','cached_input_tokens','reasoning_output_tokens'] as const;
type SavedObservation = {
 identity_key:string;
 call_id:string;
 provider:string;
 model:string;
 role:string;
 profile_id:string;
 revision:number|null;
 status:string;
 started_at:string;
 ended_at:string;
 metrics:Record<string,string|number|null>;
};
function knownSubtotal(rows:readonly SavedObservation[]){return Object.fromEntries(tokenKeys.map(key=>{let sum:number|null=null;for(const row of rows){const value=row.metrics[key];if(typeof value!=='number'||!Number.isSafeInteger(value)||value<0)continue;const next:number=(sum??0)+value;if(!Number.isSafeInteger(next))return [key,null];sum=next;}return [key,sum];}));}
const text=(value:unknown)=>typeof value==='string'&&value.length>0&&value.length<=128&&!value.includes('://')?value:'未提供';
function latestMetrics(rows:readonly SavedObservation[]){
 let latest:SavedObservation|undefined;
 for(const row of rows){
  if(!Number.isFinite(Date.parse(row.started_at)))continue;
  if(!latest||row.started_at>latest.started_at||row.started_at===latest.started_at&&row.identity_key>latest.identity_key)latest=row;
 }
 return latest?.metrics;
}

// This is a view of already returned rows, not a session accumulator. Core owns
// authoritative totals and restore coverage. Duplicate conflicts are withheld.
export function ModelObservationUsage({observations,coverage,scope='本次结果'}:{observations:unknown;coverage?:unknown;scope?:string}){
 const [page,setPage]=useState(0);
 const {rows,conflicts,invalid}=useMemo(()=>{
  const seen=new Map<string,{identity:string;row:SavedObservation}>(),bad=new Set<string>();let invalid=0;
  const input:readonly unknown[]=Array.isArray(observations)?observations:[];
  for(const raw of input){const source=record(raw);
   if(source.schema!=='ai-model-call-observation-1'||typeof source.call_id!=='string'||!/^[a-f0-9]{32}$/.test(source.call_id)){invalid++;continue;}
   const identityKey=typeof source.job_id==='string'&&typeof source.run_id==='string'?JSON.stringify([source.job_id,source.run_id,source.call_id]):source.call_id;
   if(bad.has(identityKey))continue;
   const metric=record(source.metrics),metrics=Object.fromEntries(metricKeys.map<[string,string|number|null]>(key=>{
    const value=metric[key];
    return [key,typeof value==='string'||typeof value==='number'?value:null];
   }));
   const row:SavedObservation={identity_key:identityKey,call_id:source.call_id,provider:text(source.provider),model:text(source.model),role:text(source.role),profile_id:text(source.profile_id),revision:typeof source.revision==='number'&&Number.isSafeInteger(source.revision)&&source.revision>=0?source.revision:null,status:modelCallStatus(source.status),started_at:text(source.started_at),ended_at:text(source.ended_at),metrics};
   const identity=JSON.stringify([source.status??null,row]),old=seen.get(identityKey);
   if(old&&old.identity!==identity){seen.delete(identityKey);bad.add(identityKey);}else if(!old)seen.set(identityKey,{identity,row});
  }
  return {rows:[...seen.values()].map(value=>value.row),conflicts:bad.size,invalid};
 },[observations]);
 const subtotal=useMemo(()=>knownSubtotal(rows),[rows]);
 const groups=useMemo(()=>{const grouped=new Map<string,typeof rows>();for(const row of rows){const key=row.provider==='未提供'||row.model==='未提供'?String(row.identity_key):JSON.stringify([row.provider,row.model]);const group=grouped.get(key)??[];group.push(row);grouped.set(key,group);}return [...grouped.entries()].map(([key,calls])=>({key,calls,subtotal:knownSubtotal(calls)}));},[rows]);
 const incompleteRows=rows.filter(row=>['input_tokens','output_tokens','total_tokens'].some(key=>{const value=row.metrics[key];return typeof value!=='number'||!Number.isSafeInteger(value)||value<0;})).length;
 const coverageValue=record(coverage);
 const start=Math.min(page,Math.max(0,Math.ceil(rows.length/20)-1))*20;
 return <section aria-label="已保存模型调用用量">
  <ModelCallUsage metrics={subtotal} scope="known_subtotal" latestCallMetrics={latestMetrics(rows)} title={scope==='本页轨迹'?'本页已确认用量':'当前结果已确认用量'}/>
  <p>用量记录可能不完整。</p>
  <details><summary>按模型和角色查看</summary>
<p>{scope}已读取{rows.length}条不同调用。本次结果覆盖：{coverageValue.aggregate_complete===false?'已标记不完整':'未提供'}；整份任务的已确认用量小计（Token）及未返回用量次数：未提供。</p>
  {coverageValue.scope==='THIS_RUN_ONLY'&&<p>统计范围：仅本次运行，未合并恢复前的其他运行。</p>}
  {coverageValue.result_rows_omitted===true&&<p>部分调用明细未包含在这份结果中，累计不完整。</p>}
  {typeof coverageValue.evidence_events_dropped==='number'&&Number.isSafeInteger(coverageValue.evidence_events_dropped)&&coverageValue.evidence_events_dropped>=0&&<p>未保存成功的用量证据事件：{coverageValue.evidence_events_dropped}条；这不是缺失Token数。</p>}
  {coverageValue.unknown_usage_possible===true&&<p>仍可能有未返回真实用量的调用，未知调用次数未提供。</p>}
  <p>这里逐条显示已保存的确认值，不把现有记录当作完整累计；失败调用已返回的用量也保留。</p>
  {(conflicts>0||invalid>0)&&<p>有{conflicts}个调用编号的记录不一致，另有{invalid}条记录格式不完整，未用于显示用量；研究结果仍可查看。</p>}
  <p>已显示调用中，{incompleteRows}条的Token用量有缺项；没有确认值的字段保持未提供。</p>
  <h4>按模型的已确认小计</h4>
  {groups.map(group=><details key={group.key}><summary>{String(group.calls[0].provider)} · {String(group.calls[0].model)} · {group.calls.length}条已显示调用</summary><ModelCallUsage metrics={group.subtotal} scope="known_subtotal" latestCallMetrics={latestMetrics(group.calls)} title="此模型已确认用量"/></details>)}
  <h4>按角色和调用查看</h4>
  {rows.slice(start,start+20).map(row=><details key={String(row.identity_key)}><summary>{String(row.role)} · {String(row.model)} · {String(row.status)}</summary><p>模型厂商：{String(row.provider)}；保存版本：{row.revision===null?'未提供':String(row.revision)}。</p><ModelCallUsage metrics={row.metrics}/></details>)}
  {rows.length>20&&<div className="pagination"><button disabled={start===0} onClick={()=>setPage(value=>Math.max(0,value-1))}>上一页模型调用</button><button disabled={start+20>=rows.length} onClick={()=>setPage(value=>value+1)}>下一页模型调用</button></div>}
  </details>
 </section>;
}
