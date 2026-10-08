"""N4 commands exercise real frozen mail and state with an offline adapter boundary."""

import json
import os
import subprocess
import sys

import pytest
import sqlalchemy as sa
from test_mail_cli import AT, RECIPIENT, ROOT, SENDER, args, files, forbid_http, forbid_local_lock
from test_mail_cli import config as config
from test_mail_cli import pending as pending
from test_notification_service import activated as activated

from signalnest.cli import main
from signalnest.instance_lock import writer_lock
from signalnest.mail.contracts import SendErrorCode, SendOutcome, SendResult, SendStage
from signalnest.schema import mail_messages
from signalnest.storage import open_initialized_engine

SMTP = """
[smtp]
host = "smtp.example.org"
port = 587
security = "starttls"
timeout_seconds = 5.0
"""
COMMANDS = (
    "mail-drain",
    "mail-status",
    "mail-retry",
    "mail-pause",
    "mail-resume",
    "notifications-policy-update",
    "notifications-reevaluate",
)


@pytest.mark.parametrize("command", COMMANDS)
def test_new_command_help_has_no_storage_network_lock_or_clock(tmp_path, command):
    script = """
import logging, socket, sqlite3, time, sys
def forbidden(*args, **kwargs):
    raise AssertionError('help attempted a runtime operation')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden
sqlite3.dbapi2.connect = forbidden
time.time = forbidden
import signalnest.instance_lock
signalnest.instance_lock.writer_lock = forbidden
from signalnest.cli import main
raise SystemExit(main([sys.argv[1], '--help']))
"""
    before = files(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", script, command],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "--config" in result.stdout, result.stderr
    assert result.stderr == "" and files(tmp_path) == before


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        ("mail-drain", ("--max-messages", "0")),
        ("mail-drain", ("--max-messages", "21")),
        ("mail-drain", ("--run-seconds", "0")),
        ("mail-drain", ("--run-seconds", "3601")),
        ("mail-drain", ("--run-seconds", "nan")),
        ("mail-drain", ("--max-messages", "private-limit")),
        ("mail-retry", ("--mail-id", "0")),
        ("mail-retry", ("--mail-id", str(2**63))),
        ("mail-retry", ("--mail-id", "private-id")),
        ("mail-status", ("--at", "-1")),
        ("mail-pause", ("--at", "private-time")),
        ("mail-resume", ("--at", str(2**63 - 1))),
        ("notifications-reevaluate", ("--operation-id", "private-invalid/id")),
        ("notifications-reevaluate", ("--operation-id", "check", "--event-id", "1")),
        ("notifications-reevaluate", ("--operation-id", "check", "--at", str(AT))),
        ("notifications-reevaluate", ("--operation-id", "check", "--preview")),
        (
            "notifications-reevaluate",
            ("--operation-id", "check", "--event-id", "0", "--at", str(AT)),
        ),
    ],
)
def test_invalid_input_fails_before_storage_or_lock(config, monkeypatch, capsys, command, extra):
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)
    before = files(config.parent)
    assert main(args(config, command, *extra)) == 2
    output = capsys.readouterr()
    assert output.out == "" and "参数错误" in output.err
    assert "private-" not in output.err
    assert files(config.parent) == before


def test_drain_requires_explicit_smtp_before_storage_or_lock(config, monkeypatch, capsys):
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)
    before = files(config.parent)
    assert main(args(config, "mail-drain")) == 2
    output = capsys.readouterr()
    assert output.out == "" and "smtp" in output.err and "没有发送" in output.err
    assert files(config.parent) == before


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        ("mail-drain", ()),
        ("mail-status", ("--at", str(AT))),
        ("mail-retry", ("--mail-id", "1", "--at", str(AT))),
        ("mail-pause", ("--at", str(AT))),
        ("mail-resume", ("--at", str(AT))),
        ("notifications-reevaluate", ("--operation-id", "resume-original")),
    ],
)
def test_missing_storage_is_not_created(config, monkeypatch, capsys, command, extra):
    config.write_text(config.read_text() + SMTP)
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)
    before = files(config.parent)
    assert main(args(config, command, *extra)) == 1
    output = capsys.readouterr()
    assert output.out == "" and "database_unavailable" in output.err
    assert files(config.parent) == before


