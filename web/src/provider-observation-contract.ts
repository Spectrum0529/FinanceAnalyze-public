import { ApiError, request } from './api.ts';
import type { RecordData } from './api.ts';
import type { Field } from './components';

export const OBSERVATION_FAMILY = 'provider-observation-1';
export const LEGACY_PROVIDERS = ['synthetic', 'local', 'nyse', 'alpaca', 'sec', 'rest'];
export const SDK_PROVIDERS = ['yfinance', 'akshare'];
export const PROVIDER_CHOICES = [
  { value: 'synthetic', label: '合成开发样本' },
  { value: 'local', label: '本地 CSV / Parquet' },
  { value: 'nyse', label: 'NYSE 离线原始响应' },
  { value: 'alpaca', label: 'Alpaca 美股行情' },
  { value: 'sec', label: 'SEC 公告与基本面' },
  { value: 'rest', label: '受限 REST' },
  { value: 'yfinance', label: 'yfinance · 美股/ETF 观察日线' },
  { value: 'akshare', label: 'AKShare（新浪）· 六只美股观察日线，仅未复权' },
];
const ORIGINS: Record<string, string> = {
  yfinance: 'https://query2.finance.yahoo.com',
  akshare: 'https://finance.sina.com.cn',
};

// Values verified against FA014's Sina candidate contracts.py; never infer from provider/source IDs.
export const SINA_ORIGIN = 'https://finance.sina.com.cn';
export const SINA_INTERFACE = 'stock_us_daily_sina_unadjusted';
const SINA_SYMBOLS = ['AAPL', 'MSFT', 'NVDA', 'AMD', 'GOOG', 'AMZN'];
export function assertObservationAcquisitionSource(provider: string, source?: RecordData) {
  if (
    provider === 'akshare' &&
    (source?.provider !== 'akshare' ||
      (source.connection as RecordData | undefined)?.endpoint !== SINA_ORIGIN)
  )
    throw new ApiError(
      0,
      'CLIENT_SOURCE_REDECLARATION_REQUIRED',
      '旧Eastmoney来源仅供查看，不能提交新浪采集；请新建明确的AKShare（新浪）来源，并建立对应数据版本。未知或未绑定的来源也不会提交。',
    );
}

