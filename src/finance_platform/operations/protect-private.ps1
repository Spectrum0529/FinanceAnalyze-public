param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference = 'Stop'
$target = (Resolve-Path -LiteralPath $Path).Path
$isDirectory = [IO.Directory]::Exists($target)
$existing = if ($isDirectory) { [IO.Directory]::GetAccessControl($target) } else { [IO.File]::GetAccessControl($target) }
$ownerSid = $existing.GetOwner([Security.Principal.SecurityIdentifier])
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$security = $existing
$security.SetAccessRuleProtection($true, $false)
foreach ($existingRule in @($security.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
    $security.RemoveAccessRuleAll($existingRule)
}
$allowed = @($ownerSid.Value, $currentSid.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique
foreach ($sidValue in $allowed) {
    $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
    $rule = if ($isDirectory) {
        [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    } else { [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow') }
    $security.AddAccessRule($rule)
}
if ($isDirectory) { [IO.Directory]::SetAccessControl($target, $security) } else { [IO.File]::SetAccessControl($target, $security) }
$actual = if ($isDirectory) { [IO.Directory]::GetAccessControl($target) } else { [IO.File]::GetAccessControl($target) }
if (-not $actual.AreAccessRulesProtected) { throw 'Private DACL protection failed' }
$sids = @($actual.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value })
if (@($sids | Where-Object { $_ -notin $allowed }).Count) { throw 'Private DACL verification failed' }
