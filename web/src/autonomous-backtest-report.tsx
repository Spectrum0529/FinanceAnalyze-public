import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, request } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice } from './components';
import { record } from './native-research-api';
import { PortfolioAnalytics } from './portfolio-analytics';
import { ServerRows } from './research-visuals';
import { bindEngineChart, projectEngineChart } from './engine-chart';
import { amountWithCurrency, reportCurrency } from './report-currency';
import type { ReportCurrency } from './report-currency';

function routes(sessionId:string,attemptId:string){return `/research/autonomous/sessions/${encodeURIComponent(sessionId)}/attempts/${encodeURIComponent(attemptId)}/backtest`;}
function mismatch(){return new Error('读到的回测内容或页码与所选报告不一致。请重新打开报告。');}
async function readPage(base:string,section:string,sha:string,offset:number,signal:AbortSignal){
 const query=new URLSearchParams({section,limit:'500',offset:String(offset),expected_sha256:sha});
 const value=await request<RecordData>(`${base}/series?${query}`,{signal});
 if(value.source_view_sha256!==sha||value.section!==section||value.offset!==offset||value.limit!==500||!Number.isSafeInteger(value.total)||Number(value.total)<0||!Array.isArray(value.items)||value.items.length>500||offset+value.items.length>Number(value.total)||value.next_offset===null&&offset+value.items.length!==value.total||value.next_offset!==null&&(!Number.isSafeInteger(value.next_offset)||Number(value.next_offset)<=offset||Number(value.next_offset)!==offset+value.items.length))throw mismatch();
 return value;
}
async function readList(base:string,section:string,sha:string,maximum:number,signal:AbortSignal){
 const result:RecordData[]=[];let offset=0,total:number|null=null;
 for(let page=0;page<Math.ceil(maximum/500)+1;page++){
  const value=await readPage(base,section,sha,offset,signal);
  if(value.kind!=='list'||Number(value.total)>maximum||total!==null&&total!==value.total)throw mismatch();
  total=Number(value.total);result.push(...(value.items as RecordData[]));
  if(value.next_offset===null){if(result.length!==total)throw mismatch();return result;}
  offset=Number(value.next_offset);
 }
 throw mismatch();
}
async function rootSummary(base:string,view:RecordData,sha:string,signal:AbortSignal){
 let projected=record(view.summary);
 if(projected._paged===true){
  if(projected.section!==''||projected.kind!=='object')throw mismatch();
  projected={};let offset=0;
  for(let page=0;page<3;page++){
   const value=await readPage(base,'',sha,offset,signal);
   if(value.kind!=='object'||Number(value.total)>1024)throw mismatch();
   for(const raw of value.items as RecordData[]){const entry=record(raw);if(typeof entry.key!=='string'||Object.hasOwn(projected,entry.key)||['__proto__','prototype','constructor'].includes(entry.key))throw mismatch();projected[entry.key]=entry.value;}
   if(value.next_offset===null)return projected;offset=Number(value.next_offset);
  }
  throw mismatch();
 }
 return projected;
}

function reportDay(value: unknown) {
 if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))return undefined;
 const time=Date.parse(value);
 return Number.isFinite(time)&&new Date(time).toISOString().slice(0,10)===value?value:undefined;
}

function ScoringWindow({value}:{value:RecordData}){
 const coverage=record(value.coverage);
 const first=reportDay(coverage.first),last=reportDay(coverage.last);
 return <><p>{first&&last&&first<=last?`参与评分的日期：${first} 至 ${last}。`:'未提供参与评分的日期。'}</p>
  {value.evaluation_scope==='DEVELOPMENT_ONLY'&&<p>比较两轮收益前，请先核对各自用了哪些日期。补充历史数据后，开始评分的日期可能变化。</p>}
 </>;
}

