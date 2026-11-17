import { useState } from 'react';
import { ApiError, display, download, post } from './api';
import type { RecordData } from './api';
import {
  ActionForm,
  DataTable,
  ErrorBox,
  HashValue,
  JsonView,
  Notice,
  Panel,
  useResource,
} from './components';
import type { Column } from './components';
import { ServerRows } from './research-visuals';
import {
  OBSERVATION_FAMILY,
  buildObservationSource,
  observationExportAllowed,
  observationOrigin,
} from './provider-observation-contract';

export function ObservationSourceForm({
  provider,
  changed,
}: {
  provider: string;
  changed: () => void;
}) {
  return (
    <>
      {provider === 'akshare' && (
        <Notice>
          新来源使用 AKShare（新浪）的未复权价格，支持 AAPL、MSFT、NVDA、AMD、GOOG、AMZN。系统先下载来源允许的历史记录，再按日期筛选。成交量单位尚未确认，模拟时留空，可查看原值。旧东方财富记录仍可查看。
        </Notice>
      )}
      <Notice>
        这些日价格仅供个人开发时参考，获准后才可导出。暂不能用作当时的成交价或旧版历史回测数据。连接地址：
        {observationOrigin(provider)}。无需输入登录信息或密钥。
      </Notice>
      <ActionForm
        key={provider}
        title="登记观察来源与用途依据"
        fields={[
          { key: 'source_id', label: '来源标识', help: '稳定唯一标识；新修订保留旧版本。' },
          { key: 'name', label: '显示名称', help: '便于识别此观察来源。' },
          {
            key: 'license_id',
            label: '用途依据标识',
            help: '记录本次个人开发用途依据，不代表外部认证。',
          },
          {
            key: 'statement',
            label: '个人开发用途依据',
            type: 'textarea',
            help: '写清数据来自哪里、为何有权使用。若要导出，也须写清导出许可以何为据。请勿填写账号、密码或密钥。',
          },
          {
            key: 'purpose',
            label: '声明用途',
            type: 'select',
            initial: 'development',
            options: [
              { value: 'development', label: '个人开发查看' },
              { value: 'development_export', label: '个人开发及按依据导出' },
            ],
            help: '默认不申请导出；是否获准以服务端返回为准。',
          },
          {
            key: 'affirmed',
            label: '确认用途依据',
            type: 'select',
            initial: '',
            options: [
              { value: '', label: '请选择' },
              { value: 'yes', label: '我确认以上填写的个人开发用途依据' },
            ],
            help: '主动确认用户声明，不表示来源方或第三方已认证。',
          },
        ]}
        submitLabel="保存观察来源与用途依据"
        onSubmit={async (v) => {
          const result = await post('/data/sources', buildObservationSource(provider, v));
          changed();
          return result;
        }}
      />
    </>
  );
}

