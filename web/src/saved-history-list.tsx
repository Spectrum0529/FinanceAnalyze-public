import { useEffect, useState } from 'react';
import { ErrorBox, Notice, Panel } from './components';
import type { HistoryCoverage, HistoryUiPort } from './history-view-model';
export function SavedHistoryList({
  port,
  onUse,
}: {
  port: HistoryUiPort;
  onUse: (history: HistoryCoverage) => void;
}) {
  const [offset, setOffset] = useState(0),
    [tick, setTick] = useState(0),
    [rows, setRows] = useState<HistoryCoverage[]>([]),
    [total, setTotal] = useState(0),
    [loading, setLoading] = useState(true),
    [error, setError] = useState<unknown>(null);
  const limit = 20;
  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    setLoading(true);
    setRows([]);
    setTotal(0);
    setError(null);
    void port
      .listHistory(offset, limit, controller.signal)
      .then(
        (page) => {
          if (alive) {
            setRows(page.items);
            setTotal(page.total);
          }
        },
        (e) => {
          if (alive && !controller.signal.aborted) setError(e);
        },
      )
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [port, offset, tick]);
  return (
    <Panel
      title="已保存的历史行情"
      action={
        <button className="secondary" onClick={() => setTick((n) => n + 1)}>
          刷新行情
        </button>
      }
    >
      <Notice>
        下表列出实际有数据的日期。回测会优先使用已保存的行情；要更新，可在回测设置中选择重新下载。
      </Notice>
      {loading ? (
        <p role="status">正在读取已保存行情…</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>股票 / 来源</th>
                <th>实际保存日期</th>
                <th>记录数和日期说明</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <strong>{row.symbol}</strong>
                    <small className="block">{row.provider}</small>
                  </td>
                  <td>
                    {row.start ?? '未取得'} 至 {row.end ?? '未取得'}
                  </td>
                  <td>
                    {row.rows} 条<small className="block">{row.message}</small>
                  </td>
                  <td>
                    <button className="text-button" onClick={() => onUse(row)}>
                      用于回测
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && (
            <p className="empty">
              还没有保存的行情。可开始回测让系统准备，或选择“只下载或补齐行情”。
            </p>
          )}
        </div>
      )}
      <div className="pagination">
        <small>共 {total} 项</small>
        <div>
          <button
            className="secondary"
            disabled={!offset || loading}
            onClick={() => setOffset((n) => Math.max(0, n - limit))}
          >
            上一页历史
          </button>
          <button
            className="secondary"
            disabled={offset + limit >= total || loading}
            onClick={() => setOffset((n) => n + limit)}
          >
            下一页历史
          </button>
        </div>
      </div>
      {error != null && <ErrorBox error={error} />}
    </Panel>
  );
}
