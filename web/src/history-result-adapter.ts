import { ApiError, request } from './api';
import type { RecordData } from './api';
import {
  changeSingleResult,
  normalizeManagedResult,
  readResultForReconcile,
  resultListPath,
} from './history-research-adapter';
import type { HistoryUiPort, ResultCommand, ResultOutcome } from './history-view-model';
// Frontend sequential orchestration over the published single-result lifecycle route.
// No batch endpoint/store is invented. Only explicit confirmation enters changeResults.
export function createResultLifecycleAdapter(): Pick<
  HistoryUiPort,
  'listResults' | 'changeResults' | 'lookupChange'
> {
  const pending = new Map<
    string,
    {
      command: ResultCommand;
      signature: string;
      outcome: ResultOutcome;
      unknownIndex: number | null;
    }
  >();
  const matches = (row: RecordData, command: ResultCommand, index: number) =>
    row.run_id === command.targets[index].id &&
    Number(row.revision) > Number(command.targets[index].revision) &&
    (command.kind === 'rename'
      ? row.name === command.name && row.deleted === false
      : row.deleted === (command.kind === 'delete'));
  const certain = (e: unknown) =>
    e instanceof ApiError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status);
  return {
    async listResults(trash, offset, limit, signal) {
      const page = await request<RecordData>(resultListPath(trash, offset, limit), { signal });
      if (
        !Array.isArray(page.items) ||
        page.items.length > limit ||
        page.offset !== offset ||
        page.limit !== limit ||
        !Number.isSafeInteger(page.total) ||
        Number(page.total) < 0
      )
        throw new Error('结果列表格式不完整。');
      return {
        items: page.items.map((v) => normalizeManagedResult(v as RecordData)),
        total: Number(page.total),
        offset,
        limit,
      };
    },
    async changeResults(command) {
      const signature = JSON.stringify(command),
        old = pending.get(command.requestId);
      if (old) {
        if (old.signature !== signature)
          throw new Error('操作编号对应的内容已变化，请核对原操作。');
        return old.outcome;
      }
      const state = {
        command: structuredClone(command),
        signature,
        outcome: {
          confirmed: true,
          completed: [],
          failures: [],
        } as ResultOutcome,
        unknownIndex: null as number | null,
      };
      pending.set(command.requestId, state);
      for (let i = 0; i < command.targets.length; i++) {
        try {
          const row = await changeSingleResult(command, i);
          if (!matches(row, command, i)) throw new Error('服务返回的记录与所选操作不一致。请先刷新查看结果状态。');
          state.outcome.completed.push(command.targets[i].id);
        } catch (e) {
          if (certain(e)) {
            state.outcome.failures.push({
              id: command.targets[i].id,
              message: e instanceof Error ? e.message : '操作被拒绝',
            });
            continue;
          }
          state.unknownIndex = i;
          state.outcome.confirmed = false;
          for (let j = i + 1; j < command.targets.length; j++)
            state.outcome.failures.push({
              id: command.targets[j].id,
              message: '本项未尝试；前一项状态待确认。',
            });
          break;
        }
      }
      return structuredClone(state.outcome);
    },
    async lookupChange(key, signal) {
      const state = pending.get(key);
      if (!state) throw new Error('找不到本次操作记录。请刷新并查看结果，避免重复提交。');
      if (state.unknownIndex === null) return structuredClone(state.outcome);
      signal.throwIfAborted();
      const i = state.unknownIndex,
        row = await readResultForReconcile(state.command.targets[i].id);
      signal.throwIfAborted();
      // This confirms the desired CURRENT state, not who performed an intervening change.
      if (matches(row, state.command, i)) {
        state.outcome.completed.push(state.command.targets[i].id);
        state.outcome.confirmed = true;
        state.unknownIndex = null;
      } else if (
        typeof row.revision === 'number' &&
        row.revision > Number(state.command.targets[i].revision)
      ) {
        state.outcome.failures.push({
          id: state.command.targets[i].id,
          message: '记录已被其他操作修改，请刷新后重新选择。',
        });
        state.outcome.confirmed = true;
        state.unknownIndex = null;
      }
      return structuredClone(state.outcome);
    },
  };
}
