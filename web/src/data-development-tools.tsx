import { MassiveDownloadProgress } from './massive-connection-status';
import { useRef, useState } from 'react';
import { ApiError, download, request } from './api';
import type { RecordData } from './api';
import { record, terminalResearchJob } from './native-research-api';
import { ErrorBox, Notice, useResource } from './components';
import { NativeDateInput } from './native-date-input';
import { AreaHelp } from './area-help';
import { useAutonomousIntent } from './autonomous-request-intent';

const dataMessages:Record<string,string>={DOWNLOAD_RESUME_STORAGE_UNBOUND:'无法确认上次下载的位置，不能接着下载。原任务记录仍保留。',NATIVE_RESUME_MISMATCH:'继续下载须使用上次选择的股票和日期。',NATIVE_RESUME_BUSY:'原任务仍在运行或已完成，请刷新状态。',NATIVE_DOWNLOAD_SDK_BUDGET:'下载预留额度已用完，继续下载也不会补充额度。',NATIVE_DOWNLOAD_ATTEMPT_BUDGET:'这次任务已达到尝试次数上限。',RESEARCH_INPUT_LIMIT:'所选范围或文件超出上限。请缩短日期或减少证券。',OPENBB_RUNTIME_UNBOUND:'本机 OpenBB 下载组件尚未就绪。',OPENBB_EXPORT_SOURCE_UNSUPPORTED:'OpenBB 格式只支持保留了官方格式的 Cboe 数据。其他来源请选择 Finance 格式。',OPENBB_FEATURE_DISABLED:'OpenBB格式未启用，可使用Finance格式。',OPENBB_DEPENDENCY_MISSING:'本机缺少 OpenBB 格式需要的组件。可以选择 Finance 格式。',OPENBB_VERSION_UNSUPPORTED:'本机 OpenBB 组件版本不支持这个格式。可以选择 Finance 格式。'};
const dataError=(e:unknown)=>e instanceof ApiError?new ApiError(e.status,e.code,dataMessages[e.code]??'本次数据操作未完成，请核对范围与保存任务记录。'):e;

export function useDataSubmit(owner:string,scope:string,path:string,handler:string,presentError=dataError){
 const journal=useAutonomousIntent<RecordData>(owner,`data:${scope}`, (raw):raw is RecordData=>{const row=record(raw);return typeof row.idempotency_key==='string'&&row.idempotency_key.length>0;});
 const [busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null),[job,setJob]=useState('');const gate=useRef(false);
 async function submit(body:RecordData){if(gate.current||journal.error)return;gate.current=true;setBusy(true);setError(null);try{
  if(!journal.current.current)journal.save({...body,idempotency_key:crypto.randomUUID()});
  const value=await request<RecordData>(path,{method:'POST',body:JSON.stringify(journal.current.current)});
  if(value.owner_id!==owner||value.handler!==handler||typeof value.job_id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value.job_id))throw new Error('无法确认返回的是本次任务。请先查看任务记录。');
  journal.clear();setJob(value.job_id);return value.job_id;
 }catch(e){setError(presentError(e));if(e instanceof ApiError&&[400,401,403,404,409,422].includes(e.status)&&e.code!=='IDEMPOTENCY_CONFLICT')journal.clear();}finally{gate.current=false;setBusy(false);}}
 return {busy,error:journal.error??error,job,pending:!!journal.current.current,body:journal.current.current,submit,resetJob:()=>setJob(''),disabled:!!journal.error};
}

