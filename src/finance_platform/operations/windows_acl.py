"""Wheel-safe Windows DACL helper; only target arrives via a child environment."""

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$target = $env:FINANCE_PLATFORM_ACL_TARGET
$isDirectory = [IO.Directory]::Exists($target)
$security = if ($isDirectory) { [IO.Directory]::GetAccessControl($target) } else { [IO.File]::GetAccessControl($target) }
$owner = $security.GetOwner([Security.Principal.SecurityIdentifier])
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User
$security.SetAccessRuleProtection($true, $false)
foreach ($oldRule in @($security.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) { $security.RemoveAccessRuleAll($oldRule) }
$allowed = @($owner.Value, $current.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique
foreach ($sidValue in $allowed) {
  $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
  $rule = if ($isDirectory) { [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow') }
          else { [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow') }
  $security.AddAccessRule($rule)
}
if ($isDirectory) { [IO.Directory]::SetAccessControl($target, $security) } else { [IO.File]::SetAccessControl($target, $security) }
$actual = if ($isDirectory) { [IO.Directory]::GetAccessControl($target) } else { [IO.File]::GetAccessControl($target) }
if (-not $actual.AreAccessRulesProtected -or $actual.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $owner.Value) { throw 'Private DACL verification failed' }
$sids = @($actual.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value })
if (@($sids | Where-Object { $_ -notin $allowed }).Count) { throw 'Unexpected private DACL principal' }
"""
