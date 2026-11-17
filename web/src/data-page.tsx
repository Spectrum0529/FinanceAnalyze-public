import { useState } from 'react';
import { post, display, download, request, ApiError } from './api';
import type { RecordData } from './api';
import {
  ActionForm,
  DataTable,
  ErrorBox,
  JsonView,
  Notice,
  Panel,
  Resource,
  ResultSummary,
  Status,
  HashValue,
} from './components';
import type { Field } from './components';
import { ServerRows } from './research-page';
import { DataCollection } from './collection-page';
import { ObservationSourceForm, ObservationResults } from './provider-observations';
import {
  isSdkProvider,
  requireKnownProvider,
  legacySourceFields,
  PROVIDER_CHOICES,
  validateObservationDataset,
  OBSERVATION_FAMILY,
} from './provider-observation-contract';

const input = (key: string, label: string, help: string, initial?: string): Field => ({
  key,
  label,
  help,
  initial,
  required: !['endpoint', 'key_env', 'secret_env', 'user_agent_env', 'max_delay_seconds'].includes(
    key,
  ),
});
const json = (key: string, label: string, help: string, value: unknown): Field => ({
  key,
  label,
  help,
  type: 'textarea',
  initial: JSON.stringify(value, null, 2),
});
const parse = (value: string) => {
  try {
    return JSON.parse(value);
  } catch {
    throw new Error('JSON 格式不正确，请检查引号与逗号。');
  }
};
export const identityExample = [
  {
    instrument_id: 'synthetic-equity',
    ticker: 'SYNTH',
    ticker_valid_from: '2025-01-01T00:00:00Z',
    ticker_valid_to: null,
    asset_class: 'US_EQUITY',
    mic: 'XNYS',
    currency: 'USD',
    tick_size: '0.01',
    lot_size: '1',
    fractional: false,
    calendar_id: 'XNYS',
    settlement: 'T+1',
    tradable: true,
  },
];
export const mappingExample = {
  fields: {
    ticker: 'ticker',
    instrument_id: 'instrument_id',
    effective_at: 'effective_at',
    available_at: 'available_at',
    open: 'open',
    high: 'high',
    low: 'low',
    close: 'close',
    volume: 'volume',
  },
  price_unit: 'USD_per_share',
  volume_unit: 'shares',
  timezone: 'UTC',
  frequency: '1Day',
  adjustment: 'raw',
};
export const tickMappingExample = {
  record_kind: 'tick',
  event_type: 'TRADE',
  fields: Object.fromEntries(
    [
      'event_id',
      'ticker',
      'instrument_id',
      'event_at',
      'available_at',
      'price',
      'volume',
      'trading_status',
    ].map((k) => [k, k]),
  ),
  price_unit: 'USD_per_share',
  volume_unit: 'shares',
  timezone: 'UTC',
  availability: {
    basis: 'SYNTHETIC_FIXTURE_CLOCK',
    reference: 'public-synthetic-upload-prints-v1',
  },
};
const printIdentity = [
  {
    ...identityExample[0],
    instrument_id: 'synthetic:alpha',
    ticker: 'FAKE',
    ticker_valid_from: '2020-01-01T00:00:00Z',
  },
];
export async function fileBase64(file: File) {
  if (!file || file.size > 1024 * 1024)
    throw new Error('请选择不超过 1 MiB 的开发 CSV / Parquet。');
  const bytes = new Uint8Array(await file.arrayBuffer());
  let value = '';
  for (let i = 0; i < bytes.length; i += 8192)
    value += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(value);
}
function LegacySourceForm({ changed, provider }: { changed: () => void; provider: string }) {
  return (
    <ActionForm
      title="登记来源与连接引用"
      fields={legacySourceFields(
        [
          input(
            'source_id',
            '来源标识',
            '稳定名称，例如 synthetic-local。修订追加来源版本，不覆盖旧版。',
          ),
          input('name', '显示名称', '中文名称，便于识别来源。'),
          input(
            'license_id',
            '许可引用',
            '合成样本填自有工程样本标识；真实来源需保留许可证据。',
            'synthetic-engineering-only',
          ),
          {
            key: 'uses',
            label: '声明用途',
            type: 'select',
            options: [
              { value: 'development', label: '仅开发使用' },
              { value: 'development_export', label: '开发与获准导出' },
            ],
            help: '合成自有样本可明确声明导出。真实来源仍须独立许可证据门；不会自动批准使用权。',
          },
          {
            ...input(
              'endpoint',
              '官方 HTTPS 端点',
              'Alpaca：https://data.alpaca.markets；SEC：https://data.sec.gov。离线来源留空。',
              '',
            ),
            showWhen: { key: 'provider', values: ['alpaca', 'sec', 'rest'] },
          },
          {
            ...input(
              'key_env',
              'Key 环境变量引用',
              '敏感值仅在进程环境填写；此处填 FINANCE_PLATFORM_DATA_ 开头的变量名。Alpaca 控制台取得 key。',
              '',
            ),
            showWhen: { key: 'provider', values: ['alpaca', 'rest'] },
          },
          {
            ...input(
              'secret_env',
              'Secret 环境变量引用',
              'Alpaca 控制台取得 secret。此处仅填变量名，不填真实值；无配置则该来源不可采集。',
              '',
            ),
            showWhen: { key: 'provider', values: ['alpaca'] },
          },
          {
            ...input(
              'user_agent_env',
              'SEC User-Agent 引用',
              'FINANCE_PLATFORM_DATA_ 开头变量；值格式为应用名称 联系邮箱。用于合规 SEC 请求。',
              '',
            ),
            showWhen: { key: 'provider', values: ['sec'] },
          },
          {
            key: 'feed',
            label: 'Alpaca feed',
            type: 'select',
            required: false,
            showWhen: { key: 'provider', values: ['alpaca'] },
            options: [
              { value: '', label: '非 Alpaca · 无' },
              { value: 'iex', label: 'IEX · 权限待验证' },
              { value: 'sip', label: 'SIP · 需对应订阅权限' },
            ],
            help: 'feed 不等于许可或历史覆盖已批准；只读测试分开验证。',
          },
          {
            key: 'source_profile',
            label: '来源提供的数据',
            type: 'select',
            options: [
              { value: 'bars', label: '日线 / 分钟 bar' },
              { value: 'bar_tick', label: 'bar 与独立成交 tick' },
              { value: 'custom', label: '高级：自定义能力与字段' },
            ],
            help: '合成 bar/tick 示例选择第二项；声明不等于已认证覆盖。',
          },
          {
            ...json('capabilities', '来源能力', '明确实际提供的类型。不能以声明代替验证。', [
              'bars',
            ]),
            showWhen: { key: 'source_profile', values: ['custom'] },
          },
          {
            ...json(
              'fields',
              '字段清单',
              '导入将检查单位和真实字段。',
              Object.keys(mappingExample.fields),
            ),
            showWhen: { key: 'source_profile', values: ['custom'] },
          },
        ],
        provider,
      )}
      submitLabel="保存来源版本"
      onSubmit={async (v) => {
        requireKnownProvider(provider);
        const r = await post('/data/sources', {
          source_id: v.source_id,
          name: v.name,
          provider,
          capabilities:
            v.source_profile === 'custom'
              ? parse(v.capabilities)
              : v.source_profile === 'bar_tick'
                ? ['bars', 'ticks', 'PIT']
                : ['bars'],
          assets: ['US_EQUITY', 'US_ETF'],
          frequencies:
            v.source_profile === 'bar_tick' ? ['1Day', '1Min', 'tick'] : ['1Day', '1Min'],
          fields:
            v.source_profile === 'custom'
              ? parse(v.fields)
              : [
                  ...new Set([
                    ...Object.keys(mappingExample.fields),
                    ...(v.source_profile === 'bar_tick'
                      ? Object.keys(tickMappingExample.fields)
                      : []),
                  ]),
                ],
          declared_history: null,
          verified_coverage: [],
          license: {
            license_id: v.license_id,
            evidence: [],
            use_allowed:
              v.uses === 'development_export' ? ['development', 'export'] : ['development'],
            rights_verified: false,
          },
          connection: {
            endpoint: v.endpoint || null,
            key_env: v.key_env || null,
            secret_env: v.secret_env || null,
            user_agent_env: v.user_agent_env || null,
            feed: v.feed || null,
          },
        });
        changed();
        return r;
      }}
    />
  );
}
export function SourceForm({ changed }: { changed: () => void }) {
  const [provider, setProvider] = useState('synthetic');
  return (
    <>
      <label className="field">
        <span>连接器</span>
        <select value={provider} onChange={(e) => setProvider(e.target.value)}>
          {PROVIDER_CHOICES.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      {isSdkProvider(provider) ? (
        <ObservationSourceForm key={provider} provider={provider} changed={changed} />
      ) : (
        <LegacySourceForm key={provider} provider={provider} changed={changed} />
      )}
    </>
  );
}
export function DataCenter({ canWrite }: { canWrite: boolean }) {
  const [refresh, setRefresh] = useState(0),
    [dataset, setDataset] = useState(''),
    [snapshot, setSnapshot] = useState('');
  const [preview, setPreview] = useState<RecordData | null>(null),
    [pending, setPending] = useState<{ dataset: string; body: Record<string, unknown> } | null>(
      null,
    );
  const [busy, setBusy] = useState(false),
    [saveError, setSaveError] = useState<unknown>(null),
    [saveResult, setSaveResult] = useState<unknown>(null),
    [exportError, setExportError] = useState<unknown>(null);
  const change = () => setRefresh((n) => n + 1);
  return (
    <>
      <Panel title="准备一份可回测的数据">
        <p>
          先登记来源和使用许可，再导入并保存一份固定数据。通过检查后，回到回测页面选用。只想练习时，可以在回测页面直接选模拟数据。
        </p>
        <a className="text-button" href="#research">
          返回回测，继续选数据或策略 →
        </a>
      </Panel>
      <Notice>
        用练习数据试一次：
        <a className="text-button" href="/synthetic_prices.csv" download>
          下载练习 CSV →
        </a>
        。这是模拟数据，仅用来练习导入和保存。
        <details><summary>查看练习数据字段和逐笔成交样例</summary>
          <p>练习 CSV：证券编号 synthetic-equity，代码 SYNTH，时区 UTC，单位 USD_per_share 和 shares。资料截止时间示例：2026-09-06T00:00:00Z。</p>
          逐笔成交样例：
          <a href="/independent_ticks/daily_bars.csv" download>
            下载日线 bar
          </a>{' '}
          ·{' '}
          <a href="/independent_ticks/daily_ticks.csv" download>
            下载独立成交 tick
          </a>{' '}
          ·{' '}
          <a href="/independent_ticks/manifest.json" download>
            来源、身份与文件摘要清单
          </a>
          。同一数据版本分别导入两份文件，选择 FAKE 合成身份及 bar + tick 质量要求；截止
          2026-10-13T00:00:00Z。
        </details>
      </Notice>
      <Panel title="数据来源与许可">
        <DataTable
          path="/data/sources"
          columns={[
            { key: 'source_id', label: '来源标识' },
            { key: 'name', label: '名称' },
            { key: 'provider', label: '连接器' },
            { key: 'version', label: '版本' },
            {
              key: 'license',
              label: '许可与用途',
              render: (r) =>
                display((r.license as RecordData)?.license_id) +
                ' · ' +
                display((r.license as RecordData)?.use_allowed),
            },
            {
              key: 'verified_coverage',
              label: '已验证覆盖',
              render: (r) =>
                Array.isArray(r.verified_coverage) && !r.verified_coverage.length
                  ? '未验证'
                  : display(r.verified_coverage),
            },
          ]}
          refresh={refresh}
          empty="还没有数据来源。可以先添加无需登录的练习来源。真实行情仍需核对许可和历史日期。"
        />
      </Panel>
      {canWrite && (
        <details>
          <summary>新增来源与连接配置</summary>
          <SourceForm changed={change} />
        </details>
      )}
      {canWrite && (
        <ActionForm
          title="建立开发数据版本"
          resourceRefresh={refresh}
          fields={[
            {
              key: 'source_id',
              label: '已登记来源',
              type: 'select',
              help: '选择服务端来源版本；真实来源使用权仍由后端校验。',
              resource: {
                path: '/data/sources',
                idKey: 'source_id',
                labelKeys: ['name', 'provider', 'version'],
              },
            },
            input('title', '数据版本名称', '例如日线合成工程样本，不冒称真实市场数据。'),
            {
              key: 'scope',
              label: '样本来源',
              type: 'select',
              options: [
                { value: 'SYNTHETIC', label: 'SYNTHETIC · 明确合成' },
                { value: 'DEVELOPMENT', label: 'DEVELOPMENT · 已授权开发数据' },
              ],
              help: '观察来源须选 DEVELOPMENT；个人开发用途声明由后端单独检查，不表示外部认证。其他来源仍执行原权限门。',
            },
            {
              key: 'identity_profile',
              label: '证券身份模板',
              type: 'select',
              options: [
                { value: 'basic', label: 'SYNTH · 简单合成导入' },
                { value: 'prints', label: 'FAKE · 公开 bar + 独立 tick 示例' },
                { value: 'custom', label: '高级：自定义已授权证券身份' },
              ],
              help: '观察来源请选择自定义真实美股/ETF身份（USD）；SYNTH/FAKE模板仅用于合成样本。',
            },
            {
              ...json(
                'identities',
                '证券身份',
                '永久 ID、ticker 有效期、MIC、币种、精度、日历和结算规则。下方默认仅为合成示例。',
                identityExample,
              ),
              showWhen: { key: 'identity_profile', values: ['custom'] },
            },
            json(
              'issuer_ids',
              '发行人 CIK（可选）',
              'SEC 发行人标识列表；不推断发行人与股份类别映射。',
              [],
            ),
          ]}
          submitLabel="保存数据版本"
          onSubmit={async (v) => {
            const source = await request<RecordData>(
              `/data/sources/${encodeURIComponent(v.source_id)}`,
            );
            const provider = String(source.provider ?? '');
            requireKnownProvider(provider);
            const identities =
              v.identity_profile === 'custom'
                ? parse(v.identities)
                : v.identity_profile === 'prints'
                  ? printIdentity
                  : identityExample;
            if (isSdkProvider(provider))
              validateObservationDataset(v.scope, v.identity_profile, identities);
            const r = await post<RecordData>('/data/datasets', {
              source_id: v.source_id,
              title: v.title,
              scope: v.scope,
              identities,
              issuer_ids: parse(v.issuer_ids),
            });
            setDataset(String(r.dataset_id));
            change();
            return r;
          }}
        />
      )}
      {canWrite && (
        <ActionForm
          title="CSV / Parquet 导入预览"
          fields={[
            {
              key: 'dataset_id',
              label: '已保存数据版本',
              type: 'select',
              initial: dataset || undefined,
              help: '选择已保存开发数据版本。',
              resource: {
                path: '/data/datasets',
                idKey: 'dataset_id',
                labelKeys: ['title', 'scope', 'dataset_id'],
              },
            },
            {
              key: 'file',
              label: '开发数据文件',
              type: 'file',
              accept: '.csv,.parquet',
              required: false,
              help: '最多 1 MiB。选择文件或粘贴 CSV 二选一；错误行仅回传行号和代码。',
            },
            {
              ...json(
                'csv_text',
                '粘贴开发 CSV',
                '文件选择不可用时粘贴完整 UTF-8 CSV（含表头）。与文件二选一；Parquet 必须选择文件。',
                '',
              ),
              initial: '',
              required: false,
            },
            {
              key: 'format',
              label: '格式',
              type: 'select',
              options: [
                { value: 'csv', label: 'CSV · UTF-8' },
                { value: 'parquet', label: 'Parquet · 金额需字符串 / Decimal' },
              ],
              help: '选择与文件实际内容一致的格式。',
            },
            {
              key: 'mapping_profile',
              label: '记录类型与映射模板',
              type: 'select',
              options: [
                { value: 'daily', label: '日线 bar · 标准字段 / UTC / USD' },
                { value: 'minute', label: '分钟 bar · 标准字段 / UTC / USD' },
                { value: 'synthetic_tick', label: '独立 TRADE tick · 明确合成 fixture 时钟' },
                { value: 'observed_tick', label: '独立 TRADE tick · 本次实际接收时钟' },
                { value: 'custom', label: '高级：自定义列名、单位与映射' },
              ],
              help: 'tick 必须来自独立成交记录；合成 fixture 时钟仅允许 SYNTHETIC。开发数据使用实际接收时钟。',
            },
            {
              ...json(
                'mapping',
                '字段映射与单位',
                '源列名可修改；单位 USD_per_share、shares；明确时区与 1Day / 1Min。',
                mappingExample,
              ),
              showWhen: { key: 'mapping_profile', values: ['custom'] },
            },
          ]}
          submitLabel="校验并预览"
          onSubmit={async (v, files) => {
            setPending(null);
            setPreview(null);
            setSaveResult(null);
            if (files.file && v.csv_text?.trim())
              throw new Error('请选择文件或粘贴 CSV 其中一种输入。');
            if (!files.file && (!v.csv_text?.trim() || v.format !== 'csv'))
              throw new Error('请选择文件，或在 CSV 格式下粘贴完整内容。');
            const textBytes = new TextEncoder().encode(v.csv_text || '');
            const body = {
              content_base64: await fileBase64(
                files.file || new File([textBytes], 'pasted-development.csv', { type: 'text/csv' }),
              ),
              format: v.format,
              mapping:
                v.mapping_profile === 'custom'
                  ? parse(v.mapping)
                  : v.mapping_profile === 'daily'
                    ? mappingExample
                    : v.mapping_profile === 'minute'
                      ? { ...mappingExample, frequency: '1Min' }
                      : v.mapping_profile === 'observed_tick'
                        ? {
                            ...tickMappingExample,
                            availability: {
                              basis: 'OBSERVED_RECEIPT_ONLY',
                              reference: 'actual-import-receipt',
                            },
                          }
                        : tickMappingExample,
            };
            const r = await post<RecordData>(
              `/data/datasets/${encodeURIComponent(v.dataset_id)}/preview`,
              body,
            );
            setPreview(r);
            if (r.can_import === true) setPending({ dataset: v.dataset_id, body });
            return r;
          }}
        />
      )}
      {preview && (
        <Panel title="导入校验结果">
          <dl className="key-value">
            <dt>输入记录数</dt>
            <dd>{display(preview.row_count)}</dd>
            <dt>有效记录</dt>
            <dd>{display(preview.valid_rows)}</dd>
            <dt>可导入</dt>
            <dd>
              <Status value={preview.can_import === true ? '校验通过' : '错误行阻断导入'} />
            </dd>
            <dt>输入 SHA-256</dt>
            <dd>{display(preview.input_hash)}</dd>
          </dl>
          <ServerRows
            value={preview.errors}
            columns={[
              { key: 'row', label: '输入行' },
              { key: 'code', label: '错误代码' },
              { key: 'message', label: '说明' },
            ]}
            empty="没有发现字段校验错误。"
          />
          {pending && (
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setSaveError(null);
                try {
                  const r = await post(
                    `/data/datasets/${encodeURIComponent(pending.dataset)}/import`,
                    pending.body,
                  );
                  setSaveResult(r);
                  setPending(null);
                  change();
                } catch (e) {
                  setSaveError(e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? '保存中…' : '保存已校验原件与数据版本'}
            </button>
          )}
          {saveError != null && <ErrorBox error={saveError} />}
          {saveResult != null && <ResultSummary value={saveResult} />}
        </Panel>
      )}
      <Panel title="数据版本">
        <DataTable
          path="/data/datasets"
          columns={[
            { key: 'dataset_id', label: '标识' },
            { key: 'title', label: '名称' },
            { key: 'source_id', label: '来源' },
            { key: 'scope', label: '样本类型' },
            {
              key: 'content_hash',
              label: '摘要',
              render: (r) => <HashValue value={r.content_hash} />,
            },
          ]}
          refresh={refresh}
          empty="选择来源建立数据版本；合成/原始数据走导入，观察来源走日线采集。"
          onSelect={(r) => setDataset(String(r.dataset_id))}
        />
      </Panel>
      {dataset && <ObservationResults key={dataset} datasetId={dataset} />}
      {dataset && (
        <Panel title="数据身份、版本与许可引用">
          <Resource path={`/data/datasets/${encodeURIComponent(dataset)}`} refresh={refresh}>
            {(r) => (
              <>
                <ResultSummary value={r} />
                <h3>证券身份与有效期</h3>
                <ServerRows
                  value={r.identities}
                  columns={[
                    { key: 'instrument_id', label: '永久证券标识' },
                    { key: 'ticker', label: 'Ticker' },
                    { key: 'mic', label: '交易所' },
                    { key: 'currency', label: '币种' },
                    { key: 'ticker_valid_from', label: '生效 / UTC' },
                    { key: 'ticker_valid_to', label: '失效 / UTC' },
                    { key: 'settlement', label: '结算' },
                  ]}
                  empty="服务未提供证券身份。"
                />
              </>
            )}
          </Resource>
          <h3>原有导入记录覆盖（不含观察日线）</h3>
          <Resource
            path={`/data/datasets/${encodeURIComponent(dataset)}/coverage`}
            refresh={refresh}
          >
            {(r) => (
              <>
                <ServerRows
                  value={r.items}
                  columns={[
                    { key: 'instrument_id', label: '证券 / 发行人' },
                    { key: 'kind', label: '类型' },
                    { key: 'frequency', label: '频率' },
                    { key: 'logical_records', label: '逻辑记录' },
                    { key: 'versions', label: '版本数' },
                    { key: 'first', label: '首次有效 / UTC' },
                    { key: 'last', label: '末次有效 / UTC' },
                  ]}
                  empty="没有已导入的合法记录。"
                />
                <Notice>
                  这里仅统计已观察的输入记录；预期市场全集仍未知，不能认证完整历史覆盖。
                </Notice>
              </>
            )}
          </Resource>
        </Panel>
      )}
      {canWrite && (
        <ActionForm
          title="保存回测当天已能取得的数据并检查质量"
          fields={[
            {
              key: 'required_kinds',
              label: '质量门需要的记录类型',
              type: 'select',
              options: [
                { value: 'legacy', label: '原默认 bar / issuer 规则' },
                { value: 'bar_tick', label: 'bar + 独立 tick · 研究与普通模拟' },
                { value: 'tick', label: '仅 tick · 数据层检查' },
              ],
              help: '研究与普通模拟还需要 bars；tick-only 不支持这些执行路径。缺失必需类型将阻断。',
            },
            {
              key: 'dataset_id',
              label: '需要冻结的数据版本',
              type: 'select',
              initial: dataset || undefined,
              help: '绑定服务端已保存版本；没有获准记录时不能伪造快照。',
              resource: {
                path: '/data/datasets',
                idKey: 'dataset_id',
                labelKeys: ['title', 'scope', 'dataset_id'],
              },
            },
            input(
              'as_of',
              '只用此时间前能取得的资料（UTC）',
              '请填写带时区的时间。系统只选这之前已能取得的版本。报告期的结束日期不能当作取得日期。',
            ),
            json(
              'expected_keys',
              '预期覆盖键',
              'record_id 字符串列表；空数组表示没有预登记完整性要求，不能冒称完整覆盖。',
              [],
            ),
            json(
              'instrument_ids',
              '证券范围',
              '快照明确绑定永久证券 ID，不会以空列表代替全市场。',
              ['synthetic-equity'],
            ),
            json(
              'expected_bars',
              '预期行情覆盖',
              '列表项含 instrument_id、effective_at、frequency（1Day / 1Min）；缺失实际阻断质量。',
              [],
            ),
            input(
              'max_delay_seconds',
              '最大延迟 / 秒',
              '可选正整数。质量门用于实际运行；留空不声明延迟认证。',
              '',
            ),
          ]}
          submitLabel="冻结快照"
          onSubmit={async (v) => {
            const selectedDataset = await request<RecordData>(
              `/data/datasets/${encodeURIComponent(v.dataset_id)}`,
            );
            if (selectedDataset.data_family === OBSERVATION_FAMILY)
              throw new ApiError(
                0,
                'PROVIDER_OBSERVATION_ONLY',
                '这类行情可在观察结果中查看或导出，暂不能用于只选当时已知资料的历史回测。',
              );
            const r = await post<RecordData>(
              `/data/datasets/${encodeURIComponent(v.dataset_id)}/snapshots`,
              {
                as_of: v.as_of,
                instrument_ids: parse(v.instrument_ids),
                expected_bars: parse(v.expected_bars),
                expected_keys: parse(v.expected_keys),
                max_delay_seconds: v.max_delay_seconds ? Number(v.max_delay_seconds) : null,
                ...(v.required_kinds === 'legacy'
                  ? {}
                  : {
                      required_kinds: v.required_kinds === 'bar_tick' ? ['bar', 'tick'] : ['tick'],
                    }),
              },
            );
            setSnapshot(String(r.snapshot_id));
            change();
            return r;
          }}
        />
      )}
      <Panel title="已保存数据与检查结果">
        <DataTable
          path="/data/snapshots"
          columns={[
            { key: 'snapshot_id', label: '标识' },
            { key: 'dataset_id', label: '数据版本' },
            { key: 'as_of', label: 'As-of / UTC' },
            {
              key: 'quality',
              label: '质量与覆盖',
              render: (r) => (
                <>
                  <Status
                    value={(r.quality as RecordData)?.allowed === true ? '质量通过' : '质量阻断'}
                  />
                  <small>
                    {' '}
                    {display((r.quality as RecordData)?.record_count)} 条 ·{' '}
                    {display((r.quality as RecordData)?.coverage_status)}
                  </small>
                </>
              ),
            },
            {
              key: 'content_hash',
              label: '摘要',
              render: (r) => <HashValue value={r.content_hash} />,
            },
          ]}
          refresh={refresh}
          empty="快照记录覆盖与质量缺口。未来修订和迟到数据不会回写旧快照。"
          onSelect={(r) => setSnapshot(String(r.snapshot_id))}
        />
      </Panel>
      {snapshot && (
        <Panel title="查看数据来源、日期和检查结果">
          <Resource path={`/data/snapshots/${encodeURIComponent(snapshot)}`}>
            {(r) => {
              const q = r.quality as RecordData;
              return (
                <>
                  <dl className="key-value">
                    <dt>只用此时间前能取得的资料（UTC）</dt>
                    <dd>{display(r.as_of)}</dd>
                    <dt>样本来源</dt>
                    <dd>{display(r.scope)}</dd>
                    <dt>证券范围</dt>
                    <dd>{display(r.instrument_ids)}</dd>
                    <dt>质量门</dt>
                    <dd>
                      <Status value={q?.allowed === true ? '质量通过' : '质量阻断'} />
                    </dd>
                    <dt>记录 / 行情数</dt>
                    <dd>
                      {display(q?.record_count)} / {display(q?.bar_count)}
                    </dd>
                    <dt>独立 tick 数</dt>
                    <dd>{display(q?.tick_count)}</dd>
                    <dt>覆盖状态</dt>
                    <dd>{display(q?.coverage_status)}</dd>
                  </dl>
                  <ServerRows
                    value={q?.issues}
                    columns={[
                      { key: 'code', label: '问题' },
                      { key: 'kind', label: '缺失类型' },
                      { key: 'count', label: '数量' },
                      { key: 'record_id', label: '定位标识' },
                    ]}
                    empty="没有已检测到的质量问题；这不等于真实市场覆盖获认证。"
                  />
                  <details>
                    <summary>查看固定数据和来源的原始记录</summary>
                    <JsonView value={r} />
                  </details>
                </>
              );
            }}
          </Resource>
          <button
            className="text-button"
            onClick={async () => {
              setExportError(null);
              try {
                await download(
                  `/data/snapshots/${encodeURIComponent(snapshot)}/export?format=csv`,
                  'snapshot.csv',
                );
              } catch (e) {
                setExportError(e);
              }
            }}
          >
            导出获准 CSV →
          </button>
          {exportError != null && <ErrorBox error={exportError} />}
          <Notice>只有获准导出的数据才能下载为文件。</Notice>
        </Panel>
      )}
      <DataCollection canWrite={canWrite} />
      <Notice>
        保存时会检查字段、单位、晚到和缺失记录。通过检查仍需另行核实市场覆盖和正式研究资格。
      </Notice>
    </>
  );
}
