import { ApiError, request } from './api.ts';
import type { RecordData } from './api';

type TeamIntent = { schema: 2; path: string; method: 'POST' | 'PUT'; body: RecordData };
const journalKey = (owner: string) => `fa-team-write-v1:${owner}`;
const inFlight = new Set<string>();
const expectedOwnerPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const firstWriteRejections = new Map([
  [400, new Set(['INVALID_INPUT'])],
  [401, new Set(['NOT_AUTHENTICATED'])],
  [403, new Set(['CSRF_REJECTED', 'FORBIDDEN', 'MODEL_PROFILE_OWNER_REQUIRED', 'ORIGIN_REJECTED'])],
  [404, new Set(['NOT_FOUND'])],
  [422, new Set(['IDEMPOTENCY_KEY_REQUIRED', 'INPUT_BUDGET_EXCEEDED', 'INVALID_INPUT'])],
]);

function object(value: unknown): value is RecordData {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function validPath(path: string, method: string) {
  return method === 'POST' && ['/agent-teams/presets', '/agent-teams/snapshots'].includes(path)
    || method === 'PUT' && /^\/agent-teams\/presets\/[A-Za-z0-9_.:-]{1,128}$/.test(path);
}

function readIntent(owner: string): TeamIntent | null {
  const raw = sessionStorage.getItem(journalKey(owner));
  if (raw === null) return null;
  const saved = JSON.parse(raw);
  if (saved?.schema !== 2 || typeof saved.path !== 'string' || !validPath(saved.path, saved.method)
    || !object(saved.body) || typeof saved.body.idempotency_key !== 'string'
    || !/^[A-Za-z0-9_.:-]{1,128}$/.test(saved.body.idempotency_key)) {
    throw new Error('上次团队操作缺少可核对的原请求。请保留此标签页并查看已保存团队，避免重复提交。');
  }
  return saved;
}

export function teamWritePending(owner: string) {
  try { return sessionStorage.getItem(journalKey(owner)) !== null; }
  catch { return true; }
}

function verifyReceipt(owner: string, intent: TeamIntent, value: unknown): RecordData {
  if (!object(value)) throw new Error('还不能确认团队设置已保存，保留上次操作以便核对。');
  const body = intent.body;
  const valid = intent.path === '/agent-teams/snapshots'
    ? typeof value.snapshot_id === 'string' && !!value.snapshot_id && value.owner_id === owner
      && value.preset_id === body.preset_id && value.preset_revision === body.revision
      && typeof value.sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.sha256)
    : typeof value.preset_id === 'string' && !!value.preset_id
      && value.name === body.name && object(value.configuration)
      && (intent.method === 'PUT'
        ? intent.path === `/agent-teams/presets/${encodeURIComponent(value.preset_id)}`
          && value.revision === Number(body.expected_revision) + 1
        : value.revision === 1);
  if (!valid) throw new Error('返回的团队设置与上次操作不一致。请先核对原操作。');
  return value;
}

async function submit(owner: string, intent: TeamIntent, original: boolean) {
  try {
    const value = await request<unknown>(intent.path, {
      method: intent.method,
      headers: { 'X-Team-Expected-Owner': owner },
      body: JSON.stringify(intent.body),
    });
    const receipt = verifyReceipt(owner, intent, value);
    sessionStorage.removeItem(journalKey(owner));
    return receipt;
  } catch (error) {
    // An explicit response to the first request can reject that write. An
    // error while recovering never proves that the earlier request failed.
    if (original && error instanceof ApiError && firstWriteRejections.get(error.status)?.has(error.code)) {
      sessionStorage.removeItem(journalKey(owner));
    }
    throw error;
  }
}

export async function teamWrite(owner: string, path: string, body: RecordData, method: 'POST' | 'PUT' = 'POST') {
  if (!expectedOwnerPattern.test(owner)) throw new Error('团队操作缺少可验证的原操作者。');
  if (!owner || inFlight.has(owner) || teamWritePending(owner)) throw new Error('已有配置写入待核对，未重复发送。');
  if (!validPath(path, method)) throw new Error('团队操作路径无效。');
  const key = body.idempotency_key ?? crypto.randomUUID();
  if (typeof key !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(key)) throw new Error('团队操作编号无效。');
  const intent: TeamIntent = { schema: 2, path, method, body: { ...body, idempotency_key: key } };
  sessionStorage.setItem(journalKey(owner), JSON.stringify(intent));
  inFlight.add(owner);
  try { return await submit(owner, intent, true); }
  finally { inFlight.delete(owner); }
}

// Recover by owner-scoped immutable command receipt. Even after an explicit
// request to retry, lookup happens first and only the original command can run.
export async function recoverTeamWrite(owner: string, retryIfMissing = false): Promise<RecordData | null> {
  if (!expectedOwnerPattern.test(owner)) throw new Error('团队操作缺少可验证的原操作者。');
  if (!owner || inFlight.has(owner)) throw new Error('正在处理上次团队操作。');
  const intent = readIntent(owner);
  if (!intent) throw new Error('没有待确认的团队操作。');
  inFlight.add(owner);
  try {
    const key = String(intent.body.idempotency_key);
    let value: unknown;
    try { value = await request<unknown>(`/agent-teams/commands/${encodeURIComponent(key)}`); }
    catch (error) {
      if (!(error instanceof ApiError && error.status === 404 && error.code === 'TEAM_WRITE_RECEIPT_NOT_FOUND')) throw error;
      return retryIfMissing ? await submit(owner, intent, false) : null;
    }
    if (!object(value) || value.owner_id !== owner || value.idempotency_key !== key) throw new Error('返回的团队与所选不一致，保留上次操作以便核对。');
    const receipt = verifyReceipt(owner, intent, value.response);
    sessionStorage.removeItem(journalKey(owner));
    return receipt;
  } finally { inFlight.delete(owner); }
}
