import { useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ApiError, post, request } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, Panel, useResource } from './components';
import { useDataSubmit } from './data-development-tools';
import { NativeDateInput } from './native-date-input';
import { record, terminalResearchJob } from './native-research-api';

const target = 'postgresql://wrds-pgdata.wharton.upenn.edu:9737/wrds';
const profile = 'crsp_ciz_us_daily_trade_v1';
const tickerPattern = /^[A-Z][A-Z0-9.-]{0,15}$/;
const datePattern = /^\d{4}-\d{2}-\d{2}$/;
const opaqueReference = /^credential:FA_[a-f0-9]{64}$/;
const lookupHandler = 'data.wrds-security-lookup';
const historyHandler = 'data.wrds-history';

export const wrdsMessages:Record<string,string> = {
  WRDS_INPUT_INVALID: '请检查代码、日期和所选数量是否符合限制。',
  WRDS_CREDENTIAL_REQUIRED: '请在这台电脑保存这个连接的 WRDS 用户名和密码。',
  WRDS_UNAVAILABLE: '本机的 WRDS 下载功能暂不可用。',
  WRDS_AUTHENTICATION_FAILED: 'WRDS 登录失败。请检查用户名和密码；如弹出 Duo 验证，请完成验证。',
  WRDS_ACCESS_DENIED: '这个 WRDS 账号无权读取所选数据。请查看订阅权限。',
  WRDS_SCHEMA_UNSUPPORTED: '这个账号可访问的 CRSP 数据不含本软件支持的 CIZ 日线表。',
  WRDS_CONNECTION_FAILED: '无法验证 WRDS 连接。请检查网络和服务状态后再试。',
  WRDS_TIMEOUT: '本次 WRDS 任务已超时，可缩小日期或证券范围后重试。',
  WRDS_DATA_LIMIT: '本次 WRDS 数据超过任务上限，请缩小范围。',
  WRDS_SECURITY_BINDING: '所选日期内，代码与 CRSP 编号并非一直对应。请核对代码曾用日期，或缩短日期范围。',
  WRDS_OHLC_UNAVAILABLE: '未取得符合要求的原始开盘价、最高价、最低价、收盘价或成交量。未保存这次下载。',
  WRDS_RESPONSE_INVALID: '无法确认 WRDS 返回的是本次所选数据，暂未采用。',
  WRDS_HISTORY_EMPTY: '所选证券和日期未返回可用成交日线。',
  WRDS_CAPTURE_REQUIRED: '请先查找证券，核对代码和 CRSP 编号后再选。',
  CREDENTIAL_GENERATION_CHANGED: '本机保存的 WRDS 登录信息已更改。旧任务仍保留，请重新提交。',
  CREDENTIAL_NOT_CONFIGURED: '请先在本机保存这个连接的 WRDS 登录信息。',
  CREDENTIAL_TARGET_MISMATCH: '已保存的登录信息不适用于这个 WRDS 连接。',
  CREDENTIAL_STORE_UNAVAILABLE: '暂不能读取或保存本机的登录信息。',
  WRDS_CREDENTIAL_LOCAL_ONLY: '请在服务所在电脑直接打开工作台管理 WRDS 凭据。',
  REAUTH_REQUIRED: '请先在账户安全验证身份，再返回此处重新填写并保存 WRDS 凭据。',
};

export function wrdsError(error: unknown) {
  const code = error instanceof ApiError && Object.hasOwn(wrdsMessages,error.code) ? error.code : 'WRDS_RESPONSE_INVALID';
  return new ApiError(error instanceof ApiError ? error.status : 0,code,wrdsMessages[code]);
}

type Security = {permno:number;ticker:string;validFrom:string;validTo:string;exchange:string;asset:string};
function matches(data: RecordData, expectedDate: string) {
  if (data.profile!==profile || data.as_of!==expectedDate || typeof data.truncated!=='boolean' ||
      !Array.isArray(data.items) || data.items.length>20) return null;
  const securities:Security[] = [];
  for (const raw of data.items) {
    const row=record(raw);
    if (typeof row.permno!=='number' || !Number.isSafeInteger(row.permno) || row.permno<1 || row.permno>999999999 ||
        typeof row.ticker!=='string' || !tickerPattern.test(row.ticker) ||
        typeof row.valid_from!=='string' || !datePattern.test(row.valid_from) || row.valid_from>expectedDate ||
        typeof row.valid_to!=='string' || !datePattern.test(row.valid_to) || row.valid_to<expectedDate ||
        !['US_EQUITY','US_ETF'].includes(String(row.asset))) return null;
    securities.push({permno:row.permno,ticker:row.ticker,validFrom:row.valid_from,validTo:row.valid_to,
      exchange:String(row.primary_exchange??'未提供'),asset:String(row.asset)});
  }
  return {securities,truncated:data.truncated};
}

