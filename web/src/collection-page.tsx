import { useState } from 'react';
import { post } from './api';
import type { RecordData, PageData } from './api';
import {
  ActionForm,
  Panel,
  Resource,
  ResourceSelect,
  Notice,
  ResultSummary,
  useResource,
  ErrorBox,
  HashValue,
} from './components';
import type { Field, Column } from './components';
import { JobDetail } from './pages';
import { ServerRows } from './research-page';
import {
  buildCollectionBody,
  datasetContext,
  SDK_PROVIDERS,
} from './provider-observation-contract';
import { ObservationJobResults } from './provider-observations';

function ReturnedRows({
  value,
  columns,
  empty,
}: {
  value: unknown;
  columns: Column[];
  empty: string;
}) {
  const [page, setPage] = useState(0);
  const rows = Array.isArray(value) ? value : [];
  const current = Math.min(page, Math.max(0, Math.ceil(rows.length / 20) - 1));
  return (
    <>
      <ServerRows
        value={rows.slice(current * 20, current * 20 + 20)}
        columns={columns}
        empty={empty}
      />
      <div className="pagination">
        <small>
          本次响应共 {rows.length} 条 · 第 {current + 1} 页（界面分页）
        </small>
        <div>
          <button
            type="button"
            className="secondary"
            disabled={current === 0}
            onClick={() => setPage(current - 1)}
          >
            前页
          </button>
          <button
            type="button"
            className="secondary"
            disabled={(current + 1) * 20 >= rows.length}
            onClick={() => setPage(current + 1)}
          >
            后页
          </button>
        </div>
      </div>
    </>
  );
}
function CollectionDetails({ id, onResume }: { id: string; onResume: (id: string) => void }) {
  return (
    <Panel title="下载进度与继续下载">
      <Resource path={`/data/collections/${encodeURIComponent(id)}`} polling>
        {(r) => (
          <>
            <ResultSummary value={{ collection_id: r.collection_id }} />
            <h3>已经保存的部分</h3>
            <ReturnedRows
              value={r.pages}
              columns={[
                { key: 'ordinal', label: '页码' },
                { key: 'rows', label: '读取条数' },
                { key: 'appended', label: '新增条数' },
                {
                  key: 'raw',
                  label: '原始数据校验值',
                  render: (x) => <HashValue value={(x.raw as RecordData)?.body_hash} />,
                },
                {
                  key: 'next_cursor',
                  label: '是否还有下一页',
                  render: (x) => (x.next_cursor == null ? '没有' : '有'),
                },
              ]}
              empty="还没有保存任何一页。可在任务记录查看运行状态。"
            />
            <h3>下载记录</h3>
            <ReturnedRows
              value={r.events}
              columns={[
                { key: 'kind', label: '事件' },
                { key: 'code', label: '错误代码' },
              ]}
              empty="还没有下载日志。可在任务记录查看运行状态。"
            />
            <button type="button" className="secondary" onClick={() => onResume(id)}>
              从上次保存的位置继续
            </button>
            <Notice>
              继续下载要用相同的数据版本、来源和设置。页数上限包含已经保存的页。若上次达到上限，请先提高上限。旧任务记录仍可查看。
            </Notice>
            <ResultSummary value={r} />
          </>
        )}
      </Resource>
    </Panel>
  );
}
function CollectionCheckpoint({ job, onResume }: { job: string; onResume: (id: string) => void }) {
  const { data, error, loading } = useResource<PageData>(
    `/jobs/${encodeURIComponent(job)}/events?limit=20&offset=0`,
  );
  if (error) return <ErrorBox error={error} />;
  if (loading) return <Notice>正在查看上次下载保存到哪里…</Notice>;
  const checkpoint = data?.items
    .map((row) => ((row.metadata as RecordData)?.details as RecordData)?.collection_id)
    .find((id) => typeof id === 'string');
  return typeof checkpoint === 'string' ? (
    <CollectionDetails id={checkpoint} onResume={onResume} />
  ) : (
    <Notice>
      暂时找不到可继续下载的位置。请查看任务状态。保存首批数据后，可从保存的位置继续。
    </Notice>
  );
}
export function DataCollection({ canWrite }: { canWrite: boolean }) {
  const [job, setJob] = useState(''),
    [collection, setCollection] = useState(''),
    [resume, setResume] = useState(''),
    [refresh, setRefresh] = useState(0);
  const field = (
    key: string,
    label: string,
    help: string,
    initial = '',
    required = true,
  ): Field => ({ key, label, help, initial, required });
  const when = (values: string[]) => ({ key: 'protocol', values });
  return (
    <>
      <Panel title="数据下载和历史记录">
        <Notice>
          后台会在你核对范围并提交任务后向来源请求数据。下载取决于你的来源权限和实际可取得的日期。练习文件请用导入入口。
        </Notice>
        <label className="field">
          <span>查看以前的下载任务</span>
          <ResourceSelect
            field={{
              key: 'collection_job',
              label: '查看以前的下载任务',
              help: '这里只显示当前列表这页里的下载任务。',
              type: 'select',
              required: false,
              initial: job,
              resource: {
                path: '/jobs',
                idKey: 'job_id',
                labelKeys: ['status', 'created_at', 'job_id'],
                filter: { key: 'handler', value: 'data.collect' },
              },
            }}
            onChange={(id) => {
              setJob(id);
              setCollection('');
            }}
          />
        </label>
      </Panel>
      {canWrite && (
        <ActionForm
          key={`collect-${resume}`}
          title={resume ? '核对范围后继续下载' : '选择范围并下载'}
          fields={[
            {
              key: 'dataset_id',
              label: '要保存到的数据版本',
              help: '系统会核对该版本的来源及访问权限。',
              type: 'select',
              resource: {
                path: '/data/datasets',
                idKey: 'dataset_id',
                labelKeys: ['title', 'source_id', 'scope'],
              },
            },
            {
              key: 'protocol',
              label: '数据来源',
              help: '请选择该数据版本原来使用的来源。REST 还需要管理员允许访问相应地址。',
              type: 'select',
              options: [
                { value: 'alpaca', label: 'Alpaca bars' },
                { value: 'sec', label: 'SEC 公告 / facts' },
                { value: 'rest', label: '高级：受限 REST 查询' },
                { value: 'yfinance', label: 'yfinance · 观察日线' },
                { value: 'akshare', label: 'AKShare（新浪）· 未复权观察日线' },
              ],
            },
            {
              ...field(
                'symbols',
                '证券范围（ticker）',
                '逗号分隔，例如 FAKE；不能空范围代替全市场。',
              ),
              showWhen: when(['alpaca']),
            },
            {
              ...field('start', '采集开始 / UTC', '带 UTC 时区的 ISO 时间，明确获准范围。'),
              showWhen: when(['alpaca']),
            },
            { ...field('end', '采集结束 / UTC', '不得早于开始。'), showWhen: when(['alpaca']) },
            {
              key: 'timeframe',
              label: '采集行情频率',
              help: '符合来源声明能力。',
              type: 'select',
              options: [
                { value: '1Day', label: '日线' },
                { value: '1Min', label: '分钟线' },
              ],
              showWhen: when(['alpaca']),
            },
            {
              ...field('limit', '每页记录上限', '1–10000 整数；不代表全集覆盖。', '100'),
              showWhen: when(['alpaca']),
            },
            {
              ...field('cik', '采集发行人 CIK', '1–10 位数字；不推断与证券映射。'),
              showWhen: when(['sec']),
            },
            {
              key: 'kind',
              label: 'SEC 请求类型',
              help: '原字段独立保留。',
              type: 'select',
              options: [
                { value: 'submissions', label: '公告与历史分页' },
                { value: 'companyfacts', label: 'Company facts' },
              ],
              showWhen: when(['sec']),
            },
            {
              ...field('query', 'REST 查询参数', '高级 JSON；不能覆盖 URL 白名单。', '{"limit":1}'),
              type: 'textarea',
              showWhen: when(['rest']),
            },
            {
              ...field(
                'observation_symbol',
                '观察证券代码',
                '每次一个已绑定的美股/ETF代码，不自动扩大证券范围。',
              ),
              showWhen: when(SDK_PROVIDERS),
            },
            {
              ...field(
                'provider_symbol',
                '提供方证券代码',
                '新浪仅支持六只目录美股，代码留空或填相同ticker（如AAPL），不使用105/106/107前缀；旧Eastmoney来源只读。yfinance相同代码可留空。',
                '',
                false,
              ),
              showWhen: when(SDK_PROVIDERS),
            },
            {
              ...field('observation_start', '观察开始日期', 'YYYY-MM-DD，包含开始日。'),
              showWhen: when(SDK_PROVIDERS),
            },
            {
              ...field('observation_end', '观察结束日期', 'YYYY-MM-DD，不包含结束日；最多366天。'),
              showWhen: when(SDK_PROVIDERS),
            },
            {
              key: 'observation_timeframe',
              label: '观察频率',
              help: '首期仅日线，最多400行，不提供独立tick。',
              type: 'select',
              options: [{ value: '1Day', label: '观察日线' }],
              showWhen: when(SDK_PROVIDERS),
            },
            {
              key: 'observation_basis',
              label: '价格基准',
              help: '这些价格仅供观察，不能证明当天的原始成交价，也不能证明回测当天已能取得。',
              type: 'select',
              options: [{ value: 'PROVIDER_OBSERVED', label: '提供方观察价（非历史原始价）' }],
              showWhen: when(SDK_PROVIDERS),
            },
            {
              ...field('max_pages', '采集总页数上限', '1–10000，续采包含已提交页。', '100'),
              showWhen: when(['alpaca', 'sec', 'rest']),
            },
            field(
              'collection_id',
              '继续下载的记录编号（可选）',
              '新下载请留空。从历史任务继续时会自动填入。',
              resume,
              false,
            ),
            field(
              'idempotency_key',
              '用于识别重复提交的编号（可选）',
              '重试同一次提交可沿用编号。更改范围或继续下载时请用新编号。',
              '',
              false,
            ),
          ]}
          submitLabel={resume ? '提交继续下载任务' : '确认范围并提交下载任务'}
          onSubmit={async (v) => {
            const context = await datasetContext(v.dataset_id, v.protocol);
            const body = buildCollectionBody(v.protocol, v, context.source);
            const r = await post<RecordData>(
              `/data/datasets/${encodeURIComponent(v.dataset_id)}/collect`,
              body,
            );
            setJob(String(r.job_id));
            setCollection(v.collection_id || `collection-${r.run_id}`);
            setRefresh((n) => n + 1);
            return r;
          }}
        />
      )}
      {job && (
        <>
          <a className="text-button" href={`#jobs?job=${encodeURIComponent(job)}`}>
            在任务记录查看这次下载 →
          </a>
          <JobDetail
            id={job}
            canWrite={canWrite}
            refresh={refresh}
            changed={(id) => {
              if (id) setJob(id);
              setRefresh((n) => n + 1);
            }}
          />
          <Resource path={`/jobs/${encodeURIComponent(job)}`} polling>
            {(r) => {
              const saved = (r.result as RecordData)?.collection_id;
              const id = typeof saved === 'string' ? saved : collection;
              return (
                <>
                  {r.result && typeof r.result === 'object' && !Array.isArray(r.result) && (
                    <ObservationJobResults value={r.result as RecordData} />
                  )}
                  {id ? (
                    <CollectionDetails id={id} onResume={setResume} />
                  ) : (
                    <CollectionCheckpoint job={job} onResume={setResume} />
                  )}
                </>
              );
            }}
          </Resource>
        </>
      )}
    </>
  );
}
