import { useEffect, useMemo, useRef, useState } from 'react';
import { init, use } from 'echarts/core';
import type { EChartsOption, EChartsType } from 'echarts';
import { LineChart } from 'echarts/charts';
import { AriaComponent, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
import type { RecordData } from './api';
import { formatAmount } from './display-amount';
import { amountWithCurrency, currencyLabel } from './report-currency';
import type { ReportCurrency } from './report-currency';
import { Notice } from './components';

use([LineChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, SVGRenderer]);

export type EngineChartSeries = { id: string; label: string; points: readonly RecordData[]; color?: string };
export type ChartStatus = 'ready' | 'loading' | 'cancelled' | 'failed';
type NativeChart = Pick<EChartsType, 'setOption' | 'resize' | 'dispose'> & {
  on?: (event: 'datazoom', listener: (value: unknown) => void) => void;
};
const record = (value: unknown): RecordData =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as RecordData : {};
const dateLabel = (value: unknown) => {
  const instant = new Date(Number(value));
  return Number.isFinite(instant.getTime()) ? instant.toISOString().slice(0, 10) : '日期未提供';
};
// Rich-text display formatting only; rawEquity retains the exact saved input, with no return calculation.
export function equityTooltip(value: unknown, currency: ReportCurrency | null = 'USD'): string {
  return (Array.isArray(value) ? value : [value]).map((item) => {
    const param = record(item), data = record(param.data);
    return `${String(param.seriesName ?? '账户总额')}\n${String(data.rawAt ?? '日期未提供')}\n${data.rawEquity == null ? '估值未提供' : amountWithCurrency(data.rawEquity, currency ?? undefined)}`;
  }).join('\n\n');
}

export function projectEngineChart(series: readonly EngineChartSeries[], currency: ReportCurrency | null = 'USD') {
  if (series.length > 4 || new Set(series.map((s) => s.id)).size !== series.length)
    throw new Error('最多可同时比较四条已保存的账户总额曲线。');
  let finitePoints = 0, totalPoints = 0, first = Infinity, last = -Infinity;
  const lines = series.map((source, index) => {
    if (!source.id || !Array.isArray(source.points) || source.points.length > 100000)
      throw new Error('曲线记录不完整或超过显示范围，请查看有界概览和原始分页。');
    let previous = -Infinity, valid = 0;
    const data = source.points.map((row) => {
      const at = row?.at;
      const time = typeof at === 'string' ? Date.parse(at) : NaN;
      const calendar = Date.parse(String(at).slice(0, 10) + 'T00:00:00Z');
      if (!Number.isFinite(time) || !Number.isFinite(calendar) ||
        !/^\d{4}-\d{2}-\d{2}(?:$|T)/.test(String(at)) ||
        new Date(calendar).toISOString().slice(0, 10) !== String(at).slice(0, 10) || time < previous)
        throw new Error('曲线日期缺失或顺序异常，未排序、补值或混入其他记录。');
      previous = time;
      first = Math.min(first, time); last = Math.max(last, time); totalPoints++;
      const raw = row.equity;
      let value: number | null = null;
      if (raw !== null && raw !== undefined && raw !== '') {
        if ((typeof raw !== 'number' && (typeof raw !== 'string' || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw))) || !Number.isFinite(Number(raw)))
          throw new Error('保存的账户总额有无效数值，暂时无法画图。');
        value = Number(raw); finitePoints++; valid++;
      }
      return { value: [time, value], rawAt: at, rawEquity: value === null ? null : raw };
    });
    const label = source.label || `结果${index + 1}`;
    return {
      id: source.id, name: series.some((s, i) => i !== index && s.label === label) ? `${label}（${index + 1}）` : label,
      type: 'line' as const, data, showSymbol: valid === 1, symbolSize: 7,
      connectNulls: false, smooth: false, animation: false,
      ...(source.color ? { itemStyle: { color: source.color }, lineStyle: { color: source.color } } : {}),
    };
  });
  // SVG paint attributes inherit these tokens when the workbench theme changes.
  const ink = 'var(--ink)';
  const axisStyle = {
    nameTextStyle: { color: ink },
    axisLine: { lineStyle: { color: 'var(--muted)' } },
    axisTick: { lineStyle: { color: 'var(--muted)' } },
    splitLine: { lineStyle: { color: 'var(--line)' } },
  };
  const option = {
    animation: false,
    aria: { enabled: true },
    color: ['#377d61', '#527db6', '#bd8645', '#9463a8'],
    grid: { left: 70, right: 24, top: 45, bottom: 75, containLabel: true },
    legend: {
      show: series.length > 1, type: 'scroll', top: 0,
      textStyle: { color: ink }, pageTextStyle: { color: ink }, inactiveColor: 'var(--muted)',
    },
    tooltip: {
      trigger: 'axis', renderMode: 'richText', formatter: (value: unknown) => equityTooltip(value, currency),
      textStyle: { color: ink }, backgroundColor: 'var(--surface-solid)', borderColor: 'var(--muted)',
    },
    xAxis: { ...axisStyle, type: 'time', splitNumber: 5, minInterval: 86400000, name: '日期（UTC）', nameLocation: 'middle', nameGap: 30, axisLabel: { color: ink, formatter: dateLabel, hideOverlap: true } },
    yAxis: { ...axisStyle, type: 'value', scale: true, name: currency === 'USD' ? '账户总额 / USD' : '账户总额 / ' + currencyLabel(currency ?? undefined), axisLabel: { color: ink, formatter: formatAmount } },
    dataZoom: [{ type: 'inside', filterMode: 'none' }, { type: 'slider', filterMode: 'none', bottom: 8, labelFormatter: dateLabel, textStyle: { color: ink } }],
    series: lines,
  } satisfies EChartsOption;
  return { option, finitePoints, totalPoints, first: Number.isFinite(first) ? dateLabel(first) : null, last: Number.isFinite(last) ? dateLabel(last) : null };
}

