"""Configured timer passes integrate actual archive/Parser/N1/N2/SQLite, all offline."""

import json
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from test_crawling import Clock
from test_crawling import settings as crawl_settings
from test_mail_planning import plan, seal_copies
from test_notification_service import ACTIVATED_AT, SOURCE, live, opportunity, rows
from test_notification_service import activated as activated

from signalnest.cli import main
from signalnest.config import ConfigurationError, MailRuntimeSettings, SmtpSettings, load_config
from signalnest.errors import IngestError
from signalnest.instance_lock import WriterLockError, writer_lock
from signalnest.mail.background import run_mail_pass, run_scheduled_cycle
from signalnest.mail.contracts import MailError, SendOutcome, SendResult, SendStage
from signalnest.schema import mail_attempts, mail_delivery, mail_message_members, mail_messages

ACCEPTED = SendResult(SendOutcome.ACCEPTED, SendStage.ACCEPTED, smtp_code=250)
ROOT = Path(__file__).resolve().parents[1]


def enabled(env, **limits):
    settings = crawl_settings(env)
    return settings.model_copy(
        update={
            "smtp": SmtpSettings(host="smtp.example.org", port=587),
            "mail_runtime": MailRuntimeSettings(enabled=True, **limits),
        }
    )


def once(env, *, settings=None, at=ACTIVATED_AT + 20, sender=None):
    return run_mail_pass(
        settings or enabled(env), now=lambda: at, sender=sender or (lambda *args: ACCEPTED)
    )


@pytest.fixture
def pending(activated):
    live(activated, opportunity())
    plan(activated, at=ACTIVATED_AT + 10)
    return activated


def test_pass_automatically_plans_then_sends_and_does_not_repeat(activated):
    env = activated
    live(env, opportunity())
    assert rows(env, mail_messages) == []
    first = once(env)
    assert first["planning"]["planned"] == first["sending"]["accepted"] == 1
    assert first["needs_attention"] is False
    frozen, members = rows(env, mail_messages), rows(env, mail_message_members)
    assert once(env, at=ACTIVATED_AT + 21)["sending"]["attempted"] == 0
    assert rows(env, mail_messages) == frozen and rows(env, mail_message_members) == members


def test_backlog_advances_in_bounded_passes_without_manual_commands(activated):
    env = activated
    live(env, opportunity())
    seal_copies(env, 13)
    counts = []
    for offset in range(4):
        result = once(env, at=ACTIVATED_AT + 20 + offset)
        counts.append((result["planning"]["planned"], result["sending"]["accepted"]))
    assert counts == [(5, 5), (5, 5), (3, 3), (0, 0)]
    assert all(r["state"] == "accepted" for r in rows(env, mail_delivery))
    assert len(rows(env, mail_message_members)) == 13


def test_single_render_block_does_not_block_frozen_mail_and_remains_diagnosable(pending):
    env = pending
    seal_copies(env, 2)
    result = once(env, settings=enabled(env, max_bytes=1024))
    assert result["planning"]["blocked_count"] == 1
    assert result["sending"]["accepted"] == 1 and result["needs_attention"]
    again = once(env, settings=enabled(env, max_bytes=1024), at=ACTIVATED_AT + 21)
    assert again["planning"]["blocked"] == [] and again["planning"]["blocked_count"] == 1
    assert again["needs_attention"]


def test_database_plan_failure_stops_before_any_smtp(pending):
    env = pending
    called = []

    def fault(connection, cursor, statement, parameters, context, executemany):
        if "mail_messages" in statement and statement.lstrip().startswith("SELECT"):
            raise OperationalError("injected", {}, RuntimeError("private database detail"))

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", fault)
    try:
        with pytest.raises(MailError):
            once(env, sender=lambda *args: called.append(1))
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", fault)
    assert called == [] and rows(env, mail_attempts) == []
    assert rows(env, mail_delivery)[0]["state"] == "pending"


def test_process_programming_error_does_not_silently_continue_to_send(pending, monkeypatch):
    from signalnest.mail import background

    def bug(*args, **kwargs):
        raise RuntimeError("unexpected code defect")

    monkeypatch.setattr(background, "plan_mail", bug)
    with pytest.raises(RuntimeError, match="unexpected code defect"):
        once(pending, sender=lambda *args: pytest.fail("must not send"))
    assert rows(pending, mail_attempts) == []


def test_pause_still_plans_locally_and_preserves_send_backlog(activated):
    from signalnest.mail.sending import set_sending_paused

    env = activated
    live(env, opportunity())
    set_sending_paused(env.engine, SOURCE, True, at=ACTIVATED_AT + 10)
    result = once(env, sender=lambda *args: pytest.fail("paused"))
    assert result["planning"]["planned"] == 1 and result["sending"]["paused"]
    assert result["sending"]["attempted"] == 0
    assert rows(env, mail_attempts) == []


def test_disabled_pass_touches_neither_store_nor_lock(state_env):
    settings = crawl_settings(state_env)
    before = sorted(p.name for p in state_env.settings.database.parent.iterdir())
    assert run_mail_pass(settings) == dict(
        enabled=False, skipped="mail_disabled", needs_attention=False
    )
    assert sorted(p.name for p in state_env.settings.database.parent.iterdir()) == before