export const isSdkProvider = (provider: string) => SDK_PROVIDERS.includes(provider);
const invalid = (message: string) => new ApiError(0, 'INVALID_INPUT', message);
export function requireKnownProvider(provider: string) {
  if (![...LEGACY_PROVIDERS, ...SDK_PROVIDERS].includes(provider))
    throw new ApiError(0, 'UNSUPPORTED_CAPABILITY', '此来源尚不受支持，未提交请求。');
}
export function legacySourceFields(fields: Field[], provider: string): Field[] {
  return fields
    .filter(
      (f) =>
        f.key !== 'provider' &&
        (f.showWhen?.key !== 'provider' || f.showWhen.values.includes(provider)),
    )
    .map((f) => (f.showWhen?.key === 'provider' ? { ...f, showWhen: undefined } : f));
}
export function observationOrigin(provider: string) {
  requireKnownProvider(provider);
  if (!isSdkProvider(provider)) throw invalid('请选择受支持的观察来源。');
  return ORIGINS[provider];
}
export function buildObservationSource(provider: string, v: Record<string, string>) {
  const endpoint = observationOrigin(provider);
  if (!v.source_id?.trim() || !v.name?.trim() || !v.license_id?.trim() || !v.statement?.trim())
    throw invalid('请填写来源标识、名称、用途依据标识和实际用途依据。');
  if (v.affirmed !== 'yes') throw invalid('请主动确认填写的个人开发用途依据。');
  if (!['development', 'development_export'].includes(v.purpose)) throw invalid('请选择用途。');
  return {
    source_id: v.source_id.trim(),
    name: v.name.trim(),
    provider,
    capabilities: ['provider_observation'],
    assets: provider === 'akshare' ? ['US_EQUITY'] : ['US_EQUITY', 'US_ETF'],
    frequencies: ['1Day'],
    fields: [
      'existing_instrument_id',
      'provider_symbol',
      'session_date',
      'open',
      'high',
      'low',
      'close',
      'volume',
      'currency',
    ],
    declared_history: null,
    verified_coverage: [],
    license: {
      license_id: v.license_id.trim(),
      rights_verified: false,
      use_allowed: v.purpose === 'development_export' ? ['development', 'export'] : ['development'],
      evidence: {
        evidence_class: 'PERSONAL_DEVELOPMENT_SOURCE_USE_DECLARATION',
        statement: v.statement.trim(),
        affirmed: true,
      },
    },
    connection: { endpoint, key_env: null, secret_env: null, user_agent_env: null, feed: null },
  };
}
function symbolBinding(
  provider: string,
  symbolInput: string,
  providerInput?: string,
  source?: RecordData,
) {
  requireKnownProvider(provider);
  if (!isSdkProvider(provider)) throw invalid('此操作需要观察来源。');
  const symbol = symbolInput?.trim(),
    mapped = providerInput?.trim();
  if (!symbol || /[,\s]/.test(symbol)) throw invalid('每次请输入一个已绑定的美股/ETF证券代码。');
  if (provider === 'akshare') {
    assertObservationAcquisitionSource(provider, source);
    if (!SINA_SYMBOLS.includes(symbol) || (mapped && mapped !== symbol))
      throw invalid(
        '新浪仅支持AAPL/MSFT/NVDA/AMD/GOOG/AMZN，提供方代码请留空或填写相同plain ticker，不能使用105/106/107前缀。',
      );
  }
  return { symbols: [symbol], ...(mapped ? { provider_symbols: { [symbol]: mapped } } : {}) };
}
export function buildProbeBody(provider: string, v: Record<string, string>, source?: RecordData) {
  requireKnownProvider(provider);
  return isSdkProvider(provider)
    ? symbolBinding(provider, v.observation_symbol, v.provider_symbol, source)
    : {};
}
export function buildObservationParameters(
  provider: string,
  v: Record<string, string>,
  source?: RecordData,
) {
  const binding = symbolBinding(provider, v.observation_symbol, v.provider_symbol, source);
  const start = v.observation_start,
    end = v.observation_end;
  const date = (s: string) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s ?? '')) throw invalid('日期请使用 YYYY-MM-DD。');
    const n = Date.parse(`${s}T00:00:00Z`);
    if (!Number.isFinite(n) || new Date(n).toISOString().slice(0, 10) !== s)
      throw invalid('日期无效。');
    return n;
  };
  const span = date(end) - date(start);
  if (span <= 0 || span > 366 * 86400000)
    throw invalid('开始日期须早于结束日期，范围不得超过366天。');
  if (v.observation_timeframe !== '1Day') throw invalid('此来源首期仅支持观察日线。');
  return { ...binding, start, end, timeframe: '1Day' };
}
export function buildCollectionBody(
  protocol: string,
  v: Record<string, string>,
  source?: RecordData,
) {
  requireKnownProvider(protocol);
  let parameters: unknown;
  if (isSdkProvider(protocol)) parameters = buildObservationParameters(protocol, v, source);
  else if (protocol === 'alpaca')
    parameters = {
      symbols: v.symbols
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      start: v.start,
      end: v.end,
      timeframe: v.timeframe,
      limit: Number(v.limit),
    };
  else if (protocol === 'sec') parameters = { cik: v.cik, kind: v.kind };
  else if (protocol === 'rest') parameters = { query: JSON.parse(v.query) };
  else throw new ApiError(0, 'UNSUPPORTED_CAPABILITY', '此来源使用导入，不支持在线采集。');
  return {
    parameters,
    max_pages: isSdkProvider(protocol) ? 1 : Number(v.max_pages),
    collection_id: v.collection_id || null,
    idempotency_key: v.idempotency_key || null,
  };
}
export function assertDatasetBinding(dataset: RecordData, source: RecordData, expected?: string) {
  const provider = String(source.provider ?? '');
  requireKnownProvider(provider);
  if (
    isSdkProvider(provider) ||
    dataset.data_family === OBSERVATION_FAMILY ||
    (expected && isSdkProvider(expected))
  ) {
    // Verify the frozen response; never retry latest or relax version equality.
    if (source.version !== dataset.source_version)
      throw new ApiError(
        0,
        'CLIENT_SOURCE_BINDING_UNRESOLVED',
        '返回的来源版本与数据版本冻结绑定不一致，未提交请求。',
      );
    if (
      !isSdkProvider(provider) ||
      dataset.data_family !== OBSERVATION_FAMILY ||
      dataset.scope !== 'DEVELOPMENT'
    )
      throw invalid('观察来源须绑定对应的 DEVELOPMENT 观察数据版本。');
    if (expected && provider !== expected) throw invalid('所选协议与数据版本来源不一致。');
  } else if (expected && provider !== expected) throw invalid('所选协议与数据版本来源不一致。');
  return provider;
}
export async function datasetContext(datasetId: string, expected?: string) {
  const dataset = await request<RecordData>(`/data/datasets/${encodeURIComponent(datasetId)}`);
  if (typeof dataset.source_id !== 'string') throw invalid('数据版本未提供来源绑定。');
  if (!Number.isSafeInteger(dataset.source_version) || Number(dataset.source_version) <= 0)
    throw invalid('数据版本未提供有效的冻结来源版本。');
  const source = await request<RecordData>(
    `/data/sources/${encodeURIComponent(dataset.source_id)}?version=${encodeURIComponent(String(dataset.source_version))}`,
  );
  if (source.source_id !== dataset.source_id || source.version !== dataset.source_version)
    throw new ApiError(
      0,
      'CLIENT_SOURCE_BINDING_UNRESOLVED',
      '返回的来源标识或版本与数据版本冻结绑定不一致，未提交请求。',
    );
  return { dataset, source, provider: assertDatasetBinding(dataset, source, expected) };
}
export function validateObservationDataset(scope: string, profile: string, identities: unknown) {
  if (
    scope !== 'DEVELOPMENT' ||
    profile !== 'custom' ||
    !Array.isArray(identities) ||
    !identities.length
  )
    throw invalid('观察来源请选择已授权开发数据，并填写真实证券身份；不能使用合成模板。');
  for (const identity of identities) {
    if (
      !identity ||
      typeof identity !== 'object' ||
      !['US_EQUITY', 'US_ETF'].includes(identity.asset_class) ||
      identity.currency !== 'USD' ||
      ['synthetic-equity', 'synthetic:alpha'].includes(identity.instrument_id)
    )
      throw invalid('请填写真实的 USD 美股/ETF证券身份，不能使用合成示例身份。');
  }
}
export function observationExportAllowed(value: RecordData) {
  const capabilities = value.use_capabilities as RecordData | undefined;
  return capabilities?.display === true && capabilities.export_allowed === true;
}
