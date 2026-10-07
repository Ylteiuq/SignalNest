"""Notification CLI freezes activation once and keeps inspection strictly offline."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from signalnest.cli import main
from signalnest.config import load_config
from signalnest.contracts import PaginationEvidence
from signalnest.ingestion_state import (
    ScanCompletion,
    finish_run_in_transaction,
    record_coverage_in_transaction,
    start_run_in_transaction,
)
from signalnest.instance_lock import writer_lock
from signalnest.schema import documents
from signalnest.storage import initialize_storage, open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
SENDER = "private-sender@example.org"
RECIPIENT = "private-recipient@example.org"


@pytest.fixture
def inputs(tmp_path):
    config = tmp_path / "signalnest.toml"
    config.write_bytes((ROOT / "config.example.toml").read_bytes())
    profile = tmp_path / "profile.toml"
    profile.write_text(
        'interest_topics = ["exchange"]\ninclude_phrases = ["private-profile-value"]\n',
        encoding="utf-8",
    )
    return config, profile


def activation_args(inputs, command="notifications-activate", *, at=200):
    config, profile = inputs
    args = [
        command,
        "--config",
        str(config),
        "--profile",
        str(profile),
        "--activation-id",
        "cli-activation",
        "--sender",
        SENDER,
        "--recipient",
        RECIPIENT,
    ]
    if at is not None:
        args += ["--at", str(at)]
    return args


def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("notification state commands must stay offline")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def forbid_lock(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("notification inspection must not acquire a writer lock")

    monkeypatch.setattr("signalnest.instance_lock.writer_lock", forbidden)


def assert_private_input_absent(output):
    assert SENDER not in output
    assert RECIPIENT not in output
    assert "private-profile-value" not in output


def ready_database(inputs):
    settings = load_config(inputs[0])
    initialize_storage(settings.storage)
    engine = open_initialized_engine(settings.storage.database)
    try:
        with engine.begin() as connection:
            start_run_in_transaction(
                connection, settings.source.id, "bootstrap-cli", 100, origin="bootstrap"
            )
            record_coverage_in_transaction(
                connection,
                "bootstrap-cli",
                "complete",
                110,
                completion=ScanCompletion(
                    pages=(
                        PaginationEvidence(
                            current_page=1,
                            total_pages=1,
                            is_last_page=True,
                            terminal_evidence="disabled_next_and_last",
                        ),
                    ),
                    home_recheck_unchanged=True,
                ),
            )
            finish_run_in_transaction(connection, "bootstrap-cli", "succeeded", 111)
            connection.execute(
                documents.insert().values(
                    source_id=settings.source.id,
                    source_document_id="1517:1",
                    detail_url="https://example.org/private-title",
                    discovered_title="private-title",
                    discovered_at=100,
                    discovery_origin="bootstrap",
                    first_discovery_run_id="bootstrap-cli",
                )
            )
    finally:
        engine.dispose()
    return settings


@pytest.mark.parametrize(
    "command", ["notifications-preview", "notifications-activate", "notifications-status"]
)
def test_help_has_no_database_lock_or_network_side_effects(tmp_path, command):
    script = """
import socket, sqlite3, sys, httpx
def forbidden(*args, **kwargs):
    raise AssertionError('notification help attempted database, HTTP or writer lock')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden
sqlite3.dbapi2.connect = forbidden
httpx.Client = forbidden
import signalnest.instance_lock
signalnest.instance_lock.writer_lock = forbidden
from signalnest.cli import main
raise SystemExit(main([sys.argv[1], '--help']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, command],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--config" in result.stdout
    if command != "notifications-status":
        for flag in (
            "--profile",
            "--activation-id",
            "--sender",
            "--recipient",
            "--mode",
            "--digest-hour",
            "--digest-minute",
            "--no-initial-recent",
            "--at",
        ):
            assert flag in result.stdout
    assert result.stderr == "" and not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "command", ["notifications-preview", "notifications-activate", "notifications-status"]
)
def test_missing_database_does_not_create_storage_or_lock(
    inputs, tmp_path, monkeypatch, capsys, command
):
    forbid_network(monkeypatch)
    forbid_lock(monkeypatch)
    args = (
        [command, "--config", str(inputs[0])]
        if command == "notifications-status"
        else activation_args(inputs, command)
    )
    assert main(args) == 1
    output = capsys.readouterr()
    assert output.out == "" and "database_unavailable" in output.err
    assert_private_input_absent(output.err)
    assert set(tmp_path.iterdir()) == set(inputs)


def test_invalid_profile_is_rejected_before_storage_or_writer_lock(inputs, monkeypatch, capsys):
    inputs[1].write_text('interest_topics = ["private-unsupported-topic"]\n', encoding="utf-8")
    forbid_network(monkeypatch)
    forbid_lock(monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid profile must be rejected before storage")

    monkeypatch.setattr("signalnest.storage.open_initialized_engine", forbidden)
    assert main(activation_args(inputs)) == 2
    output = capsys.readouterr()
    assert output.out == "" and "画像错误" in output.err
    assert "private-unsupported-topic" not in output.err
    assert_private_input_absent(output.err)


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--activation-id", "x" * 65),
        ("--sender", "private-invalid-sender"),
        ("--recipient", "private-invalid-recipient"),
        ("--digest-hour", "24"),
        ("--digest-minute", "60"),
        ("--at", "-1"),
        ("--digest-hour", "private-invalid-hour"),
        ("--digest-minute", "private-invalid-minute"),
        ("--at", "private-invalid-clock"),
        ("--mode", "private-invalid-mode"),
    ],
)
def test_invalid_options_are_finite_and_rejected_before_writer_lock(
    inputs, monkeypatch, capsys, flag, value
):
    forbid_network(monkeypatch)
    forbid_lock(monkeypatch)
    args = activation_args(inputs)
    if flag in args:
        args[args.index(flag) + 1] = value
    else:
        args += [flag, value]
    assert main(args) == 2
    output = capsys.readouterr()
    assert output.out == "" and "通知参数错误" in output.err
    assert "private-invalid" not in output.err
    assert_private_input_absent(output.err)


