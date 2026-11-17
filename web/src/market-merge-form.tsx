import { useState } from 'react';
import { post } from './api';
import type { RecordData } from './api';
import { ErrorBox, JsonView } from './components';
import { AreaHelp } from './area-help';
import { record } from './native-research-api';
import type { MarketRef } from './market-data-manager';

const path = '/data/library/managed';
const policies: Record<string, string> = {
  reject: '相同记录只留一份，有差异就暂停',
  prefer_first: '冲突时用第一份',
  prefer_second: '冲突时用第二份',
  use_verification: '用一份新行情核对全段',
};
const reasons: Record<string, string> = {
  MARKET_MERGE_CONFLICT: '同一天的价格或成交量不同。请查看差异，再选择用哪份。',
  MARKET_MERGE_INCOMPATIBLE: '这两份数据的证券、来源或价格算法不同，不能合并。',
  MARKET_MERGE_ADJUSTMENT_UNVERIFIED: '请另下载一份涵盖两段日期的行情，用它核对复权价格。',
  MARKET_MERGE_INCOMPATIBLE_VERIFICATION: '用来核对的行情与所选数据的来源、证券、日期或价格算法不一致。',
  MARKET_MERGE_VERIFICATION_CHANGED: '新下载的行情中有变动或新增的记录。请先查看差异，再决定是否使用它。',
  MARKET_MERGE_VERIFICATION_COVERAGE: '新下载的行情缺少原数据里的交易日。请换一份完整行情。',
};
const same = (value: unknown, expected: MarketRef) => {
  const ref = record(value);
  return ref.source === expected.source && ref.id === expected.id &&
    ref.version === expected.version && ref.content_hash === expected.content_hash;
};
const keyFor = (ref: MarketRef) => JSON.stringify([ref.source, ref.id, ref.version, ref.content_hash]);
const previewValue = (value: unknown) => typeof value === 'number' || typeof value === 'string' && value ? value : '未提供';
const rowCount = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? `${value} 行` : '未提供';

