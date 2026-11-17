import { SavedCandidateBacktest } from './saved-candidate-backtest';
import { useEffect, useRef, useState } from 'react';
import { ApiError } from './api';
import { record } from './native-research-api';
import { Panel, Notice, ErrorBox } from './components';
import { ResearchInputPages, useResearchInputs } from './autonomous-inputs';
import { useAutonomousIntent } from './autonomous-request-intent';
import { candidateMetadata, readResearchCandidate, saveResearchCandidate } from './research-candidates-api';
import type { ResearchCandidate, SaveResearchCandidate } from './research-candidates-api';
import type { AutonomousArtifact } from './autonomous-research-api';

export function ResearchCandidateLibrary({selected,changed,disabled,userId}:{selected:string[];changed:(ids:string[])=>void;disabled:boolean;userId:string}){
 const [refresh,setRefresh]=useState(0),[id,setId]=useState(''),[backtestId,setBacktestId]=useState('');
 const state=useResearchInputs('/research/autonomous/candidates',50,refresh);
 let rows:ResearchCandidate[]=[],error:unknown=null;
 try{rows=state.items.map(candidateMetadata);}catch(e){error=e;}
 return <Panel title="我的策略与因子候选"><Notice>可选最多 4 个以前保存的策略或因子，给新研究作参考。选中不会运行代码，也不代表方案已经验证。</Notice><p>已选 {selected.length}/4 项。新研究仍需选择数据、模型和额度。</p><button type="button" onClick={()=>setRefresh(n=>n+1)}>刷新候选库</button>{error!=null?<ErrorBox error={error}/>:<ul>{rows.map(row=><li key={row.candidate_id}><label><input type="checkbox" checked={selected.includes(row.candidate_id)} disabled={disabled||!selected.includes(row.candidate_id)&&selected.length>=4} onChange={e=>changed(e.target.checked?[...selected,row.candidate_id]:selected.filter(v=>v!==row.candidate_id))}/>{row.name} · {row.kind==='strategy'?'策略':'因子'}</label><button type="button" className="text-button" onClick={()=>setId(row.candidate_id)}>查看保存代码与来源</button><button type="button" disabled={disabled} onClick={()=>setBacktestId(row.candidate_id)}>再次回测（不调用模型）</button><a href={`#my-results?autonomous_session=${encodeURIComponent(row.session_id)}`}>原研究记录 →</a></li>)}</ul>}{!state.loading&&!state.error&&!error&&!rows.length&&<p>这一页没有保存的方案。可打开已完成的实验，给策略或因子命名并保存。</p>}<ResearchInputPages state={state} noun="候选"/>{!!selected.length&&<button type="button" disabled={disabled} onClick={()=>changed([])}>清空本次选择</button>}{id&&<CandidateDetail key={id} id={id}/>} {backtestId&&<SavedCandidateBacktest key={`${userId}:${backtestId}`} candidateId={backtestId} userId={userId} canWrite={!disabled}/>}</Panel>;
}
function CandidateDetail({id}:{id:string}){
 const [value,setValue]=useState<Awaited<ReturnType<typeof readResearchCandidate>>|null>(null),[error,setError]=useState<unknown>(null);
 useEffect(()=>{const abort=new AbortController();void readResearchCandidate(id,abort.signal).then(v=>{if(!abort.signal.aborted)setValue(v);}).catch(e=>{if(!abort.signal.aborted)setError(e);});return()=>abort.abort();},[id]);
 return <section aria-label="候选保存详情">{error!=null?<ErrorBox error={error}/>:!value?<p>正在读取已保存的方案…</p>:<><h3>{value.metadata.name}</h3><p>保存于{value.metadata.created_at}；来自开发阶段的研究；保存后仍只是供查看的代码。</p><a href={`#my-results?autonomous_session=${encodeURIComponent(value.metadata.session_id)}`}>重开原研究与实验 →</a><details><summary>查看完整代码（只读）</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{value.source}</pre></details></>}</section>;
}

export function SaveResearchCandidateForm({artifact,userId,canWrite}:{artifact:AutonomousArtifact;userId:string;canWrite:boolean}){
 const journal=useAutonomousIntent<SaveResearchCandidate>(userId,`candidate:${artifact.session_id}:${artifact.attempt_id}:${artifact.logical_id}`,(raw):raw is SaveResearchCandidate=>{const row=record(raw);return row.session_id===artifact.session_id&&row.attempt_id===artifact.attempt_id&&row.logical_id===artifact.logical_id&&typeof row.name==='string'&&row.name.length>0&&row.name.length<=80;});
 const [name,setName]=useState(journal.current.current?.name??''),[busy,setBusy]=useState(false),[saved,setSaved]=useState<ResearchCandidate|null>(null),[error,setError]=useState<unknown>(null);const gate=useRef(false);
 async function save(){if(gate.current||!canWrite||journal.error)return;gate.current=true;setBusy(true);setError(null);try{
  if(!journal.current.current)journal.save({session_id:artifact.session_id,attempt_id:artifact.attempt_id,logical_id:artifact.logical_id,name:name.trim()});
  const result=await saveResearchCandidate(journal.current.current!,artifact.full_sha256);journal.clear();setSaved(result);
 }catch(e){setError(e);if(e instanceof ApiError&&[400,401,403,404,422].includes(e.status))journal.clear();}finally{gate.current=false;setBusy(false);}}
 return <section aria-label="命名保存研究候选"><h3>保存为可复用候选</h3><p>只有开发阶段成功实验的完整策略或因子可以保存。确认上次保存只会检查同一次操作；不会运行代码。</p>{journal.error&&<ErrorBox error={journal.error}/>}<label>候选名称<input value={name} maxLength={80} disabled={busy||!canWrite||!!journal.current.current||!!journal.error} onChange={e=>setName(e.target.value)}/></label><button type="button" disabled={busy||!canWrite||!!journal.error||!name.trim()||!!saved} onClick={()=>void save()}>{journal.current.current?'确认上次是否已保存':'保存此方案'}</button>{error!=null&&<ErrorBox error={error}/>} {saved&&<Notice>候选已保存：{saved.name}。<a href={`#intelligent?research_tab=autonomous&reuse_candidate=${encodeURIComponent(saved.candidate_id)}`}>给新的自主研究作参考 →</a></Notice>}</section>;
}
