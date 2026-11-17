import { ApiError, request } from './api';
import type { RecordData } from './api';

type WriteMethod = 'POST' | 'PUT' | 'DELETE' | 'PATCH';
type PendingWrite = { path: string; method: WriteMethod; key: string };
export type PendingWriteResource = {
  kind: 'job' | 'connection' | 'dataset' | 'model_profile' | 'upload';
  id: string;
  job_id?: string;
  run_id?: string;
  owner_id?: string;
};
type Recovery = { status: 'UNKNOWN' | 'CLOSED' } | { status: 'COMMITTED'; path: string; resource: PendingWriteResource };
const originalBodies = new Map<string, { key: string; body: string }>();
const inFlight = new Set<string>();
export const uxPendingChanged = 'fa-ux-pending-changed';
const slot = (owner: string, area: string) => `fa-ux-pending-v1:${JSON.stringify([owner, area])}`;

export function uxPending(owner: string, area: string) {
  try { return sessionStorage.getItem(slot(owner, area)) !== null; }
  catch { return true; }
}

function readPending(storageKey: string): PendingWrite {
  const value = JSON.parse(sessionStorage.getItem(storageKey) ?? 'null');
  if (typeof value?.path !== 'string' || !/^\/(?!\/)[^#\s]+$/.test(value.path) ||
      typeof value?.key !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(value.key)) {
    throw new Error('无法读取原操作编号。请先查看已保存记录。');
  }
  const method = value.method ?? (value.path.includes('?expected_revision=') ? 'DELETE' :
    /^\/model-profiles\/[^/]+$/.test(value.path) ? 'PUT' : 'POST');
  if (!['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) throw new Error('原操作方法无法核对。');
  return { path: value.path, method, key: value.key };
}

function clearPending(storageKey: string) {
  sessionStorage.removeItem(storageKey);
  originalBodies.delete(storageKey);
  window.dispatchEvent(new Event(uxPendingChanged));
}

export function definitelyRejected(error: unknown) {
  return error instanceof ApiError && error.code !== 'INVALID_RESPONSE' &&
    [400, 401, 403, 404, 405, 413, 415, 422].includes(error.status);
}

async function send(owner: string, area: string, intent: PendingWrite, body: string) {
  const { path, method, key } = intent;
  const target = method === 'DELETE' ? `${path}${path.includes('?') ? '&' : '?'}idempotency_key=${encodeURIComponent(key)}` : path;
  const result = await request<RecordData>(target, {
    method, ...(method === 'DELETE' ? {} : { body }),
  });
  const upload = result?.original_upload;
  const preview = area === 'preview' && ['READY', 'NEEDS_INPUT'].includes(String(result?.status)) &&
    upload && typeof upload === 'object' && !Array.isArray(upload) && typeof upload.upload_id === 'string';
  if (!result || typeof result !== 'object' ||
      !['profile_id', 'dataset_id', 'connection_id', 'job_id'].some(field => typeof result[field] === 'string') &&
        result.deleted !== true && !preview) {
    throw new Error('无法确认操作是否完成。请先核对上次操作。');
  }
  if (typeof result.job_id === 'string' && (result.owner_id !== owner || typeof result.run_id !== 'string')) {
    throw new Error('返回的任务不属于当前账户。请检查任务记录。');
  }
  return result;
}

export async function uxMutation(owner: string, area: string, path: string, body: RecordData, method: WriteMethod = 'POST') {
  const storageKey = slot(owner, area);
  if (!owner || uxPending(owner, area)) throw new Error('还有上次操作待核对。');
  const intent = { path, method, key: crypto.randomUUID() };
  const encodedBody = JSON.stringify({ ...body, idempotency_key: intent.key });
  sessionStorage.setItem(storageKey, JSON.stringify(intent));
  originalBodies.set(storageKey, { key: intent.key, body: encodedBody });
  inFlight.add(storageKey);
  window.dispatchEvent(new Event(uxPendingChanged));
  try {
    const result = await send(owner, area, intent, encodedBody);
    clearPending(storageKey);
    return result;
  } catch (error) {
    if (definitelyRejected(error)) clearPending(storageKey);
    throw error;
  } finally {
    inFlight.delete(storageKey);
  }
}

export function canRepeatUxMutation(owner: string, area: string) {
  const storageKey = slot(owner, area);
  try {
    const original = originalBodies.get(storageKey);
    return Boolean(original && original.key === readPending(storageKey).key);
  } catch { return false; }
}

export async function recoverUxMutation(owner: string, area: string, choice: 'lookup' | 'repeat' | 'close' = 'lookup'): Promise<Recovery> {
  const storageKey = slot(owner, area);
  if (!owner || inFlight.has(storageKey)) throw new Error('正在处理原操作，请稍后核对。');
  const intent = readPending(storageKey);
  inFlight.add(storageKey);
  try {
    const close = choice === 'close';
    const response = await request<{ status: string; resource: PendingWriteResource | null }>('/operations/pending-write/' + (close ? 'close' : 'lookup'), {
      method: 'POST', body: JSON.stringify({ path: intent.path, method: intent.method, idempotency_key: intent.key, ...(close ? { confirm: true } : {}) }),
    });
    if (response?.status === 'CLOSED' && response.resource === null) {
      clearPending(storageKey);
      return { status: 'CLOSED' };
    }
    if (response?.status === 'UNKNOWN' && response.resource === null) {
      const original = originalBodies.get(storageKey);
      if (choice !== 'repeat' || original?.key !== intent.key) return { status: 'UNKNOWN' };
      const value = await send(owner, area, intent, original.body);
      const resource: PendingWriteResource =
        typeof value.job_id === 'string' ? { kind: 'job', id: value.job_id, job_id: value.job_id, run_id: String(value.run_id), owner_id: owner } :
        typeof value.connection_id === 'string' ? { kind: 'connection', id: value.connection_id } :
        typeof value.dataset_id === 'string' ? { kind: 'dataset', id: value.dataset_id } :
        area === 'preview' ? { kind: 'upload', id: String((value.original_upload as RecordData).upload_id) } :
        { kind: 'model_profile', id: String(value.profile_id ?? intent.path.split('?')[0].split('/').at(-1)) };
      clearPending(storageKey);
      return { status: 'COMMITTED', path: intent.path, resource };
    }
    const resource = response?.resource;
    const kind = area === 'research' || area === 'import' || area === 'membership' ? 'job' :
      area === 'preview' ? 'upload' : area === 'models' ? 'model_profile' :
      intent.path === '/data/library/connections' ? 'connection' : 'dataset';
    if (response?.status !== 'COMMITTED' || !resource || typeof resource.id !== 'string' || !resource.id ||
        resource.kind !== kind ||
        (resource.kind === 'job' && (resource.owner_id !== owner || resource.id !== resource.job_id || typeof resource.run_id !== 'string'))) {
      throw new Error('服务返回的原操作回执不完整，待核对记录仍保留。');
    }
    clearPending(storageKey);
    return { status: 'COMMITTED', path: intent.path, resource };
  } finally {
    inFlight.delete(storageKey);
  }
}
