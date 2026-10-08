"""Local mail planning and frozen previews exercise the actual N1 and N2 services.

Setup seeds a complete-scan fact; it does not perform a network traversal or SMTP.
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa
from bs4 import BeautifulSoup

from signalnest.cli import main
from signalnest.config import load_config
from signalnest.contracts import PaginationEvidence
from signalnest.ingestion import ResponseInput, import_page, process_response, record_response
from signalnest.ingestion_state import (
    ScanCompletion,
    finish_run_in_transaction,
    record_coverage_in_transaction,
    start_run_in_transaction,
)
from signalnest.instance_lock import writer_lock
from signalnest.notifications.contracts import Profile
from signalnest.notifications.state import ActivationOptions, activate_notifications
from signalnest.rawstore import RawStore
from signalnest.schema import email_outbox, mail_messages
from signalnest.storage import initialize_storage, open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "research/fixtures"
AT = int(datetime(2026, 9, 4, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
NOTICE = "https://uc.whu.edu.cn/info/1517/128231.htm"
SENDER = "private-sender@example.org"
RECIPIENT = "private-recipient@example.org"


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "signalnest.toml"
    path.write_bytes((ROOT / "config.example.toml").read_bytes())
    return path


def forbid_local_lock(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only/invalid mail commands must not acquire a writer lock")

    monkeypatch.setattr("signalnest.instance_lock.writer_lock", forbidden)


def forbid_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("local mail commands must not build an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden)


def args(config, command="mail-plan", *extra):
    return [command, "--config", str(config), *extra]


def files(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def response(source_id, at, *, page_type="list", url=HOME, identity=None):
    return ResponseInput(
        source_id=source_id,
        page_type=page_type,
        source_document_id=identity,
        requested_url=url,
        final_url=url,
        fetched_at=at,
        status_code=200,
    )


@pytest.fixture
def pending(config):
    settings = load_config(config)
    initialize_storage(settings.storage)
    engine = open_initialized_engine(settings.storage.database)
    store = RawStore(settings.storage.data_dir)
    try:
        with writer_lock(settings.storage.database):
            import_page(
                engine,
                store,
                response(settings.source.id, AT - 100),
                (FIXTURES / "student-notices-page1.html").read_bytes(),
                AT - 99,
            )
            with engine.begin() as connection:
                start_run_in_transaction(
                    connection,
                    settings.source.id,
                    "mail-cli-bootstrap",
                    AT - 50,
                    origin="bootstrap",
                )
                record_coverage_in_transaction(
                    connection,
                    "mail-cli-bootstrap",
                    "complete",
                    AT - 40,
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
                finish_run_in_transaction(
                    connection, "mail-cli-bootstrap", "partial_failure", AT - 39
                )
            activate_notifications(
                engine,
                settings.source.id,
                Profile(
                    institution="whu",
                    study_level="undergraduate",
                    interest_topics=("exchange",),
                    high_value_topics=("exchange",),
                ),
                ActivationOptions(
                    activation_id="mail-cli-activation", sender=SENDER, recipient=RECIPIENT
                ),
                at=AT,
            )
            with engine.begin() as connection:
                start_run_in_transaction(
                    connection, settings.source.id, "mail-cli-live", AT + 1, origin="regular"
                )
            tree = BeautifulSoup(
                (FIXTURES / "current-notice-detail.html").read_bytes(), "html.parser"
            )
            tree.select_one(".title_nei > b").string = "国际交流项目报名通知"
            tree.select_one(".title_nei > i").string = "时间：2026-09-04"
            body = tree.select_one("#vsb_content > .v_news_content")
            body.clear()
            paragraph = tree.new_tag("p")
            paragraph.string = (
                "面向武汉大学本科生，现启动国际交流项目报名。"
                "报名时间：2026年9月4日9:00至2026年9月4日18:00。"
            )
            body.append(paragraph)
            evidence_id = record_response(
                engine,
                store,
                response(
                    settings.source.id,
                    AT + 2,
                    page_type="notice",
                    url=NOTICE,
                    identity="1517:128231",
                ),
                str(tree).encode(),
            )
            process_response(
                engine,
                store,
                evidence_id,
                AT + 3,
                processing_origin="live",
                ingestion_run_id="mail-cli-live",
            )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(email_outbox)
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
    return settings


@pytest.mark.parametrize("command", ["mail-plan", "mail-preview"])
def test_mail_help_has_no_storage_lock_http_or_clock(config, tmp_path, command):
    script = """
