import { post, request } from './api';
import type { RecordData } from './api';
import type { HistoryIntent } from './history-view-model';
import { researchHistoryBody } from './history-research-adapter';
import { isCertainDailyRejection } from './provider-backtest-api';
import { dailyStorageKey } from './provider-backtest-page';
// Reuse the current per-user receipt slot; never create a second browser history store.
export function createHistorySubmissionAdapter(userId: string) {
  if (!userId) throw new Error('请先登录，才能把提交记录保存到你的账户。');
  const slot = dailyStorageKey(userId);
  const read = () => {
    const raw = sessionStorage.getItem(slot);
    return raw ? (JSON.parse(raw) as { request: RecordData; jobId?: string }) : null;
  };
  const checked = (job: RecordData) => {
    if (
      job.owner_id !== userId ||
      job.handler !== 'research.daily_backtest' ||
      typeof job.job_id !== 'string' ||
      !job.job_id
    )
      throw new Error('原提交的任务归属或类型不匹配。');
    return { jobId: job.job_id };
  };
  async function lookup(key: string) {
    const saved = read();
    if (!saved || saved.request.idempotency_key !== key)
      throw new Error('找不到上次提交记录。请查看任务列表。');
    const job = await request<RecordData>(`/research/daily/submissions/${encodeURIComponent(key)}`);
    const receipt = checked(job);
    try {
      sessionStorage.setItem(slot, JSON.stringify({ ...saved, jobId: receipt.jobId }));
    } catch {
      /* Original request/key already persisted; GET recovery remains possible. */
    }
    return receipt;
  }
  return {
    lookup,
    async start(intent: HistoryIntent, key: string) {
      const body = researchHistoryBody(intent, key),
        saved = read();
      if (saved) {
        if (
          saved.request.idempotency_key !== key ||
          JSON.stringify(saved.request) !== JSON.stringify(body)
        )
          throw new Error('请先核对当前原提交；不会更换编号或覆盖选择。');
        return lookup(key);
      }
      sessionStorage.setItem(slot, JSON.stringify({ request: body })); // Fail before POST if persistence is unavailable.
      try {
        const job = await post<RecordData>('/research/daily/backtests', body);
        const receipt = checked(job);
        try {
          sessionStorage.setItem(slot, JSON.stringify({ request: body, jobId: receipt.jobId }));
        } catch {
          /* Keep the saved original key; do not resend. */
        }
        return receipt;
      } catch (error) {
        if (isCertainDailyRejection(error)) {
          const current = read();
          if (current?.request.idempotency_key === key) sessionStorage.removeItem(slot);
        }
        throw error;
      }
    },
  };
}
