# Process-only source selection. Callers must restore it in their own finally block.
function Set-PlatformSourceRoot {
    param([Parameter(Mandatory=$true)][string]$RootPath)
    foreach ($relative in @('src\finance_platform\__init__.py', 'src\finance_platform\operations\source_binding.py', 'src\finance_platform\operations\protect-private.ps1', 'src\predictor\__init__.py', 'uv.lock', 'pyproject.toml')) {
        if (-not (Test-Path -LiteralPath (Join-Path $RootPath $relative) -PathType Leaf)) { throw ('Source installation input missing: ' + $relative) }
    }
    $previous = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONPATH', (Join-Path $RootPath 'src'), 'Process')
    return [pscustomobject]@{previous=$previous}
}

function Restore-PlatformSourceEnvironment {
    param([Parameter(Mandatory=$true)]$Selection)
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $Selection.previous, 'Process')
}