@pytest.fixture
def frozen(config, pending, capsys):
    assert main(args(config, "mail-plan", "--at", str(AT + 10))) == 0
    capsys.readouterr()
    config.write_text(config.read_text() + SMTP)
    engine = open_initialized_engine(pending.storage.database)
    try:
        with engine.connect() as connection:
            mail_id = connection.execute(sa.select(mail_messages.c.id)).scalar_one()
    finally:
        engine.dispose()
    return pending, mail_id


def test_status_is_read_only_and_never_invokes_smtp(config, frozen, monkeypatch, capsys):
    settings, _ = frozen
    forbid_http(monkeypatch)
    forbid_local_lock(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("mail-status must not call the SMTP adapter")

    monkeypatch.setattr("signalnest.mail.sending.send_frozen", forbidden)
    before = settings.storage.database.read_bytes()
    assert main(args(config, "mail-status", "--at", str(AT + 20))) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["counts"]["pending"] == 1 and not result["paused"]
    assert SENDER not in output.out + output.err and RECIPIENT not in output.out + output.err
    assert settings.storage.database.read_bytes() == before


def test_drain_invokes_adapter_once_and_never_resends_accepted(config, frozen, monkeypatch, capsys):
    settings, mail_id = frozen
    calls = []

    def accepted(message, smtp):
        calls.append((message.mail_id, message.rendered.payload_sha256, smtp.host))
        return SendResult(SendOutcome.ACCEPTED, SendStage.ACCEPTED, smtp_code=250)

    monkeypatch.setattr("signalnest.mail.sending.send_frozen", accepted)
    forbid_http(monkeypatch)
    assert main(args(config, "mail-drain", "--max-messages", "1")) == 0
    first = capsys.readouterr()
    result = json.loads(first.out)
    assert result["attempted"] == 1 and result["accepted"] == 1
    assert SENDER not in first.out + first.err and RECIPIENT not in first.out + first.err
    assert main(args(config, "mail-drain")) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["attempted"] == 0
    assert len(calls) == 1 and calls[0][0] == mail_id
    assert main(args(config, "mail-retry", "--mail-id", str(mail_id))) == 1
    assert "mail_already_accepted" in capsys.readouterr().err
    assert main(args(config, "mail-status")) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["accepted"] == 1


def test_uncertain_sending_has_attention_exit_and_safe_diagnostic(
    config, frozen, monkeypatch, capsys
):
    _, mail_id = frozen
    calls = []

    def unknown(message, _settings):
        calls.append(message.mail_id)
        return SendResult(
            SendOutcome.UNCERTAIN, SendStage.BODY_OR_FINAL, SendErrorCode.DISCONNECTED
        )

    monkeypatch.setattr("signalnest.mail.sending.send_frozen", unknown)
    assert main(args(config, "mail-drain")) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["uncertain"] == 1 and result["needs_attention"]
    assert SENDER not in output.out + output.err and RECIPIENT not in output.out + output.err
    assert main(args(config, "mail-status")) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["counts"]["uncertain"] == 1 and status["uncertain_count"] == 1
    assert calls == [mail_id]


def test_manual_retry_grants_one_attempt_without_resetting_count(
    config, frozen, monkeypatch, capsys
):
    _, mail_id = frozen
    calls = []

    def rejected(message, _settings):
        calls.append(message.mail_id)
        return SendResult(
            SendOutcome.PERMANENT,
            SendStage.RCPT,
            SendErrorCode.SERVER_REJECTED,
            smtp_code=550,
        )

    monkeypatch.setattr("signalnest.mail.sending.send_frozen", rejected)
    assert main(args(config, "mail-drain")) == 1
    assert json.loads(capsys.readouterr().out)["permanent"] == 1
    assert main(args(config, "mail-retry", "--mail-id", str(mail_id))) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["manual_retry_pending"] and not first["already_granted"]
    assert main(args(config, "mail-retry", "--mail-id", str(mail_id))) == 0
    assert json.loads(capsys.readouterr().out)["already_granted"]
    assert main(args(config, "mail-status")) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["messages"][0]["attempt_count"] == 1
    assert status["messages"][0]["manual_retry_pending"]

    def accepted(message, _settings):
        calls.append(message.mail_id)
        return SendResult(SendOutcome.ACCEPTED, SendStage.ACCEPTED, smtp_code=250)

    monkeypatch.setattr("signalnest.mail.sending.send_frozen", accepted)
    assert main(args(config, "mail-drain")) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] == 1
    assert main(args(config, "mail-status")) == 0
    final = json.loads(capsys.readouterr().out)
    assert final["messages"][0]["attempt_count"] == 2
    assert not final["messages"][0]["manual_retry_pending"]
    assert calls == [mail_id, mail_id]