export function ProviderCapabilities({provider}:{provider:string}){
 const state=useResource<RecordData>('/data/library/providers');
 const row=Array.isArray(state.data?.items)?state.data.items.map(record).find(v=>v.provider===provider):undefined;
 return <section aria-label="来源实际能力"><AreaHelp topic="source-capabilities" title="来源支持什么"/>{state.error?<ErrorBox error={state.error}/>:!row?<p>还不知道此来源支持哪些数据。保存连接后可尝试下载，并查看结果。</p>:<><p>{row.credentials_required===true?'需要在本机保存密钥。':'此来源不要求账号密钥。'}{typeof row.max_symbols==='number'&&`每次最多${row.max_symbols}个证券。`}{Array.isArray(row.assets)&&`支持范围：${row.assets.map(v=>v==='US_ETF'?'美国 ETF（交易所交易基金）':v==='US_EQUITY'?'美国股票':String(v)).join('、')}。`}</p>{Array.isArray(row.unsupported_symbols)&&row.unsupported_symbols.length>0&&<p>此来源明确不支持：{row.unsupported_symbols.join('、')}。</p>}{['akshare','yfinance'].includes(provider)&&<p>{provider==='akshare'?'模拟使用前复权价格，也就是为比较而调整过的历史价格。':'模拟使用按拆股调整过的价格，不计现金分红。'}能否连接，哪些证券和日期有数据，请看下载结果。</p>}{provider==='openbb_cboe'&&<><p>OpenBB/Cboe采集：{({DISABLED:'本机未启用',RUNTIME_UNBOUND:'尚未启用本机行情组件',DEPENDENCY_MISSING:'缺少所需的本机行情组件',VERSION_UNSUPPORTED:'本机行情组件版本不兼容',ENABLED_RUNTIME_UNVERIFIED:'已启用，暂未验证能否取到行情'} as Record<string,string>)[String(row.status)]??'状态未提供'}。</p><p>无需密钥。价格如何调整仍待核对，暂不能确认能否用于 Qlib 或组合回测。</p></>}<p>这些是可选范围。是否能连接来源、取到哪些日期，请看下载结果。</p></>}</section>;
}

export function NativeDownloadProgress({jobId,owner,canWrite,finished,changed}:{jobId:string;owner:string;canWrite:boolean;finished:boolean;changed:(id:string)=>void}){
 const [refresh,setRefresh]=useState(0);const state=useResource<RecordData>(`/data/library/downloads/${encodeURIComponent(jobId)}/progress`,refresh+Number(finished)),v=state.data;
 const write=useDataSubmit(owner,`resume:${jobId}`,'/data/library/download','data.library-download');
 const valid=v?.schema==='native-download-progress-1'&&v.job_id===jobId&&v.budget_basis==='CONSERVATIVE_SDK_CALL_RESERVATIONS_NOT_HTTP_OR_BILLING'&&['completed_units','total_units','attempts_used','attempts_maximum','sdk_units_reserved','sdk_units_maximum'].every(k=>Number.isSafeInteger(v[k])&&Number(v[k])>=0);
 const reasons:Record<string,string>={READY:'可以按原选择继续下载',STALE_ATTEMPT:'已有更新任务，请查看最新任务',COMPLETED:'下载已完成',NOT_TERMINAL:'原任务仍未结束',ATTEMPT_BUDGET_EXHAUSTED:'累计尝试次数已用完',SDK_BUDGET_EXHAUSTED:'下载预留额度已用完'};
 async function resume(){if(!canWrite||!valid||!v||!write.pending&&v.eligible_resume!==true)return;await write.submit({request:{...record(v.original_request),resume_id:v.resume_id}});}
 return <section aria-label="公开来源下载进度"><AreaHelp topic="native-resume" title="公开来源下载续传"/><button type="button" onClick={()=>setRefresh(n=>n+1)}>刷新下载进度</button>{write.error!=null&&<ErrorBox error={write.error}/>} {state.error?<p>暂时还没有分段进度。可先查看任务状态，不会自动重新下载。</p>:!valid?<p>还不能确认这次任务已分段保存的数据。</p>:<><p>已完整保存的证券{String(v!.completed_units)}/{String(v!.total_units)}；累计尝试{String(v!.attempts_used)}/{String(v!.attempts_maximum)}；预留调用次数{String(v!.sdk_units_reserved)}/{String(v!.sdk_units_maximum)}。</p><p>预留次数是下载额度估算，实际请求次数和费用以供应商为准。能否继续下载：{reasons[String(v!.resume_reason)]??'服务尚未确认'}。</p><button type="button" disabled={!canWrite||write.busy||write.disabled||!write.pending&&v!.eligible_resume!==true} onClick={()=>void resume()}>{write.pending?'核对上次继续下载':'继续下载未完成的证券'}</button><p>继续下载会新建任务并保留已下载数据。日期、名称、证券和剩余额度不变。全部完成后才会保存为一份完整数据。</p></>}{write.job&&<><button type="button" onClick={()=>changed(write.job)}>查看继续下载的进度 →</button><a href={`#data?download_job=${encodeURIComponent(write.job)}`}>查看下载任务 →</a></>}</section>;
}

