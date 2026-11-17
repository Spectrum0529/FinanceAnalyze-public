import { useState } from 'react';
import { display, post } from './api';
import type { RecordData } from './api';
import {
  ActionForm,
  DataTable,
  JsonView,
  Notice,
  Panel,
  Resource,
  Status,
  ResultSummary,
  useResource,
} from './components';
import type { Field } from './components';
import { fileBase64 } from './data-page';
import { MigrationStrategyApproval } from './migration-strategy-approval';
import type { AccountReauthentication } from './platform-account-session';
const input = (key: string, label: string, help: string, initial?: string): Field => ({
  key,
  label,
  help,
  initial,
});
const parse = (value: string) => {
  try {
    return JSON.parse(value);
  } catch {
    throw new Error('参数 JSON 格式不正确。');
  }
};
const trustField: Field = {
  key: 'trusted',
  label: '代码信任',
  type: 'select',
  options: [
    { value: 'false', label: '未批准运行' },
    { value: 'true', label: '已审查的自用策略包（仅管理员可批准）' },
  ],
  help: '只批准你已核对来源和内容的代码。运行有环境和时间限制，仍不能隔离所有风险。',
};
export function Strategies({ canWrite, migrationAdmin, ownerId, reauthenticate, motion }: {
  canWrite: boolean; migrationAdmin?: boolean; ownerId?: string;
  reauthenticate?: AccountReauthentication; motion?: () => boolean;
}) {
  const [refresh, setRefresh] = useState(0),
    [selected, setSelected] = useState<RecordData | null>(null),
    [config, setConfig] = useState(''),
    [instance, setInstance] = useState('');
  const portfolio = useResource<RecordData>(instance ? '/research/portfolio/catalog' : null, refresh);
  const eligibleDaily = Array.isArray(portfolio.data?.sdk_instances) && portfolio.data.sdk_instances.some(value =>
    value && typeof value === 'object' && !Array.isArray(value) && value.instance_id === instance);
  const change = () => setRefresh((n) => n + 1);
  return (
    <>
      <Panel title="准备一个可回测的策略" helpTopic="strategy-package">
        <p>创建模板或导入自己的策略包。只有通过检查、已获准运行的策略才能用于回测。</p>
        <a className="text-button" href="#research">
          返回回测，继续选数据或策略 →
        </a>
      </Panel>
      {canWrite && (
        <div className="grid-2">
          <ActionForm
            title="用模板创建策略"
            fields={[
              input('strategy_id', '策略标识', '以小写字母开头，可用字母、数字、下划线或短横线，最多 64 个字符。'),
              {
                key: 'style',
                label: '策略类型',
                type: 'select',
                options: [
                  { value: 'event', label: '事件驱动' },
                  { value: 'batch', label: '批量给股票打分' },
                  { value: 'target', label: '按目标比例或股数持仓' },
                  { value: 'model', label: '用已训练模型预测' },
                  { value: 'external', label: '使用外部程序的信号' },
                ],
                help: '选模型预测时，请先训练并保存模型。',
              },
              {
                key: 'model_id',
                label: '已保存模型文件',
                type: 'select',
                required: false,
                help: '选择已保存的模型文件。',
                showWhen: { key: 'style', values: ['model'] },
                resource: {
                  path: '/models',
                  idKey: 'model_id',
                  labelKeys: ['version', 'train_cutoff', 'model_id'],
                },
              },
              trustField,
            ]}
            submitLabel="创建并保存策略版本"
            onSubmit={async (v) => {
              const r = await post('/strategies/template', {
                strategy_id: v.strategy_id,
                style: v.style,
                model_id: v.model_id || null,
                trusted: v.trusted === 'true',
              });
              change();
              return r;
            }}
          />
          <ActionForm
            title="导入自己的策略包"
            fields={[
              {
                key: 'file',
                label: 'ZIP 策略包',
                type: 'file',
                accept: '.zip',
                help: 'ZIP 不超过 1 MiB；所需文件见帮助。上传时不会运行代码。',
              },
              trustField,
            ]}
            submitLabel="检查并保存策略包"
            onSubmit={async (v, files) => {
              const r = await post('/strategies/import', {
                archive_base64: await fileBase64(files.file),
                trusted: v.trusted === 'true',
              });
              change();
              return r;
            }}
          />
        </div>
      )}
      <Panel title="已保存的策略版本">
        <DataTable
          path="/strategies"
          columns={[
            { key: 'strategy_id', label: '策略' },
            { key: 'version', label: '版本' },
            {
              key: 'manifest',
              label: '类型 / 输出',
              render: (r) =>
                display((r.manifest as RecordData)?.style) +
                ' · ' +
                display((r.manifest as RecordData)?.outputs),
            },
            {
              key: 'trusted',
              label: '运行许可',
              render: (r) => (
                <Status value={r.trusted === true ? '已批准自用' : '未批准运行'} />
              ),
            },
            { key: 'package_hash', label: '策略包校验值' },
          ]}
          refresh={refresh}
          empty="还没有策略。请先创建模板或导入策略包。"
          onSelect={(r) => setSelected(r)}
        />
      </Panel>
      {selected && (
        <>
          <Panel
            title={`策略说明 · ${display(selected.strategy_id)} / ${display(selected.version)}`}
          >
            <ResultSummary value={selected} />
            <dl className="key-value">
              <dt>策略类型</dt>
              <dd>{display((selected.manifest as RecordData)?.style)}</dd>
              <dt>输出类型</dt>
              <dd>{display((selected.manifest as RecordData)?.outputs)}</dd>
              <dt>支持模式</dt>
              <dd>{display((selected.manifest as RecordData)?.modes)}</dd>
            </dl>
            <details>
              <summary>查看参数格式和数据要求（原始文件）</summary>
              <JsonView value={selected.manifest} />
            </details>
            {migrationAdmin && ownerId && reauthenticate && motion && <MigrationStrategyApproval
              key={`${selected.strategy_id}:${selected.version}:${selected.package_hash}`} selected={selected} ownerId={ownerId}
              reauthenticate={reauthenticate} motion={motion} approved={() => {
                setSelected(current => current === selected ? { ...selected, trusted: true } : current);
                change();
              }} />}
          </Panel>
          {canWrite && (
            <ActionForm
              key={`${selected.strategy_id}:${selected.version}`}
              title="校验并固定本次使用的参数"
              fields={[
                {
                  key: 'parameters',
                  label: '参数 JSON',
                  type: 'textarea',
                  initial: '{}',
                  help: '按上方策略说明填写。修改参数会另存设置，原设置保留。',
                },
                {
                  key: 'mode',
                  label: '模式',
                  type: 'select',
                  options: [
                    { value: 'BACKTEST', label: '历史回测' },
                    { value: 'LOCAL_SIMULATION', label: '本机模拟' },
                  ],
                  help: '请选择策略说明里支持的模式。这里不支持实盘。',
                },
              ]}
              submitLabel="检查并保存设置"
              onSubmit={async (v) => {
                const r = await post<RecordData>('/strategies/configs', {
                  strategy_id: selected.strategy_id,
                  version: selected.version,
                  parameters: parse(v.parameters),
                  mode: v.mode,
                });
                setConfig(String(r.config_id));
                change();
                return r;
              }}
            />
          )}
        </>
      )}
      {canWrite && (
        <ActionForm
          key={`create-instance:${config}`}
          title="创建可供回测选择的运行配置"
          fields={[
            {
              key: 'config_id',
              label: '已保存的参数设置',
              type: 'select',
              initial: config || undefined,
              help: '用一份已检查的参数设置创建运行配置。',
              resource: {
                path: '/strategies/configs',
                idKey: 'config_id',
                labelKeys: ['strategy_id', 'version', 'mode', 'config_id'],
              },
            },
          ]}
          submitLabel="创建运行配置"
          onSubmit={async (v) => {
            const r = await post<RecordData>('/strategies/instances', v);
            setInstance(String(r.instance_id));
            return r;
          }}
        />
      )}
      <Panel title="固定本次使用的参数">
        <DataTable
          path="/strategies/configs"
          refresh={refresh}
          columns={[
            { key: 'config_id', label: '配置' },
            { key: 'strategy_id', label: '策略' },
            { key: 'version', label: '版本' },
            { key: 'mode', label: '模式' },
            { key: 'config_hash', label: '设置校验值' },
          ]}
          empty="还没有已检查的参数设置。"
          onSelect={(r) => setConfig(String(r.config_id))}
        />
      </Panel>
      <Panel title="保存的策略运行设置">
        <DataTable
          path="/strategies/instances"
          refresh={refresh}
          polling
          columns={[
            { key: 'instance_id', label: '运行配置编号' },
            { key: 'config_id', label: '参数设置编号' },
            { key: 'status', label: '状态', render: (r) => <Status value={r.status} /> },
            { key: 'updated_at', label: '更新时间 / UTC' },
          ]}
          empty="还没有可查看的运行配置。"
          onSelect={(r) => setInstance(String(r.instance_id))}
        />
      </Panel>
      {instance && (
        <Panel title="运行配置详情">
          <Resource
            path={`/strategies/instances/${encodeURIComponent(instance)}`}
            refresh={refresh}
            polling
          >{value => <>
            {value.instance_id === instance && value.status === 'VALIDATED' && eligibleDaily &&
              <a className="text-button" href={'#research?' + new URLSearchParams({ strategy_instance_id: instance }).toString()}>
                用这个策略运行日线回测 →
              </a>}
            {value.instance_id === instance && value.status === 'VALIDATED' && !eligibleDaily &&
              <p>{portfolio.loading ? '正在检查能否用于日线回测…' : '这份配置暂不能用于日线回测。请检查是否已批准运行、是否有任务占用，以及策略是否支持日线信号。'}</p>}
            <ResultSummary value={value} />
          </>}</Resource>
        </Panel>
      )}
      {canWrite && (
        <ActionForm
          key={`transition-instance:${instance}`}
          title="检查或管理运行配置"
          resourceRefresh={refresh}
          fields={[
            {
              key: 'instance_id',
              label: '要操作的运行配置',
              type: 'select',
              initial: instance || undefined,
              help: '选择一份已保存的运行配置。当前不能执行的操作会给出提示。',
              resource: {
                path: '/strategies/instances',
                idKey: 'instance_id',
                labelKeys: ['status', 'created_at', 'instance_id'],
              },
            },
            {
              key: 'action',
              label: '操作',
              type: 'select',
              options: [
                { value: 'validate', label: '检查配置' },
                { value: 'pause', label: '暂停' },
                { value: 'stop', label: '停止' },
                { value: 'archive', label: '归档' },
              ],
              help: '要运行或恢复，请从回测或模拟页面提交新任务。',
            },
          ]}
          submitLabel="执行操作并查看最新状态"
          onSubmit={async (v) => {
            const r = await post(
              `/strategies/instances/${encodeURIComponent(v.instance_id)}/${v.action}`,
            );
            setInstance(v.instance_id);
            change();
            return r;
          }}
        />
      )}
      <Notice>模板只用于搭建和测试策略，不能证明能赚钱。外部信号必须检查来源、顺序、重复、时间和有效期。</Notice>
    </>
  );
}
