import { useEffect, useRef, useState } from 'react';
import { ErrorBox } from './components';
import { record } from './native-research-api';
import { autonomousResearchApi } from './autonomous-research-api';
import type { AutonomousCursorPage, AutonomousIndexKind } from './autonomous-research-api';

export function useAutonomousPage<T>(
 api:ReturnType<typeof autonomousResearchApi>,sessionId:string,kind:AutonomousIndexKind,
 tick:number,attemptId?:string,enabled=true,
){
 const scope=JSON.stringify([sessionId,kind,attemptId??null]);
 const [position,setPosition]=useState({scope,cursor:'',history:[] as string[]}),[reload,setReload]=useState(0);
 const cursor=position.scope===scope?position.cursor:'',history=position.scope===scope?position.history:[];
 type Snapshot = {scope:string;cursor:string;page:AutonomousCursorPage<T>};
 const [snapshot,setSnapshot]=useState<Snapshot|null>(null);
 const [error,setError]=useState<unknown>(null),[loading,setLoading]=useState(false);
 const last=useRef<Snapshot|null>(null), followPage=useRef<()=>void>(()=>{});
 const page=enabled&&snapshot?.scope===scope&&snapshot.cursor===cursor?snapshot.page:null;

 useEffect(()=>{
  if(!enabled)return;
  let active=true,queued=false;
  let request:AbortController|undefined;
  const rowKey=kind==='events'?'event_id':kind==='attempts'?'attempt_id':'artifact_id';

  async function load(follow:boolean){
   if(!active||document.hidden)return;
   if(request){queued=queued||follow;return;}
   const old=last.current;
   const oldPage=follow&&old?.scope===scope&&old.cursor===cursor?old.page:null;
   // Append-only records use the tail cursor. Unfinished attempt rows may also
   // change in place, so reread that page and verify its existing identities.
   const refreshAttempts=kind==='attempts'&&!!oldPage?.items.some(row=>{
    const status=record(row).status;
    return status!=='SUCCEEDED'&&status!=='FAILED'&&status!=='UNKNOWN';
   });
   const tail=refreshAttempts?null:oldPage;
   if(tail&&(tail.has_more||tail.items.length===100))return;
   const current=new AbortController();
   request=current;
   if(!follow)setLoading(true);
   try{
    let value=await api.cursorPage<T>(sessionId,kind,{cursor:tail?.next_cursor??cursor,
     limit:tail?100-tail.items.length:100,attemptId,signal:current.signal});
    if(!active||current.signal.aborted)return;
    if(refreshAttempts&&oldPage?.items.some((row,index)=>{
     const previous=record(row),fresh=record(value.items[index]);
     return previous.attempt_id!==fresh.attempt_id||previous.ordinal!==fresh.ordinal;
    }))throw new Error('刷新实验页时返回的记录身份不匹配，已保留上次页面。');
    if(tail){
     if(!value.items.length&&value.next_cursor===tail.next_cursor&&value.has_more===tail.has_more){setError(null);return;}
     const seen=new Set(tail.items.map(row=>record(row)[rowKey]));
     if(value.items.some(row=>seen.has(record(row)[rowKey])))throw new Error('后续分页返回重复记录，未合并显示。');
     value={...value,limit:100,items:[...tail.items,...value.items]};
    }
    if(!oldPage||JSON.stringify(value)!==JSON.stringify(oldPage)){
     const next={scope,cursor,page:value};
     last.current=next;
     setSnapshot(next);
    }
    setError(null);
   }catch(e){
    if(active&&!current.signal.aborted)setError(e);
   }finally{
    if(request===current){
     request=undefined;
     if(active){
      if(!follow)setLoading(false);
      if(queued){queued=false;void load(true);}
     }
    }
   }
  }
  function onVisibility(){
   if(!document.hidden)return;
   queued=false;
   request?.abort();
   request=undefined;
   setLoading(false);
  }
  followPage.current=()=>{if(!request||last.current?.scope===scope)void load(true);};
  document.addEventListener('visibilitychange',onVisibility);
  void load(false);
  return()=>{
   active=false;
   request?.abort();
   followPage.current=()=>{};
   document.removeEventListener('visibilitychange',onVisibility);
  };
 },[api,sessionId,kind,attemptId,cursor,reload,scope,enabled]);

 useEffect(()=>{followPage.current();},[tick]);
 function next(){
  if(loading||!page)return;
  if(page.items.length)setPosition({scope,cursor:page.next_cursor,history:[...history,cursor]});
  setReload(value=>value+1);
 }
 function previous(){
  if(!loading&&history.length)setPosition({scope,cursor:history[history.length-1],history:history.slice(0,-1)});
 }
 return {page,error,loading,next,previous,reloadPage:()=>setReload(value=>value+1),hasPrevious:history.length>0};
}

export function AutonomousPageControls({state,noun}:{state:{page:AutonomousCursorPage<unknown>|null;error:unknown;loading:boolean;next:()=>void;previous:()=>void;reloadPage:()=>void;hasPrevious:boolean};noun:string}){
 return <>{state.error!=null&&<ErrorBox error={state.error}/>}<div className="pagination"><button disabled={state.loading||!state.hasPrevious} onClick={state.previous}>上一页{noun}</button><button disabled={state.loading||!state.page} onClick={state.next}>{state.page?.has_more?`加载更多${noun}`:`检查后续新${noun}`}</button><button disabled={state.loading} onClick={state.reloadPage}>刷新当前页{noun}</button></div><p>每页最多100条；仅显示当前页。{state.loading?'正在读取…':state.page?.has_more?'还有保存记录，可继续加载。':'已读到当前页末尾；运行中仍可检查后续新增记录。'}</p></>;
}
