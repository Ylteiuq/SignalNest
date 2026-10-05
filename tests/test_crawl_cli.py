"""CLI paths exercise the actual coordinator, files and SQLite with offline HTTP."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from signalnest.cli import main
from signalnest.config import load_config
from signalnest.instance_lock import writer_lock
from signalnest.schema import documents, ingestion_runs, notice_versions, raw_responses
from signalnest.storage import initialize_storage, open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
SECOND = "https://uc.whu.edu.cn/tzgg/xstz/23.htm"


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def time(self):
        return 2000000000 + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "signalnest.toml"
    path.write_bytes((ROOT / "config.example.toml").read_bytes())
    return path


def args(config, *options):
    return ["crawl-once", "--config", str(config), "--scan", "limited", *options]


def install_offline_http(monkeypatch, responder):
    from signalnest import crawling

    actual = crawling.crawl_once

    def offline(settings, options, *, run_id=None):
        return actual(
            settings,
            options,
            run_id=run_id,
            transport=httpx.MockTransport(responder),
            clock=Clock(),
        )

    monkeypatch.setattr(crawling, "crawl_once", offline)


def response(content, status=200):
    return httpx.Response(
        status,
        headers={"Content-Type": "text/html; charset=utf-8"},
        stream=httpx.ByteStream(content),
    )


@pytest.mark.parametrize("command", ["help", "config-check"])
def test_read_only_cli_has_no_database_network_or_lock_side_effect(config, tmp_path, command):
    script = """
import socket, sqlite3, sys
def forbidden(*args, **kwargs):
    raise AssertionError('read-only CLI attempted a side effect')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
sqlite3.connect = forbidden
sqlite3.dbapi2.connect = forbidden
import signalnest.instance_lock
signalnest.instance_lock.writer_lock = forbidden
from signalnest.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    invocation = (
        ["crawl-once", "--help"] if command == "help" else ["config-check", "--config", str(config)]
    )
    result = subprocess.run(
        [sys.executable, "-c", script, *invocation],
        cwd=tmp_path.parent,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert set(tmp_path.iterdir()) == {config}
    if command == "help":
        assert "--scan" in result.stdout and "--max-requests" in result.stdout


@pytest.mark.parametrize(
    "option,value",
    [
        ("--max-pages", "0"),
        ("--max-pages", "-1"),
        ("--max-details", "-1"),
        ("--max-requests", "0"),
        ("--max-requests", "100001"),
        ("--run-seconds", "0"),
        ("--run-seconds", "nan"),
        ("--resource-seconds", "inf"),
        ("--max-body-bytes", "0"),
        ("--max-body-bytes", "67108865"),
    ],
)
def test_invalid_budget_is_nonzero_without_creating_storage(
    config, tmp_path, capsys, option, value
):
    assert main(args(config, option, value)) == 2
    output = capsys.readouterr()
    assert "采集参数错误" in output.err
    assert not output.out
    assert set(tmp_path.iterdir()) == {config}


def test_scan_mode_must_be_explicit(config, tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        main(["crawl-once", "--config", str(config)])
    assert caught.value.code == 2
    assert "--scan" in capsys.readouterr().err
    assert set(tmp_path.iterdir()) == {config}


def test_uninitialized_database_error_does_not_create_storage(config, tmp_path, capsys):
    assert main(args(config)) == 1
    assert "storage-init" in capsys.readouterr().err
    assert set(tmp_path.iterdir()) == {config}


def test_writer_lock_rejection_happens_before_any_http(config, monkeypatch, capsys):
    settings = load_config(config)
    initialize_storage(settings.storage)
    sent = []

    def responder(request):
        sent.append(request)
        return response(b"<html>must not be fetched</html>")

    install_offline_http(monkeypatch, responder)
    with writer_lock(settings.storage.database):
        assert main(args(config)) == 1
    assert not sent
    assert "writer_lock_busy" in capsys.readouterr().err


def test_real_cli_scans_two_pages_and_repeat_is_idempotent(config, monkeypatch, capsys):
    settings = load_config(config)
    initialize_storage(settings.storage)
    sent = []
    pages = {
        HOME: (ROOT / "research/fixtures/student-notices-page1.html").read_bytes(),
        SECOND: (ROOT / "research/fixtures/student-notices-page2.html").read_bytes(),
    }

    def responder(request):
        sent.append(str(request.url))
        return response(pages[str(request.url)])

    install_offline_http(monkeypatch, responder)
    invocation = args(config, "--max-pages", "2", "--max-details", "0")
    assert main(invocation) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["coverage"] == "limited"
    assert first["coverage_error_code"] == "page_limit"
    assert first["pages_committed"] == 2
    assert first["scanned_entries"] == first["new_documents"] == 50
    assert first["details_attempted"] == first["details_succeeded"] == first["details_failed"] == 0
    assert first["physical_requests"] == 2
    assert first["remaining_due"] == first["remaining_unprocessed"] == 50
    assert sent == [HOME, SECOND]
    assert main(invocation) == 0
    repeat = json.loads(capsys.readouterr().out)
    assert repeat["coverage"] == "limited"
    assert repeat["new_documents"] == 0
    assert repeat["scanned_entries"] == 50
    assert repeat["physical_requests"] == 2
    engine = open_initialized_engine(settings.storage.database)
    try:
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(documents)) == 50
            assert connection.scalar(sa.select(sa.func.count()).select_from(notice_versions)) == 0
            assert connection.scalar(sa.select(sa.func.count()).select_from(raw_responses)) == 4
            assert connection.scalar(sa.select(sa.func.count()).select_from(ingestion_runs)) == 2
            assert {
                row.coverage for row in connection.execute(sa.select(ingestion_runs.c.coverage))
            } == {"limited"}
    finally:
        engine.dispose()
    assert len(list((settings.storage.data_dir / "raw").glob("*.bin"))) == 2


def test_response_failure_outputs_summary_and_nonzero_exit(config, monkeypatch, capsys):
    settings = load_config(config)
    initialize_storage(settings.storage)
    install_offline_http(monkeypatch, lambda request: response(b"PRIVATE_BODY", status=403))
    assert main(args(config, "--max-details", "0")) == 1
    output = capsys.readouterr()
    summary = json.loads(output.out)
    assert summary["result"] != "succeeded"
    assert summary["coverage"] == "interrupted"
    assert "PRIVATE_BODY" not in output.err
    assert "Traceback" not in output.err


def test_real_request_budget_returns_interrupted_summary(config, monkeypatch, capsys):
    settings = load_config(config)
    initialize_storage(settings.storage)
    sent = []

    def responder(request):
        sent.append(str(request.url))
        return response((ROOT / "research/fixtures/student-notices-page1.html").read_bytes())

    install_offline_http(monkeypatch, responder)
    assert main(args(config, "--max-requests", "1", "--max-details", "0")) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["result"] == "interrupted"
    assert summary["coverage"] == "limited"
    assert summary["coverage_error_code"] == "request_limit"
    assert summary["physical_requests"] == 1
    assert summary["new_documents"] == 25
    assert summary["remaining_unprocessed"] == 25
    assert sent == [HOME]