def test_same_instance_lock_rejects_before_plan_or_send(pending):
    with (
        writer_lock(pending.settings.database),
        pytest.raises(WriterLockError, match="writer_lock_busy"),
    ):
        once(pending, sender=lambda *args: pytest.fail("locked"))
    assert rows(pending, mail_attempts) == []
    assert once(pending)["sending"]["accepted"] == 1


@pytest.mark.parametrize("mode", ["regular", "full"])
def test_ordinary_collection_failure_still_sends_existing_mail(pending, mode):
    env = pending
    settings = enabled(env)
    budget = getattr(settings.runtime, mode).model_copy(update={"max_details": 0})
    settings.runtime = settings.runtime.model_copy(update={mode: budget})
    clock = Clock()
    clock.epoch = ACTIVATED_AT + 20

    def forbidden(request):
        return httpx.Response(403, headers={"Content-Type": "text/html"}, content=b"denied")

    result = run_scheduled_cycle(
        settings,
        mode,
        clock=clock,
        transport=httpx.MockTransport(forbidden),
        sender=lambda *args: ACCEPTED,
    )
    assert result["collection"]["result"] == "failed"
    assert result["collection"]["coverage"] == "interrupted"
    assert result["mail"]["sending"]["accepted"] == 1
    assert result["needs_attention"]


def test_systemic_collection_write_failure_never_runs_mail(pending):
    env = pending
    clock = Clock()
    clock.epoch = ACTIVATED_AT + 20

    def fault(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO raw_responses"):
            raise OperationalError("fault injection", {}, RuntimeError("private DB error"))

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", fault)
    try:
        with pytest.raises(IngestError):
            run_scheduled_cycle(
                enabled(env),
                "regular",
                clock=clock,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(403, content=b"denied")
                ),
                sender=lambda *args: pytest.fail("system error must stop"),
            )
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", fault)
    assert rows(env, mail_attempts) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": 1},
        {"enabled": "true"},
        {"max_plan_messages": 0},
        {"max_plan_messages": 21},
        {"max_events": 0},
        {"max_events": 101},
        {"max_events": True},
        {"max_bytes": 1023},
        {"max_bytes": 1048577},
        {"unexpected": True},
    ],
)
def test_runtime_config_bounds(changes):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MailRuntimeSettings(**changes)


def test_enabled_background_requires_smtp_before_storage_or_env_read(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(
        (ROOT / "config.example.toml").read_text().replace("enabled = false", "enabled = true")
    )
    with pytest.raises(ConfigurationError, match="requires an explicit smtp"):
        load_config(path)
    assert main(["scheduled-mail", "--config", str(path)]) == 2
    assert list(tmp_path.iterdir()) == [path]
    assert "smtp" in capsys.readouterr().err


def test_cli_disabled_background_is_side_effect_free(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text((ROOT / "config.example.toml").read_text())
    assert main(["scheduled-mail", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == "mail_disabled"
    assert list(tmp_path.iterdir()) == [path]


def test_cli_enabled_pass_plans_while_paused_and_reports_real_state(activated, tmp_path, capsys):
    from test_background_mail_process import configuration

    from signalnest.mail.sending import set_sending_paused

    live(activated, opportunity())
    set_sending_paused(activated.engine, SOURCE, True, at=ACTIVATED_AT + 10)
    path = configuration(activated, tmp_path)
    assert main(["scheduled-mail", "--config", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["planning"]["planned"] == 1
    assert result["sending"]["paused"] and result["sending"]["attempted"] == 0
    assert rows(activated, mail_delivery)[0]["state"] == "pending"
    assert rows(activated, mail_attempts) == []


def test_cli_enabled_lock_failure_is_finite_and_does_not_write(pending, tmp_path, capsys):
    from test_background_mail_process import configuration

    path = configuration(pending, tmp_path)
    before = rows(pending, mail_messages)
    with writer_lock(pending.settings.database):
        assert main(["scheduled-mail", "--config", str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "writer_lock_busy" in output.err
    assert rows(pending, mail_messages) == before and rows(pending, mail_attempts) == []


def test_cli_help_for_background_does_not_need_config_or_database(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["scheduled-mail", "--help"])
    assert error.value.code == 0 and "--config" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_template_has_independent_mail_timer_env_and_bounded_supervisor():
    service = (ROOT / "deploy/systemd/signalnest-mail.service").read_text()
    collect = (ROOT / "deploy/systemd/signalnest@.service").read_text()
    timer = (ROOT / "deploy/systemd/signalnest-mail.timer").read_text()
    assert " scheduled-mail --config /etc/signalnest/signalnest.toml" in service
    for value in (
        "Type=oneshot",
        "TimeoutStopSec=30s",
        "KillMode=control-group",
        "EnvironmentFile=-/etc/signalnest/smtp.env",
        "ReadWritePaths=/var/lib/signalnest",
        "LogNamespace=signalnest",
        "Restart=no",
    ):
        assert value in service and value in collect
    assert "TimeoutStartSec=10min" in service and "TimeoutStartSec=20min" in collect
    assert "OnBootSec=2min" in timer and "Persistent=true" in timer
    assert "OnCalendar=*-*-* *:02/5:00 Asia/Shanghai" in timer