function watchSize(element: HTMLElement, resize: () => void) {
  if (typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    return () => observer.disconnect();
  }
  window.addEventListener('resize', resize);
  return () => window.removeEventListener('resize', resize);
}

// Lifecycle glue only; rendering/zoom/axes/legend are the actual ECharts implementation.
export function bindEngineChart(
  element: HTMLElement,
  onError: () => void,
  create: (element: HTMLElement) => NativeChart = (node) => init(node, undefined, { renderer: 'svg', locale: 'ZH' }),
  watch: (element: HTMLElement, resize: () => void) => () => void = watchSize,
) {
  let chart: NativeChart | null = null, stopped = false;
  let zoom: { start: number; end: number } | undefined;
  let unwatch = () => {};
  const dispose = () => {
    if (stopped) return;
    stopped = true; unwatch(); chart?.dispose(); chart = null;
  };
  try {
    chart = create(element);
    chart.on?.('datazoom', (event: unknown) => {
      const value = record(event);
      const range = Array.isArray(value.batch) ? record(value.batch[0]) : value;
      if (typeof range.start === 'number' && typeof range.end === 'number' &&
          Number.isFinite(range.start) && Number.isFinite(range.end)) {
        zoom = { start: range.start, end: range.end };
      }
    });
    unwatch = watch(element, () => {
      if (stopped) return;
      try { chart?.resize(); } catch { dispose(); onError(); }
    });
  } catch { dispose(); onError(); }
  return {
    update(option: EChartsOption) {
      if (stopped) return;
      try {
        const configuredZoom = option.dataZoom;
        const next = zoom && Array.isArray(configuredZoom)
          ? { ...option, dataZoom: configuredZoom.map(view => ({ ...view, ...zoom })) } : option;
        chart?.setOption(next, { notMerge: true, lazyUpdate: false });
      }
      catch { dispose(); onError(); }
    },
    dispose,
  };
}

export function EngineChart({ series, status = 'ready', label = '账户总额 / USD' }: {
  series: readonly EngineChartSeries[];
  status?: ChartStatus;
  label?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const binding = useRef<ReturnType<typeof bindEngineChart> | null>(null);
  const [drawError, setDrawError] = useState(false), [retry, setRetry] = useState(0);
  const view = useMemo(() => {
    try { return { ...projectEngineChart(series), error: '' }; }
    catch (error) { return { option: {}, finitePoints: 0, totalPoints: 0, first: null, last: null, error: error instanceof Error ? error.message : '曲线暂时无法绘制。' }; }
  }, [series]);
  const ready = status === 'ready' && !view.error && view.finitePoints > 0;
  useEffect(() => {
    if (!ready || !container.current) return;
    setDrawError(false);
    const current = bindEngineChart(container.current, () => setDrawError(true));
    binding.current = current;
    return () => { current.dispose(); if (binding.current === current) binding.current = null; };
  }, [ready, retry]);
  useEffect(() => { if (ready) binding.current?.update(view.option); }, [ready, view.option, retry]);
  if (status === 'loading') return <Notice>正在读取曲线；不会显示前一次任务的结果。</Notice>;
  if (status === 'cancelled') return <Notice>任务已取消，未绘制本次曲线。</Notice>;
  if (status === 'failed') return <Notice>任务未完成，暂无本次曲线；可在任务记录查看原因。</Notice>;
  if (view.error) return <Notice>{view.error}</Notice>;
  if (!view.finitePoints) return <Notice>暂无合法估值点。未决估值不会填成零。</Notice>;
  return (
    <figure className="equity-chart">
      <figcaption>{label} · {view.first} 至 {view.last} · {view.finitePoints}/{view.totalPoints} 个有效估值点。图表不重算收益，空值保留为缺口。</figcaption>
      {drawError && <Notice>曲线暂时无法绘制，原始记录仍可查看。<button className="text-button" onClick={() => setRetry((n) => n + 1)}>重新绘制</button></Notice>}
      <div ref={container} style={{ width: '100%', height: 300, minWidth: 0 }} role="img" aria-label={`${label}，${view.first} 至 ${view.last}，${series.length} 条曲线`} />
    </figure>
  );
}