export function MarketMergeForm({ choices, connections, saved, prepareVerification }: {
  choices: { reference: MarketRef; label: string }[];
  connections: RecordData[];
  saved: (id: string) => void;
  prepareVerification?: (spec: RecordData) => void;
}) {
  const [verification, setVerification] = useState('');
  const [policy, setPolicy] = useState('reject');
  const [name, setName] = useState(''), [connection, setConnection] = useState('');
  const [preview, setPreview] = useState<RecordData | null>(null);
  const [pending, setPending] = useState<RecordData | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState<unknown>(null);
  const [complete, setComplete] = useState('');
  if (choices.length < 2) return <p>勾选两份行情，查看哪些交易日重叠、数据是否一致。</p>;
  const inputs = choices.slice(0, 2).map(row => row.reference);
  const checks = choices.slice(2);
  const verified = checks.find(row => keyFor(row.reference) === verification)?.reference;
  const command = { inputs, conflict_policy: policy, ...(verified ? { verification_reference: verified } : {}) };
  const requested = record(preview?.requested_range);
  const actual = record(preview?.coverage);
  const check = record(preview?.verification_request);
  const matches = preview && preview.conflict_policy === policy &&
    (verified ? same(preview.verification_reference, verified) : preview.verification_reference == null);
  const options = Array.isArray(preview?.allowed_policies)
    ? preview.allowed_policies.filter((item): item is string => typeof item === 'string' && Object.hasOwn(policies, item))
    : ['reject'];

  async function inspect() {
    if (busy || pending) return;
    setPreview(null); setBusy(true); setError(null);
    try {
      const value = await post<RecordData>(path + '/merge-preview', command);
      if (!Array.isArray(value.inputs) || value.inputs.length !== 2 || !value.inputs.every((ref, index) => same(ref, inputs[index])) ||
          value.conflict_policy !== policy || (verified ? !same(value.verification_reference, verified) : value.verification_reference != null) ||
          typeof value.preview_hash !== 'string' || !/^[0-9a-f]{64}$/.test(value.preview_hash)) {
        throw Error('无法确认预览对应当前选择。请再点一次“预览合并”。');
      }
      setPreview(value);
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  }
  async function publish() {
    if (busy || complete || !preview || preview.can_merge !== true || !matches || !name.trim()) return;
    const original = pending ?? {
      ...command, preview_hash: preview.preview_hash, name: name.trim(), idempotency_key: crypto.randomUUID(),
      ...(connection ? { connection_id: connection } : {}),
    };
    setPending(original); setBusy(true); setError(null);
    try {
      const receipt = await post<RecordData>(path + '/merge', original);
      if (typeof receipt.dataset_id !== 'string' || typeof receipt.version !== 'number' ||
          typeof receipt.content_hash !== 'string') throw Error('还不能确认新行情已保存。请点“核对上次保存”。');
      setPending(null); setComplete(receipt.dataset_id); saved(receipt.dataset_id);
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  }
  return <fieldset aria-label="合并行情版本">
    <legend>合并两份行情</legend>
    <AreaHelp topic="market-merge" title="合并两份行情" />
    <p>预览比较已保存的行情，不下载新数据。保存后会生成一份新数据，原行情仍保留。</p>
    <ol>{choices.slice(0, 2).map(item => <li key={keyFor(item.reference)}>{item.label} · 版本 {item.reference.version}</li>)}</ol>
    {checks.length > 0 && <label>用于核对全段的行情（可选）<select value={verification} disabled={busy || !!pending}
      onChange={event => { setVerification(event.target.value); setPolicy('reject'); }}>
      <option value="">暂不选核对行情</option>
      {checks.map(item => <option key={keyFor(item.reference)} value={keyFor(item.reference)}>{item.label} · 版本 {item.reference.version}</option>)}
    </select></label>}
    <label>合并规则<select value={policy} disabled={busy || !!pending} onChange={event => setPolicy(event.target.value)}>
      {(options.includes(policy) ? options : [...options, policy]).map(value => <option key={value} value={value}>{policies[value]}</option>)}
    </select></label>
    <button type="button" disabled={busy || !!pending} onClick={() => void inspect()}>预览合并</button>
    {preview && <div aria-label="合并预览">
      <p>所选日期 {previewValue(requested.start)} 至 {previewValue(requested.end)}（含首尾）。实际有数据的日期 {previewValue(actual.start)} 至 {previewValue(actual.end)}，记录数：{rowCount(preview.rows)}。</p>
      <p>相同记录：{rowCount(preview.duplicates)}；同日数据不同：{rowCount(preview.conflict_count)}。</p>
      {preview.can_merge !== true && <p role="status">{reasons[String(preview.reason_code)] ?? '暂不能合并所选行情。请检查日期、来源和价格算法。'}</p>}
      {preview.requires_verification === true && <p>
        请重新下载 {Array.isArray(check.tickers) ? check.tickers.join('、') : '同一批证券'}，{String(check.start)} 至 {String(check.end)}。
        {prepareVerification && <button type="button" onClick={() => prepareVerification(check)}>带入证券和日期</button>}
        下载保存后，在行情列表勾选它，再从“用于核对全段的行情”中选中。
      </p>}
      {preview.verification_reference != null && <p>
        和新下载的数据相比，相同：{rowCount(preview.verification_matching_rows)}；不同：{rowCount(preview.verification_changed_rows)}。
        新下载多出的记录：{rowCount(preview.verification_added_rows)}；少了原数据中的记录：{rowCount(preview.verification_missing_rows)}。<br />若同一天的记录来自两份原行情，核对结果会分别计入。
      </p>}
      {[preview.conflicts, preview.verification_conflicts, preview.verification_missing].some(value => Array.isArray(value) && value.length) &&
        <details><summary>查看价格、成交量或日期差异的原始记录</summary>
          <JsonView value={{ conflicts: preview.conflicts, verification: preview.verification_conflicts, missing: preview.verification_missing }} />
        </details>}
      {matches && preview.can_merge === true && <div>
        {preview.verification_reference != null && <p>保存后会用新下载的完整行情生成一份新数据，两份原行情仍保留。</p>}
        <label>合并后的行情名称<input value={name} disabled={busy || !!pending} onChange={event => setName(event.target.value)} maxLength={120} /></label>
        <label>关联的来源连接<select value={connection} disabled={busy || !!pending} onChange={event => setConnection(event.target.value)}>
          <option value="">使用同一来源的已有连接</option>
          {connections.map(row => <option key={String(row.connection_id)} value={String(row.connection_id)}>{String(row.name)}</option>)}
        </select></label>
        <button type="button" disabled={busy || !name.trim() || !!complete} onClick={() => void publish()}>
          {pending ? '核对上次保存' : '保存合并后的行情'}
        </button>
      </div>}
    </div>}
    {error != null && <ErrorBox error={error} />}
  </fieldset>;
}
