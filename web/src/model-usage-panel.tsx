import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import type { RecordData } from './api';
import { ApiError } from './api';
import { observeResource } from './resource-reader';
import { record } from './native-research-api';
import { ModelCallUsage, modelCallStatus } from './model-call-usage';
import { ModelObservationUsage } from './model-observation-usage';

const keys=['input_tokens','output_tokens','total_tokens','cached_input_tokens','reasoning_output_tokens'];
const labels=['输入Token','输出Token','合计Token','缓存输入Token','推理输出Token'];
const identifier=(value:unknown):value is string=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
const count=(value:unknown)=>typeof value==='number'&&Number.isSafeInteger(value)&&value>=0?value:null;
const shown=(value:unknown)=>count(value)===null?'未提供':String(value);
const coverages=['UNKNOWN','INCOMPLETE','COMPLETE_FOR_RECORDED_SOURCES'];
const safeName=(value:unknown)=>typeof value==='string'&&value.length<=128&&!value.includes('://')?value:'未提供';

function useUsageRead(path:string,active=false,keepOnTransient=false){
 const [state,setState]=useState<{path:string;data:RecordData|null;error:unknown}>({path,data:null,error:null});
 useEffect(()=>{let stop=()=>{};stop=observeResource<RecordData>(path,{
  begin:()=>setState(prior=>keepOnTransient&&prior.path===path?prior:{path,data:null,error:null}),
  data:data=>setState({path,data,error:null}),
  error:error=>{
   const status=error instanceof ApiError?error.status:null;
   const temporary=status===0||status===429||status!==null&&status>=500&&status!==501;
   setState(prior=>({path,data:keepOnTransient&&temporary&&prior.path===path?prior.data:null,error}));
   if(status!==null&&[401,403,404,501].includes(status))stop();
  },
  settled:()=>{},
 },active?2500:false);return()=>stop();},[path,active,keepOnTransient]);
 return state.path===path?state:{path,data:null,error:null};
}
function validSummary(value:RecordData|null,kind:string,id:string,offset:number){
 return value?.schema==='model-usage-summary-1'&&value.scope_kind===kind&&value.scope_id===id&&value.limit===20&&value.offset===offset&&Array.isArray(value.models)&&value.models.length<=20&&count(value.recorded_call_count)!==null&&count(value.models_total)!==null&&coverages.includes(String(value.coverage))&&['NOT_RECORDED','PARTIAL','RECORDED'].includes(String(value.availability))&&value.coverage_basis==='RECORDED_UNIQUE_CALL_IDENTITIES_NOT_PROVIDER_BILLING'&&(value.next_offset===null||count(value.next_offset)!==null&&Number(value.next_offset)>offset);
}
function SummaryMetrics({value,title}:{value:RecordData;title:string}){
 const latest=record(value.latest_call);
 return <ModelCallUsage title={title} metrics={value.known_subtotals} scope="known_subtotal" latestCallMetrics={latest.metrics} missingUsageCalls={value.unknown_usage_call_count} speedSummary={value}/>;
}
function CoverageDetails({value}:{value:RecordData}){
 const totals=record(value.known_field_call_counts),complete=record(value.complete_for_recorded_calls);
 const latest=record(value.latest_call);
 return <><p>最后保存调用的角色：{safeName(latest.role)}；模型：{safeName(latest.model)}；版本：{shown(latest.revision)}；状态：{modelCallStatus(latest.status)}。</p><p>已记录调用：{shown(value.recorded_call_count)}；其中用量未知：{shown(value.unknown_usage_call_count)}；冲突排除：{shown(value.conflicting_call_count)}。</p><p>这是已保存来源的用量；记录齐全也不保证捕获了所有真实调用。模型分组分页和重复刷新不增加累计数。</p><table><thead><tr><th>字段</th><th>已提供的调用数</th><th>对已记录调用是否齐全</th></tr></thead><tbody>{keys.map((key,i)=><tr key={key}><td>{labels[i]}</td><td>{shown(totals[key])}</td><td>{complete[key]===true?'齐全':complete[key]===false?'不齐全':'未提供'}</td></tr>)}</tbody></table><p>平均速度只使用同时有确认输出和请求耗时的{shown(value.speed_pair_count)}次调用，按合计输出除以合计请求耗时计算，包含等待时间。</p></>;
}
function ModelRows({value,basis}:{value:RecordData;basis:unknown}){
 return <>{(Array.isArray(value.models)?value.models:[]).map(record).map((group,index)=><details key={JSON.stringify([group.provider,group.model,index])}><summary>{safeName(group.provider)} · {safeName(group.model)}</summary><SummaryMetrics title="此模型已记录用量" value={{...group,speed_basis:typeof basis==='string'?basis:null}}/><CoverageDetails value={group}/></details>)}</>;
}
function ModelPages({first,path,kind,id}:{first:RecordData;path:string;kind:string;id:string}){
 const [positions,setPositions]=useState([0]),offset=positions[positions.length-1];
 const previous=()=>setPositions(values=>values.slice(0,-1));
 const next=(position:number)=>setPositions(values=>[...values,position]);
 if(offset===0)return <><ModelRows value={first} basis={first.speed_basis}/><PageActions value={first} previous={previous} next={next} canPrevious={false}/></>;
 return <RemoteModels key={offset} path={path} kind={kind} id={id} offset={offset} previous={previous} next={next}/>;
}
function PageActions({value,previous,next,canPrevious}:{value:RecordData;previous:()=>void;next:(position:number)=>void;canPrevious:boolean}){
 return <div className="pagination"><button disabled={!canPrevious} onClick={previous}>上一页模型</button><button disabled={value.next_offset===null||count(value.next_offset)===null} onClick={()=>next(Number(value.next_offset))}>下一页模型</button></div>;
}
function RemoteModels({path,kind,id,offset,previous,next}:{path:string;kind:string;id:string;offset:number;previous:()=>void;next:(position:number)=>void}){
 const state=useUsageRead(`${path}?limit=20&offset=${offset}`),value=state.data;
 return <>{validSummary(value,kind,id,offset)&&value?<><ModelRows value={value} basis={value.speed_basis}/><PageActions value={value} previous={previous} next={next} canPrevious/></>:<><p>这页模型用量暂未提供。</p><button onClick={previous}>返回上一页模型</button></>}</>;
}
function TracePage({id,active}:{id:string;active:boolean}){
 const [offset,setOffset]=useState(0),state=useUsageRead(`/research/tasks/${encodeURIComponent(id)}/model-trace?limit=20&offset=${offset}`,active&&offset===0),value=state.data;
 const valid=value?.schema==='ai-model-trace-page-1'&&value.job_id===id&&value.limit===20&&value.offset===offset&&value.not_a_success_report===true&&coverages.includes(String(value.coverage))&&Array.isArray(value.items)&&value.items.length<=20&&count(value.total)!==null&&(value.next_offset===null||count(value.next_offset)!==null&&Number(value.next_offset)>offset);
 const rows=valid&&Array.isArray(value?.items)?value.items.map(record):[];
 const observations=rows.map(row=>{const observed=record(row.model_observation);return observed.job_id===row.job_id&&observed.run_id===row.run_id?observed:null;}).filter(Boolean);
 return <>{valid&&value?<><p>这是保存的调用证据，不是研究成功报告；没有记录不表示没有实际调用。</p><ModelObservationUsage scope="本页轨迹" observations={observations}/><div className="pagination"><button disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-20))}>上一页调用轨迹</button><button disabled={value.next_offset===null} onClick={()=>setOffset(Number(value.next_offset))}>下一页调用轨迹</button></div></>:<p>调用轨迹暂未提供。</p>}</>;
}

