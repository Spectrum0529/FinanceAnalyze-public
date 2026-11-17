import { useRef, useState } from 'react';

// Only the exact outstanding request is retained; server state is always read anew.
export function useAutonomousIntent<T>(owner:string, scope:string, valid:(value:unknown)=>value is T) {
  const key=`fa-autonomous-intent-v1:${JSON.stringify([owner,scope])}`;
  const [initial]=useState(()=>{
    try {
      const raw=sessionStorage.getItem(key);
      if(raw===null)return {body:null,error:null};
      if(raw.length>2000000)throw new Error();
      const saved=JSON.parse(raw);
      if(saved.schema!==1||!valid(saved.body))throw new Error();
      return {body:saved.body as T,error:null};
    }catch{return {body:null,error:new Error('无法核对本页上次请求记录，已阻止新的提交。请保留当前页面与记录，稍后核对任务；不会自动清除或重发。')};}
  });
  const current=useRef<T|null>(initial.body);
  return {
    current, error:initial.error,
    save(body:T){
      if(initial.error)throw initial.error;
      if(current.current)throw new Error('已有请求待确认，未创建新请求。');
      sessionStorage.setItem(key,JSON.stringify({schema:1,body}));
      current.current=body;
    },
    clear(){sessionStorage.removeItem(key);current.current=null;},
  };
}
