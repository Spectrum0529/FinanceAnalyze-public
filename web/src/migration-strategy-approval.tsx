import { useEffect, useRef, useState } from 'react';
import { ApiError, request } from './api';
import type { RecordData } from './api';
import { ErrorBox, useResource } from './components';
import { record } from './native-research-api';
import { accountWrite, hasCode } from './platform-account-session';
import type { AccountReauthentication } from './platform-account-session';
import { RecentProof } from './recent-proof';

const migrationPath = '/onboarding/migration';
const approvalErrors: Record<string, string> = {
  MIGRATION_STRATEGY_HASH_MISMATCH: '当前策略包与原迁入记录的校验值不一致，请重新打开策略详情核对。',
  MIGRATION_STRATEGY_PACKAGE_CHANGED: '已保存的策略包未通过复验，无法批准运行。',
};

export function MigrationStrategyApproval({ selected, ownerId, reauthenticate, motion, approved }: {
  selected: RecordData;
  ownerId: string;
  reauthenticate: AccountReauthentication;
  motion: () => boolean;
  approved: () => void;
}) {
  const migration = useResource<RecordData>(migrationPath);
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [proof, setProof] = useState<{ otp: boolean } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const requestLife = useRef<AbortController | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => () => requestLife.current?.abort(), []);
  const { strategy_id: id, version, package_hash: hash } = selected;
  const rows = Array.isArray(migration.data?.imported) ? migration.data.imported : [];
  const isMigrated = rows.some(value => {
    const row = record(value), target = record(row.target);
    return row.kind === 'strategy' && target.strategy_id === id && target.version === version && target.package_hash === hash;
  });
  if (!isMigrated || record(migration.data?.actions).approve_strategy !== true ||
      typeof id !== 'string' || typeof version !== 'string' || typeof hash !== 'string' || !/^[a-f0-9]{64}$/.test(hash)) return null;

  async function approve(allowProof = true) {
    if (!checked || requestLife.current) return;
    const operation = new AbortController();
    requestLife.current = operation;
    const { signal } = operation;
    setBusy(true);
    setError(null);
    try {
      const path = migrationPath + '/strategies/' + encodeURIComponent(String(id)) + '/' + encodeURIComponent(String(version)) + '/approve';
      const value = await accountWrite<RecordData>(path, 'POST', { package_hash: hash }, signal);
      if (value.owner_id !== ownerId || value.strategy_id !== id || value.version !== version ||
          value.package_hash !== hash || value.trusted !== true) {
        throw new Error('暂未确认此版本获准运行。请刷新策略列表核对。');
      }
      approved();
    } catch (cause) {
      if (signal.aborted) return;
      if (allowProof && hasCode(cause, 'REAUTH_REQUIRED')) {
        try {
          const factors = await request<RecordData>('/auth/mfa', { signal });
          if (typeof factors.enabled === 'boolean' && (!factors.enabled || factors.available === true)) {
            setProof({ otp: factors.enabled });
            return;
          }
        } catch { /* Keep the original authorization failure visible. */ }
      }
      if (!signal.aborted) setError(cause instanceof ApiError && approvalErrors[cause.code] ? new Error(approvalErrors[cause.code]) : cause);
    } finally {
      requestLife.current = null;
      if (!signal.aborted) setBusy(false);
    }
  }
  if (selected.trusted === true) return <p role="status">这个版本已获准运行。</p>;
  return <>
    <details>
      <summary>审核并允许运行</summary>
      <p>只批准你已核对来源、入口代码和依赖锁的策略包。核对的是 {id} 版本 {version}。</p>
      <p>策略包校验值（SHA-256）<code>{hash}</code></p>
      <label><input type="checkbox" checked={checked} disabled={busy || !!proof}
        onChange={event => setChecked(event.currentTarget.checked)} />我已核对这份策略包，允许这个版本在本机运行</label>
      <button ref={button} type="button" disabled={!checked || busy || !!proof} onClick={() => void approve()}>批准这个版本</button>
      {error != null && <ErrorBox error={error} />}
    </details>
    {proof && <RecentProof needsOtp={proof.otp} verify={reauthenticate} opener={button.current} motion={motion}
      description="请验证本机账户身份，以批准刚才核对的策略版本。"
      done={verified => { setProof(null); if (verified) void approve(false); }} />}
  </>;
}
