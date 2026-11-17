param([string]$Root = (Join-Path $PSScriptRoot '..\..'))
$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $Root).Path
$statePath = Join-Path $rootPath '.local\platform\processes\native.json'
if (-not (Test-Path -LiteralPath $statePath)) { Write-Output 'No managed native process state'; exit 0 }
$entries = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
foreach ($entry in $entries) {
    if ($entry.PSObject.Properties.Name -contains 'root' -and $entry.root -ne $rootPath) { throw 'Process state belongs to another source root' }
    $process = Get-Process -Id $entry.process_id -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    if ($process.StartTime.ToUniversalTime().Ticks -ne $entry.started_ticks -or $process.Path -ne $entry.executable) { throw 'Process identity differs; refusing to stop it' }
    # Forced interruption retains durable leases/runs; restart uses core expired-job recovery.
    & taskkill.exe /PID $entry.process_id /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Managed process tree did not stop' }
}
Remove-Item -LiteralPath $statePath
Write-Output 'Managed API/worker stopped; persistent data and logs retained'
