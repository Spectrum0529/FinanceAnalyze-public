# 第一次使用 / Getting started

这条路线用真实 Yahoo 行情和虚拟资金完成一次回测，不需要配置 AI。

## 安装和登录

按 [安装步骤](../README.zh-CN.md#安装) 完整解压并运行安装器。第一次启动时，在启动器点 **获取首次设置码**；把设置码填入自动打开的本机网页，再创建自己的管理员账号。设置码会过期，页面提示过期时重新获取。

以后打开安装文件夹中的 `FinanceAnalyze.exe`，登录已有账号即可。工作台默认在 `http://127.0.0.1:8000/`；这是本机地址，不是供其他电脑访问的网站。

## 第一次回测

1. 登录后选择 **先跑一个例子**。
2. 查看证券 AAPL、日期 2025-01-02 至 2025-03-31、虚拟本金 10,000 美元，以及页面显示的交易费用。
3. 点 **确认并开始示例**。软件先下载并保存行情，再模拟买入并持有。
4. 等待任务完成，点 **查看结果**。预览里可以快速查看；点 **打开完整报告** 查看更多内容。
5. 关闭预览或按 `Esc` 回到原来的位置。以后进入 **D · 研究结果**，仍可找到保存的报告。

下载时需要联网。收益随数据和费用计算，不是承诺赚取的金额。报告中的未卖出持仓会按最后价格估算，结束时不会自动卖出。

## 遇到中断

| 看到的情况 | 该怎么做 |
| --- | --- |
| 页面已关闭或刷新 | 重新进入示例查看原进度，已接收的任务可能仍在运行 |
| 提交后不知道是否成功 | 点 **核对并继续**，先找回原任务 |
| 步骤失败 | 查看原因，修正网络或设置后点 **重试这一步** |
| 想停止示例 | 点 **请求停止**，等页面确认状态 |

不要反复点击开始来猜测是否提交成功。[更多启动问题](OPERATIONS.md#常见问题)

## 下一步

- 换证券或日期：[下载和管理行情](USER_GUIDE.md#下载和管理行情)。
- 使用自己的策略、加入指数对照：[回测和结果](USER_GUIDE.md#回测和结果)。
- 让 Codex 或 DeepSeek 帮你操作：[连接 AI 助手](platform/MCP_CONNECTION.md)。
- 让软件自己调用模型：[模型设置](AI_SETUP_GUIDE.md)。

## English quick steps

Install from the Release ZIP, keep the payload beside Setup, and create the first account using the launcher's setup code. Choose **先跑一个例子**, review the displayed funds and fees, and press **确认并开始示例**. After the Yahoo download and simulation finish, use **查看结果** or **打开完整报告**. **核对并继续** checks an uncertain request; **重试这一步** retries a failed step. The [English README](../README.md) explains the installer and supported scope.
