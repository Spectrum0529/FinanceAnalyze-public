import { useRef, useState } from 'react';
import type { RecordData } from './api';
import { ErrorBox, Notice } from './components';
import { recoverTeamWrite } from './team-write';

export function TeamWriteRecovery({ owner, disabled, recovered }: {
  owner: string;
  disabled: boolean;
  recovered: (receipt: RecordData) => void;
}) {
  const gate = useRef(false);
  const [busy, setBusy] = useState(false);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function confirm(retry = false) {
    if (disabled || gate.current) return;
    gate.current = true;
    setBusy(true);
    setMissing(false);
    setError(null);
    try {
      const receipt = await recoverTeamWrite(owner, retry);
      if (receipt) recovered(receipt);
      else setMissing(true);
    } catch (failure) { setError(failure); }
    finally {
      gate.current = false;
      setBusy(false);
    }
  }

  return <Notice>
    <p>上次修改团队还没有确认结果。</p>
    <button type="button" disabled={disabled || busy} onClick={() => void confirm()}>查看上次团队修改结果</button>
    {missing && <>
      <p>暂时找不到结果。你可以再查一次，或按原来的内容重试。</p>
      <button type="button" disabled={disabled || busy} onClick={() => void confirm(true)}>按原内容重试团队修改</button>
    </>}
    {error != null && <ErrorBox error={error} />}
  </Notice>;
}
