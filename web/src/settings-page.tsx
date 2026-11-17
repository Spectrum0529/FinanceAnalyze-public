import { StorageLocationsSettings } from './storage-locations-settings';
import { ComputationCacheSettings } from './computation-cache-settings';
import { useState } from 'react';
import { post, idOf, display } from './api';
import type { RecordData } from './api';
import {
  ActionForm,
  DataTable,
  Notice,
  Panel,
  Resource,
  Status,
  JsonView,
  ResultSummary,
} from './components';
import { SourceForm } from './data-page';
import { datasetContext, buildProbeBody, isSdkProvider } from './provider-observation-contract';
import { JobDetail } from './pages';
import { ServerRows } from './research-page';
export function Settings({ isAdmin, storageCanWrite = isAdmin, cacheCanWrite = storageCanWrite, storageOwner = '', showMigration = false }: { isAdmin: boolean; storageCanWrite?: boolean; cacheCanWrite?: boolean; storageOwner?: string; showMigration?: boolean }) {
  const [refresh, setRefresh] = useState(0),
    [source, setSource] = useState(''),
    [probeJob, setProbeJob] = useState('');
  const [advancedOpened, setAdvancedOpened] = useState(false);
  return (
    <>
      {showMigration && <Panel title="迁移到另一台电脑" helpTopic="migration"><a href="#settings?section=migration">打开迁机助手 →</a></Panel>}
      <StorageLocationsSettings key={storageOwner} ownerId={storageOwner} canWrite={storageCanWrite}/>
      <ComputationCacheSettings key={storageOwner} ownerId={storageOwner} canWrite={cacheCanWrite}/>
      {isAdmin && (
        <>
          <Panel title="后台任务与恢复">
            <Resource path="/operations/health" refresh={refresh}>
              {(r) => <OperationsHealth value={r} />}
            </Resource>
          </Panel>
          <ActionForm
            title="故障报告"
            fields={[]}
            submitLabel="生成故障报告（隐藏敏感信息）"
            onSubmit={() => post('/operations/diagnostics')}
          />
        </>
      )}
      <details className="panel" onToggle={event => { if (event.currentTarget.open) setAdvancedOpened(true); }}>
        <summary>高级数据连接设置</summary>
        <p>用于查看来源支持的功能、排查已保存的连接，或对指定的数据版本运行只读测试。</p>
        {advancedOpened && <>
          <Panel title="数据来源支持范围">
            <Resource path="/data/catalog">
              {(r) => (
                <ServerRows
                  value={r.items}
                  columns={[
                    { key: 'name', label: '来源' },
                    { key: 'provider', label: '来源类型' },
                    { key: 'capabilities', label: '支持的功能' },
                    { key: 'frequencies', label: '频率' },
                    { key: 'history', label: '历史数据范围' },
                    { key: 'external_dependency', label: '使用条件' },
                    {
                      key: 'verified_market_coverage',
                      label: '已验证市场范围',
                      render: () => '尚未确认，查看来源详情',
                    },
                  ]}
                  empty="来源目录尚未提供。"
                />
              )}
            </Resource>
          </Panel>
          <Panel title="已保存的数据连接">
            <DataTable
              path="/data/sources"
              columns={[
                { key: 'source_id', label: '来源编号' },
                { key: 'name', label: '名称' },
                { key: 'provider', label: '来源类型' },
                { key: 'connection', label: '连接地址与密钥引用' },
                { key: 'verified_coverage', label: '已核对的数据范围' },
              ]}
              refresh={refresh}
              empty="还没有保存数据来源。可以先导入练习数据。"
              onSelect={(r) => setSource(idOf(r))}
            />
          </Panel>
          {isAdmin && <SourceForm changed={() => setRefresh((n) => n + 1)} />}
          {source && (
            <Panel title="保存过的来源设置">
              <Resource path={`/data/sources/${encodeURIComponent(source)}`} refresh={refresh} />
            </Panel>
          )}
          {isAdmin && (
            <ActionForm
              title="测试数据来源"
              fields={[
                {
                  key: 'dataset_id',
                  label: '使用该来源的数据版本',
                  type: 'select',
                  help: '请选择在数据管理中保存的版本。',
                  resource: {
                    path: '/data/datasets',
                    idKey: 'dataset_id',
                    labelKeys: ['title', 'source_id', 'dataset_id'],
                  },
                },
                { key: 'observation_symbol', label: '要测试的证券代码', required: false, help: '测试 yfinance 或 AKShare 时，填写这个数据版本里的一只证券。测试其他来源可以留空。' },
                { key: 'provider_symbol', label: '来源使用的证券代码', required: false, help: '测试 AKShare 时必填。测试 yfinance 时，如果与上面相同，可留空。' },
              ]}
              submitLabel="运行只读测试"
              onSubmit={async (v) => {
                const context = await datasetContext(v.dataset_id);
                const result = await post<RecordData>(`/data/datasets/${encodeURIComponent(v.dataset_id)}/connection-test`, buildProbeBody(context.provider, v));
                if (isSdkProvider(context.provider) && typeof result.job_id === 'string') setProbeJob(result.job_id);
                return result;
              }}
            />
          )}
          {probeJob && <JobDetail id={probeJob} canWrite={isAdmin} refresh={refresh} changed={(id) => { if (id) setProbeJob(id); setRefresh((n) => n + 1); }} />}
          <Notice>
            测试只检查登录和数据访问情况。使用许可仍需另行确认。官方设置说明
            <a className="text-button" href="https://docs.alpaca.markets/docs/market-data-api">
              Alpaca Market Data
            </a>{' '}
            ·{' '}
            <a
              className="text-button"
              href="https://www.sec.gov/search-filings/edgar-application-programming-interfaces"
            >
              SEC EDGAR API
            </a>
            。
          </Notice>
        </>}
      </details>
      {isAdmin && (
        <>
          <ActionForm
            title="创建工作区账户"
            fields={[
              {
                key: 'username',
                label: '用户名',
                help: '输入新账户的登录名。',
              },
              {
                key: 'password',
                label: '初始密码',
                type: 'password',
                help: '请用 15 到 128 个字符，并避开常见密码。',
              },
              {
                key: 'role',
                label: '角色',
                type: 'select',
                options: [
                  { value: 'researcher', label: '研究员 · 查看和修改自己的内容' },
                  { value: 'viewer', label: '只读成员 · 查看获准的内容' },
                  { value: 'admin', label: '管理员 · 管理账户和服务' },
                ],
                help: '研究员和只读成员无权查看他人的账户和密钥。',
              },
            ]}
            submitLabel="创建账户"
            onSubmit={async (v) => {
              const length = Array.from(v.password).length;
              if (length < 15 || length > 128) throw new Error('初始密码需要 15 到 128 个字符。');
              const r = await post('/users', v);
              setRefresh((n) => n + 1);
              return r;
            }}
          />
          <Panel title="账户与权限">
            <DataTable
              path="/users"
              columns={[
                { key: 'user_id', label: '标识' },
                { key: 'username', label: '用户' },
                { key: 'role', label: '角色' },
                { key: 'created_at', label: '创建时间 / UTC' },
              ]}
              refresh={refresh}
              empty="暂无可查看的账户。"
            />
          </Panel>
        </>
      )}
    </>
  );
}
const readinessLabels = { status: { ready: '已就绪' } };
const notificationLabels = {
  configuration_effect: { RESTART_REQUIRED: '重启服务后生效' },
  provider_verification: { NOT_RUN: '尚未验证' },
  scope: { LOCAL_RECEIVER_ONLY: '仅在本机测试' },
};