export function WrdsCredential({canWrite,onReference}:{canWrite:boolean;onReference:(reference:string)=>void}){
  const [busy,setBusy]=useState(false),[unknown,setUnknown]=useState(false);
  const [configured,setConfigured]=useState<boolean|null>(null),[error,setError]=useState<unknown>(null);
  const username=useRef<HTMLInputElement>(null),password=useRef<HTMLInputElement>(null),gate=useRef(false);
  async function readOrSave(method:'GET'|'PUT'){
    if(gate.current||method==='PUT'&&(!canWrite||unknown))return;
    const user=username.current?.value??'', secret=password.current?.value??'';
    if(method==='PUT'&&(!/^[A-Za-z0-9_.@-]{1,128}$/.test(user)||!secret)){
      setError(new Error('请填写有效的 WRDS 用户名和密码。'));return;
    }
    gate.current=true;setBusy(true);setError(null);onReference('');
    if(password.current)password.current.value='';
    if(username.current)username.current.value='';
    try{
      const result=await request<RecordData>('/operations/provider-credentials/wrds',{method,
        ...(method==='PUT'?{body:JSON.stringify({provider:'wrds',endpoint_id:null,value:{username:user,password:secret}})}:{})});
      if(result.slot!=='wrds'||typeof result.configured!=='boolean'||method==='PUT'&&!result.configured)
        throw new Error('还不能确认保存的是这次填写的登录信息');
      if(result.configured){
        if(result.provider!=='wrds'||result.endpoint_id!==null||result.base_url!==target||result.binding_verified!==true||
            typeof result.secret_ref!=='string'||!opaqueReference.test(result.secret_ref)||
            typeof result.credential_generation!=='string'||!/^[a-f0-9]{32}$/.test(result.credential_generation))
          throw new Error('还不能确认保存的是这次填写的登录信息');
        onReference(result.secret_ref);
      }
      setConfigured(result.configured);setUnknown(false);
    }catch(e){
      setConfigured(null);setError(wrdsError(e));if(method==='PUT'&&!(e instanceof ApiError&&e.code==='REAUTH_REQUIRED'))setUnknown(true);
    }finally{gate.current=false;setBusy(false);}
  }
  return <details><summary>设置 WRDS 登录</summary>
    <p>登录信息只保存在本机。保存时不会登录 WRDS；查询或下载时可能需要完成 Duo 验证。</p>
    <label>WRDS 用户名<input ref={username} autoComplete="off" maxLength={128} disabled={busy||unknown||!canWrite}/></label>
    <label>WRDS 密码<input ref={password} type="password" autoComplete="off" maxLength={1024} disabled={busy||unknown||!canWrite}/></label>
    <div className="form-actions">
      <button disabled={busy||unknown||!canWrite} onClick={()=>void readOrSave('PUT')}>在本机保存登录信息</button>
      <button disabled={busy} onClick={()=>void readOrSave('GET')}>使用已保存的登录信息</button>
    </div>
    {configured!==null&&<p role="status">{configured?'已选用本机保存的登录信息；还未确认账号权限。':'本机还没有保存 WRDS 登录信息。'}</p>}
    {unknown&&<Notice>还不能确认是否保存成功。请先点“使用已保存的登录信息”查看。</Notice>}
    {error!=null&&<ErrorBox error={error}/>}
    {error instanceof ApiError&&error.code==='REAUTH_REQUIRED'&&<a href="#security">打开账户安全</a>}
  </details>;
}