export function DataProductJob({id,owner,handler,completed}:{id:string;owner:string;handler:string;completed:()=>void}){
 const state=useResource<RecordData>(`/jobs/${encodeURIComponent(id)}`,0,1500,terminalResearchJob),value=state.data;
 return state.error?<ErrorBox error={state.error}/>:value?.job_id!==id||value.owner_id!==owner||value.handler!==handler?<p>正在核对本次任务…</p>:<><p>任务：{({QUEUED:'排队中',RUNNING:'执行中',SUCCEEDED:'已完成',FAILED:'失败',CANCELLED:'已停止'} as Record<string,string>)[String(value.status)]??'状态待确认'}。</p>{value.status==='SUCCEEDED'?<button type="button" onClick={completed}>打开任务结果 →</button>:['FAILED','CANCELLED'].includes(String(value.status))?<Notice>{dataMessages[String(value.error_code)]??'未保存数据。仍可查看任务记录。'}</Notice>:<p>任务还在处理，请稍后查看结果。</p>}</>;
}

export function DatedRosterImport({owner,canWrite,changed}:{owner:string;canWrite:boolean;changed:()=>void}){
 const [label,setLabel]=useState(''),[members,setMembers]=useState(''),[date,setDate]=useState(''),[source,setSource]=useState('');
 const write=useDataSubmit(owner,'roster','/data/library/universes/import','data.universe-import');
 const locked=!canWrite||write.busy||write.pending||write.disabled||!!write.job;
 return <details><summary>导入自己的股票名单</summary><AreaHelp topic="dated-roster" title="导入带日期名单"/><p>填写名单、日期和来源。系统只记录你填的内容，不访问链接；请自己核对回测当天能否取得这份名单。</p><label>名单名称<input maxLength={120} value={label} disabled={locked} onChange={e=>setLabel(e.target.value)}/></label><label>名单日期<NativeDateInput value={date} disabled={locked} onChange={e=>setDate(e.target.value)}/></label><label>股票代码<textarea value={members} disabled={locked} onChange={e=>setMembers(e.target.value)} placeholder="逗号、空格或换行分隔"/></label><label>来源说明<input maxLength={500} value={source} disabled={locked} onChange={e=>setSource(e.target.value)}/></label><button type="button" disabled={!canWrite||write.busy||write.disabled||!!write.job||!write.pending&&(!label.trim()||!members.trim()||!date||!source.trim())} onClick={()=>void write.submit({roster:{label:label.trim(),members:[...new Set(members.toUpperCase().split(/[\s,，]+/).filter(Boolean))].sort(),as_of:date,source:source.trim()}})}>{write.pending?'核对上次导入':'保存股票名单'}</button>{write.error!=null&&<ErrorBox error={write.error}/>} {write.job&&<DataProductJob id={write.job} owner={owner} handler="data.universe-import" completed={changed}/>}</details>;
}

