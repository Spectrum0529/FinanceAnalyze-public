# Third-party notices

AKShare is the explicitly derived `1.19.3+guard1` build of upstream 1.19.3, not
an unmodified upstream distribution. Two disclosed guard changes replace the
remote factor eval with JSON parsing and reject an all-NaN adjusted result
instead of falling back to unadjusted data. The native decoder/price arithmetic
remain upstream code. The upstream MIT notice is retained in
`vendor/wheels/AKShare-MIT-LICENSE`. The derived wheel is
`vendor/wheels/akshare-1.19.3+guard1-py3-none-any.whl`, SHA-256
`67b3545b568815ca751d49a6d91dd4b268f36ce6dca0b0250c8d94e9ab0f203c`.

Bundled native components and their notices are listed in the [third-party license guide](licenses/README.md). For exact upstream sources and rebuild or replacement information, see the [source index](licenses/THIRD_PARTY_SOURCE_INDEX.json). Other dependency distributions carry their own notices and are fixed by the applicable lockfile.

FinanceAnalyze's original code is covered by the project [MIT License](LICENSE). Third-party components retain their own terms.

Apache ECharts 6.1.0 is reused through its unmodified public modular API.
Its original Apache-2.0 LICENSE and NOTICE are retained in
`licenses/ECHARTS-6.1.0-LICENSE.txt` and `licenses/ECHARTS-6.1.0-NOTICE.txt`.
The fixed upstream commit is c5a48f5f97d23e5379720870b8444cd05b50ffb4.
zrender 6.1.0 (BSD-3-Clause) and tslib 2.3.0 notices are retained in
`licenses/ZRENDER-6.1.0-LICENSE.txt`, `licenses/TSLIB-2.3.0-LICENSE.txt` and
`licenses/TSLIB-2.3.0-CopyrightNotice.txt`. The exact dependency source and
registry integrity remain bound by `web/pnpm-lock.yaml`; generated bundle
notices remain in `web/dist/.vite/license.md`.

[Build from source or rebuild with a compatible replacement dependency](docs/BUILDING.md).