export function ModelUsagePanel({kind,id,active=false,trace=false,fallback}:{kind:'session'|'job';id:string;active?:boolean;trace?:boolean;fallback?:ReactNode}){
 if(!identifier(id))return <>{fallback??<p>模型用量未提供。</p>}</>;
 return <UsageReader key={`${kind}:${id}`} kind={kind} id={id} active={active} trace={trace} fallback={fallback}/>;
}
function UsageReader({kind,id,active,trace,fallback}:{kind:'session'|'job';id:string;active:boolean;trace:boolean;fallback?:ReactNode}){
 const path=kind==='session'?`/research/autonomous/sessions/${encodeURIComponent(id)}/model-usage`:`/research/tasks/${encodeURIComponent(id)}/model-usage`;
 const state=useUsageRead(`${path}?limit=20&offset=0`,active,true),value=state.data,[open,setOpen]=useState(false);
 const billing=<p>费用金额与币种未提供。外部 MCP 客户端的模型账单不在工作台的用量记录中。</p>;
 if(!validSummary(value,kind,id,0)||!value)return <>{fallback??<ModelCallUsage metrics={null} scope="known_subtotal" title="已记录用量"/>}{!fallback&&<p>模型用量暂未提供。</p>}{billing}</>;
 return <section aria-label="任务已记录模型用量"><SummaryMetrics value={value} title="已记录用量"/>{state.error!=null&&<p role="status">本次刷新未成功，仍显示此任务上次读取的用量。</p>}<p>{value.availability==='NOT_RECORDED'?'尚未提供用量记录。':value.coverage==='INCOMPLETE'||value.availability==='PARTIAL'?'部分调用未提供完整用量。':value.coverage==='UNKNOWN'?'用量覆盖情况未提供。':'用量覆盖仅限已保存的来源。'}</p>{billing}<details onToggle={event=>setOpen(event.currentTarget.open)}><summary>按模型和角色查看</summary><CoverageDetails value={value}/>{open&&<><ModelPages first={value} path={path} kind={kind} id={id}/>{trace&&kind==='job'&&<TracePage id={id} active={active}/>}</>}</details></section>;
}

