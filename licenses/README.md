# Third-party licenses and source code

FinanceAnalyze's original source is MIT licensed. The libraries and runtimes it uses retain their own terms. Market data, service accounts and user-created strategy packages are not licensed by the project's MIT file.

Keep this directory and each distribution's own `.dist-info/licenses`, `.dist-info/sboms`, LICENSE, LICENCE, COPYING and NOTICE files when redistributing an installer. A dependency lock records versions and downloaded artifact hashes; it does not replace the license texts.

## Runtime notices

| Component | Terms and source |
| --- | --- |
| Python 3.12.14, python-build-standalone 20260924 | The runtime's `LICENSE.txt` plus [component notices](python-build-standalone-20260924/). These notices come from the exact full build archive corresponding to the selected Windows x64 release. [Build source](https://github.com/astral-sh/python-build-standalone/tree/20260924). |
| Psycopg and psycopg-binary 3.3.9 | LGPL-3.0-only. [LGPL text](Psycopg-3.3.9-LGPL-3.0.txt), [GPL text](GPL-3.0-only.txt), [source and build scripts](https://github.com/psycopg/psycopg/tree/3.3.9). The Windows binary also contains [PostgreSQL 18.4](PostgreSQL-18.4-LICENSE.txt) and [OpenSSL 3.6.5](OpenSSL-3.6.5-LICENSE.txt). |
| cryptography 50.0.2 | Retain its Apache/BSD notices and upstream SBOM files. The selected Windows wheel additionally contains [OpenSSL 4.0.3](OpenSSL-4.0.3-LICENSE.txt). [Supplemental Rust dependency notices](CRYPTOGRAPHY-50.0.2-RUST-NOTICES.txt) and their [versioned source records](CRYPTOGRAPHY-50.0.2-RUST-SOURCES.json) cover the 32 registry crates in its source lock and official wheel's Rust SBOM. |
| MiniRacer 0.14.1 | Retain its ISC license and authors list. The Windows binary contains [V8 14.4.258.23](V8-14.4.258.23-LICENSE.txt), [V8 dependency notices](v8-14.4.258.23/) and ICU data. The latter is under the [Unicode license](v8-14.4.258.23/ICU-LICENSE.txt). |
| curl-cffi / curl-impersonate | Retain curl-cffi's MIT license and the [curl-impersonate 2.3.1 component notices](curl-impersonate-2.3.1/), including BoringSSL and compression/HTTP libraries. |
| sqlite-vec 0.1.9 | [MIT](sqlite-vec-0.1.9-LICENSE-MIT.txt) or [Apache-2.0](sqlite-vec-0.1.9-LICENSE-APACHE.txt). |
| ECharts, zrender, tslib and React | Preserve the versioned notices in this directory and the generated `web/dist/.vite/license.md`. ECharts' NOTICE is required alongside its Apache license. |

The V8 supplement contains notices for its source dependencies, including platform-conditional components; it is not a claim that every optional dependency is executed on Windows. Upstream copyright names and contact details in required notices are intentionally preserved.

## LGPL and MPL source access

For executable Windows build and modified-library instructions, see [Building from source](../docs/BUILDING.md).

[THIRD_PARTY_SOURCE_INDEX.json](THIRD_PARTY_SOURCE_INDEX.json) lists fixed versions and source archives for the LGPL/MPL portions, with source checksums where the dependency lock provides them. Psycopg's full source includes the Cython library and binary-package build scripts. The Python files included in the installed packages are also source code. Upstream sources retain the terms declared within those archives; they do not become MIT merely because FinanceAnalyze uses them.

Psycopg is a separately loaded Python package and native extension. You may modify its LGPL-covered parts and reverse engineer the combined work to debug those modifications. FinanceAnalyze imposes no license term preventing those activities. The application source, dependency manifest and build scripts are provided so that an interface-compatible modified library can be recombined with it. For a modified distribution, point the dependency source at your rebuilt library, regenerate the lock and build the application and launcher from the same source tree. A newly built distribution must have newly generated integrity records; do not disable account authorization, certificate checks, source verification or data-access rules. Keep private accounts and datasets outside the build tree.

MPL-covered portions include certifi's certificate bundle, tqdm and portions of orjson. Their source access and notices remain available through the source index and packaged license files. The independent FinanceAnalyze files keep their MIT license.

The installer does not grant rights to third-party names or trademarks. Each component's original warranty and liability terms remain in effect.
