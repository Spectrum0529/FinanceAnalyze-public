import { useEffect, useState } from 'react';
import type { ReactNode, FormEvent } from 'react';
import { ApiError, display, safeJson } from './api';
import type { Json, PageData, RecordData } from './api';
import { AreaHelp, plainError } from './area-help';
import { observeResource } from './resource-reader';

export function ErrorBox({ error, retry }: { error: unknown; retry?: () => void }) {
  const e =
    error instanceof ApiError
      ? error
      : new ApiError(0, 'CLIENT_ERROR', error instanceof Error ? error.message : '操作未完成');
  return (
    <div className="message error" role="alert">
      <strong>
        {e.status === 403 ? '访问受限' : e.status === 404 ? '未找到' : '操作未完成'}
      </strong>
      <p>{plainError(e.message, e.status, e.code)}</p><details><summary>查看错误原文和编号</summary><p>{e.message}</p>
      <small>
        {e.code}
        {e.requestId && ` · 请求 ${e.requestId}`}
      </small>
      {e.details && <pre>{safeJson(e.details)}</pre>}</details>
      {retry && (
        <button className="secondary" onClick={retry}>
          重新加载
        </button>
      )}
      {e.status === 401 && (
        <button className="secondary" onClick={() => window.location.reload()}>
          刷新页面并重新登录
        </button>
      )}
    </div>
  );
}
export function Notice({ children }: { children: ReactNode }) {
  return <div className="message notice">{children}</div>;
}
export function Status({ value }: { value: Json | undefined }) {
  const s = display(value);
  return (
    <span
      className={`status ${/failed|error|blocked|rejected|TIMED_OUT|NOT_CONFIGURED|UNVERIFIED|NOT_VERIFIED|degraded|不可用|阻断|未可信/i.test(s) ? 'bad' : /succeeded|verified|completed|active|LOCAL_RECEIVER_ACCEPTED|可用|通过/i.test(s) ? 'good' : ''}`}
    >
      {Object.hasOwn(statusLabels, s) ? statusLabels[s] : s}
    </span>
  );
}
const statusLabels: Record<string, string> = {
  QUEUED: '排队中', RUNNING: '运行中', IN_FLIGHT: '运行中', PAUSED: '已暂停',
  SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消', TIMED_OUT: '已超时',
  ACTIVE: '使用中', DISABLED: '已停用', CONFIGURED: '已保存设置', NOT_CONFIGURED: '尚未设置',
  VERIFIED: '已核实', VALIDATED: '已通过检查', UNVERIFIED: '尚未核实', NOT_VERIFIED: '尚未核实', UNKNOWN: '未知',
  BLOCKED: '暂无法继续', REJECTED: '未通过', UNAVAILABLE: '暂不可用',
  STOPPED: '已停止', COMPLETED: '已完成', PENDING: '等待处理',
  REVOKED: '已撤销', EXPIRED: '已过期', DEGRADED: '部分功能暂不可用',
  LOCAL_RECEIVER_ACCEPTED: '本机已收到', CANCELLING: '正在取消',
};
export function useResource<T>(
  path: string | null,
  refresh: number = 0,
  polling: boolean | number = false,
  stopPolling?: (data: T) => boolean,
) {
  const [data, setData] = useState<T | null>(null),
    [error, setError] = useState<unknown>(null),
    [loading, setLoading] = useState(true);
  useEffect(
    () => {
      if (path === null) {
        setData(null);
        setError(null);
        setLoading(false);
        return;
      }
      return observeResource<T>(
        path,
        {
          begin: () => {
            setLoading(true);
            setData(null);
          },
          data: (next) => {
            setData(next);
            setError(null);
          },
          error: setError,
          settled: () => setLoading(false),
        },
        polling,
        stopPolling,
      );
    },
    [path, refresh, polling, stopPolling],
  );
  return { data, error, loading };
}
export function JsonView({ value }: { value: unknown }) {
  return <pre className="json-view">{safeJson(value)}</pre>;
}
export function ResultSummary({
  value,
  statusLabel = '记录状态',
  omitStatus = false,
  diagnosticLabel = '服务端诊断详情',
  valueLabels,
}: {
  value: unknown;
  statusLabel?: string;
  omitStatus?: boolean;
  diagnosticLabel?: string;
  valueLabels?: Record<string, Record<string, string>>;
}) {
  const result =
    value && typeof value === 'object' && !Array.isArray(value) ? (value as RecordData) : null;
  const labels: Record<string, string> = {
    job_id: '后台任务',
    run_id: '运行标识',
    parent_run_id: '重试父运行',
    status: statusLabel,
    mode: '运行模式',
    source_id: '来源',
    dataset_id: '数据版本',
    snapshot_id: '快照',
    config_id: '冻结配置',
    instance_id: '策略实例',
    model_id: '模型文件',
    account_id: '账户',
    backup_id: '备份',
    schedule_id: '调度计划',
    collection_id: '采集标识',
    outcome: '投递结果',
    http_status: '接收端 HTTP 状态',
    enabled: '已启用',
    configured: '已配置',
    configuration_effect: '配置生效方式',
    automatic_delivery: '自动投递',
    provider_verification: '供应商验证',
    name: '名称',
    handler: '处理器',
    revision: '修订',
    rule: '调度规则',
    interval_seconds: '间隔 / 秒',
    next_due_at: '下次触发 / UTC',
    id: '标识',
    version: '版本',
    sha256: '内容摘要',
    content_hash: '内容摘要',
    package_hash: '策略包摘要',
    sample_hash: '样本摘要',
    train_cutoff: '训练截止 / UTC',
    model_hash: '模型摘要',
    created_at: '创建时间 / UTC',
    manifest_sha256: '备份清单摘要',
    restore_id: '隔离恢复标识',
    bytes: '文件总字节数',
    clean: '对账一致',
    session_restore: '会话恢复策略',
    trading_resume: '交易恢复策略',
    appended: '追加记录',
    can_import: '可导入',
    valid_rows: '有效行',
    row_count: '输入行',
    comparable: '可比较',
    equal: '一致',
    configuration: '配置状态',
    dependency: '依赖状态',
    sdk_version: '实际SDK版本',
    public_endpoint_observed: '已观测公开端点',
    sample_rows: '测试样本行数',
    coverage: '覆盖范围',
    use_basis_state: '用途依据状态',
    error_code: '错误代码',
    requests: '实际请求数',
    authentication: '认证',
    permission: '权限',
    fields: '字段',
    history: '历史范围',
    license: '许可',
    scope: '样本范围',
    reference: '不可变引用',
    message: '说明',
    emergency_stop: '紧停状态',
  };
  const fields = result
    ? Object.keys(labels).filter(
        (k) => !(omitStatus && k === 'status') && result[k] !== undefined && result[k] !== null,
      )
    : [];
  return (
    <>
      {fields.length ? (
        <dl className="key-value">
          {fields.map((k) => {
            const raw = result![k];
            const localized = typeof raw === 'string' ? valueLabels?.[k]?.[raw] : undefined;
            return (
              <div className="detail-pair" key={k}>
                <dt>{labels[k]}</dt>
                <dd>
                  {typeof localized === 'string' ? localized : k === 'outcome' ? (
                    <Status value={raw} />
                  ) : typeof raw === 'boolean' ? (
                    raw ? '是' : '否'
                  ) : k === 'reference' && raw && typeof raw === 'object' ? (
                    `版本 ${display((raw as RecordData).version)} · 摘要 ${String((raw as RecordData).content_hash ?? '').slice(0, 12)}…`
                  ) : typeof raw === 'string' && /^[0-9a-f]{64}$/i.test(raw) ? (
                    <HashValue value={raw} />
                  ) : typeof raw === 'object' ? (
                    '已返回结构化结果，详见诊断'
                  ) : (
                    display(raw)
                  )}
                </dd>
              </div>
            );
          })}
        </dl>
      ) : (
        <p>服务已返回结果；请查看对应记录及诊断详情。</p>
      )}
      <details>
        <summary>{diagnosticLabel}</summary>
        <JsonView value={value} />
      </details>
    </>
  );
}
export function isTerminalJob(job: RecordData) {
  return ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(job.status));
}
export function CurrentJobSummary({ job }: { job: RecordData }) {
  return (
    <>
      <dl className="key-value">
        <dt>当前任务状态</dt>
        <dd>
          <Status value={job.status} />
        </dd>
        <dt>任务进度</dt>
        <dd>{display(job.progress)}%</dd>
        <dt>已请求取消</dt>
        <dd>
          {job.cancel_requested === true ? '是' : job.cancel_requested === false ? '否' : '未提供'}
        </dd>
        <dt>状态更新时间（世界标准时间 UTC）</dt>
        <dd>{display(job.updated_at)}</dd>
      </dl>
      {typeof job.error_code === 'string' && <details><summary>查看失败代码</summary><p>{job.error_code}</p></details>}
      <small>
        {isTerminalJob(job) ? '该任务已结束。' : '任务状态会自动更新。'}
      </small>
    </>
  );
}
function SubmittedJobStatus({ jobId }: { jobId: string }) {
  const [refresh, setRefresh] = useState(0);
  const { data, error, loading } = useResource<RecordData>(
    `/jobs/${encodeURIComponent(jobId)}`,
    refresh,
    true,
    isTerminalJob,
  );
  return (
    <div>
      <strong>后台任务进度</strong>
      {loading ? (
        <p>正在读取当前任务状态…</p>
      ) : error ? (
        <>
          <p>暂时无法查看任务进度。请刷新任务状态。</p>
          <ErrorBox error={error} />
        </>
      ) : data ? (
        <CurrentJobSummary job={data} />
      ) : (
        <p>暂时找不到这条任务。</p>
      )}
      <div className="form-actions">
        <button type="button" className="secondary" onClick={() => setRefresh((n) => n + 1)}>
          刷新当前任务状态
        </button>
        <a className="text-button" href={`#jobs?job=${encodeURIComponent(jobId)}`}>
          打开任务详情 →
        </a>
      </div>
    </div>
  );
}
export function ActionResult({ value }: { value: unknown }) {
  const result =
    value && typeof value === 'object' && !Array.isArray(value) ? (value as RecordData) : null;
  const jobId = typeof result?.job_id === 'string' && result.job_id ? result.job_id : null;
  return (
    <div className="operation-result" role="status">
      {jobId ? <SubmittedJobStatus key={jobId} jobId={jobId} /> : <strong>本次操作结果</strong>}
      <ResultSummary
        value={value}
        statusLabel="操作状态"
        omitStatus={!!jobId}
        diagnosticLabel={jobId ? '查看提交时的原始记录' : '查看原始结果'}
      />
    </div>
  );
}
export function Resource({
  path,
  refresh,
  polling = false,
  children,
}: {
  path: string;
  refresh?: number;
  polling?: boolean | number;
  children?: (data: RecordData) => ReactNode;
}) {
  const [tick, setTick] = useState(0);
  const state = useResource<RecordData>(path, (refresh || 0) + tick, polling);
  if (state.loading)
    return (
      <div className="loading" role="status">
        正在读取保存记录…
      </div>
    );
  if (state.error) return <ErrorBox error={state.error} retry={() => setTick((v) => v + 1)} />;
  return state.data ? (
    <>{children ? children(state.data) : <ResultSummary value={state.data} />}</>
  ) : (
    <Notice>服务未返回记录。</Notice>
  );
}
export type Column = { key: string; label: string; render?: (row: RecordData) => ReactNode };
export function DataTable({
  path,
  columns,
  refresh = 0,
  polling = false,
  empty,
  onSelect,
}: {
  path: string;
  columns: Column[];
  refresh?: number;
  polling?: boolean | number;
  empty: string;
  onSelect?: (row: RecordData) => void;
}) {
  const [page, setPage] = useState(1),
    [tick, setTick] = useState(0);
  const { data, error, loading } = useResource<PageData>(
    `${path}${path.includes('?') ? '&' : '?'}limit=20&offset=${(page - 1) * 20}`,
    refresh + tick,
    polling,
  );
  if (loading)
    return (
      <div className="loading" role="status">
        正在读取记录…
      </div>
    );
  if (error) return <ErrorBox error={error} retry={() => setTick((v) => v + 1)} />;
  if (!data || !Array.isArray(data.items))
    return (
      <ErrorBox
        error={
          new ApiError(
            0,
            'SCHEMA_MISMATCH',
            '列表响应必须含 items、total、limit、offset。请修复 API 合约。',
          )
        }
      />
    );
  return (
    <>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key}>{c.label}</th>
              ))}
              {onSelect && <th>操作</th>}
            </tr>
          </thead>
          <tbody>
            {data.items.map((row, i) => (
              <tr key={String(row.id ?? row.job_id ?? i)}>
                {columns.map((c) => (
                  <td key={c.key}>{c.render ? c.render(row) : display(row[c.key])}</td>
                ))}
                {onSelect && (
                  <td>
                    <button className="text-button" onClick={() => onSelect(row)}>
                      查看详情
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        {!data.items.length && (
          <div className="empty">
            <span>○</span>
            <strong>暂无记录</strong>
            <p>{empty}</p>
          </div>
        )}
      </div>
      <div className="pagination">
        <small>
          共 {data.total} 条 · 第 {page} 页
        </small>
        <div>
          <button className="secondary" disabled={page <= 1} onClick={() => setPage((v) => v - 1)}>
            上一页
          </button>
          <button
            className="secondary"
            disabled={page * data.limit >= data.total}
            onClick={() => setPage((v) => v + 1)}
          >
            下一页
          </button>
        </div>
      </div>
    </>
  );
}
export type Field = {
  key: string;
  label: string;
  type?: 'text' | 'password' | 'number' | 'textarea' | 'select' | 'file';
  help: string;
  options?: { value: string; label: string }[];
  initial?: string;
  required?: boolean;
  accept?: string;
  showWhen?: { key: string; values: string[] };
  resource?: {
    path: string;
    idKey: string;
    labelKeys: string[];
    filter?: { key: string; value: string };
  };
};
export function ResourceSelect({
  field: f,
  onChange,
  refresh = 0,
}: {
  field: Field;
  onChange: (value: string) => void;
  refresh?: number;
}) {
  const [offset, setOffset] = useState(0),
    [tick, setTick] = useState(0),
    [value, setValue] = useState(f.initial ?? '');
  const source = f.resource!;
  const { data, error, loading } = useResource<PageData>(
    `${source.path}${source.path.includes('?') ? '&' : '?'}limit=20&offset=${offset}`,
    refresh + tick,
  );
  const rows = (Array.isArray(data?.items) ? data.items : []).filter(
    (r) => !source.filter || r[source.filter.key] === source.filter.value,
  );
  useEffect(() => {
    if (f.initial) {
      setValue(f.initial);
      setTick((n) => n + 1);
    }
  }, [f.initial]);
  return (
    <>
      <select
        name={f.key}
        value={value}
        required={f.required !== false}
        disabled={loading || !!error}
        onChange={(e) => {
          setValue(e.target.value);
          onChange(e.target.value);
        }}
      >
        <option value="">
          {loading
            ? '正在读取已保存资源…'
            : f.required === false
              ? '不绑定 / 无'
              : '请选择已保存资源'}
        </option>
        {value && !rows.some((r) => String(r[source.idKey]) === value) && (
          <option value={value}>已选资源 · {value.slice(-12)}</option>
        )}
        {rows.map((r) => (
          <option key={String(r[source.idKey])} value={String(r[source.idKey])}>
            {source.labelKeys
              .map((k) => {
                const x = display(r[k]);
                return k === source.idKey ? x.slice(-12) : x;
              })
              .join(' · ')}
          </option>
        ))}
      </select>
      {error != null && <ErrorBox error={error} retry={() => setTick((n) => n + 1)} />}
      <button type="button" className="text-button" onClick={() => setTick((n) => n + 1)}>
        刷新已保存资源
      </button>
      {data && (
        <div className="picker-pagination">
          <small>
            {source.filter
              ? `全部资源共 ${data.total} 条 · 本页匹配 ${rows.length} 条`
              : `共 ${data.total} 条`}{' '}
            · 第 {offset / 20 + 1} 页
          </small>
          <button
            type="button"
            className="text-button"
            disabled={offset === 0}
            onClick={() => setOffset((v) => v - 20)}
          >
            前页
          </button>
          <button
            type="button"
            className="text-button"
            disabled={offset + 20 >= data.total}
            onClick={() => setOffset((v) => v + 20)}
          >
            后页
          </button>
        </div>
      )}
    </>
  );
}
export function HashValue({ value }: { value: Json | undefined }) {
  const [copied, setCopied] = useState(false);
  const full = display(value);
  return (
    <span className="hash-value" title={full}>
      {full.length > 20 ? `${full.slice(0, 12)}…` : full}
      {value != null && (
        <button
          className="text-button"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(full);
              setCopied(true);
            } catch {
              setCopied(false);
            }
          }}
        >
          {copied ? '已复制' : '复制'}
        </button>
      )}
    </span>
  );
}
export function ActionForm({
  title,
  fields,
  submitLabel,
  onSubmit,
  disabled = false,
  resourceRefresh = 0,
}: {
  title: string;
  fields: Field[];
  submitLabel: string;
  onSubmit: (values: Record<string, string>, files: Record<string, File>) => Promise<unknown>;
  disabled?: boolean;
  resourceRefresh?: number;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [result, setResult] = useState<unknown>(null);
  const [choices, setChoices] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields
        .filter((f) => f.type === 'select')
        .map((f) => [f.key, f.initial ?? f.options?.[0]?.value ?? '']),
    ),
  );
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    const form = e.currentTarget,
      entries = new FormData(form),
      values: Record<string, string> = {},
      files: Record<string, File> = {};
    entries.forEach((value, key) => {
      if (value instanceof File) {
        if (value.size) files[key] = value;
      } else values[key] = value;
    });
    try {
      for (const field of fields.filter(
        (f) =>
          f.resource &&
          f.required !== false &&
          (!f.showWhen || f.showWhen.values.includes(choices[f.showWhen.key])),
      )) {
        if (!values[field.key]) throw new Error(`请等待资源加载并选择${field.label}。`);
      }
      const response = await onSubmit(values, files);
      setResult(response ?? { message: '操作已由服务确认。请在下方读取最新保存记录。' });
      for (const input of form.querySelectorAll<HTMLInputElement>('input[type=password]'))
        input.value = '';
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel">
      <h2>{title}</h2>
      <form onSubmit={submit}>
        <div className="form-grid">
          {fields
            .filter((f) => !f.showWhen || f.showWhen.values.includes(choices[f.showWhen.key]))
            .map((f) => (
              <label key={f.key} className={f.type === 'textarea' ? 'wide' : ''}>
                <span>
                  {f.label}
                  {f.required !== false && <em> *</em>}
                </span>
                {f.resource ? (
                  <ResourceSelect
                    field={f}
                    refresh={resourceRefresh}
                    onChange={(value) => setChoices((v) => ({ ...v, [f.key]: value }))}
                  />
                ) : f.type === 'select' ? (
                  <select
                    name={f.key}
                    defaultValue={f.initial}
                    required={f.required !== false}
                    onChange={(e) => setChoices((v) => ({ ...v, [f.key]: e.target.value }))}
                  >
                    {f.options?.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                ) : f.type === 'textarea' ? (
                  <textarea
                    name={f.key}
                    defaultValue={f.initial}
                    required={f.required !== false}
                    rows={5}
                  />
                ) : (
                  <input
                    name={f.key}
                    type={f.type || 'text'}
                    defaultValue={f.type !== 'file' ? f.initial : undefined}
                    required={f.required !== false}
                    accept={f.accept}
                    autoComplete={f.type === 'password' ? 'new-password' : 'off'}
                  />
                )}
                <small>{f.help}</small>
              </label>
            ))}
        </div>
        <button disabled={busy || disabled} type="submit">
          {busy ? '正在提交…' : submitLabel}
        </button>
        <small className="required-note">提交后以服务端记录为准。超时请先刷新查询。</small>
      </form>
      {error != null && <ErrorBox error={error} />}
      {result !== null && <ActionResult value={result} />}
    </section>
  );
}
export function Panel({
  title,
  helpTopic,
  children,
  action,
  className,
}: {
  title: string;
  helpTopic?: string;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <section className={className ? 'panel ' + className : 'panel'}>
      <div className="panel-heading">
        <h2>{title}</h2>
        {action}
        <AreaHelp title={title} topic={helpTopic} />
      </div>
      {children}
    </section>
  );
}
