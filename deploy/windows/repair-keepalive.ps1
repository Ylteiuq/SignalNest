#requires -Version 5.1
<#
.SYNOPSIS
Reviews or updates the existing SignalNest WSL keepalive task, without restarting it.
.DESCRIPTION
Run as the Windows account that owns the registered WSL distribution and task.
First use -InspectOnly. Apply requires the exact reviewed action fingerprint.
-WhatIf does not create directories, backups, scripts, triggers, or log settings.
The task principal is preserved. Updating its action does not restart a running
instance; the new launcher logs on its next start. A coordinated stop/start is a
separate maintenance action, and this script never stops or starts the task.
To roll back, disable the task before stopping it, restore task-before.xml with
Register-ScheduledTask -Xml (Get-Content -Raw <backup>) under the same account,
restore any backed-up runner, then enable/start it when maintenance is complete.
Do not print the saved task XML into shared application logs.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$TaskName = 'SignalNest-WSL-KeepAlive',
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$Distribution = 'Ubuntu-24.04',
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')]
    [string]$LinuxUser = 'signalnest',
    [ValidatePattern('^[a-f0-9]{64}$')]
    [string]$ExpectedActionSha256,
    [switch]$InspectOnly,
    [switch]$EnableTaskHistory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$wslPath = 'C:\Program Files\WSL\wsl.exe'
$powershellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$baseDirectory = Join-Path $env:LOCALAPPDATA 'SignalNest'
$runtimeDirectory = Join-Path $baseDirectory 'keepalive'
$runnerPath = Join-Path $runtimeDirectory 'wsl-keepalive.ps1'
$sourceRunner = Join-Path $PSScriptRoot 'wsl-keepalive.ps1'
$oldOutputPath = Join-Path $baseDirectory 'wsl-keepalive.log'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$ownerSid = $identity.User

function Assert-RegularPath([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'keepalive_reparse_path'
        }
    }
}

function Get-ActionFingerprint($Action) {
    # All three public action fields are bound, including exact URI-like argv.
    $serialized = [ordered]@{
        execute = [string]$Action.Execute
        arguments = [string]$Action.Arguments
        working_directory = [string]$Action.WorkingDirectory
    } | ConvertTo-Json -Compress
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($serialized)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $algorithm.Dispose() }
}