import socket, sqlite3, sys, httpx, time
def forbidden(*args, **kwargs):
    raise AssertionError('mail help attempted a runtime operation')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden
sqlite3.dbapi2.connect = forbidden
httpx.Client = forbidden
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
    assert result.returncode == 0, result.stderr
    assert "--config" in result.stdout
    for flag in (
        ("--at", "--max-messages", "--max-events", "--max-bytes", "--preview")
        if command == "mail-plan"
        else ("--mail-id",)
    ):
        assert flag in result.stdout
    assert result.stderr == "" and files(tmp_path) == before


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        ("mail-plan", ("--at", str(AT))),
        ("mail-plan", ("--at", str(AT), "--preview")),
        ("mail-preview", ("--mail-id", "1")),
    ],
)
def test_missing_storage_does_not_create_files_or_lock(
    config, tmp_path, monkeypatch, capsys, command, extra
):
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)
    before = files(tmp_path)
    assert main(args(config, command, *extra)) == 1
    output = capsys.readouterr()
    assert output.out == "" and "database_unavailable" in output.err
    assert files(tmp_path) == before


@pytest.mark.parametrize("command", ["mail-plan", "mail-preview"])
def test_invalid_configuration_fails_before_mail_storage(config, monkeypatch, capsys, command):
    config.write_text("[private-invalid-config", encoding="utf-8")
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)
    extra = ("--at", str(AT)) if command == "mail-plan" else ("--mail-id", "1")
    assert main(args(config, command, *extra)) == 2
    output = capsys.readouterr()
    assert output.out == "" and "配置错误" in output.err


def test_existing_uninitialized_database_is_not_implicitly_migrated(config, monkeypatch, capsys):
    settings = load_config(config)
    settings.storage.database.parent.mkdir(parents=True)
    with sqlite3.connect(settings.storage.database) as connection:
        connection.execute("CREATE TABLE preexisting(value TEXT)")
        connection.execute("INSERT INTO preexisting VALUES ('keep')")
    before = settings.storage.database.read_bytes()
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)
    assert main(args(config, "mail-plan", "--at", str(AT), "--preview")) == 1
    output = capsys.readouterr()
    assert output.out == "" and "database_unavailable" in output.err
    assert settings.storage.database.read_bytes() == before


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        ("mail-plan", ("--max-messages", "0")),
        ("mail-plan", ("--max-messages", "21")),
        ("mail-plan", ("--max-events", "0")),
        ("mail-plan", ("--max-events", "101")),
        ("mail-plan", ("--max-bytes", "1023")),
        ("mail-plan", ("--max-bytes", "1048577")),
        ("mail-plan", ("--max-messages", "private-invalid-number")),
        ("mail-plan", ("--at", "-1")),
        ("mail-plan", ("--at", str(2**63 - 1))),
        ("mail-plan", ("--at", "private-invalid-clock")),
        ("mail-preview", ("--mail-id", "0")),
        ("mail-preview", ("--mail-id", "-1")),
        ("mail-preview", ("--mail-id", str(2**63))),
        ("mail-preview", ("--mail-id", "private-invalid-id")),
    ],
)
def test_invalid_mail_inputs_fail_before_database_or_lock(
    config, monkeypatch, capsys, command, extra
):
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid mail input must not open storage")

    monkeypatch.setattr("signalnest.storage.open_initialized_engine", forbidden)
    assert main(args(config, command, *extra)) == 2
    output = capsys.readouterr()
    assert output.out == "" and "邮件参数错误" in output.err
    assert "private-invalid" not in output.err and "Traceback" not in output.err


