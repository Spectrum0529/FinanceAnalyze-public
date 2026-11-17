<p align="center">
  <img src="web/src/v434/assets/logo-light.svg" alt="" width="72">
</p>

<h1 align="center">FinanceAnalyze</h1>

<p align="center"><strong>Get market data, test a strategy, and see how it would have performed.</strong></p>

<p align="center">
  <a href="https://github.com/Spectrum0529/FinanceAnalyze-public/releases/latest"><strong>Download for Windows</strong></a>
  · <a href="#try-your-first-backtest">Try an example</a>
  · <a href="README.zh-CN.md">简体中文</a>
</p>

FinanceAnalyze is an open-source Windows app for exploring investment ideas with historical prices and virtual money. Use it to answer questions like “How would this strategy have done after trading fees?” or “What if I had put the same money into SPY on day one?”

The app runs on your computer and opens in your browser. The interface is currently in Chinese. English translations of the main buttons are included below.

## Install

1. Download **`FinanceAnalyze-Windows-x64.zip`** under **Assets** on the [latest release page](https://github.com/Spectrum0529/FinanceAnalyze-public/releases/latest).
2. Extract the ZIP and run **`FinanceAnalyze-Setup.exe`**. Keep `FinanceAnalyze.payload.zip` beside Setup while installing.
3. Click **安装或继续** (Install or continue). Use the default location or choose an empty folder you can write to on a local drive. Click **打开软件** (Open the app) when Setup finishes.
4. In the launcher, click **获取首次设置码** (Get setup code). Enter the code in the local page to create your account.

Python and the required dependencies come with the installer. For subsequent visits, open `FinanceAnalyze.exe` and sign in. [Installation help and repair](docs/OPERATIONS.md)

## Try your first backtest

1. Sign in and click **先跑一个例子** (Run an example).
2. Click **确认并开始示例** (Confirm and start). FinanceAnalyze downloads AAPL prices from Yahoo and simulates buying and holding the stock with **$10,000 in virtual funds**.
3. Click **查看结果** (View result) to see the preview, or **打开完整报告** (Open full report) for more detail. Find the saved report again in **D · 研究结果** (Results).

The example needs an internet connection to fetch prices. You can run it without an AI API key or paid model account. [Step-by-step guide](docs/GETTING_STARTED.md)

## Explore the workbench

| Area | What you can do |
| --- | --- |
| **A · Strategies and backtests** | Pick a built-in or supported custom strategy, set starting cash and trading fees, and compare it with investing the same amount in SPY, QQQ or ASHR. Toggle each comparison line on the chart. |
| **B · AI research** | Configure a model for research or connect Codex, DeepSeek Harness or another compatible local MCP client. Choose what each assistant can do and revoke access per client. |
| **C · Market data** | Download prices or import a CSV, reuse datasets across backtests, and merge compatible date ranges. |
| **D · Results** | Open a report preview from the list, revisit saved results, and move eligible reports to the trash or restore them. |

Data connectors include **Yahoo via yfinance, AKShare, Tushare Pro, Massive, WRDS**, and optional **OpenBB** sources. Coverage and feature support vary by source, and some require an account or subscription. [Data source guide](docs/USER_GUIDE.md#数据来源)

Use [the MCP guide](docs/platform/MCP_CONNECTION.md) to let a local assistant operate the app. Use [the model setup guide](docs/AI_SETUP_GUIDE.md) to run AI research inside FinanceAnalyze. Online model requests may send selected research inputs to the provider and incur charges.

## Build from source

The repository contains a Python backend, a React interface, and the Windows launcher and installer. See the [build guide](docs/BUILDING.md) for the required tools and reproducible build steps. Issues with a version number, steps to reproduce, and the error message are welcome. [Report an issue](https://github.com/Spectrum0529/FinanceAnalyze-public/issues)

## License and scope

FinanceAnalyze's original code is available under the [MIT License](LICENSE). Bundled dependencies retain their own licenses, and market data is governed by provider terms. [Third-party notices](THIRD_PARTY_NOTICES.md)

This release is designed for local use. Backtests use historical data and simulated trades. Live trading is disabled.
