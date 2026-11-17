param([string]$Root = (Join-Path $PSScriptRoot '..\..'), [string]$Python)
$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $Root).Path
$python = if ($Python) { (Resolve-Path -LiteralPath $Python).Path } else { Join-Path $rootPath '.venv\Scripts\python.exe' }
. (Join-Path $rootPath 'tools\platform_ops\source-install.ps1')
$sourceSelection = Set-PlatformSourceRoot -RootPath $rootPath
try {
    $binding = & $python -P -m finance_platform.operations.source_binding --root $rootPath
    if ($LASTEXITCODE -ne 0) { throw 'Source/interpreter binding failed' }
    Write-Output ($binding -join "`n")
    & $python -P -m finance_platform.operations --root $rootPath doctor
    if ($LASTEXITCODE -ne 0) { throw 'Dependency doctor failed' }
} finally {
    Restore-PlatformSourceEnvironment -Selection $sourceSelection
}
