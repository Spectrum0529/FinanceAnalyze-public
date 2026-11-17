import { ApiError, request, resetApiSession, setCsrf } from './api';
import { localSessionFor } from './local-personal-session';

export type AccountRole = 'admin' | 'researcher' | 'viewer';
export type AccountUser = { user_id: string; username: string; role: AccountRole };
export type WorkbenchSession = {
  user: AccountUser;
  csrf_token: string;
  accountMode: boolean;
  expires_at?: string;
  recent_until?: string;
};
export type SessionRotation = {
  csrf_token: string;
  expires_at: string;
  recent_until: string;
};
export function isSessionRotation(response: unknown): response is SessionRotation {
  if (!response || typeof response !== 'object') return false;
  const value = response as Partial<SessionRotation>;
  return typeof value.csrf_token === 'string' && Boolean(value.csrf_token) &&
    typeof value.expires_at === 'string' && Number.isFinite(Date.parse(value.expires_at)) &&
    typeof value.recent_until === 'string' && Number.isFinite(Date.parse(value.recent_until));
}
export type AccountReauthentication = (data: FormData, signal: AbortSignal) => Promise<void>;

export async function reauthenticateAccount(
  data: FormData, signal: AbortSignal,
  rotate: (receipt: SessionRotation, signal: AbortSignal) => Promise<void>,
) {
  const receipt = await accountWrite('/auth/reauthenticate', 'POST', {
    password: String(data.get('password') ?? ''),
    ...(data.has('otp') ? { otp: String(data.get('otp') ?? '') } : {}),
  }, signal);
  if (!isSessionRotation(receipt)) throw new Error('无法确认身份是否更新。请重新打开工作台。');
  await rotate(receipt, signal);
}
export type AccountStatus = {
  access_mode: 'accounts' | 'local-personal';
  interactive_login_required: boolean;
  initialized: boolean;
  account_state?: 'empty' | 'legacy_personal' | 'ready' | 'ambiguous';
  setup_kind: 'first_admin' | 'upgrade_personal' | null;
  recovery_supported: boolean;
  mfa_available: boolean;
};
export type WorkbenchAccess =
  | { kind: 'personal'; session: WorkbenchSession }
  | { kind: 'accounts'; status: AccountStatus; session: WorkbenchSession | null }
  | { kind: 'preview' };

export function hasCode(error: unknown, code: string) {
  return error instanceof ApiError && error.code === code;
}

export function accountWrite<T>(
  path: string, method: 'POST' | 'PATCH' | 'DELETE', payload?: unknown, signal?: AbortSignal,
) {
  return request<T>(path, { method, body: payload === undefined ? undefined : JSON.stringify(payload), signal });
}

export function isAccountUser(user: unknown): user is AccountUser {
  if (!user || typeof user !== 'object') return false;
  const value = user as Partial<AccountUser>;
  return typeof value.user_id === 'string' && Boolean(value.user_id) &&
    typeof value.username === 'string' && Boolean(value.username) &&
    ['admin', 'researcher', 'viewer'].includes(value.role ?? '');
}

function confirmedSession(response: unknown, accountMode: boolean): WorkbenchSession {
  const session = response as Partial<WorkbenchSession> | null;
  if (!isAccountUser(session?.user) || typeof session?.csrf_token !== 'string' || !session.csrf_token) {
    throw new Error('没能确认登录状态，请重新打开工作台。');
  }
  return { ...session, user: session.user, csrf_token: session.csrf_token, accountMode };
}

export function acceptSession(session: WorkbenchSession) {
  setCsrf(session.csrf_token);
}

export async function getAccountSession(signal: AbortSignal) {
  return confirmedSession(await request('/auth/me', { signal, cache: 'no-store' }), true);
}