const rowColumns: Column[] = [
  { key: 'existing_instrument_id', label: '证券标识' },
  { key: 'provider_symbol', label: '提供方代码' },
  { key: 'session_date', label: '交易日标签' },
  { key: 'open', label: '开盘价' },
  { key: 'high', label: '最高价' },
  { key: 'low', label: '最低价' },
  { key: 'close', label: '收盘价' },
  {
    key: 'volume',
    label: '成交量',
    render: (r) => (r.volume == null ? '未提供' : display(r.volume)),
  },
  { key: 'currency', label: '币种' },
  { key: 'price_unit', label: '价格单位' },
  { key: 'volume_unit', label: '成交量单位' },
  { key: 'provider_basis_claim', label: '提供方价格说明' },
  { key: 'volume_share_basis', label: '成交量统计方式' },
  { key: 'observed_received_at', label: '实际收到的时间（UTC）' },
  { key: 'sdk_transform_flags', label: '缺项/变换提示' },
];
export function ObservationRows({ rows }: { rows: unknown }) {
  return (
    <ServerRows
      value={rows}
      columns={rowColumns}
      empty="本页没有观察记录；以服务端结果与提示为准。"
    />
  );
}
export function ObservationDetail({ id }: { id: string }) {
  const [page, setPage] = useState(0),
    [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const state = useResource<RecordData>(
    `/data/observations/${encodeURIComponent(id)}?limit=20&offset=${page * 20}`,
    tick,
  );
  async function exportResult(format: 'csv' | 'json') {
    if (!state.data || !observationExportAllowed(state.data)) return;
    setBusy(true);
    setError(null);
    try {
      await download(
        `/data/observations/${encodeURIComponent(id)}/export?format=${format}`,
        `${id}.${format}`,
      );
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }
  const r = state.data;
  return (
    <Panel title={`观察日线 · ${id}`}>
      <button type="button" className="secondary" onClick={() => setTick((n) => n + 1)}>
        刷新观察详情
      </button>
      {state.loading ? (
        <Notice>正在读取观察结果…</Notice>
      ) : state.error ? (
        <ErrorBox error={state.error} />
      ) : !r ? (
        <Notice>服务未返回观察结果。</Notice>
      ) : r.data_family !== OBSERVATION_FAMILY ||
        r.price_basis !== 'PROVIDER_OBSERVED' ||
        r.raw_compatible !== false ||
        !Array.isArray(r.rows) ||
        typeof r.total !== 'number' ? (
        <ErrorBox error={new ApiError(0, 'SCHEMA_MISMATCH', '无法读取这份数据。请刷新结果后重试。')} />
      ) : (r.use_capabilities as RecordData)?.display !== true ? (
        <Notice>当前结果未提供获准展示能力。</Notice>
      ) : (
        <>
          <Notice>
            这是来源提供的参考价。交易日期不能说明资料最早何时能取得；暂不能用于旧版历史回测
            或研究样本。
          </Notice>
          <dl className="key-value">
            <dt>价格基准</dt>
            <dd>{display(r.price_basis)}</dd>
            <dt>实际收到的时间（UTC）</dt>
            <dd>{display(r.observed_received_at)}</dd>
            <dt>资料最早可取得的时间</dt>
            <dd>
              {r.historical_first_available_at == null
                ? '尚未找到当时能取得这些资料的证明'
                : display(r.historical_first_available_at)}
            </dd>
            <dt>结果摘要</dt>
            <dd>
              <HashValue value={r.content_hash} />
            </dd>
            <dt>提示</dt>
            <dd>{display(r.warnings)}</dd>
          </dl>
          <ObservationRows rows={r.rows} />
          <div className="pagination">
            <small>
              共 {r.total} 条 · 第 {page + 1} 页
            </small>
            <div>
              <button
                className="secondary"
                disabled={page === 0}
                onClick={() => setPage((n) => n - 1)}
              >
                上一页
              </button>
              <button
                className="secondary"
                disabled={(page + 1) * 20 >= r.total}
                onClick={() => setPage((n) => n + 1)}
              >
                下一页
              </button>
            </div>
          </div>
          {observationExportAllowed(r) ? (
            <div className="detail-actions">
              <button disabled={busy} onClick={() => void exportResult('csv')}>
                导出获准 CSV
              </button>
              <button disabled={busy} onClick={() => void exportResult('json')}>
                导出获准 JSON
              </button>
            </div>
          ) : (
            <Notice>
              这份数据不能导出：
              {display(
                (r.use_capabilities as RecordData)?.export_denied_reason ??
                  '服务未提供允许导出的明确状态',
              )}
              。
            </Notice>
          )}
          <details>
            <summary>数据来源、使用许可和获取记录</summary>
            <JsonView
              value={{
                source_ref: r.source_ref,
                use_basis_ref: r.use_basis_ref,
                sdk_ref: r.sdk_ref,
                request_parameters: r.request_parameters,
                raw_http_entities: r.raw_http_entities,
                sdk_result_snapshot: r.sdk_result_snapshot,
                basis_verification: r.basis_verification,
                row_basis_evidence: r.rows.map((row) => {
                  const item = row as RecordData;
                  return {
                    session_date: item.session_date,
                    source_adjustment_request: item.source_adjustment_request,
                    source_timestamp: item.source_timestamp,
                    source_timestamp_timezone: item.source_timestamp_timezone,
                    basis_reference_time: item.basis_reference_time,
                    currency_and_unit_evidence: item.currency_and_unit_evidence,
                  };
                }),
              }}
            />
          </details>
        </>
      )}
      {error != null && <ErrorBox error={error} />}
    </Panel>
  );
}
export function ObservationJobResults({ value }: { value: RecordData }) {
  const [selected, setSelected] = useState('');
  if (value.data_family !== OBSERVATION_FAMILY) return null;
  const ids = Array.isArray(value.result_ids)
    ? value.result_ids.filter((v): v is string => typeof v === 'string')
    : [];
  return (
    <>
      <Notice>
        观察记录：{display(value.observed_rows)}；覆盖：{display(value.coverage)}
        。任务完成只表示保存了返回的价格。是否为原始价、当时能否取得以及来源验证情况，仍需另外核实。
      </Notice>
      {ids.length ? (
        ids.map((id) => (
          <button key={id} type="button" className="secondary" onClick={() => setSelected(id)}>
            查看观察结果 · {id.slice(-12)}
          </button>
        ))
      ) : (
        <Notice>服务尚未返回观察结果标识。</Notice>
      )}
      {selected && <ObservationDetail key={selected} id={selected} />}
    </>
  );
}
export function ObservationResults({ datasetId }: { datasetId: string }) {
  const { data, error, loading } = useResource<RecordData>(
    `/data/datasets/${encodeURIComponent(datasetId)}`,
  );
  const [selected, setSelected] = useState(''),
    [refresh, setRefresh] = useState(0);
  if (loading) return null;
  if (error) return <ErrorBox error={error} />;
  if (data?.data_family !== OBSERVATION_FAMILY) return null;
  return (
    <Panel title="已保存的参考价格">
      <Notice>这些价格可查看，获准后可导出。暂不能视为当时的原始价格，也不能用于旧版历史回测。下载完成后刷新列表。</Notice>
      <button type="button" className="secondary" onClick={() => setRefresh((n) => n + 1)}>
        刷新观察结果
      </button>
      <DataTable
        path={`/data/datasets/${encodeURIComponent(datasetId)}/observations`}
        refresh={refresh}
        columns={[
          { key: 'result_id', label: '观察结果' },
          { key: 'provider', label: '来源' },
          { key: 'sdk_version', label: '版本' },
          { key: 'row_count', label: '记录数' },
          { key: 'session_date_min', label: '起始交易日' },
          { key: 'session_date_max', label: '最后交易日' },
          { key: 'observed_received_at', label: '实际收到的时间（UTC）' },
          { key: 'price_basis', label: '价格基准' },
          {
            key: 'export_allowed',
            label: '导出',
            render: (v) =>
              v.export_allowed === true
                ? '已获准'
                : v.export_allowed === false
                  ? `未获准 · ${display(v.export_denied_reason)}`
                  : '未提供',
          },
        ]}
        empty="尚无观察采集结果。"
        onSelect={(v) => setSelected(String(v.result_id))}
      />
      {selected && <ObservationDetail key={selected} id={selected} />}
    </Panel>
  );
}
