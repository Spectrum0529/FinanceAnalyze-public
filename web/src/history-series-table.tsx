import { useState } from 'react';
import type { RecordData } from './api';
import { useResource, ErrorBox, Notice } from './components';
import type { Column } from './components';
import { ServerRows } from './research-visuals';
import { seriesPath } from './history-research-adapter';
export function HistorySeriesTable({
  run,
  kind,
  columns,
  empty,
}: {
  run: string;
  kind: 'equity_curve' | 'trades' | 'decisions' | 'orders' | 'rejections' | 'unsubmitted_outputs';
  columns: Column[];
  empty: string;
}) {
  const [offset, setOffset] = useState(0),
    [refresh, setRefresh] = useState(0);
  const limit = 25;
  const { data, error, loading } = useResource<RecordData>(
    seriesPath(run, kind, offset, limit),
    refresh,
  );
  if (loading) return <Notice>正在读取这页原始记录…</Notice>;
  if (error) return <ErrorBox error={error} retry={() => setRefresh((n) => n + 1)} />;
  if (
    !data ||
    data.run_id !== run ||
    data.kind !== kind ||
    data.offset !== offset ||
    data.limit !== limit ||
    !Array.isArray(data.items) ||
    data.items.length > limit ||
    data.items.some((row) => !row || typeof row !== 'object' || Array.isArray(row)) ||
    !Number.isSafeInteger(data.total) ||
    Number(data.total) < 0 ||
    data.items.length !== Math.min(limit, Math.max(0, Number(data.total) - offset))
  )
    return <ErrorBox error={new Error('分页记录与当前结果或页码不匹配，未混入其他结果。')} />;
  return (
    <>
      <ServerRows value={data.items} columns={columns} empty={empty} />
      <div className="pagination">
        <small>
          原始记录共 {String(data.total)} 条 · 当前第 {offset / limit + 1} 页；保留服务原始精度
        </small>
        <div>
          <button
            className="secondary"
            disabled={!offset}
            onClick={() => setOffset((n) => Math.max(0, n - limit))}
          >
            上一页原始记录
          </button>
          <button
            className="secondary"
            disabled={offset + limit >= Number(data.total)}
            onClick={() => setOffset((n) => n + limit)}
          >
            下一页原始记录
          </button>
        </div>
      </div>
    </>
  );
}
