param(
    [Parameter(Mandatory=$true)][ValidateSet('inspect','start','init','stop','prepare-mfa-key')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Root,
    [ValidateRange(1024,65535)][int]$Port = 8000,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$RequestId
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$lease = $null
$held = $false
try {
    $rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    $digest = [Security.Cryptography.SHA256]::Create()
    try { $key = -join ($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($rootPath.ToUpperInvariant())) | ForEach-Object { $_.ToString('x2') }) }
    finally { $digest.Dispose() }
    $lease = [Threading.Mutex]::new($false, ('Global\FinanceAnalyze.Operation.' + $key))
    try { $held = $lease.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { throw 'OPERATION_BUSY' }
    if ($Action -eq 'prepare-mfa-key' -and (Test-Path -LiteralPath (Join-Path $rootPath '.local\platform\processes\native.json'))) {
        throw 'MFA_STOP_REQUIRED'
    }
    $python = Join-Path $rootPath '.venv\Scripts\python.exe'
    $binding = $null
    if ($Action -ne 'stop') {
        . (Join-Path $rootPath 'tools\platform_ops\source-install.ps1')
        $selection = Set-PlatformSourceRoot -RootPath $rootPath
        try {
            $raw = @(& $python -P -m finance_platform.operations.source_binding --root $rootPath 2>$null)
            if ($LASTEXITCODE -ne 0) { throw 'SOURCE_CHECK_FAILED' }
            $source = ($raw -join "`n") | ConvertFrom-Json
            $binding = @{ root=$source.root; root_hash=$source.root_hash; source_hash=$source.source_hash; lock_sha256=$source.lock_sha256 }
        } finally { Restore-PlatformSourceEnvironment -Selection $selection }
    }
    $captured = @()
    if ($Action -eq 'start') {
        # Intentionally memory-only. Never transcript/redirect initialization stdout to a file.
        $null = @(& (Join-Path $rootPath 'tools\platform_ops\start.ps1') -Root $rootPath -Python $python -SkipSync -NoConsole -Port $Port 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'START_FAILED' }
    } elseif ($Action -in @('init','prepare-mfa-key')) {
        $selection = Set-PlatformSourceRoot -RootPath $rootPath
        try {
            $operatorArgs = @('-P','-m','finance_platform.operations.launch',$Action,'--root',$rootPath,'--port',$Port)
            if ($Action -eq 'init') { $operatorArgs += '--require-bound-api' }
            $captured = @(& $python @operatorArgs 2>$null)
            if ($LASTEXITCODE -ne 0) {
                $failure = $null
                if ($captured.Count -eq 1) {
                    try { $failure = [string]$captured[0] | ConvertFrom-Json -ErrorAction Stop } catch { }
                }
                if ($Action -eq 'init') {
                    if ($failure.code -ceq 'ACCOUNT_DB_CHANGED') { throw 'ACCOUNT_DB_CHANGED' }
                    throw 'INIT_FAILED'
                }
                if ($failure.code -ceq 'MFA_STOP_REQUIRED') { throw 'MFA_STOP_REQUIRED' }
                throw 'MFA_PREPARE_FAILED'
            }
        } finally { Restore-PlatformSourceEnvironment -Selection $selection }
    } elseif ($Action -eq 'stop') {
        $captured = @(& (Join-Path $rootPath 'tools\platform_ops\stop.ps1') -Root $rootPath 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'STOP_FAILED' }
    }
    $answer = @{ protocol='FinanceAnalyzeLauncher/1'; request_id=$RequestId; kind='ok'; action=$Action; binding=$binding }
    if ($Action -eq 'init') {
        $found = $false
        foreach ($line in $captured) {
            try { $item = [string]$line | ConvertFrom-Json -ErrorAction Stop } catch { continue }
            if ($item.PSObject.Properties.Name -contains 'setup_token') {
                if ($item.setup_token -notmatch '^[A-Za-z0-9_-]{32,128}$') { throw 'INIT_FAILED' }
                $answer.setup_token = $item.setup_token
                if ($item.setup_kind -notin @('first_admin','upgrade_personal') -or $item.expires_in_seconds -ne 1800) { throw 'INIT_FAILED' }
                $answer.setup_kind = $item.setup_kind
                $answer.expires_in_seconds = $item.expires_in_seconds
                $found = $true
            } elseif (($item.PSObject.Properties.Name -contains 'initialized') -and $item.initialized -eq $true) {
                $answer.initialized = $true
                $found = $true
            }
        }
        if (-not $found) { throw 'INIT_FAILED' }
    } elseif ($Action -eq 'prepare-mfa-key') {
        if ($captured.Count -ne 1) { throw 'MFA_PREPARE_FAILED' }
        try { $item = [string]$captured[0] | ConvertFrom-Json -ErrorAction Stop } catch { throw 'MFA_PREPARE_FAILED' }
        if ($item.mfa_key -cnotin @('prepared','already_configured')) { throw 'MFA_PREPARE_FAILED' }
        $answer.mfa_key = $item.mfa_key
    }
    $captured = $null
    $answer | ConvertTo-Json -Depth 5 -Compress
    exit 0
} catch {
    # No raw exception/stdout/stderr: these can contain an initialization token.
    $code = 'BRIDGE_FAILED'
    if ($_.Exception.Message -eq 'Dependency doctor failed') { $code = 'DEPENDENCY_CHECK_FAILED' }
    elseif ($_.Exception.Message -in @('OPERATION_BUSY','SOURCE_CHECK_FAILED','START_FAILED','INIT_FAILED','STOP_FAILED','MFA_STOP_REQUIRED','MFA_PREPARE_FAILED','ACCOUNT_DB_CHANGED')) { $code = $_.Exception.Message }
    @{protocol='FinanceAnalyzeLauncher/1';request_id=$RequestId;action=$Action;kind='error';code=$code} | ConvertTo-Json -Compress
    exit 2
} finally {
    if ($held) { $lease.ReleaseMutex() }
    if ($lease) { $lease.Dispose() }
}
