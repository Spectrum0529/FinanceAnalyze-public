# Build FinanceAnalyze from source

This guide is for developers creating a Windows x64 installer from the public source, including a version that uses an interface-compatible modified dependency. For normal installation, use the [getting started guide](GETTING_STARTED.md).

The build tools generate new integrity records from your source, dependency lock and packaged files. They do not require a FinanceAnalyze maintainer's private signing key. Keep account authentication, TLS checks, data permissions and integrity checks enabled.

## Tools and source files

Use Windows x64 with:

| Tool | Version or requirement |
| --- | --- |
| PowerShell | 7.3 or later, so failed native commands stop the commands below |
| Python used for build scripts | 3.12.14, x64 |
| uv used to update a dependency lock | 0.12.5 |
| Node.js | At least 22.12.0, as required by `web/package.json` |
| pnpm | 11.25.0, as pinned in `web/package.json` |
| C# compiler | Windows .NET Framework compiler at `%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\csc.exe` |
| Windows archive tool | `%SystemRoot%\System32\tar.exe`, including support for the pinned `.tar.zst` archive |

`Build-Release.ps1` independently downloads and checks the exact runtime inputs: Python 3.12.14 from python-build-standalone release `20260924`, and uv `0.12.5`. It also fetches their pinned license files. An internet connection is needed to build the installer; the packaged application dependencies are prepared for offline installation.

Start with a fresh source checkout or an extracted source archive. Keep these source inputs together:

- `src/`, `web/`, `tools/`, `configs/`, `resources/`, `docs/` and `licenses/`;
- `pyproject.toml`, `uv.lock`, `.python-version`, `LICENSE`, `THIRD_PARTY_NOTICES.md` and both README files;
- the wheel files referenced by `uv.lock` under `vendor/wheels/` and `workers/tradingagents/vendor/wheels/`, with their notices;
- `web/package.json`, `web/pnpm-lock.yaml`, the TypeScript/Vite configuration and all interface source/assets.

Keep other published worker sources if you plan to develop those optional components. The standard Windows installer resolves the root `uv.lock`; it does not package every optional worker environment.

Do not use an active installation as the build checkout. Keep `.local/`, accounts, downloaded datasets, reports, backups, `.env`, API credentials, machine-specific `configs/*.actual.json` and SDK `runtime_binding.json` out of the source. A fresh `.venv` or cache is generated for the build; copying one from a user's installation is unnecessary. `FinanceAnalyze.exe` and `web/dist/` are generated below and are intentionally ignored by Git.

## 1. Choose the source and an empty build location

Open PowerShell 7 in your source checkout. Replace these example paths with your own. The build location must be outside the checkout and must not already exist.

```powershell
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
if ($PSVersionTable.PSVersion -lt [version]'7.3') {
    throw 'Use PowerShell 7.3 or later for this build guide.'
}

Set-Location -LiteralPath 'C:\src\FinanceAnalyze'
$Project = (Get-Location).Path
$BuildRoot = [IO.Path]::GetFullPath('C:\Build\FinanceAnalyze-custom-01')
$ProjectPrefix = $Project.TrimEnd('\') + '\'
if ($BuildRoot.Equals($Project, [StringComparison]::OrdinalIgnoreCase) -or
    $BuildRoot.StartsWith($ProjectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Choose a build location outside the source checkout.'
}
if (Test-Path -LiteralPath $BuildRoot) {
    throw 'Choose a new build location for each build.'
}
[void][IO.Directory]::CreateDirectory($BuildRoot)

$BuildPython = (Get-Command python.exe -ErrorAction Stop).Source
$BuildUv = (Get-Command uv.exe -ErrorAction Stop).Source
& $BuildPython -I -B -c 'import platform,sys; assert platform.python_version() == "3.12.14" and sys.maxsize > 2**32'
if ((& $BuildUv --version) -notmatch '^uv 0\.12\.5(?:\s|$)') {
    throw 'Use uv 0.12.5.'
}
if ((& pnpm --version) -ne '11.25.0') {
    throw 'Use pnpm 11.25.0.'
}
& node --version
```