export function BoundedReferenceSelection({owner,canWrite,connection,changed}:{owner:string;canWrite:boolean;connection:RecordData|null;changed:()=>void}){
 const [date,setDate]=useState(''),[exchange,setExchange]=useState(''),[kind,setKind]=useState('CS'),[maximum,setMaximum]=useState('100'),[active,setActive]=useState(true);
 const write=useDataSubmit(owner,`reference:${connection?.connection_id??'none'}`,'/data/library/download','data.library-download');
 const locked=!canWrite||write.busy||write.pending||write.disabled||!!write.job;
 return <details><summary>按交易所和证券类型找股票</summary><AreaHelp topic="bounded-roster" title="有限范围名单"/><p>需要 Massive 名单权限。按代码顺序最多取指定数量，不代表全市场、指数成分或每天的成分。只保存名单，不下载价格。</p><label>名单日期<NativeDateInput value={date} disabled={locked} onChange={e=>setDate(e.target.value)}/></label><label>交易所<select value={exchange} disabled={locked} onChange={e=>setExchange(e.target.value)}><option value="">美国股票市场</option><option value="XNAS">纳斯达克</option><option value="XNYS">纽约证券交易所</option><option value="XASE">NYSE American</option></select></label><label>证券类型<select value={kind} disabled={locked} onChange={e=>setKind(e.target.value)}><option value="CS">普通股</option><option value="ETF">ETF（交易所交易基金）</option></select></label><label>最多保存多少个代码<input type="number" min={1} max={1000} value={maximum} disabled={locked} onChange={e=>setMaximum(e.target.value)}/></label><label><input type="checkbox" checked={active} disabled={locked} onChange={e=>setActive(e.target.checked)}/>只要所选日期标为活跃的证券</label><button type="button" disabled={!canWrite||connection?.provider!=='massive'||write.busy||write.disabled||!!write.job||!write.pending&&(!date||!Number.isSafeInteger(Number(maximum))||Number(maximum)<1||Number(maximum)>1000)} onClick={()=>void write.submit({request:{connection_id:connection?.connection_id??null,membership:{exchange:exchange||null,security_type:kind,as_of:date,maximum:Number(maximum),active}}})}>{write.pending?'核对上次筛选':'获取并保存筛选结果'}</button>{connection?.provider!=='massive'&&<p>先选 Massive 连接。也可以导入自己的名单。</p>}{write.error!=null&&<ErrorBox error={write.error}/>} {write.job&&<DataProductJob id={write.job} owner={owner} handler="data.library-download" completed={changed}/>}</details>;
}

