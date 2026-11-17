import { useEffect, useRef, useState } from 'react';
import { ApiError, post } from './api';
import type { RecordData } from './api';
import { ErrorBox, Notice, Panel, useResource } from './components';
import { NativeDateInput } from './native-date-input';
import { record } from './native-research-api';
import { definitelyRejected } from './ux-mutation';
import { ResultPreviewLink } from './result-preview';
import { savedResultTitle } from './backtest-presentation';
import { exportManagedResult } from './result-managed-export';

const kinds = { all: '全部结果', backtest: '回测', research: '数据与因子研究', ai: 'AI 报告', model: '模型训练', imported: '迁入报告' };
const sources = { '': '全部来源', tasks: '策略与手动研究', daily: '日线回测', research: '专业研究工具', jobs: '任务结果', autonomous: '自主研究', migration: '从旧电脑迁入' };
const availability: Record<string, string> = {
  RESULT_LOCAL_ONLY: '这份报告仅可在本机工作台查看。', RESULT_EXPORT_UNAVAILABLE: '当前报告不支持导出。', RESULT_DELETED: '结果在回收站。',
};
type Action = 'rename' | 'delete' | 'restore';
type Target = { result_id: string; expected_revision: number };
const actions: Record<Action, string> = { rename: '改名', delete: '移入回收站', restore: '恢复' };
const localTime = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
function SavedTime({ value }: { value: unknown }) {
  if (typeof value !== 'string' || !value) return <small>保存时间未提供</small>;
  const zoned = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
  const time = zoned ? Date.parse(value) : NaN;
  const displayTime = Number.isFinite(time) ? localTime.format(time) : '保存时间待核对';
  return <details style={{ position: 'relative', zIndex: 1 }}>
    <summary aria-label={`${displayTime}，查看原始保存时间`}><time dateTime={value}>{displayTime}</time></summary>
    <small>原始保存时间：{value}</small>
  </details>;
}

