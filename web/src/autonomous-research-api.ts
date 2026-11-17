import { ApiError, request } from './api';

// FA013 Core r04 plus pagination_contract.r02; runtime capabilities remain authoritative.
export type AutonomousTarget = 'strategy' | 'factor' | 'joint';
export type AutonomousAction = 'run' | 'pause' | 'resume' | 'cancel' | 'restart' | 'seal_final';
export type AutonomousStatus = 'DRAFT' | 'READY' | 'RUNNING' | 'PAUSE_REQUESTED' | 'PAUSED' | 'CANCEL_REQUESTED' | 'CANCELLED' | 'BLOCKED' | 'BUDGET_EXHAUSTED' | 'SEALED' | 'COMPLETED';
export type AutonomousReference = { resource_id: string; version: string; content_hash: string };
export type AutonomousMetric = { name: string; direction: 'maximize' | 'minimize'; weight: number };
export type AutonomousBudget = { attempts: number; model_requests: number; tokens: number; cost_microunits: number; compute_seconds: number; artifact_bytes: number; prefixes:number };
export type AutonomousReservation = AutonomousBudget & { attempts: 1 };
export type AutonomousSpec = {
  start?:string|null;end?:string|null;name: string; target: AutonomousTarget; objective: string; saved_candidate_ids?:string[];team_role_map?:Partial<Record<ResearchRole,string>>; scoring: AutonomousMetric[];
  development: AutonomousReference[]; final_test: AutonomousReference | null; models: Partial<Record<ResearchRole, AutonomousReference>>; team_snapshot?: AutonomousReference | null;
  budget: AutonomousBudget; experiment: AutonomousReservation; deadline_seconds: number;
  engine_options?:{cash:number;fee_rate:string;spread_rate:string;seed:number};
};
export type ResearchRole = 'hypothesis' | 'coder' | 'critic' | 'reporter';
export type PrepareAutonomousSession = { name:string;target:AutonomousTarget;objective:string;saved_candidate_ids?:string[];scoring:AutonomousMetric[];development_ids:string[];final_test_id:string|null;models:Partial<Record<ResearchRole,{profile_id:string;revision:number|null}>>;default_model?:{profile_id:string;revision:number|null};team_role_map?:Partial<Record<ResearchRole,string>>;team_snapshot_id?:string|null;budget:AutonomousBudget;deadline_seconds:number;cost_policy?:'COUNTERS_ONLY';engine_options?:{cash:number;fee_rate:string;spread_rate:string;seed:number} };
export type AutonomousCapabilities = {schema:string;supports_final_test?:boolean;executor:{ready:boolean;reason?:string};evaluation:{metrics:string[];metrics_by_target?:Record<string,string[]>;version?:string;reservations?:Record<string,AutonomousReservation>};metrics_by_target?:Record<string,string[]>;roles?:ResearchRole[];providers?:string[];currency?:string|null};
export type PreparedAutonomousSession = {schema:string;spec:AutonomousSpec;spec_hash:string;evaluation_scope:string;model_selection?:{default_model_used:boolean;role_overrides:string[];team_snapshot_usage:string|null;role_preview?:{role:string;team_member:string|null;profile_id:string;revision:number;sha256:string;provider:string;model:string}[]}};
export type CreateAutonomousSession = { spec: AutonomousSpec; idempotency_key: string };
export type AutonomousControl = { action: AutonomousAction; expected_revision: number; idempotency_key: string; accept_unknown_charge?: boolean; candidate_hash?: string | null };
export type AutonomousExecutionProgress = {attempt_id:string;event_id:number;recorded_at:string;stage:'execute';completed_prefixes:number;total_prefixes:number;current_symbol:string|null;current_index:number|null};
export type AutonomousSession = {
  resolved?: { capability_limits?: { market_semantics?: string; currency?: string } };
  session_id: string; owner_id: string; revision: number; status: AutonomousStatus;
  generation: number; spec: AutonomousSpec; spent: AutonomousBudget;
  active_attempt: string | null; candidate_hash: string | null; final_attempt: string | null;
  latest_execution_progress?: AutonomousExecutionProgress | null;
  created_at: string; updated_at: string; allowed_actions: AutonomousAction[]; evaluation_scope:string;
};
export type AutonomousAttempt = {
  session_id: string; attempt_id: string; ordinal: number; generation: number; phase: string;
  status: string; job_id: string; run_id: string; reservation: AutonomousReservation;
  outcome: { status: string; error_code: string | null; metrics: Record<string, number> } | null;
  created_at: string; updated_at: string;
};
export type AutonomousEvent = { session_id: string; event_id: number; kind: string; metadata: Record<string, unknown>; recorded_at: string };
export type AutonomousPage<T> = { items: T[]; next_offset: number | null };
export type AutonomousIndexKind = 'attempts'|'events'|'artifacts';
export type AutonomousCursorPage<T> = {schema:'autonomous-page-2';session_id:string;kind:AutonomousIndexKind;attempt_id:string|null;limit:number;items:T[];next_cursor:string;has_more:boolean;next_offset:number|null};
export type AutonomousSessionList = { items: AutonomousSession[]; limit: number; offset: number };
export type AutonomousArtifact = {
  artifact_id: string; session_id: string; attempt_id: string; phase: string;
  logical_id:string;part_index:number;part_count:number;full_sha256:string;kind: 'proposal' | 'strategy' | 'factor' | 'evaluation' | 'feedback' | 'checkpoint';
  media_type: 'application/json' | 'text/plain' | 'text/x-python'; content: string; sha256: string; bytes: number;
};

