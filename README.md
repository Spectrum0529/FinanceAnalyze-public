# FinanceAnalyze

Download market data, test investment strategies with virtual funds, and review saved reports in a local Windows workbench.

[简体中文](README.zh-CN.md) · [Get started](docs/GETTING_STARTED.md) · [User guide](docs/USER_GUIDE.md) · [Connect an AI assistant](docs/platform/MCP_CONNECTION.md)

## What it does

- Downloads market data or imports CSV files, then saves datasets for reuse.
- Runs built-in and supported custom strategies with the starting balance and trading costs you choose.
- Compares a strategy with investing the same starting balance in SPY, QQQ, and ASHR. Each comparison has its own chart color and visibility control.
- Opens result previews without losing your place in the list. Saved datasets and reports support management, including moving eligible items to the trash and restoring them.
- Lets local Codex, DeepSeek Harness, and compatible MCP clients use authorized workbench tools. Permissions can be revoked per client.

Calculations run on your computer. The first example needs an internet connection, but no AI API key or model balance. The current interface is primarily Chinese; the steps below include the labels you will see.

## Install

The installer targets **Windows x64** and includes Python and the required application dependencies.

1. Open the [latest Windows release](https://github.com/Spectrum0529/FinanceAnalyze-public/releases/latest). Download `FinanceAnalyze-Windows-x64.zip` and the companion `.sha256` file for the same release.
2. Extract the entire ZIP. Keep `FinanceAnalyze.payload.zip` next to `FinanceAnalyze-Setup.exe`.
3. Open Setup, select **安装或继续** (Install or continue), and choose an installation folder. When finished, select **打开软件** (Open the app).
4. For the first account, select **获取首次设置码** (Get the initial setup code) in the launcher. Enter that code on the local page and create your administrator account. On later visits, sign in with your own password.

To check the downloaded ZIP, open PowerShell in its folder and compare the result with the companion checksum:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath '.\FinanceAnalyze-Windows-x64.zip'
```

The **Code → Download ZIP** archive is source code, not the ready-to-use installer. Use the Release attachment for the steps above. [Startup, repair, and backups](docs/OPERATIONS.md)

## Run your first backtest

1. Sign in and choose **先跑一个例子** (Run an example).
2. Review AAPL, January 2–March 31, 2025, USD 10,000 in virtual funds, and the displayed fees. Select **确认并开始示例** (Confirm and start).
3. The app downloads real Yahoo daily prices, saves the dataset, and simulates buying and holding AAPL.
4. Select **查看结果** (View result). Close the preview to return to your place, or choose **打开完整报告** (Open full report). Reopen it later in **D · 研究结果** (Results).

If a request appears unfinished, use **核对并继续** (Check and continue). After a failed step, inspect the reason before selecting **重试这一步** (Retry this step). [Step-by-step guide](docs/GETTING_STARTED.md)

## Data and AI connections

Data integrations include Yahoo through yfinance, AKShare, Tushare Pro, Massive, WRDS, and optional OpenBB sources. These are different connectors, not promises that every dataset or market supports every feature. Provider accounts, subscriptions, source availability, and optional components determine what you can use. CSV import is also available. [Data sources and limits](docs/USER_GUIDE.md#数据来源)

There are two separate ways to use AI:

| You want to… | Start here |
| --- | --- |
| Let Codex or DeepSeek operate the local workbench | [Connect an AI assistant](docs/platform/MCP_CONNECTION.md) |
| Let FinanceAnalyze send research requests to a model provider | [Model and API key setup](docs/AI_SETUP_GUIDE.md) |

## Security and privacy

Keep the workbench local to your computer. This release is not a ready-to-expose public website or remote MCP service. Accounts, keys, private datasets, reports, and backups do not belong in GitHub issues or source uploads. Data downloads contact the selected provider; approved model research can send the selected information to that model provider and may incur charges.

Backtests use virtual funds and are simulations, not promises of future returns. Live trading is disabled. WRDS and other restricted data remain subject to their provider's terms and the app's sharing restrictions. [Account, backup, and connection help](docs/OPERATIONS.md)

## Project layout

| Folder | Contents |
| --- | --- |
| `src/` | Application services and calculation code |
| `web/` | Workbench interface |
| `workers/` | Optional research and data components |
| `configs/`, `resources/` | Generic configuration examples and application resources |
| `tools/` | Build, installation, and local operation tools |
| `docs/`, `licenses/` | User instructions and license notices |

## License

FinanceAnalyze's original source code is licensed under the [MIT License](LICENSE). Third-party components retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The MIT license does not grant rights to a provider's market data, paid subscriptions, or user-supplied strategy packages.
