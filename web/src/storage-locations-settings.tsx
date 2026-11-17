import { useEffect, useRef, useState } from 'react';
import { ApiError } from './api';
import { StorageLocationsView } from './storage-locations-view';
import type { StorageLocationRowView } from './storage-locations-view';
import { storageCategories,storageLocationsApi,storageProblem,absoluteWindowsDirectory } from './storage-locations-api';
import type { StorageCategory,StorageCategoryView,StorageCheck,StorageIntent } from './storage-locations-api';

type Pending={schema:'storage-location-intent-1';owner:string;category:StorageCategory;body:StorageIntent};
const slot=(owner:string,category:StorageCategory)=>`fa-storage-pending-v1:${JSON.stringify([owner,category])}`;
function readPending(owner:string,category:StorageCategory):Pending|null|'blocked'{try{const raw=sessionStorage.getItem(slot(owner,category));if(raw===null)return null;const value=JSON.parse(raw) as Pending;
 if(Object.keys(value).sort().join(',')!=='body,category,owner,schema'||Object.keys(value.body??{}).sort().join(',')!=='directory,expected_revision,idempotency_key'||value.schema!=='storage-location-intent-1'||value.owner!==owner||value.category!==category||!value.body||!Number.isSafeInteger(value.body.expected_revision)||value.body.expected_revision<0||typeof value.body.idempotency_key!=='string'||!/^[A-Za-z0-9_.:-]{1,128}$/.test(value.body.idempotency_key)||value.body.directory!==null&&!absoluteWindowsDirectory(value.body.directory))return 'blocked';return value;
 }catch{return 'blocked';}}
