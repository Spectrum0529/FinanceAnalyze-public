export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type RecordData = { [key: string]: Json };
export type PageData = { items: RecordData[]; total: number; limit: number; offset: number };
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
    public details?: Json,
  ) {
    super(message);
  }
}
let csrf = '';
let apiSession = new AbortController();
export function setCsrf(value: string) {
  csrf = value;
}
export function resetApiSession() {
  csrf = '';
  apiSession.abort();
  apiSession = new AbortController();
}
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const caller = options.signal;
  const session = apiSession.signal;
  let abortedBy: 'caller' | 'timeout' | undefined;
  const abort = (source: 'caller' | 'timeout') => {
    if (!controller.signal.aborted) {
      abortedBy = source;
      controller.abort();
    }
  };
  const onCallerAbort = () => abort('caller');
  if (caller?.aborted) onCallerAbort();
  else caller?.addEventListener('abort', onCallerAbort, { once: true });
  session.addEventListener('abort', onCallerAbort, { once: true });
  const timer = window.setTimeout(() => abort('timeout'), 20000);
  try {
    controller.signal.throwIfAborted();
    const headers = new Headers(options.headers);
    headers.set('Accept', 'application/json');
    if (options.body && !(options.body instanceof FormData))
      headers.set('Content-Type', 'application/json');
    if (csrf && options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrf);
    const response = await fetch(`/api/v1${path}`, {
      cache: 'no-store',
      ...options,
      headers,
      credentials: 'same-origin',
      signal: controller.signal,
    });
    const raw = await response.text();
    controller.signal.throwIfAborted();
    let data: unknown;
    try {
      data = raw ? JSON.parse(raw) : null;
    } catch {
      throw new ApiError(
        response.status,
        'INVALID_RESPONSE',
        '服务返回了页面或不认识的格式。请核对服务地址，再试一次。',
      );
    }
    if (!response.ok) {
      const parsed = (data && typeof data === 'object' ? data : {}) as Record<string, unknown>;
      const body = (
        parsed.error && typeof parsed.error === 'object' ? parsed.error : parsed
      ) as Record<string, unknown>;
      const error = new ApiError(
        response.status,
        typeof body.code === 'string' && body.code ? body.code : `HTTP_${response.status}`,
        String(
          body.message ||
            (response.status === 502 || response.status === 503
              ? '研究服务暂不可用。请在本机打开 FinanceAnalyze，确认服务已启动。'
              : response.status === 403
                ? '当前账户无权执行此操作。请联系管理员核对权限。'
                : response.status === 401
                  ? '会话已结束，请重新登录。'
                  : response.status === 404
                    ? '找不到这条记录，或当前服务还不提供这项功能。'
                    : '操作未完成。请先刷新，看看上次操作是否已经保存。'),
        ),
        String(body.request_id || parsed.request_id || response.headers.get('X-Request-ID') || ''),
        body.details as Json,
      );
      if (response.status === 401 && error.code === 'NOT_AUTHENTICATED' && csrf && !session.aborted) {
        window.dispatchEvent(new Event('finance-account-session-ended'));
      }
      throw error;
    }
    return data as T;
  } catch (error) {
    if (abortedBy) {
      throw new ApiError(
        0,
        abortedBy === 'caller' ? 'REQUEST_CANCELLED' : 'REQUEST_TIMEOUT',
        abortedBy === 'caller'
          ? '已停止等待。如果刚提交过操作，请先检查任务或列表，避免重复提交。'
          : '等待已超过 20 秒。请先刷新检查操作结果，避免重复提交。',
      );
    }
    if (error instanceof ApiError) throw error;
    throw new ApiError(
      0,
      'NETWORK_UNAVAILABLE',
      '无法连接研究服务。请确认 FinanceAnalyze 服务已启动，然后重试。',
    );
  } finally {
    window.clearTimeout(timer);
    caller?.removeEventListener('abort', onCallerAbort);
    session.removeEventListener('abort', onCallerAbort);
  }
}
export function post<T>(path: string, body: unknown = {}): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body) });
}
export async function download(path: string, filename: string): Promise<void> {
  const controller = new AbortController();
  const session = apiSession.signal;
  const cancel = () => controller.abort();
  session.addEventListener('abort', cancel, { once: true });
  const timer = window.setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(`/api/v1${path}`, {
      credentials: 'same-origin',
      signal: controller.signal,
    });
    if (!response.ok) {
      let body: Record<string, unknown> = {};
      try {
        const parsed = await response.json();
        body = parsed.error ?? parsed;
      } catch {
        /* preserve stable status */
      }
      throw new ApiError(
        response.status,
        String(body.code || `HTTP_${response.status}`),
        String(body.message || '导出未获服务端批准。请查看用途许可与质量报告。'),
        response.headers.get('X-Request-ID') || undefined,
      );
    }
    const blob = await response.blob();
    controller.signal.throwIfAborted();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw new ApiError(0, 'DOWNLOAD_UNAVAILABLE', '下载未完成，请检查服务连接后重新操作。');
  } finally {
    window.clearTimeout(timer);
    session.removeEventListener('abort', cancel);
  }
}
export function display(value: Json | undefined): string {
  return value === undefined || value === null
    ? '—'
    : typeof value === 'object'
      ? JSON.stringify(value)
      : String(value);
}
export function safeJson(value: unknown): string {
  return JSON.stringify(
    value,
    (key, v) =>
      /^(password|token|api_key|secret|private_key|authorization|csrf_token|session_token)$/i.test(
        key,
      )
        ? '[已隐藏]'
        : v,
    2,
  );
}
export function idOf(row: RecordData): string {
  return display(
    row.id ??
      row.job_id ??
      row.dataset_id ??
      row.report_id ??
      row.model_id ??
      row.instance_id ??
      row.config_id ??
      row.client_order_id ??
      row.run_id ??
      row.strategy_id ??
      row.account_id ??
      row.snapshot_id ??
      row.source_id ??
      row.connection_id ??
      row.schedule_id ??
      row.alert_id ??
      row.backup_id,
  );
}
