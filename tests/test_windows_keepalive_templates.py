"""Static safety checks for Windows templates; these are not Windows execution tests."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "deploy/windows"


def source(name: str) -> str:
    return (WINDOWS / name).read_text(encoding="ascii")


@pytest.mark.parametrize("name", ["repair-keepalive.ps1", "wsl-keepalive.ps1"])
def test_scripts_do_not_load_application_secrets_or_change_host_policy(name):
    text = source(name)
    commands = re.sub(r"(?s)<#.*?#>", "", text)
    commands = re.sub(r"(?m)^\s*#.*$", "", commands)
    for forbidden in (
        "smtp.env",
        "Set-ExecutionPolicy",
        "powercfg",
        "wsl --shutdown",
        "Invoke-Expression",
        "-Password",
    ):
        assert forbidden not in commands


def test_update_preserves_running_task_and_has_no_implicit_restart():
    text = source("repair-keepalive.ps1")
    commands = re.sub(r"(?s)<#.*?#>", "", text)
    lifecycle_commands = re.findall(
        r"(?m)^\s*((?:Start|Stop|Disable|Enable|Unregister|Register)-ScheduledTask)\b",
        commands,
    )
    assert lifecycle_commands == []
    assert "principal_changed = $false" in text
    mutation = re.search(r"(?m)^Set-ScheduledTask .*", text)
    assert mutation is not None
    assert "-Principal" not in mutation.group()
    assert text.index("keepalive_wrong_owning_account") < mutation.start()
    assert text.index("keepalive_action_changed_since_review") < mutation.start()


def test_inspection_and_whatif_stop_before_mutations():
    text = source("repair-keepalive.ps1")
    inspect = text.index("if ($InspectOnly)")
    approval = text.index("$PSCmdlet.ShouldProcess($TaskName")
    backup = text.index("Export-ScheduledTask -TaskName")
    publication = text.index("Copy-Item -LiteralPath $sourceRunner")
    mutation = text.index("Set-ScheduledTask -TaskName")
    assert inspect < approval < backup < publication < mutation
    assert "outcome = 'inspected'" in text
    assert "$plan.outcome = 'not_applied'" in text
    assert "keepalive_reviewed_action_fingerprint_required" in text


def test_backup_precedes_definition_change_and_is_private():
    text = source("repair-keepalive.ps1")
    mutation = text.index("Set-ScheduledTask -TaskName")
    for filename in (
        "task-before.xml",
        "settings-before.clixml",
        "actions-before.clixml",
        "legacy-output.log",
        "runner-before.ps1",
    ):
        assert text.index(filename) < mutation
    assert "$acl.SetOwner($ownerSid)" in text
    assert "$acl.SetAccessRuleProtection($true, $false)" in text
    assert "[IO.File]::Replace($stagedRunner, $runnerPath, $null)" in text


def test_task_script_policy_is_checked_without_changing_or_bypassing_it():
    text = source("repair-keepalive.ps1")
    policy_guard = text.index("throw 'keepalive_script_policy_blocked'")
    assert policy_guard < text.index("Export-ScheduledTask -TaskName")
    assert "Get-ExecutionPolicy -List" in text
    assert "Get-AuthenticodeSignature -LiteralPath $sourceRunner" in text
    assert "'Restricted' { $false }" in text
    assert "'AllSigned' { $runnerSignature -eq 'Valid' }" in text
    # A process policy used for the installer cannot qualify a future task.
    assert "@('MachinePolicy', 'UserPolicy', 'CurrentUser', 'LocalMachine')" in text
    assert re.search(r"(?<![A-Za-z])-ExecutionPolicy\b", text) is None


def test_managed_repeat_replaces_existing_one_and_has_no_duration():
    text = source("repair-keepalive.ps1")
    repeat = re.search(r"(?m)^\$repeat = New-ScheduledTaskTrigger .*", text)
    assert repeat is not None
    assert "-Once" in repeat.group()
    assert "-Minutes 5" in repeat.group()
    assert "-RepetitionDuration" not in repeat.group()
    mutation = re.search(r"(?m)^Set-ScheduledTask .*", text)
    assert mutation is not None
    assert "-Trigger ($login + @($repeat))" in mutation.group()
    assert "$task.Triggers +" not in mutation.group()
    assert "keepalive_repeat_trigger_not_supported" in text
    assert "$settings.DisallowStartIfOnBatteries = $false" in text
    assert "$settings.StopIfGoingOnBatteries = $false" in text


def test_launcher_bounds_metadata_logs_and_returns_native_exit_code():
    text = source("wsl-keepalive.ps1")
    assert "$maximumLogBytes = 1MB" in text
    assert "$archiveCount = 2" in text
    assert "[IO.File]::AppendAllText($logPath, $line, $utf8)" in text
    assert "at_utc = [DateTime]::UtcNow.ToString('o')" in text
    assert "event = 'start'" in text
    assert "event = 'exit'" in text
    assert "$exitCode = [int]$LASTEXITCODE" in text
    assert text.rstrip().endswith("exit $exitCode")
    assert "--exec /usr/bin/sleep infinity 1> $null 2> $null" in text
    # Test-only executables cannot be selected by the installed task arguments.
    updater = source("repair-keepalive.ps1")
    installed_arguments = re.search(r"(?m)^\$newArguments = .*\n.*", updater)
    assert installed_arguments is not None
    assert "WslExecutable" not in installed_arguments.group()