type Checked={input:string;revision:number;view:StorageCheck};
type RowState={draft?:string;checked?:Checked;message?:string;error?:string;pending:Pending|null|'blocked'};
export function StorageLocationsSettings(props:{ownerId:string;canWrite:boolean}){return <StorageLocationsController key={props.ownerId} {...props}/>;}
function StorageLocationsController({ownerId,canWrite}:{ownerId:string;canWrite:boolean}){
 const [values,setValues]=useState<StorageCategoryView[]>([]),[loading,setLoading]=useState(true),[error,setError]=useState<string>(),[busy,setBusy]=useState<{category:StorageCategory;kind:'check'|'save'|'reset'|'confirm'}|null>(null);
 const [rows,setRows]=useState<Record<StorageCategory,RowState>>(()=>Object.fromEntries(storageCategories.map(category=>[category,{pending:ownerId?readPending(ownerId,category):'blocked'}])) as Record<StorageCategory,RowState>);
 const gate=useRef(false),alive=useRef(false),loadSequence=useRef(0),data=useRef(values),state=useRef(rows);state.current=rows;data.current=values;
 function update(category:StorageCategory,patch:Partial<RowState>){if(alive.current){const next={...state.current,[category]:{...state.current[category],...patch}};state.current=next;setRows(next);}}
 async function load(resetCategory?:StorageCategory,signal?:AbortSignal){const sequence=++loadSequence.current;if(alive.current){setLoading(true);setError(undefined);}try{const received=await storageLocationsApi.get(signal);if(alive.current&&sequence===loadSequence.current){setValues(received);data.current=received;setRows(previous=>Object.fromEntries(storageCategories.map(category=>{const value=received.find(row=>row.category===category)!;return [category,{...previous[category],checked:undefined,draft:previous[category].draft===undefined||category===resetCategory?value.directory??'':previous[category].draft}];})) as Record<StorageCategory,RowState>);}}catch(e){if(alive.current&&!signal?.aborted&&sequence===loadSequence.current)setError(storageProblem(e));}finally{if(alive.current&&sequence===loadSequence.current)setLoading(false);}}
 useEffect(()=>{alive.current=true;const abort=new AbortController();void load(undefined,abort.signal);return()=>{alive.current=false;loadSequence.current++;abort.abort();};},[]);
 async function check(category:StorageCategory){if(gate.current||!canWrite||!ownerId||state.current[category].pending||readPending(ownerId,category))return;const value=data.current.find(row=>row.category===category),input=state.current[category].draft??'';if(!value)return;
  if(!absoluteWindowsDirectory(input)){update(category,{checked:undefined,error:'请粘贴文件夹的完整路径，例如 D:\\FinanceAnalyze\\MarketData。'});return;}
  gate.current=true;setBusy({category,kind:'check'});update(category,{checked:undefined,error:undefined,message:undefined});try{const result=await storageLocationsApi.check(category,input);if(result.valid)update(category,{checked:{input,revision:value.revision,view:result},message:'检查通过。点击“保存”后，新位置才会生效。'});else update(category,{error:storageProblem(new ApiError(422,result.code,result.message))});}catch(e){update(category,{error:storageProblem(e)});}finally{gate.current=false;if(alive.current)setBusy(null);}}
 async function save(category:StorageCategory,reset=false,replay=false){if(gate.current||!canWrite||!ownerId)return;const current=state.current[category],value=data.current.find(row=>row.category===category);if(!value)return;
  let pending=current.pending;
  if(replay){if(!pending||pending==='blocked')return;}else{if(pending||readPending(ownerId,category))return;const checked=current.checked;if(!reset&&(!checked||checked.input!==current.draft||checked.revision!==value.revision||!checked.view.valid||checked.view.directory===null))return;
   pending={schema:'storage-location-intent-1',owner:ownerId,category,body:{directory:reset?null:checked!.view.directory,expected_revision:value.revision,idempotency_key:crypto.randomUUID()}};
   try{sessionStorage.setItem(slot(ownerId,category),JSON.stringify(pending));}catch{update(category,{error:'浏览器无法记录此次操作，设置尚未提交。'});return;}}
  gate.current=true;setBusy({category,kind:replay?'confirm':reset?'reset':'save'});update(category,{pending,error:undefined,message:undefined});
  try{await storageLocationsApi.save(category,pending.body);sessionStorage.removeItem(slot(ownerId,category));update(category,{pending:null,checked:undefined,message:'设置已保存，正在刷新实际使用的目录。'});if(alive.current)await load(category);}
  catch(e){let definite=e instanceof ApiError&&[400,401,403,404,409,422,501].includes(e.status)&&!['INVALID_RESPONSE','IDEMPOTENCY_CONFLICT'].includes(e.code);if(definite){try{sessionStorage.removeItem(slot(ownerId,category));}catch{definite=false;}}
   update(category,{pending:definite?null:pending,checked:undefined,error:storageProblem(e),message:definite?undefined:'暂时无法确认保存结果。'});}
  finally{gate.current=false;if(alive.current)setBusy(null);}}
 const views:StorageLocationRowView[]=values.map(value=>{const row=rows[value.category];return {category:value.category,effectiveDirectory:value.effective_directory,defaultDirectory:value.default_directory,usesDefault:value.uses_default,draftDirectory:row.draft??'',canEdit:canWrite&&!!ownerId&&!busy&&!error,readyToSave:!!row.checked&&row.checked.input===row.draft&&row.checked.revision===value.revision,busy:busy?.category===value.category?busy.kind:null,resultUncertain:!!row.pending,publicMessage:row.pending==='blocked'?'上次操作结果无法确认。请先核对当前使用的目录。':row.message,errorMessage:row.error,edit:directory=>{if(!gate.current&&!row.pending)update(value.category,{draft:directory,checked:undefined,message:undefined,error:undefined});},check:()=>void check(value.category),save:()=>void save(value.category),reset:()=>void save(value.category,true),confirmOriginal:row.pending&&row.pending!=='blocked'?()=>void save(value.category,false,true):undefined};});
 return <StorageLocationsView rows={views} loading={loading} publicError={error} refresh={()=>{if(!gate.current)void load();}}/>;
}
