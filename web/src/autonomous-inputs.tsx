import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import type { RecordData } from './api';
import { record } from './native-research-api';
import { ErrorBox, useResource } from './components';

// Bounded, explicitly paged reads of existing catalogue APIs; no hidden full scan.
export function useResearchInputs(path:string,size:number,revision:number){
 const [state,setState]=useState<{items:RecordData[];next:number;more:boolean;loading:boolean;error:unknown}>({items:[],next:0,more:true,loading:true,error:null});
 const [page,setPage]=useState(0);const epoch=useRef(0);
 useEffect(()=>{epoch.current++;setPage(0);setState({items:[],next:0,more:true,loading:true,error:null});},[path,revision]);
 useEffect(()=>{
  const generation=epoch.current,abort=new AbortController();
  setState(s=>({...s,loading:true,error:null}));
  void request<RecordData>(`${path}${path.includes('?')?'&':'?'}limit=${size}&offset=${page}`,{signal:abort.signal}).then(value=>{
   if(abort.signal.aborted||generation!==epoch.current)return;
   const {items,total,offset,limit}=value;
   if(typeof total!=='number'||!Number.isSafeInteger(total)||total<0||offset!==page||limit!==size||
      !Array.isArray(items)||items.length!==Math.min(size,Math.max(0,total-page))||
      items.some(item=>!item||typeof item!=='object'||Array.isArray(item)))
     throw new Error('没有读到完整列表。请刷新后再查看。');
   const rows=items as RecordData[];
   setState(s=>({items:page===0?rows:[...s.items,...rows],next:page+size,more:page+size<total,loading:false,error:null}));
  }).catch(error=>{if(!abort.signal.aborted&&generation===epoch.current)setState(s=>({...s,loading:false,error}));});
  return()=>abort.abort();
 },[path,size,page,revision]);
 return {...state,loadMore:()=>{if(!state.loading&&state.more)setPage(state.next);}};
}

export function ResearchInputPages({state,noun}:{state:ReturnType<typeof useResearchInputs>;noun:string}){
 return <>{state.error!=null&&<ErrorBox error={state.error}/>}
  {state.loading?<p>正在读取{noun}…</p>:(!state.error||state.items.length>0)&&<p>已读取{state.items.length}项{noun}。</p>}
  {state.more&&<button type="button" disabled={state.loading||state.error!=null} onClick={state.loadMore}>加载更多{noun}</button>}
 </>;
}

export function ResearchModelSummary({roles,profiles}:{roles:Record<string,string>;profiles:RecordData[]}){
 return <details><summary>查看所选角色的模型、推理强度与指令</summary><p>这里只查看已保存的设置，不读取密钥。开始研究时才检查能否调用模型。</p>{Object.entries(roles).filter(([,ref])=>ref).map(([role,ref])=>{
  let id:unknown,revision:unknown;try{[id,revision]=JSON.parse(ref);}catch{return null;}
  const saved=profiles.find(row=>row.profile_id===id&&row.revision===revision);
  const configuration=record(saved?.configuration),model=record(configuration.model),prompt=record(configuration.prompt),budgets=record(configuration.budgets);
  const label=({default:'默认模型',hypothesis:'提出思路',coder:'编写策略或因子',critic:'检查结果',reporter:'整理报告'} as Record<string,string>)[role]??role;
  return <section key={role} aria-label={`${label}保存配置`}><h4>{label} · {String(saved?.name??'保存版本未在当前目录中')}</h4>{saved?<><p>{String(model.provider??'未提供')} · {String(model.model??'未提供')} · 版本{String(revision)}；推理强度：{String(model.effort??'模型默认')}。</p><p>输入上限{String(budgets.input_tokens??'未提供')} token；输出上限{String(budgets.output_tokens??'未提供')} token；历史{String(budgets.history_messages??'未提供')}条；超限处理：{budgets.context_policy==='truncate_oldest'?'先省去最早的消息':budgets.context_policy==='reject'?'拒绝并说明':'未提供'}。</p>{Object.entries({...prompt,context:configuration.context}).map(([key,value])=><details key={key}><summary>{({system:'预设指令',task:'任务要求',output:'输出格式',context:'给模型参考的资料'} as Record<string,string>)[key]??key}</summary><p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{typeof value==='string'&&value?value:'未填写'}</p></details>)}</>:<SavedRevisionPreview profileId={String(id)} revision={Number(revision)}/>}</section>;
 })}<a href="#intelligent?research_tab=models">编辑保存模型配置 →</a></details>;
}


function SavedRevisionPreview({profileId,revision}:{profileId:string;revision:number}){
 const state=useResource<RecordData>(`/model-profiles/${encodeURIComponent(profileId)}/revisions/${revision}`);
 if(state.error)return <ErrorBox error={state.error}/>;
 const saved=state.data;
 if(!saved)return <p>正在读取该成员选用的模型版本…</p>;
 if(saved.profile_id!==profileId||saved.revision!==revision)return <p>无法确认这是原先选好的版本。请重新选择；当前设置还没有改变。</p>;
 const configuration=record(saved.configuration),model=record(configuration.model),prompt=record(configuration.prompt),budgets=record(configuration.budgets);
 return <><p>{String(model.provider??'未提供')} · {String(model.model??'未提供')} · 版本{revision}；推理强度：{String(model.effort??'模型默认')}。</p><p>输入上限{String(budgets.input_tokens??'未提供')} token；输出上限{String(budgets.output_tokens??'未提供')} token；历史{String(budgets.history_messages??'未提供')}条。</p>{Object.entries({...prompt,context:configuration.context}).map(([key,value])=><details key={key}><summary>{({system:'预设指令',task:'任务要求',output:'输出格式',context:'给模型参考的资料'} as Record<string,string>)[key]??key}</summary><p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{typeof value==='string'&&value?value:'未填写'}</p></details>)}</>;
}

export function researchTeamMembers(team:RecordData|undefined){
 const refs=record(record(team?.configuration).profile_refs);
 const labels:Record<string,string>={default:'团队默认',market_analyst:'市场分析',social_analyst:'情绪分析',news_analyst:'新闻分析',fundamentals_analyst:'基本面分析',bull_researcher:'多方研究',bear_researcher:'空方研究',research_manager:'研究经理',trader:'交易决策',risky_analyst:'积极风险',safe_analyst:'保守风险',neutral_analyst:'中性风险',risk_manager:'风险经理'};
 return Object.entries(refs).flatMap(([slot,raw])=>{const row=record(raw);return Object.hasOwn(labels,slot)&&typeof row.profile_id==='string'&&Number.isSafeInteger(row.revision)&&Number(row.revision)>=1&&typeof row.sha256==='string'&&/^[a-f0-9]{64}$/.test(row.sha256)?[{slot,label:labels[slot],profileId:row.profile_id,revision:Number(row.revision),reference:JSON.stringify([row.profile_id,row.revision])}]:[];});
}