@pytest.mark.parametrize("command", ["notifications-preview", "notifications-activate"])
def test_unrepresentable_time_is_rejected_before_database_or_writer_lock(
    inputs, monkeypatch, capsys, command
):
    forbid_network(monkeypatch)
    forbid_lock(monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid notification time must be rejected before storage")

    monkeypatch.setattr("signalnest.storage.open_initialized_engine", forbidden)
    assert main(activation_args(inputs, command, at=2**63 - 1)) == 2
    output = capsys.readouterr()
    assert output.out == "" and "notification_time_invalid" in output.err
    assert str(2**63 - 1) not in output.err
    assert "Traceback" not in output.err
    assert_private_input_absent(output.err)


@pytest.mark.parametrize(
    ("command", "event_name"),
    [
        ("notifications-preview", "status_read"),
        ("notifications-activate", "notifications_enabled"),
        ("notifications-status", "status_read"),
    ],
)
def test_success_logs_only_safe_event_context(inputs, monkeypatch, capsys, command, event_name):
    settings = ready_database(inputs)
    forbid_network(monkeypatch)
    args = (
        [command, "--config", str(inputs[0])]
        if command == "notifications-status"
        else activation_args(inputs, command)
    )
    assert main(args) == 0
    output = capsys.readouterr()
    assert isinstance(json.loads(output.out), dict)
    event = json.loads(output.err)
    assert event["event"] == event_name
    assert event["source_id"] == settings.source.id and event["stage"] == "notification"
    assert len(event["run_id"]) == 32
    assert set(event) == {"time", "level", "event", "source_id", "run_id", "stage"}
    assert_private_input_absent(output.out + output.err)
    assert "private-title" not in output.out + output.err


@pytest.mark.parametrize("command", ["notifications-preview", "notifications-status"])
def test_read_commands_are_unchanged_while_writer_lock_is_held(
    inputs, monkeypatch, capsys, command
):
    settings = ready_database(inputs)
    forbid_network(monkeypatch)
    args = (
        [command, "--config", str(inputs[0])]
        if command == "notifications-status"
        else activation_args(inputs, command)
    )
    with writer_lock(settings.storage.database):
        before = settings.storage.database.read_bytes()
        entries = set(settings.storage.database.parent.iterdir())
        forbid_lock(monkeypatch)
        assert main(args) == 0
        output = capsys.readouterr()
        result = json.loads(output.out)
        assert result["source_id"] == settings.source.id
        assert_private_input_absent(output.out + output.err)
        assert "private-title" not in output.out
        assert settings.storage.database.read_bytes() == before
        assert set(settings.storage.database.parent.iterdir()) == entries


def test_uninitialized_database_is_not_migrated_by_preview_or_status(inputs, monkeypatch, capsys):
    settings = load_config(inputs[0])
    settings.storage.database.parent.mkdir()
    sqlite3.connect(settings.storage.database).close()
    before = settings.storage.database.read_bytes()
    entries = set(settings.storage.database.parent.iterdir())
    forbid_network(monkeypatch)
    forbid_lock(monkeypatch)
    for args in (
        activation_args(inputs, "notifications-preview"),
        ["notifications-status", "--config", str(inputs[0])],
    ):
        assert main(args) == 1
        output = capsys.readouterr()
        assert output.out == "" and "database_unavailable" in output.err
        assert settings.storage.database.read_bytes() == before
        assert set(settings.storage.database.parent.iterdir()) == entries


def test_activation_cannot_write_while_another_writer_holds_lock(inputs, monkeypatch, capsys):
    settings = ready_database(inputs)
    forbid_network(monkeypatch)
    with writer_lock(settings.storage.database):
        before = settings.storage.database.read_bytes()
        assert main(activation_args(inputs)) == 1
        output = capsys.readouterr()
        assert output.out == "" and "writer_lock_busy" in output.err
        assert_private_input_absent(output.err)
        assert settings.storage.database.read_bytes() == before


def test_preview_reports_incomplete_scan_and_activation_refuses_to_write(
    inputs, monkeypatch, capsys
):
    settings = load_config(inputs[0])
    initialize_storage(settings.storage)
    before = settings.storage.database.read_bytes()
    forbid_network(monkeypatch)
    assert main(activation_args(inputs, "notifications-preview")) == 0
    output = capsys.readouterr()
    preview = json.loads(output.out)
    assert preview["enabled"] is False and preview["ready"] is False
    assert preview["blocker"] == "complete_scan_required"
    assert_private_input_absent(output.out + output.err)
    assert settings.storage.database.read_bytes() == before
    assert main(activation_args(inputs)) == 1
    output = capsys.readouterr()
    assert output.out == "" and "notification_complete_scan_required" in output.err
    assert_private_input_absent(output.err)
    assert settings.storage.database.read_bytes() == before


def test_explicit_activation_options_are_frozen_without_private_output(inputs, monkeypatch, capsys):
    from signalnest.schema import notification_channel_state

    settings = ready_database(inputs)
    forbid_network(monkeypatch)
    args = activation_args(inputs) + [
        "--mode",
        "digest_only",
        "--digest-hour",
        "18",
        "--digest-minute",
        "35",
        "--no-initial-recent",
    ]
    assert main(args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["reused"] is False
    assert_private_input_absent(output.out + output.err)
    engine = open_initialized_engine(settings.storage.database)
    try:
        with engine.connect() as connection:
            row = connection.execute(sa.select(notification_channel_state)).mappings().one()
            assert row["notification_mode"] == "digest_only"
            assert row["initial_recent_review"] is False
            assert row["digest_hour"] == 18 and row["digest_minute"] == 35
            assert row["activation_at"] == 200
    finally:
        engine.dispose()


def test_activation_default_clock_reuse_preserves_original_time_and_member_boundary(
    inputs, monkeypatch, capsys
):
    from signalnest.schema import notification_activation_members, notification_channel_state

    settings = ready_database(inputs)
    forbid_network(monkeypatch)
    monkeypatch.setattr("signalnest.cli.time.time", lambda: 200)
    args = activation_args(inputs, at=None)
    assert main(args) == 0
    first_output = capsys.readouterr()
    first = json.loads(first_output.out)
    assert first["reused"] is False
    assert_private_input_absent(first_output.out + first_output.err)
    engine = open_initialized_engine(settings.storage.database)
    try:
        with engine.begin() as connection:
            channel_before = dict(
                connection.execute(sa.select(notification_channel_state)).mappings().one()
            )
            members_before = [
                dict(row)
                for row in connection.execute(sa.select(notification_activation_members)).mappings()
            ]
            connection.execute(
                documents.insert().values(
                    source_id=settings.source.id,
                    source_document_id="1517:2",
                    detail_url="https://example.org/later",
                    discovered_title="private-later-title",
                    discovered_at=300,
                )
            )
        monkeypatch.setattr("signalnest.cli.time.time", lambda: 400)
        assert main(args) == 0
        repeated_output = capsys.readouterr()
        repeated = json.loads(repeated_output.out)
        assert repeated["reused"] is True
        assert_private_input_absent(repeated_output.out + repeated_output.err)
        assert first["activation_at"] == repeated["activation_at"] == 200
        assert repeated["installation_id"] == first["installation_id"]
        with engine.connect() as connection:
            assert (
                dict(connection.execute(sa.select(notification_channel_state)).mappings().one())
                == channel_before
            )
            assert [
                dict(row)
                for row in connection.execute(sa.select(notification_activation_members)).mappings()
            ] == members_before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "change",
    ["activation_id", "recipient", "sender", "mode", "digest", "initial_recent", "profile"],
)
def test_activation_conflicts_leave_channel_and_members_unchanged(
    inputs, monkeypatch, capsys, change
):
    settings = ready_database(inputs)
    forbid_network(monkeypatch)
    assert main(activation_args(inputs)) == 0
    capsys.readouterr()
    before = settings.storage.database.read_bytes()
    args = activation_args(inputs, at=400)
    if change == "profile":
        inputs[1].write_text('interest_topics = ["research"]\n', encoding="utf-8")
    elif change == "activation_id":
        args[args.index("--activation-id") + 1] = "other-activation"
    elif change in {"sender", "recipient"}:
        args[args.index(f"--{change}") + 1] = "other-private@example.org"
    elif change == "mode":
        args += ["--mode", "digest_only"]
    elif change == "digest":
        args += ["--digest-minute", "30"]
    else:
        args += ["--no-initial-recent"]
    assert main(args) == 1
    output = capsys.readouterr()
    assert output.out == "" and "notification_activation_conflict" in output.err
    assert_private_input_absent(output.err)
    assert "other-private@example.org" not in output.err
    assert settings.storage.database.read_bytes() == before


def test_unexpected_business_exception_is_finite_and_private(inputs, monkeypatch, capsys):
    settings = ready_database(inputs)
    forbid_network(monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError(f"secret-body {RECIPIENT}")

    monkeypatch.setattr("signalnest.notifications.state.activate_notifications", fail)
    before = settings.storage.database.read_bytes()
    assert main(activation_args(inputs)) == 1
    output = capsys.readouterr()
    assert output.out == "" and "unexpected_error" in output.err
    assert "secret-body" not in output.err and "Traceback" not in output.err
    assert_private_input_absent(output.err)
    assert settings.storage.database.read_bytes() == before
