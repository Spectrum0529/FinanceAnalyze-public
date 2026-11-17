import { AreaHelp } from './area-help';
import type { ReactNode } from 'react';

// UI projections only. The API adapter must derive choices and capabilities from its contract.
export type ModelChoice = { id: string; label: string; provider: string; efforts: readonly string[] };
export type BindingDraft = { provider: string; model: string; effort: string };

export function ModelBindingFields({ value, choices, changed, disabled = false, inherit = false }: {
  value: BindingDraft; choices: readonly ModelChoice[]; changed: (value: BindingDraft) => void;
  disabled?: boolean; inherit?: boolean;
}) {
  const providers = [...new Set([...choices.map((choice) => choice.provider), ...(value.provider ? [value.provider] : [])])];
  const models = choices.filter((choice) => choice.provider === value.provider);
  const selected = models.find((choice) => choice.id === value.model);
  return <div className="form-grid">
    <label>模型厂商<select disabled={disabled} value={value.provider} onChange={(event) => changed({ provider: event.target.value, model: '', effort: '' })}>
      <option value="">{inherit ? '使用团队默认模型' : '请选择厂商'}</option>
      {providers.map((provider) => <option key={provider} value={provider}>{provider}</option>)}
    </select></label>
    <label>模型<select disabled={disabled || !value.provider} value={value.model} onChange={(event) => changed({ ...value, model: event.target.value, effort: '' })}>
      <option value="">{inherit && !value.provider ? '使用团队默认模型' : '请选择模型'}</option>
      {value.model && !selected && <option value={value.model}>{value.model}（自定义，保持原配置）</option>}{models.map((choice) => <option key={choice.id} value={choice.id}>{choice.label}</option>)}
    </select></label>
    <label>推理强度<select disabled={disabled || !selected?.efforts.length} value={value.effort} onChange={(event) => changed({ ...value, effort: event.target.value })}>
      <option value="">{selected?.efforts.length ? '模型默认' : '此模型暂不能调整'}</option>
      {selected?.efforts.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
    </select></label><AreaHelp topic="reasoning" title="推理强度" />
  </div>;
}

export function PromptText({ label, value, changed, disabled = false, help }: {
  label: string; value: string; changed: (value: string) => void; disabled?: boolean; help?: string;
}) {
  return <div><label>{label}<textarea rows={5} value={value} disabled={disabled} onChange={(event) => changed(event.target.value)} />{help && <small>{help}</small>}</label><AreaHelp topic={/上下文|资料/.test(label) ? 'context' : 'prompts'} title={label} /></div>;
}

export function AdvancedModelOptions({ children }: { children: ReactNode }) {
  return <details><summary>高级选项 · 参考资料和用量上限</summary>{children}</details>;
}
