import { useState } from 'react';
import { useResource } from './components';
import { ModelCallUsage } from './model-call-usage';
import { record } from './native-research-api';
import { goalTaskPath } from './goal-research-api';

const pageSize = 20;
const stageNames = { PLAN: '理解目标', REVIEW: '检查研究结果' } as const;

function UsagePage({ taskId, revision, offset, onPrevious, onNext }: {
  taskId: string; revision: number; offset: number;
  onPrevious?: () => void; onNext: (offset: number) => void;
}) {
  const { data, error, loading } = useResource<unknown>(
    goalTaskPath(taskId) + '/model-usage?limit=' + pageSize + '&offset=' + offset, revision,
  );
  const page = record(data);
  const seen = new Set<string>();
  const rows = page.model_observations;
  const items = Array.isArray(rows) ? rows.map(record).filter((item) => {
    if (typeof item.effect_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(item.effect_id) ||
        seen.has(item.effect_id) || (item.stage !== 'PLAN' && item.stage !== 'REVIEW') ||
        (item.availability !== 'RECORDED' && item.availability !== 'UNKNOWN')) return false;
    seen.add(item.effect_id);
    return true;
  }) : [];
  const valid = page.schema === 'goal-model-usage-1' && page.task_id === taskId && page.limit === pageSize && page.offset === offset &&
    Array.isArray(rows) && rows.length <= pageSize && items.length === rows.length;
  const next = valid && typeof page.next_offset === 'number' && Number.isInteger(page.next_offset) &&
    page.next_offset === offset + items.length && page.next_offset > offset && page.next_offset <= 10000
    ? page.next_offset : undefined;
  if (loading) return <p role="status">正在读取实际模型用量…</p>;
  return <>
    {error != null || !valid || items.length === 0 ? <p>实际模型用量未提供。</p> :
      items.map(item => <div key={String(item.effect_id)}>
        {item.availability === 'UNKNOWN' && <p>此调用的实际 Token 用量未提供。</p>}
        <ModelCallUsage title={stageNames[item.stage as keyof typeof stageNames]} metrics={item.metrics} />
      </div>)}
    {(onPrevious || next !== undefined) && <div className="pagination">
      <button type="button" disabled={!onPrevious} onClick={onPrevious}>上一页用量</button>
      <button type="button" disabled={next === undefined} onClick={() => { if (next !== undefined) onNext(next); }}>下一页用量</button>
    </div>}
  </>;
}

export function GoalModelUsage({ taskId, revision }: { taskId: string; revision: number }) {
  const [positions, setPositions] = useState([0]);
  const offset = positions.at(-1) ?? 0;
  return <UsagePage key={offset} taskId={taskId} revision={revision} offset={offset}
    onPrevious={positions.length > 1 ? () => setPositions(value => value.slice(0, -1)) : undefined}
    onNext={position => setPositions(value => [...value, position])} />;
}