def test_disabled_channel_is_not_silently_successful(config, monkeypatch, capsys):
    settings = load_config(config)
    initialize_storage(settings.storage)
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)
    before = settings.storage.database.read_bytes()
    assert main(args(config, "mail-plan", "--at", str(AT), "--preview")) == 1
    output = capsys.readouterr()
    assert output.out == "" and "mail_not_enabled" in output.err
    assert settings.storage.database.read_bytes() == before


def test_plan_preview_is_byte_identical_read_only_even_under_writer_lock(
    config, pending, tmp_path, monkeypatch, capsys
):
    forbid_http(monkeypatch)
    before = files(tmp_path)
    with writer_lock(pending.storage.database):
        forbid_local_lock(monkeypatch)
        assert main(args(config, "mail-plan", "--at", str(AT + 10), "--preview")) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["preview_only"] is True and len(result["messages"]) == 1
    assert "国际交流项目报名通知" in output.out
    assert SENDER not in output.err and RECIPIENT not in output.err
    assert "国际交流项目报名通知" not in output.err
    assert files(tmp_path) == before


def test_planning_respects_instance_lock(config, pending, capsys):
    with writer_lock(pending.storage.database):
        assert main(args(config, "mail-plan", "--at", str(AT + 10))) == 1
    output = capsys.readouterr()
    assert output.out == "" and "writer_lock_busy" in output.err


def test_frozen_mail_preview_and_repeated_planning_are_reproducible(
    config, pending, monkeypatch, capsys
):
    forbid_http(monkeypatch)
    assert main(args(config, "mail-plan", "--at", str(AT + 10))) == 0
    first = capsys.readouterr()
    json.loads(first.out)
    assert SENDER not in first.err and RECIPIENT not in first.err
    engine = open_initialized_engine(pending.storage.database)
    try:
        with engine.connect() as connection:
            mail_id = connection.execute(sa.select(mail_messages.c.id)).scalar_one()
    finally:
        engine.dispose()
    before = pending.storage.database.read_bytes()
    assert main(args(config, "mail-plan", "--at", str(AT + 20))) == 0
    capsys.readouterr()
    assert pending.storage.database.read_bytes() == before
    forbid_local_lock(monkeypatch)
    preview_args = args(config, "mail-preview", "--mail-id", str(mail_id))
    assert main(preview_args) == 0
    one = capsys.readouterr()
    assert main(preview_args) == 0
    two = capsys.readouterr()
    assert json.loads(one.out) == json.loads(two.out)
    assert SENDER in one.out and RECIPIENT in one.out
    assert "国际交流项目报名通知" in one.out
    assert SENDER not in one.err and RECIPIENT not in one.err
    assert "国际交流项目报名通知" not in one.err
    assert pending.storage.database.read_bytes() == before


def test_unknown_frozen_mail_has_finite_failure(config, pending, monkeypatch, capsys):
    forbid_local_lock(monkeypatch)
    forbid_http(monkeypatch)
    assert main(args(config, "mail-preview", "--mail-id", "999999")) == 1
    output = capsys.readouterr()
    assert output.out == "" and "mail_not_found" in output.err
    assert SENDER not in output.err and RECIPIENT not in output.err


def test_database_failure_is_not_reported_success_or_echoed(config, pending, monkeypatch, capsys):
    from sqlalchemy.exc import SQLAlchemyError

    def broken(*args, **kwargs):
        raise SQLAlchemyError("private-password private-body private-sql-parameters")

    monkeypatch.setattr("signalnest.mail.planning.plan_mail", broken)
    forbid_http(monkeypatch)
    assert main(args(config, "mail-plan", "--at", str(AT + 10))) == 1
    output = capsys.readouterr()
    assert output.out == "" and "database_unavailable" in output.err
    assert "private-password" not in output.err
    assert "private-body" not in output.err and "private-sql-parameters" not in output.err
