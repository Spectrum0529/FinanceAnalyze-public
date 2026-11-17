import { request } from './api';
export async function localSessionFor(status: { access_mode?: string; interactive_login_required?: boolean }, signal?: AbortSignal) {
  if (status.access_mode !== 'local-personal' || status.interactive_login_required !== false) return null;
  const session = await request<{ user: { user_id: string; username: string; role: string }; csrf_token: string }>('/auth/local-session', { method: 'POST', signal });
  if (!session?.user?.user_id || typeof session.csrf_token !== 'string' || !session.csrf_token || !['admin', 'researcher', 'viewer'].includes(session.user.role)) throw new Error('个人本地会话响应不完整，未进入工作区。');
  return session;
}
