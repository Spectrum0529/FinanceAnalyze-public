import { useResource } from './components';
import { datasetBacktestHref } from './index-benchmarks';
import type { RecordData } from './api';

export function SavedDataBacktestLink({ reference, jobId }: { reference: RecordData | null; jobId?: string }) {
  const href = datasetBacktestHref(reference?.dataset_id, reference?.version, reference?.content_hash);
  const id = href ? String(reference?.dataset_id) : '';
  const { data } = useResource<RecordData>(id ? '/data/library/datasets/' + encodeURIComponent(id) : null);
  if (!href || !data || data.dataset_id !== id || data.kind !== 'dataset' || data.trashed === true ||
      data.version !== reference?.version || data.content_hash !== reference?.content_hash ||
      (jobId !== undefined && data.origin_job_id !== jobId)) return null;
  const metadata = data.metadata;
  if (metadata && typeof metadata === 'object' && !Array.isArray(metadata) && metadata.currency === 'CNY') {
    return <p>这份是人民币行情。可在已保存数据中查看，美元组合回测无法使用。</p>;
  }
  return <a href={href}>用这份数据回测 →</a>;
}