export function WrdsJob({id,owner,handler,canWrite,children,retry}:{id:string;owner:string;handler:string;canWrite:boolean;children:(data:RecordData)=>ReactNode;retry?:()=>void}){
  const [error,setError]=useState<unknown>(null),[busy,setBusy]=useState(false),[refresh,setRefresh]=useState(0);
  const state=useResource<RecordData>(`/jobs/${encodeURIComponent(id)}`,refresh,1500,terminalResearchJob);
  const job=state.data;
  if(state.error!=null)return <ErrorBox error={state.error} retry={()=>setRefresh(n=>n+1)}/>;
  if(!job)return <p>正在读取当前任务。</p>;
  if(job.job_id!==id||job.owner_id!==owner||job.handler!==handler)return <ErrorBox error={wrdsError(null)}/>;
  if(job.status==='SUCCEEDED')return children(record(job.result));
  if(job.status==='CANCELLED'||job.status==='FAILED'){
    const failure=record(job.error);
    return <>{job.status==='CANCELLED'?<p role="status">任务已取消。</p>:
      <ErrorBox error={wrdsError(new ApiError(0,String(job.error_code??failure.code),''))}/>}
      {canWrite&&retry&&<button onClick={retry}>调整范围并重新提交</button>}</>;
  }
  return <><p role="status">{job.status==='RUNNING'?'正在读取所选 WRDS 数据。':'等待服务执行。'}</p>
    <button disabled={!canWrite||busy||job.cancel_requested===true} onClick={async()=>{
      setBusy(true);setError(null);
      try{await post(`/jobs/${encodeURIComponent(id)}/cancel`);setRefresh(n=>n+1);}
      catch(e){setError(wrdsError(e));}finally{setBusy(false);}
    }}>请求停止任务</button>{error!=null&&<ErrorBox error={error}/>}</>;
}