def test_pause_resume_and_writer_lock_are_real(config, frozen, monkeypatch, capsys):
    settings, _ = frozen

    def forbidden(*_args, **_kwargs):
        raise AssertionError("paused channel must not call the adapter")

    monkeypatch.setattr("signalnest.mail.sending.send_frozen", forbidden)
    assert main(args(config, "mail-pause", "--at", str(AT + 20))) == 0
    assert json.loads(capsys.readouterr().out)["paused"]
    assert main(args(config, "mail-drain")) == 1
    assert json.loads(capsys.readouterr().out)["paused"]
    assert main(args(config, "mail-resume", "--at", str(AT + 21))) == 0
    assert not json.loads(capsys.readouterr().out)["paused"]
    with writer_lock(settings.storage.database):
        assert main(args(config, "mail-pause")) == 1
    assert "writer_lock_busy" in capsys.readouterr().err


def test_drain_database_failure_is_safe_nonzero(config, frozen, monkeypatch, capsys):
    from sqlalchemy.exc import SQLAlchemyError

    def broken(*_args, **_kwargs):
        raise SQLAlchemyError("private-smtp-password private-mail-body")

    monkeypatch.setattr("signalnest.mail.sending.drain_mail", broken)
    assert main(args(config, "mail-drain")) == 1
    output = capsys.readouterr()
    assert output.out == "" and "database_unavailable" in output.err
    assert "private-" not in output.err


def test_policy_update_preview_and_replay_do_not_rewrite_frozen_mail(
    config, frozen, monkeypatch, capsys
):
    settings, mail_id = frozen
    profile = config.parent / "personal.toml"
    profile.write_bytes((ROOT / "profile.example.toml").read_bytes())
    command = args(
        config,
        "notifications-policy-update",
        "--operation-id",
        "policy-cli-test",
        "--profile",
        str(profile),
        "--at",
        str(AT + 20),
    )
    forbid_http(monkeypatch)
    before = settings.storage.database.read_bytes()
    with monkeypatch.context() as readonly:
        forbid_local_lock(readonly)
        assert main([*command, "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["changed"] is True and not preview["creates_historical_mail"]
    assert settings.storage.database.read_bytes() == before
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["complete"] is True and not first["reused"]
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["reused"] is True
    engine = open_initialized_engine(settings.storage.database)
    try:
        with engine.connect() as connection:
            assert connection.execute(sa.select(mail_messages.c.id)).scalar_one() == mail_id
    finally:
        engine.dispose()


def test_bounded_reevaluation_preview_and_clockless_resume(config, activated, monkeypatch, capsys):
    from test_notification_maintenance import none_event

    config.write_text(
        config.read_text()
        .replace('data_dir = "data"', f'data_dir = "{activated.settings.data_dir}"')
        .replace(
            'database = "data/signalnest.sqlite3"',
            f'database = "{activated.settings.database}"',
        )
    )
    event_id = none_event(activated)
    command = args(
        config,
        "notifications-reevaluate",
        "--operation-id",
        "reevaluate-cli-test",
        "--event-id",
        str(event_id),
        "--at",
        str(AT + 30),
    )
    forbid_http(monkeypatch)
    before = activated.settings.database.read_bytes()
    with monkeypatch.context() as readonly:
        forbid_local_lock(readonly)
        assert main([*command, "--preview"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["preview"] and preview["results"][0]["event_id"] == event_id
    assert activated.settings.database.read_bytes() == before
    assert main(command) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial["complete"] and initial["evaluated_at"] == AT + 30

    def forbidden_clock():
        raise AssertionError("resume must not use a new evaluation time")

    # Logging samples wall time itself; only the CLI's explicit operation clock is guarded.
    monkeypatch.setattr("signalnest.cli.time", type("Clock", (), {"time": forbidden_clock}))
    assert (
        main(
            args(
                config,
                "notifications-reevaluate",
                "--operation-id",
                "reevaluate-cli-test",
            )
        )
        == 0
    )
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["reused"] and resumed["results"] == initial["results"]
    assert resumed["evaluated_at"] == initial["evaluated_at"]
