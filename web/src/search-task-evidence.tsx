import { ModelObservationUsage } from './model-observation-usage';
import { useState } from 'react';
import { ApiError } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, useResource } from './components';
import { record } from './native-research-api';
import { formatAmount } from './display-amount';
const labels:Record<string,string>={VALIDATION_STARTED:'固定选定方案，用较晚的行情检查',VALIDATION_COMPLETED:'较晚区间验证完成',TRIAL_STARTED:'开始候选回测',TRIAL_COMPLETED:'候选回测完成',TRIAL_FAILED:'候选回测失败',TOOL_COMPLETED:'工具步骤完成',MODEL_REQUEST_STARTED:'已发起模型请求',MODEL_TOOL_CALL:'模型提出受控工具调用',MODEL_CALL_OBSERVATION:'模型调用用量已保存',MODEL_OBSERVATION_COVERAGE:'本次调用记录覆盖说明'};
const tools:Record<string,string>={strategy_trial:'回测候选',strategy_compare:'对比训练结果',strategy_finalize:'固定选定方案并检查'};
function candidate(value:unknown){const row=record(value);const n=(v:unknown)=>Number.isSafeInteger(v)?String(v):'未提供';return row.template==='buy_hold'?'买入持有':row.template==='sma'?`${n(row.slow)}日均线`:row.template==='sma_cross'?`${n(row.fast)}/${n(row.slow)}日均线交叉`:'服务保存的候选';}
const safeCode=(value:unknown)=>typeof value==='string'&&/^[A-Z][A-Z0-9_]{0,95}$/.test(value)?value:'SEARCH_ATTEMPT_FAILED';
export function SearchTaskEvidence({id,terminal}:{id:string;terminal:boolean}){
 const [open,setOpen]=useState(false);
 return <details onToggle={event=>setOpen(event.currentTarget.open)}><summary>查看实际步骤与候选尝试</summary>{open&&<Evidence id={id} terminal={terminal}/>}</details>;
}
function Evidence({id,terminal}:{id:string;terminal:boolean}){
 const [refresh,setRefresh]=useState(0),[offset,setOffset]=useState(0);
 const state=useResource<RecordData>(`/research/tasks/${encodeURIComponent(id)}/search-evidence?limit=20&offset=${offset}`,refresh,terminal?false:1500);
 const value=state.data;
 const items=Array.isArray(value?.items)?value.items:[];
 const coverageRow=items.map(record).filter(row=>record(row.evidence).event==='MODEL_OBSERVATION_COVERAGE'&&typeof row.sequence==='number'&&Number.isSafeInteger(row.sequence)&&row.sequence>=0).sort((a,b)=>Number(a.sequence)-Number(b.sequence)).at(-1);
 const pageCoverage=record(record(coverageRow?.evidence).coverage);
 const valid=value?.family==='strategy-search-evidence-1'&&value.job_id===id&&value.view==='summary'&&value.original_evidence_retained===true&&value.not_a_success_report===true&&value.resume_supported===false&&Array.isArray(value.items);
 return <section aria-label="真实任务执行轨迹"><Notice>以下为已保存任务轨迹的分页摘要，原始证据仍完整保留。可能包含失败或停止前完成的尝试。它不是成功报告，也不支持断点续跑。</Notice><button className="text-button" onClick={()=>setRefresh(n=>n+1)}>刷新任务轨迹</button>
  {state.error?<ErrorBox error={new ApiError(0,'SEARCH_EVIDENCE_UNAVAILABLE','暂时无法读取任务轨迹，请核对任务权限或稍后刷新；不会将缺失轨迹当作成功。')}/>:!value?<p>正在读取服务保存的步骤…</p>:!valid?<ErrorBox error={new ApiError(0,'SEARCH_EVIDENCE_MISMATCH','返回的步骤与所选任务不一致。请重新打开任务。')}/>:!items.length?<p>暂时没有保存的步骤。请查看当前任务状态。</p>:<ol>{items.map(record).map(row=>{
   const event=record(row.evidence),attempt=record(event.attempt),trial=record(event.trial??event.validation),reconciliation=record(trial.metrics),step=record(event.step),call=record(event.call);
   return <li key={String(row.sequence)}><strong>记录 {String(row.sequence)} · {labels[String(event.event)]??'已保存步骤'}</strong>
    {event.attempt!=null&&<p>候选 {String(attempt.attempt??'未提供')} · {candidate(attempt.candidate)}</p>}
    {event.event==='TRIAL_COMPLETED'&&<p>训练净收益：{formatAmount(reconciliation.net_profit)}；费用：{formatAmount(reconciliation.fees)}。仅代表此候选的训练结果。</p>}
    {event.event==='VALIDATION_COMPLETED'&&<p>较晚区间净收益：{formatAmount(reconciliation.net_profit)}；费用：{formatAmount(reconciliation.fees)}。这是选定方案的一次检查。整个研究是否已完成，请查看任务状态。</p>}
    {event.event==='TRIAL_FAILED'&&<><p>该候选未完成有效计算；已保留失败尝试，不计作策略成功。</p><details><summary>查看受控错误代码</summary><code>{safeCode(attempt.code)}</code></details></>}
    {event.event==='TOOL_COMPLETED'&&<p>{tools[String(step.tool)]??'受控工具步骤'} · 第 {String(step.step??'未提供')} 步</p>}
    {event.event==='MODEL_TOOL_CALL'&&<p>{tools[String(call.tool)]??'受控工具调用'}</p>}
    <details><summary>证据身份（技术信息）</summary><p>原证据哈希：<code>{typeof row.sha256==='string' && /^[a-f0-9]{64}$/.test(row.sha256)?row.sha256:'未提供'}</code></p>{trial.result_id!=null&&<p>结果引用：{String(trial.result_id)}</p>}</details>
   </li>;
  })}</ol>}
  {valid&&(coverageRow!==undefined||items.some(raw=>record(record(raw).evidence).model_observation!=null))&&<ModelObservationUsage scope="本页轨迹" observations={items.map(record).map(row=>record(row.evidence).model_observation).filter(Boolean)} coverage={pageCoverage}/>}
  {valid&&<div className="pagination" aria-label="任务轨迹分页"><span>本页 {items.length} 条；服务共记录 {String(value.total??'未提供')} 条。</span><button disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-20))}>上一页轨迹</button><button disabled={!Number.isSafeInteger(value.next_offset)||Number(value.next_offset)<=offset||Number(value.next_offset)>10000} onClick={()=>setOffset(Number(value.next_offset))}>下一页轨迹</button></div>}

 </section>;
}
