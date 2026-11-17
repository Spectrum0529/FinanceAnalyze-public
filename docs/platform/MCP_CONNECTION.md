# 连接 AI 助手 / Connect an AI assistant

MCP 是让助手使用软件工具的连接方式。连好后，可以直接请助手查找数据、运行已允许的任务和管理报告，不需要每次代你点击页面。

## 连接 Codex 或 DeepSeek Harness

1. 在同一台电脑上启动 FinanceAnalyze 和要使用的助手。
2. 在启动器点 **连接 AI 助手**，或登录后打开 **B · AI 研究 → 连接外部 AI**。
3. 选择 **Codex** 或 **DeepSeek Harness**，再选择所需权限：

| 选项 | 助手可以做什么 |
| --- | --- |
| **只查看** | 查找你有权共享的行情、任务和报告 |
| **帮我操作** | 还可下载允许的公开行情、运行任务，以及管理可共享的数据与结果 |

4. 点击 **连接 Codex** 或 **连接 DeepSeek Harness**。若出现再次验证身份的提示，直接在 FinanceAnalyze 页面输入自己的账号密码。
5. 等助手读取工具，回页面点 **检查连接状态**。显示已连接后，可以试着说：“请列出我在 FinanceAnalyze 中可分享的报告。”

日常连接不需要把密码、密钥或配对码发到聊天里。**允许软件调用我配置的 AI 模型** 是额外选项，默认关闭；启用后，模型研究仍需选择来源、模型和预算，并可能收费。

## 已授权，但助手没有工具

先确认本机工作台能打开。然后在助手的 MCP 设置中找到 `financeanalyze`，重新加载或重新连接一次；不同客户端版本的按钮名称可能不同。必要时重新打开助手，再回工作台点 **检查连接状态**。

如果提示连接指向另一份安装，按工作台生成的配置检查当前安装位置。不要把旧电脑的连接配置直接当作新电脑配置使用。

## 分别撤销权限

在 **高级设置** 的授权列表中，对相应助手点 **撤销**。单独撤销一个客户端，不会同时撤销另一个客户端。

**退出网页账号、登录会话更换或到期** 是另一种情况：关联该次登录的授权会结束。需要继续使用时，在当前登录中重新连接。已被接受的后台任务可能继续运行；到 D 页查看结果。

WRDS 等本机限定来源及相关报告在工作台内查看，不会因为选择“帮我操作”而自动变成可对外分享的数据。

## 其他兼容客户端

自动连接不适用时，再展开 **高级设置**，选择独立的客户端标识并 **读取客户端配置**。按照页面给出的配置与手动配对步骤操作；每个客户端使用自己的授权。配对码只在页面提供的本机命令隐藏输入处填写，不放进配置文件或聊天。

本版适用于同一台 Windows 电脑上的兼容客户端，使用本机进程通信（stdio）。浏览器里的云端 AI、另一台电脑和互联网远程访问需要另行部署，本版不提供可直接公开的远程 MCP 地址。

## English steps

Start FinanceAnalyze and your AI client on the same computer. Open **连接外部 AI**, choose Codex or DeepSeek Harness, and choose **只查看** (view only) or **帮我操作** (operate). Complete identity verification in the workbench, then reload `financeanalyze` in the client's MCP settings if needed. Use **检查连接状态** to check again.

Each client has its own grant and revoke control. Signing out or replacing/expiring the associated browser session ends those session-linked grants. Permission for potentially billable model calls is separate. Other local stdio clients can follow the generated advanced configuration and pairing instructions. Local-only data remains subject to its sharing restrictions.
