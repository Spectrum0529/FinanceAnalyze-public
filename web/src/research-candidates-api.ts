import { ApiError, request } from './api';
import { record } from './native-research-api';
export type ResearchCandidate={candidate_id:string;name:string;kind:'strategy'|'factor';source_sha256:string;session_id:string;attempt_id:string;logical_id:string;created_at:string};
export type SaveResearchCandidate={session_id:string;attempt_id:string;logical_id:string;name:string};
const mismatch=()=>new ApiError(0,'RESEARCH_CANDIDATE_RECEIPT_MISMATCH','无法确认方案已保存，请刷新方案列表。');
export function candidateMetadata(raw:unknown):ResearchCandidate{
 const row=record(raw);
 if(typeof row.candidate_id!=='string'||!/^candidate-[a-f0-9]{64}$/.test(row.candidate_id)||!['strategy','factor'].includes(String(row.kind))||typeof row.name!=='string'||!row.name||row.name.length>80||typeof row.source_sha256!=='string'||!/^[a-f0-9]{64}$/.test(row.source_sha256)||['session_id','attempt_id','logical_id','created_at'].some(k=>typeof row[k]!=='string'||!row[k]))throw mismatch();
 return row as unknown as ResearchCandidate;
}
export async function saveResearchCandidate(body:SaveResearchCandidate,expectedHash:string){
 const value=candidateMetadata(await request('/research/autonomous/candidates',{method:'POST',body:JSON.stringify(body)}));
 if(value.session_id!==body.session_id||value.attempt_id!==body.attempt_id||value.logical_id!==body.logical_id||value.name!==body.name.trim()||value.source_sha256!==expectedHash)throw mismatch();
 return value;
}
export async function readResearchCandidate(id:string,signal:AbortSignal){
 if(!/^candidate-[a-f0-9]{64}$/.test(id))throw mismatch();
 const value=record(await request(`/research/autonomous/candidates/${encodeURIComponent(id)}`,{signal}));
 const metadata=candidateMetadata(value),body=record(value.candidate);
 if(metadata.candidate_id!==id||value.host_execution_allowed!==false||body.schema!=='research-reusable-candidate-1'||body.kind!==metadata.kind||body.source_sha256!==metadata.source_sha256||body.executable_on_host!==false||typeof body.source!=='string'||new TextEncoder().encode(body.source).length>49152)throw mismatch();
 const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body.source));
 if(Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('')!==metadata.source_sha256)throw mismatch();
 return {metadata,source:body.source};
}
