#requires -Version 5.1
<#
.SYNOPSIS
Keeps the checked WSL distribution alive and records bounded launch/exit facts.
.DESCRIPTION
Installed by repair-keepalive.ps1 in the owning user's private runtime directory.
This script does not collect notices or load application/SMTP credentials.
A forced process termination may leave a start record without an exit record.
#>
[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$Distribution = 'Ubuntu-24.04',
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')]
    [string]$LinuxUser = 'signalnest',
    # Isolated Windows tests may supply a local fake executable. The installed
    # task never passes an override and always uses the checked WSL executable.
    [ValidatePattern('^[A-Za-z]:\\[^\r\n]+\.exe$')]
    [string]$WslExecutable = 'C:\Program Files\WSL\wsl.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$wslPath = $WslExecutable
$logPath = Join-Path $PSScriptRoot 'wsl-keepalive.jsonl'
$maximumLogBytes = 1MB
$archiveCount = 2
$runId = [Guid]::NewGuid().ToString('N')
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Assert-RegularPath([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $attributes = [IO.File]::GetAttributes($Path)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'keepalive_reparse_path'
        }
    }
}

function Write-KeepaliveEvent([hashtable]$Fields) {
    Assert-RegularPath $PSScriptRoot
    Assert-RegularPath $logPath
    for ($index = 1; $index -le $archiveCount; $index++) {
        Assert-RegularPath ($logPath + '.' + $index)
    }
    if ((Test-Path -LiteralPath $logPath) -and
        (Get-Item -LiteralPath $logPath).Length -ge $maximumLogBytes) {
        $lastArchive = $logPath + '.' + $archiveCount
        if (Test-Path -LiteralPath $lastArchive) {
            Remove-Item -LiteralPath $lastArchive -Force
        }
        for ($index = $archiveCount - 1; $index -ge 1; $index--) {
            $from = $logPath + '.' + $index
            if (Test-Path -LiteralPath $from) {
                Move-Item -LiteralPath $from -Destination ($logPath + '.' + ($index + 1))
            }
        }
        Move-Item -LiteralPath $logPath -Destination ($logPath + '.1')
    }
    $record = [ordered]@{
        format = 'signalnest-wsl-keepalive-v1'
        at_utc = [DateTime]::UtcNow.ToString('o')
        run_id = $runId
        process_id = $PID
        distribution = $Distribution
    }
    foreach ($key in $Fields.Keys) { $record[$key] = $Fields[$key] }
    $line = ($record | ConvertTo-Json -Depth 3 -Compress) + [Environment]::NewLine
    [IO.File]::AppendAllText($logPath, $line, $utf8)
}

$exitCode = 70
$outcome = 'launcher_failure'
try {
    if (-not (Test-Path -LiteralPath $wslPath -PathType Leaf)) {
        throw 'keepalive_wsl_missing'
    }
    Write-KeepaliveEvent @{ event = 'start' }
} catch {
    [Console]::Error.WriteLine('keepalive_start_failed')
    exit 74
}

try {
    # sleep has no useful stdout. Only finite exit metadata is retained; the
    # previous launcher's output file is backed up separately by the installer.
    $ErrorActionPreference = 'Continue'
    & $wslPath --distribution $Distribution --user $LinuxUser --exec /usr/bin/sleep infinity 1> $null 2> $null
    $exitCode = [int]$LASTEXITCODE
    $outcome = if ($exitCode -eq 0) { 'normal_exit' } else { 'wsl_nonzero_exit' }
} catch {
    $exitCode = 70
    $outcome = 'launcher_failure'
} finally {
    $ErrorActionPreference = 'Stop'
    try {
        $unsignedCode = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$exitCode), 0)
        Write-KeepaliveEvent @{
            event = 'exit'
            outcome = $outcome
            exit_code = $exitCode
            exit_code_hex = ('0x' + $unsignedCode.ToString('X8'))
        }
    } catch {
        [Console]::Error.WriteLine('keepalive_exit_log_failed')
        $exitCode = 74
    }
}
exit $exitCode