If you are changing a third-party library, complete the [dependency steps below](#use-a-modified-compatible-library) before continuing. For an unchanged build, retain the published lockfiles.

## 2. Build the interface

```powershell
Push-Location -LiteralPath (Join-Path $Project 'web')
try {
    & pnpm install --frozen-lockfile
    & pnpm run build
}
finally {
    Pop-Location
}
```

This type-checks the interface and creates `web/dist/`. Keep the generated `web/dist/.vite/license.md`; the Vite configuration generates these dependency notices. Do not copy a different version's prebuilt interface into this checkout.

## 3. Rebuild the launcher

```powershell
$LauncherOut = Join-Path $BuildRoot 'launcher'
& $BuildPython -I -B -X utf8 `
    (Join-Path $Project 'tools/platform_ops/launcher/build.py') `
    --project-root $Project --output-directory $LauncherOut
Copy-Item -LiteralPath (Join-Path $LauncherOut 'FinanceAnalyze.exe') `
    -Destination (Join-Path $Project 'FinanceAnalyze.exe')
```

The builder generates `BuildPins.cs` from the current startup, source-binding and launcher bridge files, then compiles the launcher using the public C# source. Run it again after changing any launcher or bound startup input. Its output directory must not contain earlier build outputs.

## 4. Build the installer and checksum

```powershell
$ReleaseWork = Join-Path $BuildRoot 'release'
& (Join-Path $Project 'tools/platform_ops/installer/Build-Release.ps1') `
    -Project $Project -Work $ReleaseWork

$ReleaseZip = Join-Path $ReleaseWork 'out/FinanceAnalyze-Windows-x64.zip'
$ReleaseHash = (Get-FileHash -LiteralPath $ReleaseZip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($ReleaseZip + '.sha256') -Encoding ascii `
    -Value ($ReleaseHash + '  FinanceAnalyze-Windows-x64.zip')
```

`$ReleaseWork` must not already exist. The script downloads the verified runtime and uv, fills a new dependency cache from the root lock, checks offline resolution, and calls `build_package.py` and `build_distribution.py`. Keep the source unchanged while it runs.

The package builder generates `SetupPins.cs` containing the new source, lock and payload hashes, then compiles `FinanceAnalyze-Setup.exe`. The distribution builder checks those outputs before creating the ZIP. These are integrity hashes; no private release-signing key is used. The build workspace contains local build records for your review; it is not part of the public source tree.

Distribute `FinanceAnalyze-Windows-x64.zip` with its `.sha256` file. The ZIP contains Setup, its matching payload and installation instructions. Keep the full ZIP contents together when installing into a separate directory. Setup creates that installation's environment and machine-specific SDK binding. Use normal account setup and sign-in when opening the rebuilt application.

Before publishing your version, check the exact resulting package, its notices and its normal application workflow. Creating a ZIP alone is not evidence that a modified library is compatible. Do not reuse an older version's acceptance result or imply that an unofficial modified build is the upstream release.

## Use a modified compatible library

You may modify LGPL-covered Psycopg and reverse engineer the combined work to debug those modifications. The [license guide](../licenses/README.md) includes the full LGPL/GPL texts and links to corresponding source and upstream build instructions.

For a modified Psycopg build:

1. Obtain the fixed source referenced in the [source index](../licenses/THIRD_PARTY_SOURCE_INDEX.json), make your changes, and build wheel files using Psycopg's upstream Windows build instructions. Match the installed Python 3.12 Windows x64 interfaces. Use a distinguishable local version such as `3.3.9+custom1`; build that version into the wheel metadata rather than renaming an unrelated wheel file.
2. Put the wheels directly in `vendor/wheels/` in this source checkout. The installer accepts locked local wheel files directly under `vendor/wheels/` or `workers/tradingagents/vendor/wheels/`. It rejects absolute paths, backslashes in lock paths, arbitrary source directories and additional nested vendor folders.
3. In the existing `[tool.uv.sources]` table in the root `pyproject.toml`, add entries matching the wheels you actually built. Do not add a second table with the same name. For example, when both wrapper and binary wheels were rebuilt:

   ```toml
   psycopg = { path = "vendor/wheels/psycopg-3.3.9+custom1-py3-none-any.whl" }
   psycopg-binary = { path = "vendor/wheels/psycopg_binary-3.3.9+custom1-cp312-cp312-win_amd64.whl" }
   ```

   Keep the application's `psycopg[binary]` dependency and its Windows platform condition. If your library's public version changes, update the dependency requirement to an interface-compatible version too. If only one library distribution changed, override only that distribution and keep the other compatible package pinned normally.

4. Regenerate the root lock with the selected Python and uv:

   ```powershell
   Push-Location -LiteralPath $Project
   try {
       & $BuildUv lock --no-config --no-python-downloads --python $BuildPython
   }
   finally {
       Pop-Location
   }
   ```

   Review `pyproject.toml` and `uv.lock`: the selected wheel path, version and SHA-256 must refer to your new artifact. Keep unrelated dependencies unchanged unless you intentionally need an update. Do not manually forge a wheel hash or reuse the old dependency cache.

5. Preserve the upstream license texts and include your modified library's corresponding source, change notice and any required build instructions when redistributing it. Update the third-party source index to describe the library you actually ship; the project's original source remains MIT licensed.
6. Continue the interface, launcher and installer steps above with fresh output directories. The package builder reads current locked vendor-wheel paths rather than a hard-coded Psycopg wheel name, and generates new integrity records. Keep authentication, TLS, data permissions and source verification enabled; editing an already installed package underneath an old installer is not the rebuild path.

Changing a dependency for an optional worker uses that worker's own manifest and lock. It does not update the standard Windows installer's root dependency set automatically.