const PREFIX = '/research/autonomous/sessions';
const actions: AutonomousAction[] = ['run', 'pause', 'resume', 'cancel', 'restart', 'seal_final'];
const states: AutonomousStatus[] = ['DRAFT', 'READY', 'RUNNING', 'PAUSE_REQUESTED', 'PAUSED', 'CANCEL_REQUESTED', 'CANCELLED', 'BLOCKED', 'BUDGET_EXHAUSTED', 'SEALED', 'COMPLETED'];
const mismatch = () => new ApiError(0, 'AUTONOMOUS_RECEIPT_MISMATCH', '返回的研究状态与所选记录不一致，暂时无法确认是否已执行或保存。请刷新。');
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw mismatch();
  return value as Record<string, unknown>;
}
function identifier(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)) throw new ApiError(0, 'INVALID_INPUT', '研究标识无效，请重新选择已保存记录。');
  return encodeURIComponent(value);
}
function budget(value: unknown) {
  const row = object(value);
  if (['attempts', 'model_requests', 'tokens', 'cost_microunits', 'compute_seconds', 'artifact_bytes', 'prefixes'].some(key => !Number.isSafeInteger(row[key]) || Number(row[key]) < 0)) throw mismatch();
}
function paging(limit: number, offset: number) {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100 || !Number.isSafeInteger(offset) || offset < 0) throw new ApiError(0, 'INVALID_INPUT', '分页范围无效。');
  return `limit=${limit}&offset=${offset}`;
}