export function Operations({ isAdmin }: { isAdmin: boolean }) {
  const [refresh, setRefresh] = useState(0);
  return (
    <>
      <Panel title="服务运行状态">
        <Resource path="/health/ready" refresh={refresh}>
          {(r) => <ResultSummary value={r} valueLabels={readinessLabels} />}
        </Resource>
      </Panel>
      {isAdmin ? (
        <>
          <Panel title="后台任务与恢复">
            <Resource path="/operations/health" refresh={refresh}>
              {(r) => <OperationsHealth value={r} />}
            </Resource>
          </Panel>
          <ActionForm
            title="检查运行问题"
            fields={[]}
            submitLabel="检查并记录告警"
            onSubmit={async () => {
              const r = await post('/operations/alerts/evaluate');
              setRefresh((n) => n + 1);
              return r;
            }}
          />
          <Panel title="本地告警">
            <DataTable
              path="/operations/alerts"
              columns={[
                { key: 'alert_id', label: '标识' },
                { key: 'code', label: '原因' },
                { key: 'severity', label: '级别' },
                { key: 'recorded_at', label: '时间 / UTC' },
              ]}
              refresh={refresh}
              empty="暂无告警。"
            />
          </Panel>
          <Panel title="本机通知与发送记录">
            <Resource path="/operations/notifications" refresh={refresh}>
              {(r) => (
                <>
                  <ResultSummary value={r} valueLabels={notificationLabels} />
                  <Notice>
                    通知只发送到这台电脑已设置的接收地址。修改设置后需重启服务。发送完毕后，请查看下方“实际结果”。
                  </Notice>
                  <ActionForm
                    title="测试本机通知"
                    fields={[]}
                    disabled={r.enabled !== true || r.configured !== true}
                    submitLabel="发送测试通知"
                    onSubmit={async () => {
                      const result = await post('/operations/notifications/test', {});
                      setRefresh((n) => n + 1);
                      return result;
                    }}
                  />
                  <ActionForm
                    title="发送已保存的告警"
                    disabled={r.enabled !== true || r.configured !== true}
                    fields={[
                      {
                        key: 'alert_id',
                        label: '已保存告警',
                        type: 'select',
                        help: '这里只发送所选告警的摘要。',
                        resource: {
                          path: '/operations/alerts',
                          idKey: 'alert_id',
                          labelKeys: ['code', 'severity', 'recorded_at'],
                        },
                      },
                    ]}
                    submitLabel="发送到本机"
                    onSubmit={async (v) => {
                      const result = await post(
                        `/operations/alerts/${encodeURIComponent(v.alert_id)}/notify`,
                        {},
                      );
                      setRefresh((n) => n + 1);
                      return result;
                    }}
                  />
                </>
              )}
            </Resource>
            <DataTable
              path="/operations/notifications/deliveries"
              refresh={refresh}
              columns={[
                { key: 'delivery_id', label: '投递记录' },
                { key: 'alert_id', label: '所选告警' },
                { key: 'recorded_at', label: '时间 / UTC' },
                { key: 'outcome', label: '实际结果', render: (r) => <Status value={r.outcome} /> },
              ]}
              empty="暂无发送记录。"
            />
          </Panel>
          <Panel title="关键操作记录">
            <DataTable
              path="/audit"
              columns={[
                { key: 'event_id', label: '事件' },
                { key: 'action', label: '操作' },
                { key: 'resource_id', label: '资源' },
                { key: 'recorded_at', label: '时间 / UTC' },
                { key: 'details', label: '详情' },
              ]}
              refresh={refresh}
              empty="暂无关键操作记录。密码和密钥不会写入此记录。"
            />
          </Panel>
          <ActionForm
            title="创建备份"
            fields={[]}
            submitLabel="提交备份任务"
            onSubmit={async () => {
              const r = await post('/operations/backups');
              setRefresh((n) => n + 1);
              return r;
            }}
          />
          <Panel title="备份状态">
            <DataTable
              path="/operations/backups"
              columns={[
                { key: 'backup_id', label: '标识' },
                { key: 'status', label: '状态', render: (r) => <Status value={r.status} /> },
                {
                  key: 'created_at',
                  label: '时间 / UTC',
                  render: (r) =>
                    r.created_at == null ? '请检查备份后查看生成时间' : display(r.created_at),
                },
                {
                  key: 'database_sha256',
                  label: '数据库校验值',
                  render: (r) => {
                    const files = Array.isArray(r.files) ? (r.files as RecordData[]) : [];
                    return display(
                      r.database_sha256 ??
                        files.find((f) => f.path === 'platform.sqlite3')?.sha256 ??
                        '尚未核对清单',
                    );
                  },
                },
                {
                  key: 'validation',
                  label: '文件对应关系',
                  render: (r) => (r.validation == null ? '尚未验证' : '请查看清单中的检查结果'),
                },
              ]}
              refresh={refresh}
              polling
              empty="还没有备份。备份不包含密钥或受限原始数据。"
            />
          </Panel>
          <ActionForm
            title="检查备份或恢复到独立位置"
            fields={[
              { key: 'backup_id', label: '备份编号', help: '从上方的备份记录复制编号。' },
              {
                key: 'action',
                label: '动作',
                type: 'select',
                options: [
                  { value: 'verify', label: '检查备份' },
                  { value: 'restore', label: '恢复到独立目录' },
                ],
                help: '恢复会把内容写到独立目录，现有工作区数据保留。',
              },
            ]}
            submitLabel="开始所选操作"
            onSubmit={(v) =>
              post(`/operations/backups/${encodeURIComponent(v.backup_id)}/${v.action}`)
            }
          />
        </>
      ) : (
        <Notice>你可以查看基础运行状态。操作记录、备份和管理操作仅管理员可用。</Notice>
      )}
      {isAdmin && <Notice>请到“任务记录”查看备份和恢复的进度。</Notice>}
    </>
  );
}
function OperationsHealth({ value: r }: { value: RecordData }) {
  const data = r.data as RecordData,
    trading = r.trading as RecordData;
  return (
    <>
      <Status value={r.readiness === 'ok' ? '后台已就绪' : r.readiness === 'degraded' ? 'DEGRADED' : 'UNKNOWN'} />
      <dl className="key-value">
        <dt>服务响应</dt>
        <dd>{r.liveness === 'ok' ? '有响应' : '未确认服务响应'}</dd>
        <dt>后台程序上次响应距今（秒）</dt>
        <dd>{display((r.component_age_seconds as RecordData)?.worker ?? '没有观测记录')}</dd>
        <dt>等待最久的任务（秒）</dt>
        <dd>{display(r.oldest_queued_seconds ?? '不可用')}</dd>
        <dt>运行许可已过期的任务数</dt>
        <dd>{display(r.expired_running_leases ?? '不可用')}</dd>
        <dt>因质量问题被拦截的数据版本数</dt>
        <dd>{display(data?.quality_blocked_snapshots ?? '模块不可用')}</dd>
        <dt>资料发布到可获取的间隔（秒）</dt>
        <dd>{display(data?.max_snapshot_delay_seconds ?? '没有观测记录')}</dd>
        <dt>冻结账户数 / 未确认发出的订单数</dt>
        <dd>
          {display(trading?.frozen_accounts ?? '模块不可用')} /{' '}
          {display(trading?.unknown_orders ?? '模块不可用')}
        </dd>
        <dt>恢复状态</dt>
        <dd>{r.recovery === 'NO_FROZEN_ACCOUNTS' ? '无冻结账户' : r.recovery === 'RECONCILIATION_REQUIRED' ? '有冻结账户需要核对' : '暂无恢复状态'}</dd>
        <dt>检查时间（UTC）</dt>
        <dd>{display(r.measurement_at ?? '不可用')}</dd>
      </dl>
      <details>
        <summary>高级｜原始运行状态</summary>
        <JsonView value={r} />
      </details>
    </>
  );
}
