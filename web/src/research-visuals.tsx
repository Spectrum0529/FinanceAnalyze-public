import { EngineChart } from './engine-chart';
import type { ChartStatus } from './engine-chart';
import { useState } from 'react';
import { display } from './api';
import type { RecordData } from './api';
import type { Column } from './components';
export function ServerRows({
  value,
  columns,
  empty,
}: {
  value: unknown;
  columns: Column[];
  empty: string;
}) {
  const rows = Array.isArray(value) ? (value as RecordData[]) : [];
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c.key}>{c.render ? c.render(r) : display(r[c.key])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length && <div className="empty">{empty}</div>}
    </div>
  );
}
export function EquityChart({
  value,
  compare = [],
  status = 'ready',
  label = '账户权益 / USD · 服务端估值记录',
}: {
  value: unknown;
  compare?: readonly { label: string; value: unknown }[];
  status?: ChartStatus;
  label?: string;
}) {
  const points = (rows: unknown) => Array.isArray(rows) ? rows as RecordData[] : [];
  return (
    <EngineChart
      label={label}
      status={status}
      series={[
        { id: 'primary', label: '当前结果', points: points(value) },
        ...compare.map((item, index) => ({
          id: 'comparison-' + String(index + 1), label: item.label, points: points(item.value),
        })),
      ]}
    />
  );
}
export function PagedServerRows({
  value,
  columns,
  empty,
  pageSize = 25,
}: {
  value: unknown;
  columns: Column[];
  empty: string;
  pageSize?: number;
}) {
  const [page, setPage] = useState(0);
  const rows = Array.isArray(value) ? value : [],
    size = Math.max(1, Math.min(100, Math.floor(pageSize))),
    current = Math.min(page, Math.max(0, Math.ceil(rows.length / size) - 1));
  return (
    <>
      <ServerRows
        value={rows.slice(current * size, (current + 1) * size)}
        columns={columns}
        empty={empty}
      />
      <div className="pagination">
        <small>
          原始记录共 {rows.length} 条 · 第 {current + 1} 页
        </small>
        <div>
          <button className="secondary" disabled={!current} onClick={() => setPage(current - 1)}>
            上一页成交
          </button>
          <button
            className="secondary"
            disabled={(current + 1) * size >= rows.length}
            onClick={() => setPage(current + 1)}
          >
            下一页成交
          </button>
        </div>
      </div>
    </>
  );
}
