import { EquityChart } from './research-visuals';
import { useEffect, useState } from 'react';
import { request } from './api';
import { seriesPath } from './history-research-adapter';
import { ErrorBox, Notice } from './components';
import type { RecordData } from './api';
// Internal projection of FA015 r03 whole-range summary, separate from exact series pages.
export type FullRangeOverview = {
  points: RecordData[];
  total: number;
  first: string;
  last: string;
  fullRange: boolean;
  method: string;
};
export async function loadFullEquity(run: string, overview: FullRangeOverview, signal: AbortSignal) {
  if (!run || !Number.isSafeInteger(overview.total) || overview.total < 1 || overview.total > 9000)
    throw new Error('曲线记录总数无效或超出有界显示范围。');
  const points: RecordData[] = [], limit = 1000;
  for (let offset = 0; offset < overview.total; offset += limit) {
    signal.throwIfAborted();
    const page = await request<RecordData>(seriesPath(run, 'equity_curve', offset, limit), { signal });
    signal.throwIfAborted();
    if (page.run_id !== run || page.kind !== 'equity_curve' || page.offset !== offset || page.limit !== limit ||
        page.total !== overview.total || !Array.isArray(page.items) ||
        page.items.length !== Math.min(limit, overview.total - offset) ||
        page.items.some((row) => !row || typeof row !== 'object' || Array.isArray(row)))
      throw new Error('曲线分页与当前结果或总数不匹配，未绘制局部或混合历史。');
    points.push(...page.items as RecordData[]);
  }
  if (points[0]?.at !== overview.first || points.at(-1)?.at !== overview.last)
    throw new Error('完整曲线首尾与保存摘要不匹配，未绘图。');
  return points;
}
export function FullRangeEquity({ overview, run }: { overview: FullRangeOverview | null; run: string }) {
  const [refresh, setRefresh] = useState(0);
  const [state, setState] = useState<{ key: string; points?: RecordData[]; error?: unknown } | null>(null);
  const total = overview?.total, first = overview?.first, last = overview?.last;
  const key = JSON.stringify([run, total, first, last]);
  useEffect(() => {
    const controller = new AbortController();
    setState(null);
    if (overview?.fullRange) void loadFullEquity(run, overview, controller.signal).then(
      (points) => { if (!controller.signal.aborted) setState({ key, points }); },
      (error: unknown) => { if (!controller.signal.aborted) setState({ key, error }); },
    );
    return () => controller.abort();
  }, [run, total, first, last, overview?.fullRange, refresh, key]);
  if (!overview) return <Notice>正在读取整个结果范围的曲线概览；不会把第一页当作完整历史。</Notice>;
  if (
    !overview.fullRange ||
    !overview.points.length ||
    overview.points[0].at !== overview.first ||
    overview.points.at(-1)?.at !== overview.last
  )
    return <Notice>当前返回的是局部曲线或范围不匹配，尚不能显示为完整历史概览。</Notice>;
  if (state?.key !== key) return <Notice>正在读取全部 {overview.total} 条原始估值；未用概览或旧任务替代。</Notice>;
  if (state.error) return <ErrorBox error={state.error} retry={() => setRefresh((n) => n + 1)} />;
  if (!state.points) return <Notice>正在读取完整曲线…</Notice>;
  return (
    <>
      <p className="muted">
        完整结果范围：{overview.first} 至 {overview.last}，原始估值 {overview.total} 条；完整曲线显示{' '}
        {state.points.length} 点。收益与回撤仍取原报告。
      </p>
      <p className="muted">在图内滚轮缩放，或将下方滑块端点向内拖动；拖动图内或滑块选区用于平移，全范围时不会缩小窗口。</p>
      <EquityChart value={state.points} label="账户权益 / USD · 全部原始估值" />
    </>
  );
}