export function autonomousResearchApi(ownerId: string) {
  identifier(ownerId);
  function session(value: unknown, expectedId?: string): AutonomousSession {
    const row = object(value);
    if (row.owner_id !== ownerId || typeof row.session_id !== 'string' || expectedId && row.session_id !== expectedId ||
        !Number.isSafeInteger(row.revision) || Number(row.revision) < 1 || !states.includes(row.status as AutonomousStatus) || !Array.isArray(row.allowed_actions) || row.allowed_actions.some(action=>!actions.includes(action as AutonomousAction)) || !['DEVELOPMENT_ONLY','FINAL_SEPARATE'].includes(String(row.evaluation_scope))) throw mismatch();
    identifier(row.session_id); budget(row.spent); object(row.spec);
    return row as unknown as AutonomousSession;
  }
  async function page<T>(sessionId: string, kind: 'attempts' | 'events', limit: number, offset: number): Promise<AutonomousPage<T>> {
    const value = object(await request<unknown>(`${PREFIX}/${identifier(sessionId)}/${kind}?${paging(limit, offset)}`));
    if (!Array.isArray(value.items) || value.items.length > limit || value.items.some(row => object(row).session_id !== sessionId) ||
        value.next_offset !== null && (!Number.isSafeInteger(value.next_offset) || Number(value.next_offset) <= offset)) throw mismatch();
    return value as unknown as AutonomousPage<T>;
  }
  return {
    async cursorPage<T>(sessionId:string,kind:AutonomousIndexKind,options:{cursor:string;limit?:number;attemptId?:string;signal?:AbortSignal}):Promise<AutonomousCursorPage<T>>{
      const limit=options.limit??100;
      if(!Number.isSafeInteger(limit)||limit<1||limit>100||typeof options.cursor!=='string'||options.cursor.length>1024||!['attempts','events','artifacts'].includes(kind)||options.attemptId&&kind!=='artifacts')throw mismatch();
      const query=new URLSearchParams({limit:String(limit),cursor:options.cursor});
      if(options.attemptId){identifier(options.attemptId);query.set('attempt_id',options.attemptId);}
      const value=object(await request<unknown>(`${PREFIX}/${identifier(sessionId)}/${kind}?${query}`,{signal:options.signal}));
      if(value.schema!=='autonomous-page-2'||value.session_id!==sessionId||value.kind!==kind||value.attempt_id!==(options.attemptId??null)||value.limit!==limit||!Array.isArray(value.items)||value.items.length>limit||typeof value.next_cursor!=='string'||value.next_cursor.length<1||value.next_cursor.length>1024||typeof value.has_more!=='boolean'||value.items.length>0&&value.next_cursor===options.cursor)throw mismatch();
      const seen=new Set<string|number>();let previous=-1;
      for(const raw of value.items){const row=object(raw);const position=kind==='events'?row.event_id:kind==='attempts'?row.ordinal:row.append_sequence;
       if(!Number.isSafeInteger(position)||Number(position)<=previous||Number(position)<1||kind!=='artifacts'&&row.session_id!==sessionId||options.attemptId&&row.attempt_id!==options.attemptId)throw mismatch();
       const id=kind==='events'?row.event_id:kind==='attempts'?row.attempt_id:row.artifact_id;
       if(typeof id!=='string'&&typeof id!=='number'||seen.has(id as string|number))throw mismatch();seen.add(id as string|number);previous=Number(position);
      }
      return value as unknown as AutonomousCursorPage<T>;
    },
    async capabilities():Promise<AutonomousCapabilities>{
      const value=object(await request<unknown>('/research/autonomous/capabilities'));
      if(value.schema!=='autonomous-capabilities-1'||typeof object(value.executor).ready!=='boolean'||!Array.isArray(object(value.evaluation).metrics))throw mismatch();
      return value as unknown as AutonomousCapabilities;
    },
    async prepare(body:PrepareAutonomousSession):Promise<PreparedAutonomousSession>{
      const value=object(await request<unknown>('/research/autonomous/prepare',{method:'POST',body:JSON.stringify(body)}));
      if(value.schema!=='autonomous-prepared-1'||typeof value.spec_hash!=='string'||!/^[a-f0-9]{64}$/.test(value.spec_hash))throw mismatch();
      const spec=object(value.spec);budget(spec.budget);object(spec.models);if(value.evaluation_scope!==(spec.final_test?'FINAL_SEPARATE':'DEVELOPMENT_ONLY'))throw mismatch();
      if(value.model_selection!==undefined){const selection=object(value.model_selection);if(typeof selection.default_model_used!=='boolean'||!Array.isArray(selection.role_overrides)||selection.role_overrides.some(role=>!['hypothesis','coder','critic','reporter'].includes(String(role)))||![null,'PROVENANCE_AND_SAVED_DEFAULT_PROFILE_ONLY','EXPLICIT_MEMBER_PROFILE_MAPPING'].includes(selection.team_snapshot_usage as null|string))throw mismatch();if(selection.role_preview!==undefined&&(!Array.isArray(selection.role_preview)||selection.role_preview.length>4||selection.role_preview.some(raw=>{const row=object(raw);return !['hypothesis','coder','critic','reporter'].includes(String(row.role))||typeof row.profile_id!=='string'||!Number.isSafeInteger(row.revision)||Number(row.revision)<1||typeof row.provider!=='string'||typeof row.model!=='string'||typeof row.sha256!=='string'||!/^[a-f0-9]{64}$/.test(row.sha256)||row.team_member!==null&&typeof row.team_member!=='string';})))throw mismatch();}
      return value as unknown as PreparedAutonomousSession;
    },
    async artifacts(sessionId:string,limit=20,offset=0):Promise<AutonomousPage<AutonomousArtifact>>{
      const value=object(await request<unknown>(`${PREFIX}/${identifier(sessionId)}/artifacts?${paging(limit,offset)}`));
      if(!Array.isArray(value.items)||value.items.length>limit||value.next_offset!==null&&(!Number.isSafeInteger(value.next_offset)||Number(value.next_offset)<=offset))throw mismatch();
      return value as unknown as AutonomousPage<AutonomousArtifact>;
    },
    // The caller retains each key across uncertain responses. No automatic retries,
    // key replacement, unknown-charge acknowledgement or state transition is invented here.
    async create(body: CreateAutonomousSession): Promise<AutonomousSession> {
      identifier(body.idempotency_key);
      return session(await request<unknown>(PREFIX, { method: 'POST', body: JSON.stringify(body) }));
    },
    async list(limit = 20, offset = 0): Promise<AutonomousSessionList> {
      const value = object(await request<unknown>(`${PREFIX}?${paging(limit, offset)}`));
      if (!Array.isArray(value.items) || value.items.length > limit || value.limit !== limit || value.offset !== offset) throw mismatch();
      return { items: value.items.map(row => session(row)), limit, offset };
    },
    async read(sessionId: string, signal?: AbortSignal): Promise<AutonomousSession> {
      return session(await request<unknown>(`${PREFIX}/${identifier(sessionId)}`, { signal }), sessionId);
    },
    async control(sessionId: string, body: AutonomousControl): Promise<AutonomousSession> {
      identifier(body.idempotency_key);
      if (!actions.includes(body.action) || !Number.isSafeInteger(body.expected_revision) || body.expected_revision < 1 ||
          body.accept_unknown_charge !== undefined && typeof body.accept_unknown_charge !== 'boolean') throw new ApiError(0, 'INVALID_INPUT', '研究控制请求无效，请刷新任务状态后重试。');
      const payload = { ...body, accept_unknown_charge: body.accept_unknown_charge ?? false };
      return session(await request<unknown>(`${PREFIX}/${identifier(sessionId)}/control`, { method: 'POST', body: JSON.stringify(payload) }), sessionId);
    },
    attempts(sessionId: string, limit = 20, offset = 0) { return page<AutonomousAttempt>(sessionId, 'attempts', limit, offset); },
    events(sessionId: string, limit = 20, offset = 0) { return page<AutonomousEvent>(sessionId, 'events', limit, offset); },
    async artifact(sessionId: string, artifactId: string, signal?:AbortSignal): Promise<AutonomousArtifact> {
      const row = object(await request<unknown>(`${PREFIX}/${identifier(sessionId)}/artifacts/${identifier(artifactId)}`,{signal}));
      if (row.session_id !== sessionId || row.artifact_id !== artifactId || typeof row.content !== 'string' || typeof row.sha256 !== 'string' ||
          !/^[a-f0-9]{64}$/.test(row.sha256) || typeof row.full_sha256!=='string' || !/^[a-f0-9]{64}$/.test(row.full_sha256) || typeof row.logical_id!=='string' || !Number.isSafeInteger(row.part_index) || !Number.isSafeInteger(row.part_count) || Number(row.part_count)<1 || Number(row.part_count)>256 || Number(row.part_index)<0 || Number(row.part_index)>=Number(row.part_count) || !Number.isSafeInteger(row.bytes) || Number(row.bytes) < 0 || Number(row.bytes) > 65536 ||
          !['proposal', 'strategy', 'factor', 'evaluation', 'feedback', 'checkpoint'].includes(String(row.kind)) ||
          !['application/json', 'text/plain', 'text/x-python'].includes(String(row.media_type))) throw mismatch();
      const bytes = new TextEncoder().encode(row.content);
      if (bytes.length !== row.bytes) throw mismatch();
      const hash = await crypto.subtle.digest('SHA-256', bytes);
      if (Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('') !== row.sha256) throw mismatch();
      return row as unknown as AutonomousArtifact;
    },
  };
}