export function WrdsLibrary({owner,connectionId,canWrite,enabled,saved}:{owner:string;connectionId:string;canWrite:boolean;enabled:boolean;saved:()=>void}){
  const [query,setQuery]=useState(''),[asOf,setAsOf]=useState(''),[lookupDate,setLookupDate]=useState(''),[lookupQuery,setLookupQuery]=useState('');
  const [selected,setSelected]=useState<Security[]>([]),[start,setStart]=useState(''),[end,setEnd]=useState(''),[name,setName]=useState('');
  const scope=`wrds:${connectionId}`;
  const lookup=useDataSubmit(owner,`${scope}:query`,'/data/library/wrds/securities/lookup',lookupHandler,wrdsError);
  const history=useDataSubmit(owner,`${scope}:history`,'/data/library/download',historyHandler,wrdsError);
  const search=query.trim().toUpperCase();
  const today=new Date().toISOString().slice(0,10);
  const span=(Date.parse(end)-Date.parse(start))/86400000+1;
  const bound=selected.length>0&&span>=1&&span<=3660&&end<=today&&selected.every(row=>row.validFrom<=start&&end<=row.validTo);
  const lookupLocked=lookup.busy||lookup.pending,historyLocked=history.busy||history.pending||!!history.job;
  async function submitLookup(){
    if(!canWrite||!enabled||historyLocked)return;
    const pending=record(lookup.body?.request);
    setLookupDate(lookup.pending?String(pending.as_of):asOf);
    setLookupQuery(lookup.pending?String(pending.query):search);
    await lookup.submit({request:{connection_id:connectionId,query:search,as_of:asOf,limit:20}});
  }
  return <>
    <Panel title="2. 找证券：WRDS">
      <p>支持 CRSP CIZ 美股和 ETF 的原始日价格。实际能查到的数据取决于账号订阅和权限。行情与报告仅限本机查看，不可通过外部连接读取或导出。</p>
      {!enabled&&<Notice>当前不能确认本机可查询或下载 WRDS 数据。</Notice>}
      <label>股票、基金代码或 CRSP 编号（PERMNO）<input value={query} maxLength={16} disabled={lookupLocked||historyLocked} onChange={e=>setQuery(e.target.value)}/></label>
      <label>查询哪一天的代码<NativeDateInput value={asOf} disabled={lookupLocked||historyLocked} onChange={e=>setAsOf(e.target.value)}/></label>
      <button disabled={!canWrite||!enabled||lookup.busy||lookup.disabled||historyLocked||
        !lookup.pending&&(!asOf||asOf>today||!tickerPattern.test(search)&&!/^[1-9]\d{0,8}$/.test(search))}
        onClick={()=>void submitLookup()}>{lookup.pending?'查看上次查询是否完成':'查找当天的证券'}</button>
      {lookup.error!=null&&<ErrorBox error={lookup.error}/>}
      {lookup.job&&<WrdsJob key={lookup.job} id={lookup.job} owner={owner} handler={lookupHandler} canWrite={canWrite}>{result=>{
        const found=matches(result,lookupDate);
        if(!found||found.securities.some(row=>/^\d+$/.test(lookupQuery)?row.permno!==Number(lookupQuery):row.ticker!==lookupQuery))
          return <ErrorBox error={wrdsError(null)}/>;
        return <><p>查询日期 {lookupDate}。{found.truncated?'还有未显示的结果。可以按 CRSP 编号精确查找。':'请核对代码、CRSP 编号和有效期，再选择。'}</p>
          {!found.securities.length&&<p>这一天没有查到匹配的证券。可试试其他日期或代码。</p>}
          <ul>{found.securities.map(row=>{
            const exists=selected.some(item=>item.permno===row.permno&&item.ticker===row.ticker);
            const conflict=selected.some(item=>item.permno===row.permno||item.ticker===row.ticker);
            return <li key={`${row.permno}:${row.ticker}:${row.validFrom}`}>
              {row.ticker} · PERMNO {row.permno} · {row.asset==='US_ETF'?'美国 ETF':'美国普通股'} · 交易所 {row.exchange} · {row.validFrom} 至 {row.validTo}
              <button disabled={!canWrite||historyLocked||conflict||selected.length>=8} onClick={()=>setSelected(items=>[...items,row])}>{exists?'已选择':`选择 ${row.ticker} · ${row.permno}`}</button>
            </li>;
          })}</ul></>;
      }}</WrdsJob>}
      {selected.length>0&&<><h3>已选证券（最多 8 个）</h3><ul>{selected.map(row=><li key={row.permno}>{row.ticker} · {row.permno} <button disabled={historyLocked} onClick={()=>setSelected(items=>items.filter(item=>item.permno!==row.permno))}>移除 {row.ticker}</button></li>)}</ul></>}
    </Panel>
    <Panel title="3. 下载并保存 WRDS 行情">
      <label>开始日期<NativeDateInput value={start} disabled={historyLocked} onChange={e=>setStart(e.target.value)}/></label>
      <label>结束日期<NativeDateInput value={end} disabled={historyLocked} onChange={e=>setEnd(e.target.value)}/></label>
      <label>数据集名称<input value={name} maxLength={160} disabled={historyLocked} onChange={e=>setName(e.target.value)}/></label>
      <p>日期必须在每只证券代码的有效期内，最多 3,660 个日历日。实际有数据的日期请看下载结果。</p>
      <button className="primary-action" disabled={!canWrite||!enabled||history.busy||history.disabled||!!history.job||!history.pending&&!bound}
        onClick={()=>void history.submit({request:{connection_id:connectionId,name:name.trim()||'WRDS CRSP',
          securities:selected.map(({permno,ticker})=>({permno,ticker})),start,end}})}>
        {history.pending?'核对上次 WRDS 下载':'下载并保存 WRDS 行情'}
      </button>
      {history.error!=null&&<ErrorBox error={history.error}/>}
      {history.job&&<><WrdsSavedJob key={history.job} id={history.job} owner={owner} canWrite={canWrite} saved={saved} retry={history.resetJob}/>
        <a href={`#data?wrds_download_job=${encodeURIComponent(history.job)}`}>查看上次 WRDS 下载进度 →</a></>}
    </Panel>
  </>;
}

export function WrdsSavedJob({id,owner,canWrite,saved,retry}:{id:string;owner:string;canWrite:boolean;saved:()=>void;retry?:()=>void}){
  return <WrdsJob id={id} owner={owner} handler={historyHandler} canWrite={canWrite} retry={retry}>{result=>
    typeof result.dataset_id==='string'&&Number.isSafeInteger(result.version)&&Number(result.version)>=1&&
    typeof result.content_hash==='string'&&/^[a-f0-9]{64}$/.test(result.content_hash)&&result.pit_status==='UNVERIFIED'
      ? <><p role="status">已保存数据集 {String(result.version)}，还需核对回测当天能否取得这些数据。</p><button onClick={saved}>查看我的数据集</button></>
      : <ErrorBox error={wrdsError(null)}/>
  }</WrdsJob>;
}
