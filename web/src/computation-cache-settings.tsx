import { useEffect, useRef, useState } from 'react';
import { ApiError } from './api';
import { useAutonomousIntent } from './autonomous-request-intent';
import { cacheKindLabel, cacheMiB, getCacheView, isCacheIntent, parseCacheCapacity, submitCacheIntent } from './computation-cache-api';
import type { CacheIntent, CacheView } from './computation-cache-api';
import { ComputationCacheView } from './computation-cache-view';

function problem(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 404 || error.status === 501 || error.code === 'UNSUPPORTED_CAPABILITY') return '当前服务暂未提供计算缓存管理。';
    if (error.status === 401 || error.status === 403) return '当前账户无权修改计算缓存。';
    if (error.status === 409) return '缓存设置有变化。请刷新后再试。';
    if (error.status === 422 || error.status === 400) return '缓存设置无法保存。请检查容量上限和当前状态。';
  }
  return '暂时无法确认操作结果。请刷新后查看当前设置。';
}

export function ComputationCacheSettings({ ownerId, canWrite }: { ownerId: string; canWrite: boolean }) {
  const journal = useAutonomousIntent(ownerId, 'computation-cache', isCacheIntent);
  const [view, setView] = useState<CacheView>();
  const [draft, setDraft] = useState<{ enabled: boolean; capacity: string; revision: number }>();
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0), [confirmingClear, setConfirmingClear] = useState(false);
  const [error, setError] = useState<string>(), [status, setStatus] = useState<string>();
  const gate = useRef(false);
  const anchor = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const [, search] = window.location.hash.split('?');
    if (new URLSearchParams(search).get('section') === 'computation-cache') anchor.current?.focus();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    void getCacheView(controller.signal).then(received => {
      if (!controller.signal.aborted) setView(received);
    }).catch(error => {
      if (!controller.signal.aborted) { setView(undefined); setError(problem(error)); }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refresh]);

  async function submit(intent: CacheIntent, replay = false) {
    if (gate.current || !canWrite || !ownerId || journal.error || (!replay && journal.current.current)) return;
    gate.current = true;
    setBusy(true);
    setStatus(undefined);
    setError(undefined);
    try {
      if (!replay) journal.save(intent);
      const receipt = await submitCacheIntent(intent);
      journal.clear();
      setView(receipt.view);
      setConfirmingClear(false);
      setDraft(undefined);
      setStatus(receipt.cleared ? `已清除 ${receipt.cleared.entries} 条缓存。需要时会重新计算。` : '缓存设置已保存。');
      // A replay can return an older receipt. Refresh the current revision and counters.
      setLoading(true);
      setRefresh(value => value + 1);
    } catch (error) {
      if (error instanceof ApiError && [400, 401, 403, 404, 409, 422, 501].includes(error.status)
        && !['INVALID_RESPONSE', 'IDEMPOTENCY_CONFLICT'].includes(error.code)) {
        try { journal.clear(); } catch { setError('无法更新浏览器中的操作记录。请重新打开设置，核对当前状态。'); return; }
        setConfirmingClear(false);
        if (error.status === 409) {
          setView(undefined);
          setDraft(undefined);
        }
      }
      setError(problem(error));
    } finally {
      gate.current = false;
      setBusy(false);
    }
  }

  const pending = journal.current.current;
  const settings = view?.settings;
  const fields = draft && draft.revision === settings?.revision ? draft
    : { enabled: settings?.enabled ?? false, capacity: settings ? String(settings.max_bytes / cacheMiB) : '' };
  const maxBytes = parseCacheCapacity(fields.capacity);
  const actions = canWrite && settings ? {
    busy: busy || loading,
    blocked: Boolean(pending || journal.error),
    ...fields,
    invalidCapacity: maxBytes === null,
    setEnabled: (enabled: boolean) => setDraft({ ...fields, enabled, revision: settings.revision }),
    setCapacity: (capacity: string) => setDraft({ ...fields, capacity, revision: settings.revision }),
    canSave: maxBytes !== null && (maxBytes !== settings.max_bytes || fields.enabled !== settings.enabled),
    save: () => { if (maxBytes !== null) void submit({ kind: 'save', body: { enabled: fields.enabled, max_bytes: maxBytes, expected_revision: settings.revision, idempotency_key: crypto.randomUUID() } }); },
    confirmingClear,
    setConfirmingClear,
    clear: () => void submit({ kind: 'clear', body: { confirm: true, expected_revision: settings.revision, idempotency_key: crypto.randomUUID() } }),
    confirmOriginal: pending ? () => void submit(pending, true) : undefined,
  } : undefined;
  return <div id="computation-cache" ref={anchor} tabIndex={-1}>
    <ComputationCacheView
      summary={view ? {
        ...view.stats, enabled: view.settings.enabled, capacityBytes: view.settings.max_bytes,
        usageBytes: view.stats.bytes, location: '本地平台数据库', eligible: view.eligible.map(name => cacheKindLabel[name]),
      } : undefined}
      loading={loading}
      error={journal.error?.message ?? error}
      status={status}
      refresh={() => { if (!gate.current) { setLoading(true); setRefresh(value => value + 1); } }}
      actions={actions}
    />
  </div>;
}
