[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(Mandatory = $true)][string]$Work
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Set-StrictMode -Version Latest
$projectPath = (Resolve-Path -LiteralPath $Project).Path
$workPath = [IO.Path]::GetFullPath($Work)
if (Test-Path -LiteralPath (Join-Path $projectPath 'src/finance_platform/data/provider_sdk/runtime_binding.json')) {
    throw 'Machine-specific SDK binding in source'
}
if (Test-Path -LiteralPath $workPath) { throw 'Build workspace already exists' }
[void][IO.Directory]::CreateDirectory($workPath)

function Get-VerifiedInput {
    param([string]$Uri, [string]$Destination, [string]$Sha256)
    Invoke-WebRequest -Uri $Uri -OutFile $Destination -TimeoutSec 120
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Sha256) {
        throw "Unexpected SHA-256 for $([IO.Path]::GetFileName($Destination))"
    }
}

$pythonArchive = Join-Path $workPath 'python.tar.gz'
Get-VerifiedInput 'https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.12.14%2B20260924-x86_64-pc-windows-msvc-install_only.tar.gz' `
    $pythonArchive 'c5303174bc29f5205decf6721ac549d4eb41c448f9b8c46cbc562d00348865bb'
$tar = Join-Path $env:SystemRoot 'System32/tar.exe'
& $tar -xf $pythonArchive -C $workPath
$pythonDir = Join-Path $workPath 'python'
$python = Join-Path $pythonDir 'python.exe'
$pythonVersion = & $python -I -B -c 'import platform, sys; print(platform.machine(), platform.python_version()); sys.exit(sys.maxsize < 2**32)'
if ($pythonVersion -cne 'AMD64 3.12.14') { throw 'Unexpected Python binary' }

$uvArchive = Join-Path $workPath 'uv.zip'
Get-VerifiedInput 'https://github.com/astral-sh/uv/releases/download/0.12.5/uv-x86_64-pc-windows-msvc.zip' `
    $uvArchive '4c4d49d8738847d9b71ba319e49a5688c93eac0fe6204b1df24e98528dddf39a'
$uvDir = Join-Path $workPath 'uv'
Expand-Archive -LiteralPath $uvArchive -DestinationPath $uvDir
$uvFiles = @(Get-ChildItem -LiteralPath $uvDir -Recurse -File -Filter 'uv.exe')
if ($uvFiles.Count -ne 1) { throw 'Unexpected uv archive layout' }
$uv = $uvFiles[0].FullName
if ((& $uv --version) -cnotmatch '^uv 0\.12\.5(?:\s|$)') { throw 'Unexpected uv version' }

$pythonFullArchive = Join-Path $workPath 'python-cpython-full.tar.zst'
Get-VerifiedInput 'https://github.com/astral-sh/python-build-standalone/releases/download/20260924/cpython-3.12.14%2B20260924-x86_64-pc-windows-msvc-pgo-full.tar.zst' $pythonFullArchive 'af130d6f2c314fc48f425726a03b8f80bb878cc139b3ae624bb050b42140d8d4'
$pythonFull = Join-Path $workPath 'python-full-metadata'
[void][IO.Directory]::CreateDirectory($pythonFull)
& $tar -xf $pythonFullArchive -C $pythonFull python/licenses python/install/python.exe python/install/python312.dll
foreach ($binary in @('python.exe', 'python312.dll')) {
    $installed = (Get-FileHash -LiteralPath (Join-Path $pythonDir $binary)).Hash
    $full = (Get-FileHash -LiteralPath (Join-Path $pythonFull "python/install/$binary")).Hash
    if ($full -cne $installed) { throw 'Python licence bundle targets different runtime bytes' }
}
$pythonLicenses = Join-Path $pythonFull 'python/licenses'

$licenses = Join-Path $workPath 'uv-licenses'
[void][IO.Directory]::CreateDirectory($licenses)
$uvSource = 'https://raw.githubusercontent.com/astral-sh/uv/210d1f6785e95a8c8c0d53e284408c9be1134700'
Get-VerifiedInput "$uvSource/LICENSE-APACHE" (Join-Path $licenses 'LICENSE-APACHE') `
    'c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4'
Get-VerifiedInput "$uvSource/LICENSE-MIT" (Join-Path $licenses 'LICENSE-MIT') `
    '860e3d7a86b84e6a7012c7a635fc64df475cebc6cce34dfeb73a5982ec58176c'

$cache = Join-Path $workPath 'uv-cache'
$env:UV_CACHE_DIR = $cache
$env:UV_PROJECT_ENVIRONMENT = Join-Path $workPath 'seed-venv'
Push-Location -LiteralPath $projectPath
try {
    & $uv sync --locked --no-config --no-install-project --link-mode copy --no-python-downloads --python $python
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $workPath 'offline-check-venv'
    & $uv sync --offline --locked --no-config --no-install-project --link-mode copy --no-python-downloads --python $python --dry-run
}
finally {
    Pop-Location
}

$out = Join-Path $workPath 'out'
[void][IO.Directory]::CreateDirectory($out)
$build = Join-Path $workPath 'package'
& $python -I -B -X utf8 (Join-Path $PSScriptRoot 'build_package.py') --project $projectPath --python $pythonDir `
    --uv $uv --cache $cache --out $build --uv-licenses $licenses --python-licenses $pythonLicenses
$manifest = Get-Content -LiteralPath (Join-Path $build 'package.json') -Raw | ConvertFrom-Json
$members = @($manifest.members.path)
$requiredMembers = @('LICENSE', 'README.md', 'README.zh-CN.md', 'resources/prediction_protocol.json', 'configs/data_sources.lock.json', 'samples/public/synthetic_market/events.json', 'licenses/python-build-standalone-20260924/LICENSE.cpython.txt', 'tools/platform_ops/mcp_stdio.py', 'docs/GETTING_STARTED.md', 'tools/platform_ops/launcher/README.txt')
foreach ($required in $requiredMembers) {
    if ($required -cnotin $members) { throw "Installer payload misses $required" }
}
if ('src/finance_platform/data/provider_sdk/runtime_binding.json' -cin $members) {
    throw 'Machine-specific SDK binding in installer payload'
}
& $python -I -B -X utf8 (Join-Path $PSScriptRoot 'build_distribution.py') $build (Join-Path $out 'FinanceAnalyze-Windows-x64.zip')
