<p align="center">
  <img src="web/src/v434/assets/logo-light.svg" alt="" width="72">
</p>

<h1 align="center">FinanceAnalyze</h1>

<p align="center"><strong>下载行情，用虚拟本金测试策略，看看它在过去的表现。</strong></p>

<p align="center">
  <a href="https://github.com/Spectrum0529/FinanceAnalyze-public/releases/latest"><strong>下载 Windows 安装包</strong></a>
  · <a href="#跑出第一份报告">试跑示例</a>
  · <a href="README.md">English</a>
</p>

FinanceAnalyze 是一款开源的 Windows 投资研究工具。你可以用历史价格回答“扣掉交易费后，这个策略过去表现怎么样？”或者“如果第一天把同样的钱都买成 SPY，会怎样？”软件在你的电脑上运行，操作界面在浏览器里打开。

## 安装

1. 到[最新发行版](https://github.com/Spectrum0529/FinanceAnalyze-public/releases/latest)的 **Assets** 下载 **`FinanceAnalyze-Windows-x64.zip`**。
2. 完整解压，双击 **`FinanceAnalyze-Setup.exe`**。安装时让 `FinanceAnalyze.payload.zip` 和它留在同一个文件夹。
3. 点 **安装或继续**。可以使用默认位置，也可以选择一个可写的本机空文件夹。装好后点 **打开软件**。
4. 在启动器点 **获取首次设置码**，到自动打开的本机页面创建账号。

安装包自带 Python 和运行所需组件。以后双击 `FinanceAnalyze.exe`，直接用自己的账号密码登录。[启动与修复说明](docs/OPERATIONS.md)

## 跑出第一份报告

1. 登录后点 **先跑一个例子**。
2. 点 **确认并开始示例**。软件会从 Yahoo 下载苹果公司（AAPL）的历史行情，用 **10,000 美元虚拟本金**模拟买入并持有。
3. 点 **查看结果**打开预览，或点 **打开完整报告**查看明细。以后可在 **D · 研究结果** 找到这份报告。

示例需要联网下载行情，可以直接运行，无需配置 AI 密钥。[第一次使用指南](docs/GETTING_STARTED.md)

## 可以做什么

| 页面 | 用途 |
| --- | --- |
| **A · 策略与回测** | 选内置策略或受支持的自建策略，设置本金和交易费。也可以对比同一笔钱一次性买入并持有 SPY、QQQ 或 ASHR 的表现，分别显示或隐藏曲线。 |
| **B · AI 研究** | 配置模型做研究，或授权同一台电脑上的 Codex、DeepSeek Harness 等兼容 MCP 的助手操作。每个助手的权限可单独撤销。 |
| **C · 数据管理** | 下载行情或导入 CSV。保存的数据可以重复用于回测，符合条件的日期区间也可以合并。 |
| **D · 研究结果** | 点报告即可预览。已保存的报告可以重新查看，符合条件的结果可移入回收站并恢复。 |

可连接 **Yahoo（yfinance）、AKShare、Tushare Pro、Massive、WRDS** 和选配的 **OpenBB** 数据来源。各来源支持的市场、功能和所需订阅不同。[数据来源说明](docs/USER_GUIDE.md#数据来源)

如果想让外部助手操作软件，参照[连接 AI 助手](docs/platform/MCP_CONNECTION.md)。如果想在软件内使用在线模型，参照[模型和密钥设置](docs/AI_SETUP_GUIDE.md)。在线请求可能把选定的研究内容发给模型提供方，并产生费用。

## 源码构建与反馈

仓库包含 Python 后端、React 界面，以及 Windows 启动器和安装器。构建工具与步骤见[源码构建指南](docs/BUILDING.md)。欢迎反馈问题，附上软件版本、复现步骤和报错文字。[提交问题](https://github.com/Spectrum0529/FinanceAnalyze-public/issues)

## 许可证与使用范围

原创代码采用 [MIT 许可证](LICENSE)。第三方组件沿用各自的许可证，行情数据按供应商条款使用。[第三方许可说明](THIRD_PARTY_NOTICES.md)

本版供本机使用。回测基于历史行情和虚拟成交，实盘交易处于关闭状态。