function New-PrivateDirectory([string]$Path) {
    Assert-RegularPath $Path
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner($ownerSid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($ownerSid.Value, 'S-1-5-18', 'S-1-5-32-544')) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            ([Security.Principal.SecurityIdentifier]$sid),
            [Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
             [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Test-ReviewedAction($Action) {
    if (-not ([string]$Action.Execute).Equals($powershellPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $arguments = [string]$Action.Arguments
    $prefix = '-NoLogo -NoProfile -NonInteractive '
    $newArguments = $prefix + '-File "' + $runnerPath + '" -Distribution "' + $Distribution + '" -LinuxUser "' + $LinuxUser + '"'
    if ($arguments -eq $newArguments) { return $true }
    if ($arguments -notmatch '^\-NoLogo \-NoProfile \-NonInteractive \-EncodedCommand ([A-Za-z0-9+/=]+)$') {
        return $false
    }
    try {
        $decoded = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]))
    } catch { return $false }
    # Accept only the deployed original launcher, not arbitrary encoded scripts.
    # This comparison is reviewable and does not execute decoded content.
    $legacy = @'
$ErrorActionPreference='Stop'
$log=Join-Path $env:LOCALAPPDATA 'SignalNest\wsl-keepalive.log'
& 'C:\Program Files\WSL\wsl.exe' --distribution {0} --user {1} --exec /usr/bin/sleep infinity *> $log
exit $LASTEXITCODE
'@ -f $Distribution, $LinuxUser
    return $decoded.Replace("`r`n", "`n").Trim() -ceq $legacy.Replace("`r`n", "`n").Trim()
}

foreach ($command in @('Get-ScheduledTask', 'Set-ScheduledTask', 'Export-ScheduledTask',
    'New-ScheduledTaskAction', 'New-ScheduledTaskTrigger', 'Get-WinEvent',
    'Get-ExecutionPolicy', 'Get-AuthenticodeSignature')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw 'keepalive_required_cmdlet_missing'
    }
}
foreach ($path in @($baseDirectory, $runtimeDirectory, $runnerPath, $sourceRunner, $oldOutputPath)) {
    Assert-RegularPath $path
}
if (-not (Test-Path -LiteralPath $sourceRunner -PathType Leaf)) { throw 'keepalive_runner_missing' }
if (-not (Test-Path -LiteralPath $wslPath -PathType Leaf)) { throw 'keepalive_wsl_missing' }
$registered = @(& $wslPath --list --quiet)
if ($LASTEXITCODE -ne 0) { throw 'keepalive_distribution_inventory_failed' }
$registered = @(($registered -join "`n").Replace([string][char]0, '').Split("`n") | ForEach-Object { $_.Trim() })
if ($registered -cnotcontains $Distribution) { throw 'keepalive_distribution_not_registered' }

$task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\'
if (@($task.Actions).Count -ne 1) { throw 'keepalive_action_count_invalid' }
try {
    $principal = New-Object Security.Principal.NTAccount([string]$task.Principal.UserId)
    $principalSid = $principal.Translate([Security.Principal.SecurityIdentifier])
} catch {
    try { $principalSid = [Security.Principal.SecurityIdentifier]$task.Principal.UserId }
    catch { throw 'keepalive_principal_unresolved' }
}
if ($principalSid.Value -ne $ownerSid.Value) { throw 'keepalive_wrong_owning_account' }
if ([string]$task.Principal.LogonType -ne 'Interactive' -or
    [string]$task.Principal.RunLevel -ne 'Limited') { throw 'keepalive_principal_not_supported' }
$login = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
$other = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' })
if ($login.Count -ne 1 -or $other.Count -gt 1) { throw 'keepalive_triggers_not_supported' }
foreach ($trigger in $other) {
    if ($trigger.CimClass.CimClassName -ne 'MSFT_TaskTimeTrigger' -or
        [string]$trigger.Repetition.Interval -ne 'PT5M' -or
        -not [string]::IsNullOrEmpty([string]$trigger.Repetition.Duration)) {
        throw 'keepalive_repeat_trigger_not_supported'
    }
}
$settings = $task.Settings
if ([string]$settings.ExecutionTimeLimit -ne 'PT0S' -or
    [string]$settings.MultipleInstances -ne 'IgnoreNew' -or
    $settings.RestartCount -ne 3 -or [string]$settings.RestartInterval -ne 'PT1M') {
    throw 'keepalive_settings_not_supported'
}
if (-not (Test-ReviewedAction $task.Actions[0])) { throw 'keepalive_action_not_supported' }
$actionSha256 = Get-ActionFingerprint $task.Actions[0]
if ($ExpectedActionSha256 -and $ExpectedActionSha256 -cne $actionSha256) {
    throw 'keepalive_action_changed_since_review'
}
$windowsPrincipal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $windowsPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$executionPolicies = @(Get-ExecutionPolicy -List)
$shellExecutionPolicy = [string](Get-ExecutionPolicy)
# A temporary policy of the installer process is not evidence about the future
# scheduled PowerShell process. The installed action has no policy override.
$taskExecutionPolicy = 'Restricted'
foreach ($scope in @('MachinePolicy', 'UserPolicy', 'CurrentUser', 'LocalMachine')) {
    $selected = @($executionPolicies | Where-Object { [string]$_.Scope -eq $scope })
    if ($selected.Count -eq 1 -and [string]$selected[0].ExecutionPolicy -ne 'Undefined') {
        $taskExecutionPolicy = [string]$selected[0].ExecutionPolicy
        break
    }
}
$runnerSignature = [string](Get-AuthenticodeSignature -LiteralPath $sourceRunner).Status
$internetMarked = $false
try {
    $zone = Get-Content -LiteralPath $sourceRunner -Stream Zone.Identifier -ErrorAction Stop
    foreach ($line in $zone) {
        if ($line -match '^ZoneId=([0-9]+)$' -and [int]$Matches[1] -ge 3) {
            $internetMarked = $true
        }
    }
} catch {
    # A newly staged local script normally has no Zone.Identifier stream.
}
$scriptPolicyAllowed = switch ($taskExecutionPolicy) {
    'Restricted' { $false }
    'AllSigned' { $runnerSignature -eq 'Valid' }
    'RemoteSigned' { -not $internetMarked -or $runnerSignature -eq 'Valid' }
    'Unrestricted' { -not $internetMarked -or $runnerSignature -eq 'Valid' }
    'Bypass' { $true }
    default { $false }
}
$historyEnabled = $null
try {
    $historyEnabled = [bool](Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational').IsEnabled
} catch {
    # Task history is a separate diagnostic improvement. An unavailable log
    # must not hide the task repair's outcome or prevent its backup.
}
$plan = [ordered]@{
    format = 'signalnest-keepalive-repair-v1'
    at_utc = [DateTime]::UtcNow.ToString('o')
    task_name = $TaskName
    task_state = [string]$task.State
    distribution = $Distribution
    owning_account_matches = $true
    action_sha256 = $actionSha256
    repeat_minutes = 5
    repetition_duration = $null
    battery_start_allowed = $true
    battery_stop_disabled = $true
    principal_changed = $false
    running_task_restarted = $false
    logging_requires_next_launch = ([string]$task.State -eq 'Running')
    task_history_requested = [bool]$EnableTaskHistory
    execution_policy_scopes = @($executionPolicies | ForEach-Object {
        [ordered]@{ scope = [string]$_.Scope; policy = [string]$_.ExecutionPolicy }
    })
    shell_effective_execution_policy = $shellExecutionPolicy
    scheduled_script_execution_policy = $taskExecutionPolicy
    runner_signature_status = $runnerSignature
    runner_internet_marked = $internetMarked
    scheduled_script_policy_allowed = [bool]$scriptPolicyAllowed
    task_history_outcome = if ($historyEnabled -eq $true) { 'already_enabled' }
        elseif ($null -eq $historyEnabled) { 'status_unavailable' } else { 'not_requested' }
    outcome = 'inspected'
    backup_directory = $null
}
if ($EnableTaskHistory -and $historyEnabled -ne $true -and -not $isAdmin) {
    $plan.task_history_outcome = 'administrator_required'
}
if ($InspectOnly) {
    $plan | ConvertTo-Json -Depth 4 -Compress
    exit 0
}
if (-not $ExpectedActionSha256) { throw 'keepalive_reviewed_action_fingerprint_required' }
if (-not $PSCmdlet.ShouldProcess($TaskName, 'Back up and update existing keepalive action, triggers and battery conditions')) {
    $plan.outcome = 'not_applied'
    $plan | ConvertTo-Json -Depth 4 -Compress
    exit 0
}
if (-not $scriptPolicyAllowed) { throw 'keepalive_script_policy_blocked' }

# Backups must exist before the only task mutation. No SMTP file is accessed.
if (-not (Test-Path -LiteralPath $baseDirectory)) {
    New-Item -ItemType Directory -Path $baseDirectory | Out-Null
}
$backupRoot = Join-Path $baseDirectory 'keepalive-backups'
New-PrivateDirectory $backupRoot
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N')
$backupDirectory = Join-Path $backupRoot $stamp
New-PrivateDirectory $backupDirectory
Export-ScheduledTask -TaskName $TaskName -TaskPath '\' |
    Set-Content -LiteralPath (Join-Path $backupDirectory 'task-before.xml') -Encoding Unicode
$task.Settings | Export-Clixml -LiteralPath (Join-Path $backupDirectory 'settings-before.clixml')
$task.Actions | Export-Clixml -LiteralPath (Join-Path $backupDirectory 'actions-before.clixml')
if (Test-Path -LiteralPath $oldOutputPath -PathType Leaf) {
    Copy-Item -LiteralPath $oldOutputPath -Destination (Join-Path $backupDirectory 'legacy-output.log')
}
if (Test-Path -LiteralPath $runnerPath -PathType Leaf) {
    Copy-Item -LiteralPath $runnerPath -Destination (Join-Path $backupDirectory 'runner-before.ps1')
}
New-PrivateDirectory $runtimeDirectory
$stagedRunner = Join-Path $runtimeDirectory ([Guid]::NewGuid().ToString('N') + '.ps1')
try {
    Copy-Item -LiteralPath $sourceRunner -Destination $stagedRunner
    if (Test-Path -LiteralPath $runnerPath) {
        [IO.File]::Replace($stagedRunner, $runnerPath, $null)
    } else {
        [IO.File]::Move($stagedRunner, $runnerPath)
    }
} finally {
    if (Test-Path -LiteralPath $stagedRunner) { Remove-Item -LiteralPath $stagedRunner }
}
$newArguments = '-NoLogo -NoProfile -NonInteractive -File "' + $runnerPath +
    '" -Distribution "' + $Distribution + '" -LinuxUser "' + $LinuxUser + '"'
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument $newArguments -WorkingDirectory $runtimeDirectory
# Do not pass RepetitionDuration: this is an indefinite repetition. Replace the
# previously managed time trigger instead of accumulating one on each upgrade.
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings.DisallowStartIfOnBatteries = $false
$settings.StopIfGoingOnBatteries = $false
Set-ScheduledTask -TaskName $TaskName -TaskPath $task.TaskPath -Settings $settings -Action $action -Trigger ($login + @($repeat)) | Out-Null
$plan.outcome = 'applied'
$plan.backup_directory = $backupDirectory
if ($EnableTaskHistory -and $historyEnabled -ne $true -and $isAdmin) {
    if ($PSCmdlet.ShouldProcess('Microsoft-Windows-TaskScheduler/Operational', 'Enable task history')) {
        try {
            $ErrorActionPreference = 'Continue'
            & (Join-Path $env:WINDIR 'System32\wevtutil.exe') sl Microsoft-Windows-TaskScheduler/Operational /e:true 1> $null 2> $null
            $historyExitCode = $LASTEXITCODE
            $ErrorActionPreference = 'Stop'
            $enabledAfter = [bool](Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational').IsEnabled
            $plan.task_history_outcome = if ($historyExitCode -eq 0 -and $enabledAfter) { 'enabled' } else { 'enable_failed' }
        } catch { $plan.task_history_outcome = 'enable_failed' }
        finally { $ErrorActionPreference = 'Stop' }
    } else { $plan.task_history_outcome = 'not_applied' }
}
$plan | ConvertTo-Json -Depth 4 -Compress |
    Set-Content -LiteralPath (Join-Path $backupDirectory 'repair-result.json') -Encoding UTF8
Export-ScheduledTask -TaskName $TaskName -TaskPath '\' |
    Set-Content -LiteralPath (Join-Path $backupDirectory 'task-after.xml') -Encoding Unicode
$plan | ConvertTo-Json -Depth 4 -Compress
