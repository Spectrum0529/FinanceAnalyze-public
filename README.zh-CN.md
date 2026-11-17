# FinanceAnalyze

在自己的 Windows 电脑上下载行情、用虚拟本金测试投资策略，并查看和管理报告。

[English](README.md) · [第一次使用](docs/GETTING_STARTED.md) · [使用指南](docs/USER_GUIDE.md) · [连接 AI 助手](docs/platform/MCP_CONNECTION.md)

## 可以做什么

- 下载行情或导入 CSV，保存后反复用于回测。
- 使用内置策略或受支持的自建策略，设置本金和交易费用。
- 与“同一天一次性投入相同本金，买入并持有 SPY、QQQ、ASHR”分别比较，选择显示哪些曲线。
- 点击结果打开预览，关闭后回到原位置；管理数据和报告，回收后可以恢复。
- 让同一台电脑上的 Codex、DeepSeek Harness 或兼容 MCP 的助手操作已授权功能，并分别撤销权限。

计算在本机完成。第一次示例需要联网下载行情，**不需要 AI 密钥或模型余额**。

## 安装

适用于 **Windows x64**。安装器已包含 Python 和应用所需依赖。

1. 在[最新 Windows 发行版](https://github.com/Spectrum0529/FinanceAnalyze-public/releases/latest)中下载同一版本的 `FinanceAnalyze-Windows-x64.zip` 和 `.sha256` 校验文件。
2. 完整解压 ZIP。不要把 `FinanceAnalyze.payload.zip` 与 `FinanceAnalyze-Setup.exe` 分开。
3. 打开 Setup，选择 **安装或继续**，选定安装文件夹。完成后点 **打开软件**。
4. 第一次使用，在启动器点 **获取首次设置码**，到本机网页输入设置码并创建管理员账号。以后使用自己的密码登录。

如需核对下载文件，在下载文件夹打开 PowerShell，运行下列命令，将显示的 SHA256 与校验文件比较：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '.\FinanceAnalyze-Windows-x64.zip'
```

**Code → Download ZIP** 下载的是源码，不是安装包。首次使用请按上面的 Releases 安装步骤操作。[启动、修复和备份](docs/OPERATIONS.md)

## 跑出第一份报告

1. 登录后点 **先跑一个例子**。
2. 核对 AAPL、2025 年 1 月 2 日至 3 月 31 日、10,000 美元虚拟本金和页面上的费用，点 **确认并开始示例**。
3. 软件会从 Yahoo 下载真实日线，保存数据，然后模拟买入并持有。
4. 完成后点 **查看结果**。关闭预览回到原处，或点 **打开完整报告**。以后在 **D · 研究结果** 重新打开。

如果提交后没有明确结果，先点 **核对并继续**；步骤失败时看清原因，再点 **重试这一步**。[详细步骤](docs/GETTING_STARTED.md)

## 接下来怎么用

| 想做的事 | 说明 |
| --- | --- |
| 下载、复用、回收、恢复或合并行情 | [数据管理](docs/USER_GUIDE.md#下载和管理行情) |
| 制作策略、回测、比较指数与查看结果 | [回测和结果](docs/USER_GUIDE.md#回测和结果) |
| 让外部 AI 帮忙操作 | [连接 AI 助手](docs/platform/MCP_CONNECTION.md) |
| 在软件内使用模型做研究 | [模型和密钥设置](docs/AI_SETUP_GUIDE.md) |
| 修复安装、备份或换电脑 | [日常维护](docs/OPERATIONS.md) |

已提供 yfinance、AKShare、Tushare Pro、Massive、WRDS，以及需额外准备组件的 OpenBB 来源连接，也可导入 CSV。每种来源支持的市场、下载范围和回测能力不同，账号权限和订阅也会影响可用数据。[来源说明](docs/USER_GUIDE.md#数据来源)

## 使用范围与隐私

本版供本机使用，尚不是可直接暴露到公网的网站或远程 MCP 服务。行情下载会连接所选供应商；同意使用模型后，指定的研究内容可能发送给模型提供方并产生费用。账号、密钥、私人行情、报告和备份请留在自己的私有存储，不要上传到公开仓库或问题反馈中。

回测是虚拟资金模拟，不能保证未来收益；真实交易保持关闭。WRDS 等数据及相关报告仍遵守来源许可和软件里的分享限制。

## 许可证

FinanceAnalyze 的原创代码采用 [MIT 许可证](LICENSE)。第三方组件继续遵守各自许可证，见 [第三方说明](THIRD_PARTY_NOTICES.md)。MIT 不包含行情供应商的数据授权、收费订阅或用户自行提供的策略包授权。
