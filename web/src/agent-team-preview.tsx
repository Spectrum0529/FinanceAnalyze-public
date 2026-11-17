import type { RecordData } from './api';
import { JsonView } from './components';
import { record } from './native-research-api';

const providerName = (value: unknown) => ({ openai: 'OpenAI', deepseek: 'DeepSeek', offline: '离线模型', anthropic: 'Anthropic', gemini: 'Google Gemini', qwen: '通义千问', kimi: 'Kimi', glm: '智谱 GLM', openai_compatible: 'OpenAI 兼容服务', ollama: '本地 Ollama' }[String(value)] ?? String(value ?? '未提供'));
const shortText = (value: unknown) => typeof value === 'string' && value ? `${value.slice(0, 120)}${value.length > 120 ? '…' : ''}` : '未追加';
const provided = (value: unknown) => value == null ? '未提供' : String(value);
export function AgentTeamPreview({ value, roleNames }: { value: RecordData; roleNames: Record<string, string> }) {
  const configuration = record(value.configuration), prompt = record(configuration.prompt), budgets = record(configuration.budgets);
  return <section aria-label="运行前预览摘要">
    <h3>运行前预览</h3>
    <p>{value.execution_ready === true ? '设置格式正确。开始时还会检查后台服务和模型能否连接。' : '当前配置尚未就绪，请查看下方提示。'}预览没有调用在线模型，不能确认连接是否可用。</p>
    <div className="table-scroll"><table><thead><tr><th>角色</th><th>厂商</th><th>模型</th><th>推理强度</th><th>每个角色的指令与参考资料</th></tr></thead><tbody>
      {Object.entries(record(value.resolved_roles)).map(([id, raw]) => {
        const role = record(raw), model = record(role.model), rolePrompt = record(role.prompt);
        return <tr key={id}><td>{roleNames[id] ?? id}</td><td>{providerName(model.provider)}</td><td>{provided(model.model)}</td><td>{model.effort == null ? '模型默认' : String(model.effort)}</td><td>
          <details><summary>指令与参考资料摘要</summary><p>系统：{shortText(rolePrompt.system)}</p><p>任务：{shortText(rolePrompt.task)}</p><p>输出：{shortText(rolePrompt.output)}</p><p>此角色的参考资料：{shortText(role.private_context)}</p></details>
        </td></tr>;
      })}
    </tbody></table></div>
    {!Object.keys(record(value.resolved_roles)).length && <p>预览没有返回团队分工。</p>}
    <details><summary>查看团队指令、资料和用量上限</summary>
    <p>团队通用指令：{shortText(prompt.system)}</p><p>团队任务要求：{shortText(prompt.task)}</p><p>团队输出格式：{shortText(prompt.output)}</p><p>团队共同参考的资料：{shortText(configuration.shared_context)}</p>
    <p>历史消息 {provided(budgets.history_messages)} 条；输入预算 {provided(budgets.input_tokens)} token；输出上限 {provided(budgets.output_tokens)} token；输入字符上限 {provided(budgets.input_chars)}。</p>
    <p>资料太长时：{budgets.context_policy === 'reject' ? '拒绝并报告' : budgets.context_policy === 'truncate_oldest' ? '先省去最早的消息，保留必要指令及完整工具记录' : '未提供'}。</p></details>
    {Array.isArray(value.reason_codes) && value.reason_codes.length > 0 && <p>当前限制：{value.reason_codes.map((code) => code === 'REAL_PROVIDER_CALLS_NOT_CONFIGURED' ? '请在模型配置中检查提供方连接' : '其他限制见技术信息').join('、')}</p>}
    <details><summary>完整配置和原因代码（技术信息）</summary><JsonView value={value} /></details>
  </section>;
}