export function SavedModelUsage({jobId,runId,source,trace=false,fallback}:{jobId:unknown;runId:unknown;source:'research'|'tasks';trace?:boolean;fallback?:ReactNode}){
 if(identifier(jobId))return <ModelUsagePanel kind="job" id={jobId} trace={trace} fallback={fallback}/>;
 if(identifier(runId))return <SavedUsageLookup key={`${source}:${runId}`} resultId={`${source}:${runId}`} runId={runId} trace={trace} fallback={fallback}/>;
 return <>{fallback??<p>保存结果没有提供任务编号，模型用量未提供。</p>}</>;
}
function SavedUsageLookup({resultId,runId,trace,fallback}:{resultId:string;runId:string;trace:boolean;fallback?:ReactNode}){
 const state=useUsageRead(`/results/${encodeURIComponent(resultId)}/summary`),data=state.data;
 const valid=data?.result_id===resultId&&data.view==='summary'&&typeof data.source_view_sha256==='string'&&/^[a-f0-9]{64}$/.test(data.source_view_sha256);
 const metadata=valid?record(data.summary):{};
 if(identifier(metadata.job_id)&&(metadata.run_id===undefined||metadata.run_id===runId))return <ModelUsagePanel kind="job" id={metadata.job_id} trace={trace} fallback={fallback}/>;
 if(valid&&metadata._paged===true&&metadata.section===''&&metadata.kind==='object')return <RootMetadataPage resultId={resultId} hash={String(data.source_view_sha256)} runId={runId} trace={trace} fallback={fallback}/>;
 return <>{fallback??<p>保存结果的允许读取信息没有提供任务编号，模型用量未提供。</p>}</>;
}
function RootMetadataPage({resultId,hash,runId,trace,fallback}:{resultId:string;hash:string;runId:string;trace:boolean;fallback?:ReactNode}){
 const [offset,setOffset]=useState(0),query=new URLSearchParams({section:'',limit:'500',offset:String(offset),expected_sha256:hash});
 const state=useUsageRead(`/results/${encodeURIComponent(resultId)}/series?${query}`),value=state.data;
 const valid=value?.result_id===resultId&&value.source_view_sha256===hash&&value.section===''&&value.kind==='object'&&value.limit===500&&value.offset===offset&&Array.isArray(value.items)&&value.items.length<=500;
 const items=valid&&Array.isArray(value?.items)?value.items.map(record):[],job=items.find(row=>row.key==='job_id')?.value,run=items.find(row=>row.key==='run_id')?.value;
 if(identifier(job)&&(run===undefined||run===runId))return <ModelUsagePanel kind="job" id={job} trace={trace} fallback={fallback}/>;
 return <>{fallback??<p>这页结果信息没有提供任务编号，模型用量未提供。</p>}{valid&&count(value?.next_offset)!==null&&Number(value?.next_offset)>offset&&Number(value?.next_offset)<=2000000&&<button onClick={()=>setOffset(Number(value?.next_offset))}>继续读取结果信息</button>}</>;
}