export function ResultCatalog({ canWrite, routeKind, manageId, changed }: {
  canWrite: boolean; routeKind: string | null; manageId: string | null; changed: () => void;
}) {
  const [kind, setKind] = useState(routeKind && Object.hasOwn(kinds, routeKind) ? routeKind : 'all');
  const [source, setSource] = useState('');
  const [state, setState] = useState('active');
  const [text, setText] = useState('');
  const [start, setStart] = useState(''), [end, setEnd] = useState('');
  const [filter, setFilter] = useState({ q: '', from_date: '', to_date: '' });
  const [offset, setOffset] = useState(0), [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<Target[]>([]);
  const [action, setAction] = useState<{ kind: Action; targets: Target[]; key: string; submittedName?: string } | null>(null);
  const [name, setName] = useState('');
  const [writing, setWriting] = useState(false), [exporting, setExporting] = useState('');
  const [error, setError] = useState<unknown>(null), [message, setMessage] = useState('');
  const exporter = useRef<AbortController | null>(null);
  useEffect(() => () => exporter.current?.abort(), []);
  useEffect(() => {
    if (routeKind && Object.hasOwn(kinds, routeKind)) { setKind(routeKind); setOffset(0); setSelected([]); }
  }, [routeKind]);
  const params = new URLSearchParams({ kind, state, limit: '50', offset: String(offset) });
  if (source) params.set('source', source);
  for (const [key, value] of Object.entries(filter)) if (value) params.set(key, value);
  const page = useResource<RecordData>('/results?' + params, revision);
  const direct = useResource<RecordData>(manageId ? '/results/' + encodeURIComponent(manageId) + '/management' : null, revision);
  const rows = Array.isArray(page.data?.items) ? page.data.items.map(record) : [];
  function resetPage() { setOffset(0); setSelected([]); }
  function target(row: RecordData): Target | null {
    return row.can_manage === true && typeof row.result_id === 'string' && typeof row.revision === 'number'
      ? { result_id: row.result_id, expected_revision: row.revision } : null;
  }
  function prepare(next: Action, targets: Target[], currentName = '') {
    if (!canWrite || writing || action || !targets.length) return;
    setName(currentName); setError(null); setMessage('');
    setAction({ kind: next, targets, key: crypto.randomUUID() });
  }
  async function apply() {
    if (!action || !canWrite || writing) return;
    const { kind: next, targets, key } = action;
    const submittedName = action.submittedName ?? name.trim();
    if (next === 'rename') setAction({ ...action, submittedName });
    setWriting(true); setError(null);
    try {
      const one = targets[0];
      const result = targets.length === 1
        ? await post<RecordData>('/results/' + encodeURIComponent(one.result_id) + '/lifecycle', {
            action: next, expected_revision: one.expected_revision, idempotency_key: key,
            ...(next === 'rename' ? { name: submittedName } : {}),
          })
        : await post<RecordData>('/results/lifecycle', { action: next, targets, idempotency_key: key });
      const rows = targets.length === 1 ? [result] : Array.isArray(result.items) ? result.items.map(record) : [];
      if (rows.length !== targets.length || !targets.every(item => rows.some(row =>
        row.result_id === item.result_id && row.revision === item.expected_revision + 1 &&
        row.deleted === (next === 'delete') && (next !== 'rename' || row.name === submittedName)))) {
        throw new Error('还不能确认所选报告均已处理。请再次点击确认，核对这次操作。');
      }
      setAction(null); setSelected([]); setRevision(value => value + 1); changed();
      setMessage(actions[next] + '完成，共 ' + rows.length + ' 份。');
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        setAction(null); setSelected([]); setRevision(value => value + 1);
      } else if (definitelyRejected(cause) && next === 'rename') {
        setAction({ ...action, submittedName: undefined, key: crypto.randomUUID() });
      }
      setError(cause);
    } finally { setWriting(false); }
  }
  async function download(row: RecordData, label: string) {
    if (exporter.current || typeof row.result_id !== 'string') return;
    const controller = new AbortController();
    exporter.current = controller; setExporting(row.result_id); setError(null);
    try { await exportManagedResult(row.result_id, label, controller.signal); }
    catch (cause) { if (!controller.signal.aborted) setError(cause); }
    finally {
      exporter.current = null;
      if (!controller.signal.aborted) setExporting('');
    }
  }
  const lifecycle = (row: RecordData, label: string) => {
    const item = target(row);
    if (!canWrite || !item) return null;
    return <>
      {row.deleted !== true && <button type="button" disabled={!!action} onClick={() => prepare('rename', [item], label)}>改名</button>}
      <button type="button" disabled={!!action} onClick={() => prepare(row.deleted === true ? 'restore' : 'delete', [item])}>
        {row.deleted === true ? '恢复' : '移入回收站'}
      </button>
    </>;
  };
  return <Panel title="我的结果">
    <p>点报告名称可先看摘要。<a href="#intelligent?research_tab=autonomous">查看自主研究步骤和尝试记录 →</a></p>
    <div className="form-actions">
      <button type="button" aria-pressed={state === 'active'} onClick={() => { setState('active'); resetPage(); }}>已保存结果</button>
      <button type="button" aria-pressed={state === 'deleted'} onClick={() => { setState('deleted'); resetPage(); }}>结果回收站</button>
      <button type="button" className="text-button" onClick={() => { setSelected([]); setRevision(n => n + 1); }}>刷新结果</button>
    </div>
    <form onSubmit={event => { event.preventDefault(); setFilter({ q: text.trim(), from_date: start, to_date: end }); resetPage(); }}>
      <label>搜索结果<input value={text} onChange={event => setText(event.target.value)} placeholder="名称、报告编号或任务编号" maxLength={160} /></label>
      <div className="v434-date-range">
        <label>最早保存日期（UTC）<NativeDateInput value={start} onChange={event => setStart(event.target.value)} /></label>
        <label>最晚保存日期（UTC）<NativeDateInput value={end} onChange={event => setEnd(event.target.value)} /></label>
      </div>
      <button disabled={Boolean(start && end && end < start)}>搜索</button>
    </form>
    <label>结果类别<select value={kind} onChange={event => { setKind(event.target.value); resetPage(); }}>
      {Object.entries(kinds).map(([value, title]) => <option key={value} value={value}>{title}</option>)}
    </select></label>
    <label>结果来源<select value={source} onChange={event => { setSource(event.target.value); resetPage(); }}>
      {Object.entries(sources).map(([value, title]) => <option key={value} value={value}>{title}</option>)}
    </select></label>
    {manageId && <section aria-label="管理所选报告">
      {direct.error != null ? <ErrorBox error={direct.error} /> : direct.data?.result_id === manageId &&
        <><h3>{String(direct.data.name)}</h3>{lifecycle(direct.data, String(direct.data.name))}</>}
    </section>}
    {canWrite && selected.length > 0 && <button type="button" disabled={!!action}
      onClick={() => prepare(state === 'deleted' ? 'restore' : 'delete', selected)}>
      {state === 'deleted' ? '恢复' : '移入回收站'}：所选 {selected.length} 份
    </button>}
    {action && <fieldset>
      <legend>确认{actions[action.kind]}</legend>
      {action.kind === 'rename'
        ? <label>报告新名称<input value={name} disabled={writing || action.submittedName !== undefined} onChange={event => setName(event.target.value)} maxLength={160} /></label>
        : <p>共 {action.targets.length} 份报告。{action.kind === 'delete' ? '移入回收站后可以恢复，原数据仍保留。' : '恢复后可以重新打开报告。'}{action.targets.length > 1 && '若任何一份处理失败，这次不会更改任何报告。'}</p>}
      <button type="button" disabled={writing || action.kind === 'rename' && !name.trim()} onClick={() => void apply()}>确认{actions[action.kind]}</button>
      <button type="button" disabled={writing} onClick={() => setAction(null)}>返回</button>
    </fieldset>}
    {message && <p role="status">{message}</p>}
    {error != null && <ErrorBox error={error} />}
    {page.error != null ? <ErrorBox error={page.error} /> : page.loading ? <p role="status">正在读取报告列表…</p>
      : !rows.length && <Notice>{state === 'deleted' ? '未找到已移入回收站的报告。可清空搜索或调整筛选。' : '未找到报告。可清空搜索、调整筛选，或前往任务记录查看进度。'}</Notice>}
    <div className="result-grid">{rows.map(row => {
      const id = String(row.result_id), label = typeof row.name === 'string' && row.name ? row.name : savedResultTitle(row);
      const item = target(row);
      return <article className="result-card" key={id}>
        {canWrite && item && <label><input type="checkbox" aria-label={`选择报告 ${label}`} checked={selected.some(value => value.result_id === id)}
          disabled={!!action} onChange={event => setSelected(previous => event.target.checked
            ? [...previous, item] : previous.filter(value => value.result_id !== id))} /></label>}
        {row.deleted !== true && row.can_read !== false
          ? <ResultPreviewLink resultId={id} title={label}>{label}</ResultPreviewLink> : <strong>{label}</strong>}
        {row.source === 'migration' && <small>迁入报告</small>}
        <SavedTime value={row.created_at} />
        {row.deleted !== true && row.can_export !== false &&
          <button className="text-button" type="button" disabled={Boolean(exporting)} onClick={() => void download(row, label)}>
            {exporting === id ? '正在导出…' : '导出报告'}
          </button>}
        {row.deleted !== true && (row.can_read === false || row.can_export === false) &&
          <small>{availability[String(row.reason_code)] ?? (row.can_read === false ? '当前权限无法读取报告。' : '当前报告不可导出。')}</small>}
        {lifecycle(row, label)}
      </article>;
    })}</div>
    <div className="pagination">
      <button type="button" disabled={!offset || page.loading} onClick={() => setOffset(n => Math.max(0, n - 50))}>上一页</button>
      <button type="button" disabled={page.loading || !page.data || offset + 50 >= Number(page.data.total)} onClick={() => setOffset(n => n + 50)}>下一页</button>
      <a href="#jobs">查看任务记录 →</a>
    </div>
  </Panel>;
}