export function DatasetResearchExport({dataset,owner,canWrite}:{dataset:RecordData;owner:string;canWrite:boolean}){
 const [start,setStart]=useState(''),[end,setEnd]=useState(''),[format,setFormat]=useState('finance-research-input-1'),[error,setError]=useState<unknown>(null);
 const capabilities=useResource<RecordData>('/data/library/providers');const formats=record(capabilities.data?.research_exports);
 const options=Array.isArray(formats.formats)?formats.formats.map(record):[];
 const selected=options.find(v=>v.format===format),sourceProvider=record(dataset.source).provider,sourceMatches=!selected?.source_provider_required||selected.source_provider_required===sourceProvider,write=useDataSubmit(owner,`export:${dataset.dataset_id}`,`/data/library/datasets/${encodeURIComponent(String(dataset.dataset_id))}/research-export`,'data.research-export');
 const locked=!canWrite||write.busy||write.pending||!!write.job||write.disabled;
 const states:Record<string,string>={DISABLED:'未启用',DEPENDENCY_MISSING:'缺少所需的本机行情组件',VERSION_UNSUPPORTED:'依赖版本不支持',ENABLED_NOT_VALIDATED:'已提供格式转换，尚未运行验证',ENABLED_FOR_ACCEPTED_CBOE_SNAPSHOTS:'已启用，仅适用于已保存官方模型的Cboe数据',REGISTERED_NOT_RUNTIME_VALIDATED:'已注册，尚未运行验证',SERVICE_UNREGISTERED:'导出服务尚未注册'};
 return <details><summary>导出选定日期的数据</summary><AreaHelp topic="research-export" title="研究数据导出"/><p>日期可留空。每次最多导出 20 万行、64 MiB；缺少的价格保留为空。导出仅使用已保存数据，不下载新行情，也不核实回测当天能否取得数据。</p>{capabilities.error!=null&&<ErrorBox error={capabilities.error}/>}<label>导出格式<select value={format} disabled={locked} onChange={e=>setFormat(e.target.value)}><option value="finance-research-input-1">Finance 格式（保留原始小数）</option><option value="finance-openbb-equity-input-1" disabled={!options.some(v=>v.format==='finance-openbb-equity-input-1'&&v.available===true&&(!v.source_provider_required||v.source_provider_required===sourceProvider))}>OpenBB 格式（仅支持符合要求的 Cboe 数据）</option></select></label>{options.map(row=><p key={String(row.format)}>{row.format==='finance-openbb-equity-input-1'?'OpenBB格式':'Finance格式'}：{states[String(row.status)]??'状态未提供'}。</p>)}{!options.length&&<p>暂未读取到可用的导出格式。</p>}<label>导出开始日期<NativeDateInput value={start} disabled={locked} onChange={e=>setStart(e.target.value)}/></label><label>导出结束日期<NativeDateInput value={end} disabled={locked} onChange={e=>setEnd(e.target.value)}/></label><button type="button" disabled={!canWrite||write.busy||write.disabled||!!write.job||!write.pending&&(selected?.available!==true||!sourceMatches||!!start&&!!end&&start>end)} onClick={()=>void write.submit({request:{reference:{dataset_id:dataset.dataset_id,version:dataset.version,content_hash:dataset.content_hash},start:start||null,end:end||null,format}})}>{write.pending?'核对上次导出':'开始导出'}</button>{(write.error??error)!=null&&<ErrorBox error={write.error??error}/>} {write.job&&<DataProductJob id={write.job} owner={owner} handler="data.research-export" completed={()=>{void download(`/data/library/exports/${encodeURIComponent(write.job)}/content`,'研究数据.json').catch(setError);}}/>}</details>;
}


export function SavedDownloadProgress({jobId,owner,canWrite}:{jobId:string;owner:string;canWrite:boolean}){
 const [refresh,setRefresh]=useState(0);
 const job=useResource<RecordData>(`/jobs/${encodeURIComponent(jobId)}`,refresh,1500,terminalResearchJob),state=useResource<RecordData>(`/data/library/downloads/${encodeURIComponent(jobId)}/progress`,refresh);
 const [next,setNext]=useState('');
 if(next)return <SavedDownloadProgress key={next} jobId={next} owner={owner} canWrite={canWrite}/>;
 if(job.error)return <ErrorBox error={job.error}/>;
 if(job.data?.job_id!==jobId||job.data.owner_id!==owner||job.data.handler!=='data.library-download')return <p>还不能确认这次下载任务。未发起新的下载。</p>;
 const backtest = job.data.status === 'SUCCEEDED' && canWrite && <SavedDataBacktestLink reference={record(job.data.result)} jobId={jobId} />;
 if(state.error)return <>{backtest}<p>任务可能刚排队，还没有分段进度。</p><ErrorBox error={dataError(state.error)} retry={() => setRefresh(value=>value+1)}/></>;
 if(state.data?.job_id!==jobId)return <>{backtest}<p>正在读取所选下载任务进度…</p></>;
 return <>{backtest}{state.data.schema==='native-download-progress-1'?<NativeDownloadProgress jobId={jobId} owner={owner} canWrite={canWrite} finished={terminalResearchJob(job.data)} changed={setNext}/>:<MassiveDownloadProgress jobId={jobId} owner={owner} canWrite={canWrite} changed={setNext}/>}</>;
}
import { SavedDataBacktestLink } from './saved-data-backtest-link';
