# 模型与密钥设置 / Model setup

## 先选你想做的事

| 想做什么 | 是否需要模型密钥 |
| --- | --- |
| 下载行情，运行普通回测，查看报告 | 不需要 |
| 让 Codex 或 DeepSeek 帮你操作 FinanceAnalyze | 在软件中[授权外部助手](platform/MCP_CONNECTION.md)；助手自己的模型设置仍由该客户端管理 |
| 让 FinanceAnalyze 自己请求在线模型做研究 | 需要在工作台配置支持的模型和所需密钥 |

软件登录密码用于打开自己的工作台；数据来源密钥用于下载供应商数据；模型 API 密钥用于调用模型。三者不能混用。

## 保存模型配置

1. 在模型提供方的 API 平台创建自己的密钥，确认可用模型、余额和收费方式。聊天产品订阅不一定包含 API 使用额度。
2. 打开 **B · AI 研究 → 模型配置**，起一个名称，选择提供方和该账号可用的模型。
3. 使用官方密钥时，保留默认官方连接。公司网关等其他地址需先由维护者登记为可信连接，再选择对应连接和密钥。
4. 展开 **配置本地密钥（可稍后）**，把密钥填入 **新密钥**，点 **保存新密钥**。
5. 点 **查看所选密钥状态**，选择 **使用此已保存密钥**，再点 **保存模型配置**。

密钥填在专用输入框，不要写进研究问题、聊天消息或截图。“本机已保存”只表示保存成功，不代表该模型已经能调用。切换提供方或连接后，重新选用匹配的密钥。

## 是否进行一次模型测试

读取本机状态不会自动调用模型。页面支持该配置的测试时，会列出提供方和模型，并要求你单独同意可能收费的请求。

愿意进行这次调用时，勾选 **我同意发送这一次测试请求，费用可能计入我的模型账号**，再点 **明确开始一次模型测试**。当前测试入口支持使用本机保存密钥的默认官方 OpenAI、DeepSeek 连接；其他配置按页面提示处理。

点 **查看模型测试进度** 查看任务；没有明确结果时，点 **核对上次模型测试**，不要连续重发。成功只说明这个配置完成了这次请求，实际费用仍以提供方账单为准。

## 做一份小研究

1. 先在 C 页保存一只证券、一个已过去交易周的行情。
2. 打开 **B · AI 研究 → 手动研究**，选择这份数据、**单模型助手**和保存的模型。
3. 输入范围明确的问题，例如：“请说明这几个交易日的价格与成交量变化，并注明数据不足之处。”
4. 点 **理解任务并查看计划**，核对数据、模型和预算，再点 **按计划开始研究**。
5. 任务完成后，到 D 页打开报告。失败时先查看任务原因，再检查密钥、余额、模型权限或数据范围。

批准研究意味着相应任务说明和允许使用的资料可能发送给所选模型提供方。受本机查看限制的数据与报告不能因为启用了模型而自动对外分享。

## English summary

Model API keys are separate from your FinanceAnalyze password and data-provider credentials. Use **B → 模型配置** to save a provider/model, save its key in the dedicated local key form, select that saved key, and save the profile. A local “saved” status is not a successful model call. An optional model test requires separate agreement to possible charges. Review the data, model, and budget before starting research. External assistant access uses [MCP authorization](platform/MCP_CONNECTION.md) instead.