export async function checkAccess(signal: AbortSignal): Promise<WorkbenchAccess> {
  let status: AccountStatus;
  try {
    status = await request<AccountStatus>('/auth/status', { signal, cache: 'no-store' });
  } catch (error) {
    if (hasCode(error, 'FRONTEND_PREVIEW_ONLY')) return { kind: 'preview' };
    throw error;
  }
  if (status?.access_mode === 'local-personal') {
    return { kind: 'personal', session: confirmedSession(await localSessionFor(status, signal), false) };
  }
  if (status?.access_mode !== 'accounts' || status.interactive_login_required !== true || typeof status.initialized !== 'boolean') {
    throw new Error('无法确认服务的登录方式。');
  }
  if (!status.initialized) return { kind: 'accounts', status, session: null };
  try {
    return { kind: 'accounts', status, session: await getAccountSession(signal) };
  } catch (error) {
    if (hasCode(error, 'NOT_AUTHENTICATED')) return { kind: 'accounts', status, session: null };
    throw error;
  }
}

export async function signIn(payload: {
  username: string; password: string; remember?: boolean; otp?: string;
}, signal: AbortSignal) {
  return confirmedSession(await accountWrite('/auth/login', 'POST', payload, signal), true);
}

export async function logoutAccount() {
  const receipt = await accountWrite<{ logged_out?: boolean }>('/auth/logout', 'POST');
  if (receipt?.logged_out !== true) throw new Error('暂时不能确认是否已退出，请刷新检查登录状态。');
}

const invalidationKey = 'finance-analyze-account-invalidated-v1';
const rotationKey = 'finance-analyze-account-rotated-v1';
const appliedBoundaryKey = 'finance-analyze-account-end-applied-v1';
export type AccountEvent = 'ended' | 'changed' | 'rotated';

export function clearSessionState() {
  resetApiSession();
  try { sessionStorage.clear(); } catch { /* Storage can be disabled. */ }
}

export function watchAccountEvents(reopen: (kind: AccountEvent) => void) {
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel(invalidationKey) : null;
  const events = new AbortController();
  let active = true;
  function applyBoundary(marker: string) {
    try { if (sessionStorage.getItem(appliedBoundaryKey) === marker) return; } catch { /* Storage is optional. */ }
    clearSessionState();
    try { sessionStorage.setItem(appliedBoundaryKey, marker); } catch { /* Storage is optional. */ }
  }
  try {
    const marker = localStorage.getItem(invalidationKey);
    if (marker?.startsWith('ended:') || marker?.startsWith('changed:')) applyBoundary(marker);
  } catch { /* The tab still receives BroadcastChannel while open. */ }
  function receive(marker: unknown) {
    if (typeof marker !== 'string') return;
    const [kind] = marker.split(':', 1);
    if (kind !== 'ended' && kind !== 'changed' && kind !== 'rotated') return;
    if (kind !== 'rotated') applyBoundary(marker);
    if (!active) return;
    active = false;
    if (kind === 'rotated') resetApiSession();
    reopen(kind);
  }
  channel?.addEventListener('message', event => receive(event.data), { signal: events.signal });
  window.addEventListener('storage', event => {
    if (event.key === invalidationKey || event.key === rotationKey) receive(event.newValue);
  }, { signal: events.signal });
  window.addEventListener('pagehide', () => {
    events.abort();
    channel?.close();
  }, { once: true });
  // A suspended document cannot keep its private React tree or CSRF on back/forward restore.
  window.addEventListener('pageshow', event => { if (event.persisted) receive('rotated:restore'); });
  return function publish(kind: AccountEvent) {
    // This marker only deduplicates notifications. It carries no session data.
    const marker = kind + ':' + (crypto.randomUUID?.() ?? String(Date.now()));
    if (kind === 'ended') active = false;
    if (kind === 'rotated') resetApiSession();
    else applyBoundary(marker);
    channel?.postMessage(marker);
    try {
      localStorage.setItem(kind === 'rotated' ? rotationKey : invalidationKey, marker);
    } catch { /* BroadcastChannel is still available when site storage is disabled. */ }
  };
}
