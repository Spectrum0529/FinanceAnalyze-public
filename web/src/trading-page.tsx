import { useRef, useState } from 'react';
import { display, post, request } from './api';
import type { RecordData } from './api';
import {
  ActionForm,
  DataTable,
  JsonView,
  Notice,
  Panel,
  Resource,
  Status,
  HashValue,
} from './components';
import type { Field } from './components';
import { identityExample } from './data-page';
import { ServerRows } from './research-page';
import { useWorkspaceSelection } from './workspace-selection';
const input = (key: string, label: string, help: string, initial?: string): Field => ({
  key,
  label,
  help,
  initial,
});
const json = (key: string, label: string, help: string, initial: unknown): Field => ({
  key,
  label,
  help,
  type: 'textarea',
  initial: JSON.stringify(initial, null, 2),
});
const parse = (s: string) => {
  try {
    return JSON.parse(s);
  } catch {
    throw new Error('JSON 格式不正确，请检查引号、逗号与括号。');
  }
};
const encode = encodeURIComponent;
const riskExample = {
  max_order_notional: '10000',
  max_account_gross: '100000',
  max_strategy_gross: '50000',
  max_concentration: '0.50',
  max_price_deviation: '0.10',
  market_reserve_buffer: '0.10',
  max_daily_loss: '0.05',
  max_drawdown: '0.10',
  max_orders_per_minute: 20,
  max_data_age_seconds: 120,
  reset_timezone: 'America/New_York',
};
export function Accounts({ canWrite }: { canWrite: boolean }) {
  const [refresh, setRefresh] = useState(0),
    [account, setAccount] = useState('');
  const change = () => setRefresh((n) => n + 1);
  return (
    <>
      {canWrite && (
        <ActionForm
          title="创建模拟现金账户"
          fields={[
            input(
              'account_id',
              '账户标识',
              '填写一个未使用的名称，例如 local-demo。',
            ),
            input(
              'initial_cash',
              '起始现金（美元）',
              '填写模拟用的金额，例如 10000.00。',
              '1000.00',
            ),
            {
              key: 'mode',
              label: '模式',
              type: 'select',
              options: [
                { value: 'LOCAL_SIMULATION', label: '本机模拟 · 无需外部账号' },
                { value: 'BROKER_PAPER', label: 'Alpaca 模拟账户 · 需连接并对账' },
              ],
              help: '真实交易关闭。Alpaca 模拟账户创建后会冻结，连接并对账后才能恢复。',
            },
            json(
              'fees',
              '模拟交易费用',
              '填写模拟用的每股费用和每笔最低费用，单位为美元。实际收费请查看供应商说明。',
              { per_share: '0.005', minimum: '1.00' },
            ),
            {
              ...input(
                'simulation_started_at',
                '模拟资金起始时间（UTC，可选）',
                '仅本机模拟使用。填写带 Z 或 +00:00 的时间，须不晚于当前时间和样本起点。如果有分红记录日，也不能晚于该日期。账户从指定现金和空持仓开始。',
                '',
              ),
              required: false,
              showWhen: { key: 'mode', values: ['LOCAL_SIMULATION'] },
            },
            json(
              'limits',
              '交易风险上限',
              '金额和比率加引号，订单速率和行情可用时长填整数。每日风险按纽约日期计算并扣除净入金。达到限额时只能减仓。',
              riskExample,
            ),
          ]}
          submitLabel="创建账户并保存风险设置"
          onSubmit={async (v) => {
            const r = await post<RecordData>('/trading/accounts', {
              account_id: v.account_id,
              initial_cash: v.initial_cash,
              mode: v.mode,
              fees: parse(v.fees),
              limits: parse(v.limits),
              currency: 'USD',
              account_type: 'cash',
              leverage: '1',
              allow_short: false,
              ...(v.mode === 'LOCAL_SIMULATION' && v.simulation_started_at?.trim()
                ? { simulation_started_at: v.simulation_started_at.trim() }
                : {}),
            });
            setAccount(String(r.account_id));
            change();
            return r;
          }}
        />
      )}
      <Panel title="模拟账户列表">
        <DataTable
          path="/trading/accounts"
          columns={[
            { key: 'account_id', label: '账户' },
            { key: 'mode', label: '模式' },
            { key: 'frozen', label: '冻结' },
            { key: 'emergency', label: '紧急停止' },
            { key: 'freeze_reason', label: '原因' },
          ]}
          refresh={refresh}
          empty="还没有可查看的模拟账户。创建后可查看余额、订单和持仓。"
          onSelect={(r) => setAccount(String(r.account_id))}
        />
      </Panel>
      {account && (
        <>
          <Panel
            title={`余额与持仓 · ${account}`}
            action={
              <button className="secondary" onClick={change}>
                刷新
              </button>
            }
          >
            <Resource path={`/trading/accounts/${encode(account)}`} refresh={refresh} polling>
              {(r) => (
                <>
                  <Status value={r.mode} />
                  <dl className="key-value">
                    <dt>模拟资金起始时间（UTC）</dt>
                    <dd>{display(r.account_initial_at ?? '接口未提供')}</dd>
                    <dt>创建记录时间（UTC）</dt>
                    <dd>
                      {display((r.initialization as RecordData)?.recorded_at ?? '接口未提供')}
                    </dd>
                    <dt>初始条件</dt>
                    <dd>{display((r.initialization as RecordData)?.assumption ?? '接口未提供')}</dd>
                  </dl>
                  <div className="report-metrics">
                    {[
                      ['现金 / USD', r.cash],
                      ['可用现金 / USD', r.available_cash],
                      ['账户净值 / USD', r.equity],
                      ['未完成订单占用 / USD', r.reserved_cash],
                      ['待结算 / USD', r.unsettled_cash],
                      ['累计费用 / USD', r.fees],
                    ].map(([label, value], i) => (
                      <div key={i}>
                        <small>{String(label)}</small>
                        <strong>{display(value)}</strong>
                      </div>
                    ))}
                  </div>
                  <h3>持仓与估值</h3>
                  <DataTable
                    path={`/trading/accounts/${encode(account)}/positions`}
                    refresh={refresh}
                    columns={[
                      { key: 'instrument_id', label: '证券编号' },
                      { key: 'quantity', label: '数量' },
                      { key: 'cost', label: '成本' },
                      { key: 'market_value', label: '市值' },
                    ]}
                    empty="暂无持仓。空白市值表示缺少估值。"
                  />
                  <h3>各策略记录的持仓</h3>
                  <ServerRows
                    value={r.strategy_contributions}
                    columns={[
                      { key: 'strategy_id', label: '策略' },
                      { key: 'instrument_id', label: '证券' },
                      { key: 'quantity', label: '策略持仓数量' },
                      { key: 'cost', label: '成本 USD' },
                    ]}
                    empty="暂无各策略单独记录的持仓。"
                  />
                  <details>
                    <summary>高级｜原始账户记录</summary>
                    <JsonView value={r} />
                  </details>
                </>
              )}
            </Resource>
          </Panel>
          <Panel title="订单明细">
            <DataTable
              path={`/trading/accounts/${encode(account)}/orders`}
              refresh={refresh}
              polling
              columns={[
                { key: 'client_order_id', label: '订单' },
                { key: 'status', label: '状态', render: (r) => <Status value={r.status} /> },
                {
                  key: 'instrument',
                  label: '证券 / 方向',
                  render: (r) =>
                    `${display((r.intent as RecordData)?.instrument_id)} / ${display((r.intent as RecordData)?.side)}`,
                },
                { key: 'filled_qty', label: '成交数量' },
                { key: 'fees', label: '费用 USD' },
                {
                  key: 'risk',
                  label: '适用的风险设置版本',
                  render: (r) => display((r.risk as RecordData)?.version),
                },
              ]}
              empty="暂无已记录的订单。"
            />
          </Panel>
          <Panel title="成交明细">
            <DataTable
              path={`/trading/accounts/${encode(account)}/fills`}
              refresh={refresh}
              polling
              columns={[
                { key: 'event_id', label: '成交事件' },
                { key: 'order_id', label: '订单' },
                { key: 'instrument_id', label: '证券' },
                { key: 'side', label: '方向' },
                { key: 'quantity', label: '数量' },
                { key: 'price', label: '价格 USD' },
                { key: 'fee', label: '费用 USD' },
                { key: 'recorded_at', label: '记录时间 / UTC' },
              ]}
              empty="此账户暂无成交记录。"
            />
          </Panel>
          <Panel title="现金和持仓变动记录">
            <DataTable
              path={`/trading/accounts/${encode(account)}/ledger`}
              refresh={refresh}
              columns={[
                { key: 'sequence', label: '序号' },
                { key: 'event_id', label: '事件' },
                { key: 'kind', label: '类型' },
                { key: 'order_id', label: '订单' },
                { key: 'amount', label: '金额 USD' },
                { key: 'quantity', label: '数量' },
                { key: 'recorded_at', label: '记录时间 / UTC' },
              ]}
              empty="暂无现金或持仓变动记录。"
            />
          </Panel>
          <Panel title="历史风险设置">
            <Resource path={`/trading/accounts/${encode(account)}/risk`} refresh={refresh} />
            <DataTable
              path={`/trading/accounts/${encode(account)}/risk/versions`}
              refresh={refresh}
              columns={[
                { key: 'version', label: '版本' },
                { key: 'content_hash', label: '摘要' },
                { key: 'actor_id', label: '修改者' },
                { key: 'created_at', label: '时间 / UTC' },
              ]}
              empty="暂无保存的风险设置。"
            />
          </Panel>
          <Panel title="已保存的对账记录">
            <DataTable
              path={`/trading/accounts/${encode(account)}/reconciliations`}
              columns={[
                { key: 'at', label: '时间 / UTC' },
                { key: 'clean', label: '一致' },
                { key: 'differences', label: '差异' },
              ]}
              refresh={refresh}
              empty="暂无对账记录。Alpaca 模拟账户须先核对现金、持仓、订单和成交。"
            />
          </Panel>
          {canWrite && (
            <>
              <RiskEditor account={account} refresh={refresh} changed={change} />
              <ActionForm
                key={`reconcile:${account}`}
                title="核对本机账户记录"
                fields={[]}
                submitLabel="开始核对"
                onSubmit={async () => {
                  const r = await post(`/trading/accounts/${encode(account)}/reconcile`);
                  change();
                  return r;
                }}
              />
              <ActionForm
                key={`instrument:${account}`}
                title="登记可交易的证券"
                fields={[
                  json(
                    'instrument',
                    '证券资料',
                    '请从获准的数据版本复制证券资料。框内示例仅用于合成数据。',
                    identityExample[0],
                  ),
                ]}
                submitLabel="登记证券"
                onSubmit={(v) =>
                  post(`/trading/accounts/${encode(account)}/instruments`, parse(v.instrument))
                }
              />
              <ActionForm
                key={`emergency:${account}`}
                title="紧急停止新增风险"
                fields={[
                  {
                    key: 'enabled',
                    label: '紧急停止状态',
                    type: 'select',
                    options: [
                      { value: 'true', label: '启用 · 阻止增加买入风险' },
                      { value: 'false', label: '解除 · 仍遵守其他风险限制' },
                    ],
                    help: '开启后，现有订单和持仓保留。账户对账不一致时会继续冻结。',
                  },
                ]}
                submitLabel="保存紧急停止状态"
                onSubmit={async (v) => {
                  const r = await post(`/trading/accounts/${encode(account)}/emergency-stop`, {
                    enabled: v.enabled === 'true',
                  });
                  change();
                  return r;
                }}
              />
              <ActionForm
                key={`cancel:${account}`}
                title="撤销模拟订单"
                fields={[
                  input(
                    'order_id',
                    'client_order_id',
                    '从上方订单列表复制编号。如果状态超时或未知，请先核对订单再重试。',
                  ),
                ]}
                submitLabel="请求撤单"
                onSubmit={async (v) => {
                  const r = await post(
                    `/trading/accounts/${encode(account)}/orders/${encode(v.order_id)}/cancel`,
                  );
                  change();
                  return r;
                }}
              />
            </>
          )}
        </>
      )}
      <Panel title="支持的交易范围">
        <Resource path="/trading/capabilities" />
      </Panel>
      <Notice>
        支持以美元交易的美股和 ETF 现金账户。做空和融资关闭。
      </Notice>
    </>
  );
}
const riskLabels: Record<string, string> = {
  max_order_notional: '单订单金额 / USD',
  max_account_gross: '账户总风险 / USD',
  max_strategy_gross: '策略总风险 / USD',
  max_concentration: '单证券集中度 / 比率',
  max_price_deviation: '价格偏离 / 比率',
  market_reserve_buffer: '市价预留缓冲 / 比率',
  max_daily_loss: '日亏损阈值 / 比率',
  max_drawdown: '回撤阈值 / 比率',
  max_orders_per_minute: '每分钟订单上限',
  max_data_age_seconds: '行情最大年龄 / 秒',
  reset_timezone: '日基准时区',
};
function RiskEditor({
  account,
  refresh,
  changed,
}: {
  account: string;
  refresh: number;
  changed: () => void;
}) {
  return (
    <Resource path={`/trading/accounts/${encode(account)}/risk`} refresh={refresh}>
      {(r) => {
        const limits = r.limits as RecordData;
        return (
          <ActionForm
            key={`risk:${account}:${r.version}`}
            title="保存新的风险设置"
            fields={[
              input('version', '新版本名称', '以前的运行记录仍引用当时的风险设置。'),
              ...Object.entries(riskLabels).map(([key, label]): Field => ({
                key,
                label,
                initial: display(limits?.[key]),
                type: ['max_orders_per_minute', 'max_data_age_seconds'].includes(key)
                  ? 'number'
                  : 'text',
                help:
                  key === 'reset_timezone'
                    ? '当前仅支持 America/New_York。'
                    : key.startsWith('max_orders') || key.startsWith('max_data_age')
                      ? '填写正整数。'
                      : '填写十进制数，例如 0.01。',
              })),
            ]}
            submitLabel="检查并保存新风险设置"
            onSubmit={async (v) => {
              const next = Object.fromEntries(
                Object.keys(riskLabels).map((key) => [
                  key,
                  ['max_orders_per_minute', 'max_data_age_seconds'].includes(key)
                    ? Number(v[key])
                    : v[key],
                ]),
              );
              const result = await post(`/trading/accounts/${encode(account)}/risk`, {
                version: v.version,
                expected_version: r.version,
                limits: next,
              });
              changed();
              return result;
            }}
          />
        );
      }}
    </Resource>
  );
}
export function Simulation({ canWrite }: { canWrite: boolean }) {
  const { reference, label } = useWorkspaceSelection();
  const accountSelectionRevision = useRef(0);
  const [account, setAccount] = useState(''),
    [replayFormGeneration, setReplayFormGeneration] = useState(0),
    [refresh, setRefresh] = useState(0),
    [selectedInstance, setSelectedInstance] = useState(''),
    [sessionKind, setSessionKind] = useState('local');
  return (
    <>
      <Panel title="模拟账户与连接">
        <DataTable
          path="/trading/accounts"
          columns={[
            { key: 'account_id', label: '账户' },
            { key: 'mode', label: '模式' },
            { key: 'frozen', label: '冻结' },
            { key: 'freeze_reason', label: '原因' },
          ]}
          refresh={refresh}
          empty="先在“模拟账户”创建本机模拟账户，再登记可交易的证券。"
          onSelect={(r) => {
            const next = String(r.account_id);
            // A pending POST must not overwrite any newer explicit selection.
            accountSelectionRevision.current += 1;
            if (next !== account) setReplayFormGeneration((n) => n + 1);
            setAccount(next);
          }}
        />
      </Panel>
      {canWrite && (
        <>
          <Panel title="本次使用的固定数据">
            {reference ? (
              <>
                <p>{label}</p>
                <HashValue value={reference.content_hash} />
              </>
            ) : (
              <Notice>
                在<a href="#research">策略与回测</a>
                选择固定的数据版本，再回到本页运行。刷新本页后，需重新选择该版本。
              </Notice>
            )}
          </Panel>
          <ActionForm
            title="用本地策略运行模拟"
            disabled={!reference}
            fields={[
              {
                key: 'instance_id',
                label: '已保存的本地策略实例',
                type: 'select',
                help: '选择已获准用于本机模拟的本地策略实例。',
                resource: {
                  path: '/strategies/instances',
                  idKey: 'instance_id',
                  labelKeys: ['status', 'created_at', 'instance_id'],
                },
              },
              {
                key: 'account_id',
                label: '本机模拟账户',
                type: 'select',
                initial: account || undefined,
                help: '继续时请选回原账户、数据和模型。更换其中任一项需要新建策略实例。',
                resource: {
                  path: '/trading/accounts',
                  idKey: 'account_id',
                  labelKeys: ['account_id', 'mode', 'freeze_reason'],
                },
              },
              {
                key: 'model_id',
                label: '固定模型版本（可选）',
                type: 'select',
                required: false,
                help: '如果策略使用模型，选策略设置中指定的已保存模型。',
                resource: {
                  path: '/models',
                  idKey: 'model_id',
                  labelKeys: ['version', 'train_cutoff', 'model_id'],
                },
              },
              {
                key: 'action',
                label: '动作',
                type: 'select',
                options: [
                  { value: 'start-simulation', label: '首次启动' },
                  { value: 'resume-simulation', label: '从上次保存的进度继续' },
                ],
                help: '会新增一次运行记录，账户余额和历史记录保留。',
              },
            ]}
            submitLabel="提交本地策略模拟"
            onSubmit={async (v) => {
              const r = await post(`/strategies/instances/${encode(v.instance_id)}/${v.action}`, {
                account_id: v.account_id,
                sample_reference: reference,
                model_id: v.model_id || null,
                idempotency_key: crypto.randomUUID(),
              });
              setSelectedInstance(v.instance_id);
              setSessionKind('local');
              setAccount(v.account_id);
              setRefresh((n) => n + 1);
              return r;
            }}
          />
          <ActionForm
            title="用外部信号运行模拟"
            disabled={!reference}
            fields={[
              {
                key: 'instance_id',
                label: '外部信号策略实例',
                type: 'select',
                help: '选择已保存且获准用于本机模拟的外部信号策略实例。',
                resource: {
                  path: '/strategies/instances',
                  idKey: 'instance_id',
                  labelKeys: ['status', 'created_at', 'instance_id'],
                },
              },
              {
                key: 'account_id',
                label: '内部模拟账户',
                type: 'select',
                help: '请选择你有权使用的本机模拟账户。',
                initial: account || undefined,
                resource: {
                  path: '/trading/accounts',
                  idKey: 'account_id',
                  labelKeys: ['account_id', 'mode', 'freeze_reason'],
                },
              },
              {
                key: 'clock_mode',
                label: '运行时间来源',
                type: 'select',
                options: [
                  { value: 'wall', label: '当前时间' },
                  { value: 'replay', label: '练习数据里的事件时间' },
                ],
                help: '使用事件时间时，只能选择合成练习数据。',
              },
            ]}
            submitLabel="提交外部信号模拟"
            onSubmit={async (v) => {
              const r = await post(
                `/strategies/instances/${encode(v.instance_id)}/start-external`,
                {
                  account_id: v.account_id,
                  sample_reference: reference,
                  clock_mode: v.clock_mode,
                  idempotency_key: crypto.randomUUID(),
                },
              );
              setAccount(v.account_id);
              setSelectedInstance(v.instance_id);
              setSessionKind('external');
              setRefresh((n) => n + 1);
              return r;
            }}
          />
        </>
      )}
      <ActionForm
        title="查看已保存的模拟记录"
        fields={[
          {
            key: 'instance_id',
            label: '要查看的策略实例',
            type: 'select',
            help: '选择已保存的策略实例，查看该策略当前的模拟状态和进度。',
            resource: {
              path: '/strategies/instances',
              idKey: 'instance_id',
              labelKeys: ['status', 'created_at', 'instance_id'],
            },
          },
          {
            key: 'kind',
            label: '模拟方式',
            type: 'select',
            options: [
              { value: 'local', label: '本地策略' },
              { value: 'external', label: '外部信号（HTTP）' },
            ],
            help: '请选择该策略实例对应的模拟方式。',
          },
        ]}
        submitLabel="查看已保存的运行状态"
        onSubmit={async (v) => {
          const r = await request<RecordData>(
            v.kind === 'local'
              ? `/strategies/instances/${encode(v.instance_id)}/simulation`
              : `/strategies/instances/${encode(v.instance_id)}`,
          );
          setSelectedInstance(v.instance_id);
          setSessionKind(v.kind);
          return r;
        }}
      />
      {selectedInstance && (
        <Panel title="模拟状态与进度">
          <Resource
            path={`/strategies/instances/${encode(selectedInstance)}`}
            refresh={refresh}
            polling={500}
          />
          {sessionKind === 'local' ? (
            <Resource
              path={`/strategies/instances/${encode(selectedInstance)}/simulation`}
              refresh={refresh}
              polling={500}
            >
              {(r) => (
                <>
                  <dl className="key-value">
                    <dt>运行状态</dt>
                    <dd>
                      <Status value={r.status} />
                    </dd>
                    <dt>使用的账户</dt>
                    <dd>{display(r.account_id)}</dd>
                    <dt>运行次数</dt>
                    <dd>{display(r.generation)}</dd>
                    <dt>后台任务</dt>
                    <dd>{display(r.job_id)}</dd>
                    <dt>已处理到（UTC）</dt>
                    <dd>{display(r.cursor_at ?? '尚未保存进度')}</dd>
                    <dt>已处理事件数</dt>
                    <dd>{display((r.checkpoint as RecordData)?.processed_events ?? '尚未处理')}</dd>
                  </dl>
                  <details>
                    <summary>高级｜原始模拟设置和进度</summary>
                    <JsonView value={r} />
                  </details>
                </>
              )}
            </Resource>
          ) : (
            <DataTable
              path={`/signals/${encode(selectedInstance)}/messages`}
              polling
              refresh={refresh}
              columns={[
                { key: 'sequence', label: '序号' },
                { key: 'message_hash', label: '消息摘要' },
                { key: 'status', label: '处理状态' },
                { key: 'error_code', label: '失败代码' },
              ]}
              empty="还没有收到通过验证的外部信号。"
            />
          )}
          {canWrite && (
            <ActionForm
              key={`sim-control:${selectedInstance}`}
              title="暂停或停止模拟"
              fields={[
                {
                  key: 'action',
                  label: '操作',
                  type: 'select',
                  options: [
                    { value: 'pause', label: '暂停并保存进度' },
                    { value: 'stop', label: '停止' },
                  ],
                  help: '暂停或停止后，策略会停止发送新信号。继续时会新建一条运行记录。',
                },
                {
                  key: 'cancel_orders',
                  label: '未完成订单',
                  type: 'select',
                  options: [
                    { value: 'false', label: '保留未完成订单' },
                    { value: 'true', label: '请求撤销此策略的未完成订单' },
                  ],
                  help: '持仓会保留。撤单失败或结果未知时，请到订单列表核对。',
                },
              ]}
              submitLabel="执行所选操作"
              onSubmit={async (v) => {
                const r = await post(
                  `/strategies/instances/${encode(selectedInstance)}/${v.action}`,
                  { cancel_orders: v.cancel_orders === 'true' },
                );
                setRefresh((n) => n + 1);
                return r;
              }}
            />
          )}
        </Panel>
      )}
      {canWrite && (
        <details>
          <summary>高级｜重放合成事件和订单请求</summary>
          <ActionForm
            key={replayFormGeneration}
            title="重放合成行情与订单请求"
            fields={[
              input(
                'account_id',
                '内部模拟账户',
                '选择已登记账户和证券。此处只接受合成练习数据。',
                account || undefined,
              ),
              json(
                'events',
                '有序合成事件',
                'JSON 需包含 event_id、instrument_id、event_at、available_at、price、volume 和 provenance。',
                [],
              ),
              json(
                'intents',
                '订单请求（JSON）',
                '填写 client_order_id、account_id、strategy_id、instrument_id、数量、方向、生效时间和 LOCAL_SIMULATION 模式。请求会经过风险检查。',
                [],
              ),
            ]}
            submitLabel="提交本机重放任务"
            onSubmit={async (v) => {
              const selectionAtSubmit = accountSelectionRevision.current;
              const r = await post(`/trading/accounts/${encode(v.account_id)}/simulation`, {
                events: parse(v.events),
                intents: parse(v.intents),
                idempotency_key: crypto.randomUUID(),
              });
              if (accountSelectionRevision.current === selectionAtSubmit) {
                setAccount(v.account_id);
              }
              setRefresh((n) => n + 1);
              return r;
            }}
          />
        </details>
      )}
      <Notice>
        查看日志、取消任务或重试，请打开{' '}
        <a className="text-button" href="#jobs">
          任务记录 →
        </a>
        。
      </Notice>
      {account && (
        <Panel title={`账户状态 · ${account}`}>
          <Resource path={`/trading/accounts/${encode(account)}`} refresh={refresh} polling />
        </Panel>
      )}
      {canWrite && (
        <>
          <ActionForm
            title="连接 Alpaca 模拟账户"
            fields={[
              input('account_id', '工作区里的 Alpaca 模拟账户', '选择类型为 BROKER_PAPER 的账户。'),
              input(
                'broker_account_id',
                'Alpaca 模拟账号编号',
                '从 Alpaca 模拟交易控制台复制账号编号。',
              ),
              input(
                'key_env',
                '模拟账户 Key 的环境变量名',
                '只填本机环境变量的名称，请勿粘贴密钥。',
              ),
              input(
                'secret_env',
                '模拟账户 Secret 的环境变量名',
                '只填变量名。请使用模拟账号专用的凭据。',
              ),
            ]}
            submitLabel="保存 Alpaca 模拟账户连接"
            onSubmit={async (v) => {
              const { account_id, ...body } = v;
              const r = await post(`/trading/accounts/${encode(account_id)}/paper/config`, body);
              setAccount(account_id);
              return r;
            }}
          />
          <ActionForm
            title="测试连接或核对 Alpaca 模拟账户"
            fields={[
              input(
                'account_id',
                '工作区里的 Alpaca 模拟账户',
                '请先保存此账户的连接设置。',
              ),
              {
                key: 'action',
                label: '操作',
                type: 'select',
                options: [
                  { value: 'probe', label: '只读连接测试' },
                  { value: 'recover', label: '核对现金、持仓、订单和成交' },
                ],
                help: '此处提供只读检查和对账。真实交易关闭。',
              },
            ]}
            submitLabel="提交检查任务"
            onSubmit={(v) =>
              post(`/trading/accounts/${encode(v.account_id)}/paper/${v.action}`, {
                idempotency_key: crypto.randomUUID(),
              })
            }
          />
        </>
      )}
      <Notice>
        所有本机模拟和 Alpaca 模拟账户均使用模拟资金。真实交易关闭。
      </Notice>
    </>
  );
}
