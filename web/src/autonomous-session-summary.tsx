import type { RecordData } from './api';
import { useResource, ErrorBox, Notice } from './components';
import { record } from './native-research-api';

const reasons:Record<string,string>={
 MODEL_REQUEST_UNCERTAIN:'上次实验结果尚不确定，需明确接受可能已计费后再继续。',
 SESSION_BLOCKED:'研究已被服务阻止；请核对保存记录与运行条件。',
 SESSION_PAUSED:'研究已暂停，检查点和已占用预算保留。',
 SESSION_PAUSE_REQUESTED:'服务正在处理暂停请求，请等到状态更新。',
 SESSION_CANCELLED:'研究已停止，已保存产物仍可阅读。',
 SESSION_CANCEL_REQUESTED:'服务正在处理停止请求，请等到状态更新。',
 SESSION_BUDGET_EXHAUSTED:'累计预算不足以开始下一轮，已占用额度不会退回。',
 SESSION_COMPLETED:'会话已完成；这不代表研究得到市场认证。',
 MODEL_SECRET_NOT_CONFIGURED:'所选模型缺少连接设置。请到“模型配置”保存密钥并选择连接。',
};
const budgets:Record<string,string>={attempts:'实验数',model_requests:'模型请求',tokens:'token',compute_seconds:'计算时间',artifact_bytes:'保存空间',prefixes:'历史时点计算',deadline:'截止时间'};
const count=(v:unknown)=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=0;
const scheduledPolicy='DURABLE_SCHEDULER_UNTIL_BUDGET_DEADLINE_OR_CONTROL';
const historyPolicy='HISTORY_ONLY_EXECUTOR_UNAVAILABLE';
export function AutonomousSessionSummary({sessionId,active}:{sessionId:string;active:boolean}){
 const state=useResource<RecordData>(`/research/autonomous/sessions/${encodeURIComponent(sessionId)}/progress?limit=1`,0,active?5000:false);
 if(state.error)return <><p>运行摘要暂未读取成功；下方仍按会话与保存记录显示，不推断任务已停止。</p><ErrorBox error={state.error}/></>;
 if(!state.data)return <p>正在读取服务保存的运行摘要…</p>;
 const value=state.data,secondsRemaining=value.deadline_seconds_remaining,counts=value.attempt_counts;
 const historyOnly=value.loop_policy===historyPolicy;
 if(value.schema!=='autonomous-session-progress-1'||value.session_id!==sessionId||
    (!historyOnly&&value.loop_policy!==scheduledPolicy)||
    (historyOnly&&(typeof value.execution_unavailable_code!=='string'||
      !/^[A-Za-z][A-Za-z0-9_]{0,95}$/.test(value.execution_unavailable_code)))||
    typeof secondsRemaining!=='number'||!Number.isFinite(secondsRemaining)||secondsRemaining<0||
    !counts||typeof counts!=='object'||Array.isArray(counts)||!Object.values(counts).every(count))
   return <Notice>返回摘要与所选任务不一致。暂时无法显示剩余时间、实验数和可继续的位置。</Notice>;
 const checkpoint=record(value.latest_checkpoint);
 const blockers=Array.isArray(value.next_round_budget_blockers)?value.next_round_budget_blockers.filter((v):v is string=>typeof v==='string'&&Object.hasOwn(budgets,v)):[];
 return (
  <section aria-label={historyOnly?'历史研究摘要':'长时间自主研究摘要'}>
   <h3>{historyOnly?'历史研究摘要与最近检查点':'研究循环与最近检查点'}</h3>
   {historyOnly ? <p>执行环境当前不可用。这里显示已保存的研究状态和检查点。</p> : <>
    <p>服务按累计预算、截止时间和暂停/停止请求决定是否开始下一轮；关闭此页面不会提交停止操作。</p>
    {secondsRemaining===0
      ? <p>会话截止时间已到。</p>
      : <p>距截止时间约{Math.ceil(secondsRemaining/60)}分钟。这是截止上限，不能保证持续运行到此刻。</p>}
   </>}
   <p>已完成实验{String(counts.SUCCEEDED??0)}次；失败{String(counts.FAILED??0)}次；结果未知{String(counts.UNKNOWN??0)}次；排队{String(counts.QUEUED??0)}次；进行中{String(counts.IN_FLIGHT??0)}次。这里是保存状态计数，不是完成百分比。</p>
   {typeof value.stop_reason==='string'&&<Notice>{historyOnly&&value.stop_reason==='SESSION_BUDGET_EXHAUSTED'
     ? '此会话预算不足以再开始一轮。'
     : reasons[value.stop_reason]??'服务记录了停止或阻止原因；请核对任务详情与保存的实验记录。'}</Notice>}
   {!historyOnly&&value.requires_unknown_charge_ack===true&&<p>继续或重新开始前，请在“中断后的处理”明确确认可能已发生的费用；不会自动勾选。</p>}
   {!historyOnly&&blockers.length>0&&<p>下一轮受限项目：{blockers.map(key=>budgets[key]).join('、')}。</p>}
   <p>{typeof checkpoint.logical_id==='string'&&checkpoint.logical_id
     ? historyOnly?'服务已有保存检查点。':'服务已有保存检查点。继续是否可用由当前会话操作决定；检查点存在不保证可恢复尚未确认的执行。'
     : '服务尚未返回保存检查点，不显示为已保存。'}</p>
   {!historyOnly&&<a href="#intelligent?research_tab=models">核对模型和连接密钥 →</a>}
  </section>
 );
}
