import { useEffect, useState } from 'react';
import { ErrorBox, Notice, Panel } from './components';
import { ResultPreviewLink } from './result-preview';
import type {
  HistoryUiPort,
  ManagedResult,
  ResultCommand,
  ResultOutcome,
} from './history-view-model';
export function HistoryResultManager({
  port,
  canWrite,
}: {
  port: HistoryUiPort;
  canWrite: boolean;
}) {
  const [trash, setTrash] = useState(false),
    [offset, setOffset] = useState(0),
    [tick, setTick] = useState(0),
    [rows, setRows] = useState<ManagedResult[]>([]),
    [total, setTotal] = useState(0);
  const [selected, setSelected] = useState<string[]>([]),
    [loading, setLoading] = useState(true),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState('');
  const [confirm, setConfirm] = useState<{
      kind: ResultCommand['kind'];
      targets: ManagedResult[];
      requestId: string;
    } | null>(null),
    [name, setName] = useState(''),
    [busy, setBusy] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const [guard] = useState(() => ({
    busy: false,
    command: null as ResultCommand | null,
  }));
  const limit = 20;
  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    setLoading(true);
    setRows([]);
    setTotal(0);
    setError(null);
    setSelected([]);
    void port
      .listResults(trash, offset, limit, controller.signal)
      .then(
        (page) => {
          if (alive) {
            if (page.items.some((r) => r.deleted !== trash))
              throw new Error('无法确认这些记录的回收站状态。请刷新。');
            setRows(page.items);
            setTotal(page.total);
          }
        },
        (e) => {
          if (alive && !controller.signal.aborted) setError(e);
        },
      )
      .catch((e) => {
        if (alive) setError(e);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [port, trash, offset, tick]);
  const manageable = rows.filter((r) => r.canManage),
    picked = manageable.filter((r) => selected.includes(r.id));
  const openAction = (kind: ResultCommand['kind'], targets: ManagedResult[]) => {
    if (
      !canWrite ||
      guard.busy ||
      uncertain ||
      !targets.length ||
      targets.some((r) => !r.canManage)
    )
      return;
    setConfirm({
      kind,
      targets: targets.map((r) => ({ ...r })),
      requestId: crypto.randomUUID(),
    });
    setName(kind === 'rename' ? targets[0].name : '');
    setError(null);
  };
  const finish = (outcome: ResultOutcome) => {
    const ids = guard.command?.targets.map((t) => t.id) ?? [];
    const received = [...outcome.completed, ...outcome.failures.map((f) => f.id)];
    if (
      outcome.confirmed &&
      (!ids.length ||
        received.length !== ids.length ||
        new Set(received).size !== received.length ||
        received.some((id) => !ids.includes(id)))
    ) {
      setError(new Error('返回的结果不是本次选择的记录。请检查上次操作。'));
      setUncertain(true);
      return;
    }
    if (!outcome.confirmed) {
      setUncertain(true);
      setNotice('还不能确认处理结果。请点“核对这次操作”；不会重复提交。');
      return;
    }
    setUncertain(false);
    setConfirm(null);
    guard.command = null;
    setSelected(outcome.failures.map((f) => f.id));
    setNotice(
      `已确认完成 ${outcome.completed.length} 项${outcome.failures.length ? `；另有 ${outcome.failures.length} 项未完成：${outcome.failures.map((f) => f.message).join('；')}` : '。'}`,
    );
    setTick((n) => n + 1);
  };
  async function mutate() {
    if (!confirm || !canWrite || guard.busy || guard.command) return;
    const clean = name.trim();
    if (confirm.kind === 'rename' && !clean) {
      setError(new Error('请输入结果名称。'));
      return;
    }
    const command: ResultCommand = {
      requestId: confirm.requestId,
      kind: confirm.kind,
      targets: confirm.targets.map((r) => ({ id: r.id, revision: r.revision })),
      ...(confirm.kind === 'rename' ? { name: clean } : {}),
    };
    guard.busy = true;
    guard.command = command;
    setBusy(true);
    setError(null);
    try {
      finish(await port.changeResults(command));
    } catch (e) {
      setError(e);
      setUncertain(true);
      setNotice('还不能确认是否保存。请先点“核对这次操作”，以免重复修改。');
    } finally {
      guard.busy = false;
      setBusy(false);
    }
  }
  async function reconcile() {
    if (!guard.command || guard.busy) return;
    guard.busy = true;
    setBusy(true);
    setError(null);
    const controller = new AbortController();
    try {
      finish(await port.lookupChange(guard.command.requestId, controller.signal));
    } catch (e) {
      setError(e);
    } finally {
      guard.busy = false;
      setBusy(false);
    }
  }
  return (
    <Panel
      title="我的回测结果"
      action={
        <button
          className="secondary"
          disabled={busy || uncertain || !!confirm}
          onClick={() => setTick((n) => n + 1)}
        >
          刷新结果
        </button>
      }
    >
      <div className="view-tabs" role="group" aria-label="结果范围">
        <button
          className={trash ? 'secondary' : ''}
          disabled={busy || uncertain || !!confirm}
          onClick={() => {
            setTrash(false);
            setOffset(0);
          }}
        >
          结果
        </button>
        <button
          className={trash ? '' : 'secondary'}
          disabled={busy || uncertain || !!confirm}
          onClick={() => {
            setTrash(true);
            setOffset(0);
          }}
        >
          回收站
        </button>
      </div>
      {!canWrite && <Notice>只读账号可以查看获准的报告。改名、回收和恢复需要编辑权限。</Notice>}
      <div className="result-toolbar">
        <label>
          <input
            type="checkbox"
            aria-label="选择本页可管理结果"
            checked={manageable.length > 0 && picked.length === manageable.length}
            disabled={!canWrite || loading || busy || uncertain || !!confirm}
            onChange={(e) => setSelected(e.target.checked ? manageable.map((r) => r.id) : [])}
          />
          选择本页
        </label>
        <span>本页已选 {picked.length} 项</span>
        {canWrite && (
          <button
            className="text-button"
            disabled={!picked.length || busy || uncertain || !!confirm}
            onClick={() => setSelected([])}
          >
            取消本页选择
          </button>
        )}
        {canWrite && (
          <button
            className="secondary"
            disabled={!picked.length || busy || uncertain || !!confirm}
            onClick={() => openAction(trash ? 'restore' : 'delete', picked)}
          >
            {trash ? '恢复所选' : '将所选结果移入回收站'}
          </button>
        )}
      </div>
      {loading ? (
        <p role="status">正在读取保存的结果…</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>选择</th>
                <th>名称 / 股票</th>
                <th>来源</th>
                <th>保存时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`选择结果 ${row.name}`}
                      checked={selected.includes(row.id)}
                      disabled={!canWrite || !row.canManage || busy || uncertain || !!confirm}
                      onChange={(e) =>
                        setSelected((ids) =>
                          e.target.checked ? [...ids, row.id] : ids.filter((id) => id !== row.id),
                        )
                      }
                    />
                  </td>
                  <td>
                    <strong>{row.name || '未命名结果'}</strong>
                    <small className="block">{row.symbol}</small>
                  </td>
                  <td>{row.provider}</td>
                  <td>{row.createdAt}</td>
                  <td>
                    <div className="row-actions">
                      {!trash && (busy || uncertain || confirm
                        ? <button className="text-button" disabled>预览报告</button>
                        : <ResultPreviewLink className="text-button" resultId={`daily:${row.id}`}
                          title={row.name || row.symbol} href={`#results?daily_run=${encodeURIComponent(row.id)}`}>
                          预览报告
                        </ResultPreviewLink>)}
                      {canWrite && row.canManage && (
                        <>
                          <button
                            className="text-button"
                            disabled={busy || uncertain || !!confirm}
                            onClick={() => openAction(trash ? 'restore' : 'rename', [row])}
                          >
                            {trash ? '恢复' : '改名'}
                          </button>
                          {!trash && (
                            <button
                              className="text-button"
                              disabled={busy || uncertain || !!confirm}
                              onClick={() => openAction('delete', [row])}
                            >
                              移入回收站
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && (
            <p className="empty">{trash ? '回收站为空。' : '还没有已保存的回测结果。'}</p>
          )}
        </div>
      )}
      <div className="pagination">
        <small>
          共 {total} 项 · 每页 {limit} 项；批量仅作用于本页已选记录
        </small>
        <div>
          <button
            className="secondary"
            disabled={offset === 0 || loading || busy || uncertain || !!confirm}
            onClick={() => setOffset((n) => Math.max(0, n - limit))}
          >
            上一页结果
          </button>
          <button
            className="secondary"
            disabled={offset + limit >= total || loading || busy || uncertain || !!confirm}
            onClick={() => setOffset((n) => n + limit)}
          >
            下一页结果
          </button>
        </div>
      </div>
      {confirm && (
        <section
          className="action-confirm"
          role="dialog"
          aria-modal="false"
          aria-label={
            confirm.kind === 'delete'
              ? '确认移入回收站'
              : confirm.kind === 'restore'
                ? '确认恢复结果'
                : '修改结果名称'
          }
        >
          <h3>
            {confirm.kind === 'delete'
              ? `将 ${confirm.targets.length} 项结果移入回收站？`
              : confirm.kind === 'restore'
                ? `恢复 ${confirm.targets.length} 项结果？`
                : '修改结果名称'}
          </h3>
          {confirm.kind === 'rename' ? (
            <label>
              <span>新名称</span>
              <input
                value={name}
                maxLength={120}
                disabled={busy || uncertain}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
          ) : (
            <p>
              {confirm.kind === 'delete'
                ? '移入回收站后可以恢复，原行情仍保留。只处理所选结果，可能只成功一部分。请查看每项结果。'
                : '只恢复所选报告，不重新下载或回测。'}
            </p>
          )}
          <p>
            {confirm.targets
              .slice(0, 4)
              .map((r) => r.name || r.symbol)
              .join('、')}
            {confirm.targets.length > 4 ? '等' : ''}
          </p>
          <div className="form-actions">
            <button
              className="secondary"
              disabled={busy || uncertain}
              onClick={() => {
                setConfirm(null);
                guard.command = null;
              }}
            >
              取消
            </button>
            <button disabled={busy || uncertain} onClick={() => void mutate()}>
              {busy
                ? '正在处理…'
                : confirm.kind === 'delete'
                  ? '确认移入回收站'
                  : confirm.kind === 'restore'
                    ? '确认恢复'
                    : '保存名称'}
            </button>
            {uncertain && (
              <button className="secondary" disabled={busy} onClick={() => void reconcile()}>
                核对这次操作
              </button>
            )}
          </div>
        </section>
      )}
      {notice && <Notice>{notice}</Notice>}
      {error != null && <ErrorBox error={error} />}
    </Panel>
  );
}
