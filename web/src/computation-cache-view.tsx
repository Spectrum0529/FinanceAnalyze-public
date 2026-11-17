import { useId } from 'react';
import { Panel } from './components';
import { cacheLimitMiB, cacheMiB } from './computation-cache-api';

type CacheSummary = {
  enabled: boolean;
  capacityBytes: number;
  usageBytes: number;
  entries: number;
  hits: number;
  misses: number;
  bypasses: number;
  evictions: number;
  location: string;
  eligible: string[];
};

type CacheActions = {
  busy: boolean;
  enabled: boolean;
  capacity: string;
  invalidCapacity: boolean;
  setEnabled: (enabled: boolean) => void;
  setCapacity: (capacity: string) => void;
  canSave: boolean;
  save: () => void;
  confirmingClear: boolean;
  setConfirmingClear: (confirming: boolean) => void;
  clear: () => void;
  blocked?: boolean;
  confirmOriginal?: () => void;
};

const number = new Intl.NumberFormat('zh-CN');
const mib = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
function size(bytes: number) {
  return bytes > 0 && bytes < cacheMiB / 100 ? `${number.format(bytes)} 字节` : `${mib.format(bytes / cacheMiB)} MiB`;
}

export function ComputationCacheView({ summary, loading, error, status, refresh, actions }: {
  summary?: CacheSummary;
  loading: boolean;
  error?: string;
  status?: string;
  refresh: () => void;
  actions?: CacheActions;
}) {
  const confirmationId = useId();
  const capacityHintId = useId();
  return (
    <Panel title="计算缓存">
      <p>重复运行相同的计算时，可以直接使用之前算出的结果。</p>
      {loading && <p role="status">正在读取缓存设置和用量…</p>}
      {error && <p role="alert">{error}</p>}
      {status && <p role="status">{status}</p>}
      {summary && <>
        <dl>
          <div><dt>当前状态</dt><dd>{summary.enabled ? '已启用' : '已停用'}</dd></div>
          <div><dt>缓存位置</dt><dd style={{ overflowWrap: 'anywhere' }}>{summary.location}</dd></div>
          <div><dt>使用量</dt><dd>{size(summary.usageBytes)} / {size(summary.capacityBytes)}，{number.format(summary.entries)} 条</dd></div>
          <div><dt>缓存统计</dt><dd>已复用 {number.format(summary.hits)} · 无可用缓存 {number.format(summary.misses)} · 跳过缓存 {number.format(summary.bypasses)} · 自动清理 {number.format(summary.evictions)}</dd></div>
        </dl>
        <p>{summary.eligible.length ? `可缓存的计算类型包括${summary.eligible.join('、')}。` : '暂未提供可缓存的计算类型。'}</p>
        {actions && <>
          {actions.blocked && <p role="status">上次操作结果待核对。请先确认原请求。</p>}
          {actions.confirmOriginal && <button type="button" disabled={actions.busy} onClick={actions.confirmOriginal}>确认上次操作结果</button>}
          <form onSubmit={event => { event.preventDefault(); if (actions.canSave && !actions.busy) actions.save(); }}>
            <fieldset disabled={actions.busy || actions.blocked}>
              <legend>缓存设置</legend>
              <label><input type="checkbox" checked={actions.enabled} onChange={event => actions.setEnabled(event.target.checked)} />启用计算缓存</label>
              <label>
                容量上限（1–{cacheLimitMiB} MiB）
                <input type="number" min="1" max={cacheLimitMiB} step="any" required value={actions.capacity}
                  aria-invalid={actions.invalidCapacity} aria-describedby={actions.invalidCapacity ? capacityHintId : undefined}
                  onChange={event => actions.setCapacity(event.target.value)} />
              </label>
              {actions.invalidCapacity && <p id={capacityHintId}>请输入 1 到 {cacheLimitMiB} 之间能换算成整数字节的数值，例如 64。</p>}
              <button type="submit" disabled={!actions.canSave}>保存缓存设置</button>
            </fieldset>
          </form>
          <button type="button" disabled={actions.busy || actions.blocked} aria-expanded={actions.confirmingClear} aria-controls={confirmationId} onClick={() => actions.setConfirmingClear(!actions.confirmingClear)}>
            {actions.confirmingClear ? '取消清除' : '清除计算缓存'}
          </button>
          <div id={confirmationId} hidden={!actions.confirmingClear}>
            <p>清除后，相关任务需要重新计算。保存的数据、报告和研究记录仍可打开。</p>
            <div className="form-actions">
              <button type="button" disabled={actions.busy || actions.blocked} onClick={actions.clear}>确认清除计算缓存</button>
            </div>
          </div>
        </>}
      </>}
      <button type="button" className="text-button" disabled={loading || actions?.busy} onClick={refresh}>刷新缓存统计</button>
    </Panel>
  );
}
