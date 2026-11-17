param([string]$Root = (Join-Path $PSScriptRoot '..\..'), [int]$Port = 8000, [switch]$SkipSync, [string]$Uv = 'uv.exe', [string]$Python, [switch]$NoConsole, [int]$ConsolePort = 5173, [switch]$Personal)
$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $Root).Path
# Both native profiles use the API's same-origin built console.
if ($Personal) { $NoConsole = $true }
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Invalid port' }
if ($ConsolePort -lt 1024 -or $ConsolePort -gt 65535) { throw 'Invalid console port' }
$python = if ($Python) { (Resolve-Path -LiteralPath $Python).Path } else { Join-Path $rootPath '.venv\Scripts\python.exe' }
. (Join-Path $rootPath 'tools\platform_ops\source-install.ps1')
$sourceSelection = Set-PlatformSourceRoot -RootPath $rootPath
$operationLock = $null
try {
if (-not (Test-Path -LiteralPath (Join-Path $rootPath 'web\dist\index.html') -PathType Leaf)) { throw 'Candidate resource missing: web\dist\index.html' }
if (-not $SkipSync) {
    & $Uv sync --locked --project $rootPath
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency sync failed' }
}
$bindingJson = & $python -P -m finance_platform.operations.source_binding --root $rootPath
if ($LASTEXITCODE -ne 0) { throw 'Source/interpreter binding failed; no platform process started' }
$binding = ($bindingJson -join "`n") | ConvertFrom-Json
& $python -P -m finance_platform.operations --root $rootPath doctor
if ($LASTEXITCODE -ne 0) { throw 'Dependency doctor failed' }
$runtime = Join-Path $rootPath '.local\platform\processes'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $rootPath 'src\finance_platform\operations\protect-private.ps1') -Path $runtime
if ($LASTEXITCODE -ne 0) { throw 'Candidate private directory protection failed' }
# This byte range is shared with the explicit Python MFA preparation action.
$lockPath = Join-Path $runtime 'native-operation.lock'
if ((Test-Path -LiteralPath $lockPath) -and ((Get-Item -LiteralPath $lockPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Invalid native operation lock'
}
$operationLock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'ReadWrite')
try { $operationLock.Lock(0, 1) } catch [IO.IOException] { throw 'OPERATION_BUSY' }
$bindingJson | Set-Content -LiteralPath (Join-Path $runtime 'doctor.binding.json') -Encoding UTF8
$statePath = Join-Path $runtime 'native.json'
if (Test-Path -LiteralPath $statePath) { throw 'Existing process state; run stop.ps1 before starting again' }
$entries = @()
$launchEpoch = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0)
try {
    foreach ($component in @('api', 'worker')) {
        $argsList = @('-P', '-m', 'finance_platform.operations.launch', $component, '--root', ('"' + $rootPath + '"'), '--port', $Port, '--console-port', $ConsolePort)
        if ($Personal) { $argsList += '--personal' }
        $process = Start-Process -FilePath $python -ArgumentList $argsList -WorkingDirectory $rootPath -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime "$component.stdout.log") -RedirectStandardError (Join-Path $runtime "$component.stderr.log")
        $entries += [pscustomobject]@{component=$component; process_id=$process.Id; started_ticks=$process.StartTime.ToUniversalTime().Ticks; executable=$python; root=$rootPath; source_hash=$binding.source_hash; lock_sha256=$binding.lock_sha256}
    }
    $deadline = (Get-Date).AddSeconds(70)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        foreach ($entry in $entries) {
            $running = Get-Process -Id $entry.process_id -ErrorAction SilentlyContinue
            if (-not $running) { throw 'A platform process exited; inspect private runtime logs' }
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/v1/health/ready" -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $sourceResponse = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/health/source" -TimeoutSec 2
                if ($sourceResponse.root_hash -ne $binding.root_hash -or $sourceResponse.source_hash -ne $binding.source_hash -or $sourceResponse.lock_sha256 -ne $binding.lock_sha256) { throw 'API source identity differs' }
                $workerBindingPath = Join-Path $runtime 'worker.binding.json'
                if (-not (Test-Path -LiteralPath $workerBindingPath)) { throw 'Worker source binding not ready' }
                $workerBinding = Get-Content -LiteralPath $workerBindingPath -Raw | ConvertFrom-Json
                if ($workerBinding.recorded_at -lt $launchEpoch -or $workerBinding.root_hash -ne $binding.root_hash -or $workerBinding.source_hash -ne $binding.source_hash -or $workerBinding.lock_sha256 -ne $binding.lock_sha256) { throw 'Worker source identity differs' }
                $apiBinding = Get-Content -LiteralPath (Join-Path $runtime 'api.binding.json') -Raw | ConvertFrom-Json
                if ($apiBinding.recorded_at -lt $launchEpoch -or $apiBinding.root_hash -ne $binding.root_hash) { throw 'API process binding not ready' }
                if (-not $Personal -and ($apiBinding.auth_database_binding -cnotmatch '^[a-f0-9]{64}$' -or
                        $apiBinding.auth_database_binding -cne $workerBinding.auth_database_binding)) { throw 'API and worker database identities differ' }
                $ready = $true; break
            }
        } catch { }
        Start-Sleep -Milliseconds 300
    }
    if (-not $ready) { throw 'API readiness deadline exceeded' }
    $entries | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $statePath -Encoding UTF8
} catch {
    foreach ($entry in $entries) {
        $running = Get-Process -Id $entry.process_id -ErrorAction SilentlyContinue
        if ($running -and $running.StartTime.ToUniversalTime().Ticks -eq $entry.started_ticks -and $running.Path -eq $entry.executable) { & taskkill.exe /PID $entry.process_id /T /F | Out-Null }
    }
    throw
}
$consoleUrl = "http://127.0.0.1:$Port/"
Write-Output "Local console: $consoleUrl"
if (-not $NoConsole) {
    try { Start-Process $consoleUrl } catch { Write-Warning "Open the local console at $consoleUrl" }
}
} finally {
    if ($null -ne $operationLock) { $operationLock.Dispose() }
    Restore-PlatformSourceEnvironment -Selection $sourceSelection
}