export function AutonomousBacktestReport({sessionId,attemptId}:{sessionId:string;attemptId:string}){
 const [snapshot,setState]=useState<{key:string;body:RecordData|null;sha:string;error:unknown;benchmarkDeferred?:boolean}|null>(null),[reload,setReload]=useState(0);
 const base=routes(sessionId,attemptId);
 const key=base+'#'+reload;
 const state=snapshot?.key===key?snapshot:null;
 const loadBenchmarkCurve=useMemo(()=>{
  if(!state?.benchmarkDeferred)return undefined;
  const sha=state.sha;
  return (signal:AbortSignal)=>readList(base,'/benchmark/curve',sha,2000,signal);
 },[base,state]);
 const verified=useRef<string | null>(null);
 useEffect(()=>{
 if(verified.current===key)return;
 verified.current=null;
 const abort=new AbortController();void(async()=>{
  const view=await request<RecordData>(`${base}/summary`,{signal:abort.signal}),sha=view.source_view_sha256;
  if(view.view!=='summary'||view.result_id!==`autonomous:${attemptId}`||typeof sha!=='string'||!/^[a-f0-9]{64}$/.test(sha))throw mismatch();
  const body=await rootSummary(base,view,sha,abort.signal);
  if(body.family!=='autonomous-backtest-result-1'||body.schema!=='autonomous-backtest-visuals-1'||body.session_id!==sessionId||body.attempt_id!==attemptId||!['DEVELOPMENT_ONLY','SEALED_FINAL'].includes(String(body.evaluation_scope)))throw mismatch();
  const benchmark=record(body.benchmark);
  const benchmarkDeferred=record(body.request).benchmark_mode!=='dca'&&benchmark.comparable===true&&benchmark.coverage_complete===true;
  const read=(section:string,maximum=2000)=>readList(base,section,sha,maximum,abort.signal);
  const [equity,benchmarkCurve,daily,monthly,yearly,top5,worst,all]=await Promise.all([
   read('/equity_curve'),benchmarkDeferred?undefined:read('/benchmark/curve'),
   read('/calendar/daily'),read('/calendar/monthly'),read('/calendar/yearly'),
   read('/security_pnl/top5',8),read('/security_pnl/worst',8),read('/security_pnl/all',8),
  ]);
  const hydrated={...body,equity_curve:equity,benchmark:benchmarkCurve?{...benchmark,curve:benchmarkCurve}:benchmark,calendar:{...record(body.calendar),daily,monthly,yearly},security_pnl:{...record(body.security_pnl),top5,worst,all}};
  if(!abort.signal.aborted){verified.current=key;setState({key,body:hydrated,sha,error:null,benchmarkDeferred});}
 })().catch(error=>{if(!abort.signal.aborted){setState({key,body:null,sha:'',error});abort.abort();}});return()=>abort.abort();},[base,sessionId,attemptId,key]);
 const sha=state?.sha??'';
 const body=state?.body,currency=body ? reportCurrency(body) : undefined,symbols=currency ? Object.keys(record(body?.per_symbol_equity)) : [];const [symbol,setSymbol]=useState('');
 return <section aria-label="自主研究实际保存回测报告"><h3>保存的回测明细</h3>{state?.error?state.error instanceof ApiError&&state.error.code==='AUTONOMOUS_BACKTEST_VIEW_UNAVAILABLE'?<Notice>这份旧报告没有用于绘图和排名的回测明细。仍可查看原实验文件。</Notice>:<ErrorBox error={state.error}/>:!body?<p>正在读取所选结果的报告…</p>:<><Notice>{body.evaluation_scope==='SEALED_FINAL'?'这里用没参与改方案的数据检查已选定的方案。结果不会用于修改方案；正式研究仍需另行批准。':'这次回测用于探索和修改方案。所用数据不能再充当独立检查。'}曲线与成交来自回测记录，未用 AI 估算。</Notice><details><summary>查看日历、盈亏和交易次数怎么算</summary><p>日历只列有行情的交易日。“完整”表示这些天都能计算收益，不一定覆盖整个自然月或全年。</p><p>仍持有的估算赚亏等于账户净盈亏减去已卖出赚亏，包含这些持仓买入时的费用。交易次数按已结束和仍持有的交易记录计算，不能当成下单次数。</p></details><ScoringWindow value={body}/><PortfolioAnalytics key={key} value={body} loadBenchmarkCurve={loadBenchmarkCurve}/><PagedTradeSection key={`${sha}:trades`} base={base} section="/trades" sha={sha} title="已卖出的交易" currency={currency}/><PagedTradeSection key={`${sha}:positions`} base={base} section="/open_positions" sha={sha} title="仍持有的交易" currency={currency}/><label>查看单只证券的账户总额<select value={symbol} onChange={e=>setSymbol(e.target.value)}><option value="">请选择证券</option>{symbols.map(name=><option key={name}>{name}</option>)}</select></label>{symbol&&symbols.includes(symbol)&&<SymbolEquity key={`${sha}:${symbol}`} base={base} symbol={symbol} sha={sha} currency={currency}/>}</>}<button type="button" onClick={()=>{setSymbol('');setReload(n=>n+1);}}>重新读取此保存报告</button></section>;
}
function PagedTradeSection({base,section,sha,title,currency}:{base:string;section:string;sha:string;title:string;currency?:ReportCurrency}){
 const [open,setOpen]=useState(false),[offset,setOffset]=useState(0),[value,setValue]=useState<RecordData|null>(null),[error,setError]=useState<unknown>(null);
 useEffect(()=>{if(!open)return;const abort=new AbortController();setValue(null);setError(null);void readPage(base,section,sha,offset,abort.signal).then(v=>{if(v.kind!=='list'||Number(v.total)>20000)throw mismatch();if(!abort.signal.aborted)setValue(v);}).catch(e=>{if(!abort.signal.aborted)setError(e);});return()=>abort.abort();},[base,section,sha,offset,open]);
 return <section><button type="button" onClick={()=>setOpen(v=>!v)}>{open?'收起':'读取'}{title}</button>{open&&(error?<ErrorBox error={error}/>:!value?<p>正在读取本页保存明细…</p>:<><ServerRows value={value.items} columns={[{key:'ticker',label:'证券'},{key:'size',label:'数量'},{key:'entry_at',label:'入场日期'},{key:'exit_at',label:'出场日期'},{key:'entry_price',label:'入场价格',render:row=>amountWithCurrency(row.entry_price,currency)},{key:'exit_price',label:'出场价格',render:row=>amountWithCurrency(row.exit_price,currency)},{key:'commission',label:'相关费用',render:row=>amountWithCurrency(row.commission,currency)},{key:'pnl',label:'保存盈亏',render:row=>amountWithCurrency(row.pnl,currency)}]} empty="这部分没有保存成交或持仓。"/><p>本页从第 {offset+1} 条开始，共 {String(value.total)} 条。还持有的证券不会按已卖出计算。</p><button type="button" disabled={!offset} onClick={()=>setOffset(n=>Math.max(0,n-500))}>上一页</button><button type="button" disabled={value.next_offset===null} onClick={()=>setOffset(Number(value.next_offset))}>下一页</button></>)}</section>;
}
function SymbolEquity({base,symbol,sha,currency}:{base:string;symbol:string;sha:string;currency?:ReportCurrency}){
 const [rows,setRows]=useState<RecordData[]|null>(null),[error,setError]=useState<unknown>(null);const host=useRef<HTMLDivElement>(null);
 useEffect(()=>{const abort=new AbortController();const path='/per_symbol_equity/'+symbol.replaceAll('~','~0').replaceAll('/','~1');void readList(base,path,sha,2000,abort.signal).then(v=>{if(!abort.signal.aborted)setRows(v);}).catch(e=>{if(!abort.signal.aborted)setError(e);});return()=>abort.abort();},[base,symbol,sha]);
 useEffect(()=>{if(!rows?.length||!host.current)return;try{const projection=projectEngineChart([{id:symbol,label:symbol,points:rows.map(row=>({at:row.at,equity:row.equity}))}],currency ?? null);const binding=bindEngineChart(host.current,()=>setError(new Error('图表未绘制，保存字段仍保留。')));binding.update(projection.option);return()=>binding.dispose();}catch(e){setError(e);}},[rows,symbol,currency]);
 return <>{error?<ErrorBox error={error}/>:rows===null?<p>正在读取这只证券的账户总额…</p>:!rows.length?<p>报告没有保存这只证券的账户总额。</p>:null}<div ref={host} role="img" aria-label={`${symbol}实际保存权益`} style={{height:260}}/></>;
}
