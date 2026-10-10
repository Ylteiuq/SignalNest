"""Real CS write/read CLI behavior, using production coordinator with offline HTTP."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_cs_crawling import clock, listing
from test_cs_parsing import HOME, NOTICE, NOTICE_URL, capture

from signalnest.cli import main
from signalnest.config import load_config
from signalnest.cs_parsing import LIST_URL, PARSER_VERSION, SOURCE_ID
from signalnest.schema import documents, ingestion_runs, notice_versions, raw_responses
from signalnest.storage import open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "cs.toml"
    path.write_bytes((ROOT / "config.cs.example.toml").read_bytes())
    return path


def rows(config, table):
    import sqlalchemy as sa

    engine = open_initialized_engine(load_config(config).storage.database)
    try:
        with engine.connect() as connection:
            return connection.execute(sa.select(table)).mappings().all()
    finally:
        engine.dispose()


def response(content):
    import httpx

    return httpx.Response(
        200, headers={"Content-Type": "text/html", "ETag": '"cs"'}, stream=httpx.ByteStream(content)
    )


def test_cs_initialized_cli_crawl_status_and_scheduled_run(config, monkeypatch, capsys):
    import httpx

    from signalnest import crawling

    actual, calls = crawling.crawl_once, 0

    def offline(settings, options, *, run_id=None):
        nonlocal calls
        calls += 1
        return actual(
            settings,
            options,
            run_id=run_id,
            clock=clock(1791515000 + calls * 100),
            transport=httpx.MockTransport(handler),
        )

    def handler(request):
        assert str(request.url) in {LIST_URL, NOTICE_URL}
        return response(listing() if str(request.url) == LIST_URL else capture(NOTICE).content)

    monkeypatch.setattr(crawling, "crawl_once", offline)
    assert main(["storage-init", "--config", str(config)]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "crawl-once",
                "--config",
                str(config),
                "--scan",
                "full",
                "--max-pages",
                "1",
                "--max-details",
                "1",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["source_id"] == SOURCE_ID and summary["home_rechecked"]
    assert summary["coverage"] == "complete" and summary["details_succeeded"] == 1
    assert main(["status", "--config", str(config)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["source_id"] == SOURCE_ID
    assert main(["scheduled-run", "--config", str(config), "--mode", "regular"]) == 0
    scheduled = json.loads(capsys.readouterr().out)
    assert scheduled["source_id"] == SOURCE_ID and scheduled["new_documents"] == 0
    assert len(rows(config, documents)) == len(rows(config, notice_versions)) == 1
    assert {r["parser_version"] for r in rows(config, ingestion_runs)} == {PARSER_VERSION}


def test_cs_cli_offline_import_and_maintenance_reparse_use_bound_parser(config, tmp_path, capsys):
    assert main(["storage-init", "--config", str(config)]) == 0
    capsys.readouterr()
    path, metadata = tmp_path / "list.html", tmp_path / "list.json"
    path.write_bytes(capture(HOME).content)
    evidence = dict(
        source_id=SOURCE_ID,
        page_type="list",
        requested_url=LIST_URL,
        final_url=LIST_URL,
        status_code=200,
        fetched_at=100,
    )
    metadata.write_text(json.dumps(evidence))
    assert (
        main(
            [
                "import-page",
                "--config",
                str(config),
                "--file",
                str(path),
                "--metadata",
                str(metadata),
                "--processed-at",
                "101",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["discovered_count"] == 15 and result["pagination"]["current_page"] == 1
    before = rows(config, raw_responses)
    assert (
        main(
            [
                "reparse",
                "--config",
                str(config),
                "--response-id",
                str(result["response_id"]),
                "--processed-at",
                "102",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["discovered_count"] == 15
    assert len(rows(config, documents)) == 15
    assert rows(config, raw_responses)[0]["fetched_at"] == before[0]["fetched_at"] == 100
    assert len(rows(config, raw_responses)) == 1


def test_cs_crawl_requires_explicit_initialization(config, capsys):
    assert main(["crawl-once", "--config", str(config), "--scan", "limited"]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "storage-init" in captured.err
    assert not load_config(config).storage.data_dir.exists()


@pytest.mark.parametrize("command", ["help", "config-check"])
def test_cs_help_and_config_check_have_no_network_database_or_lock(config, command):
    code = """
import socket, sqlite3, sys
def forbidden(*args, **kwargs):
    raise AssertionError('read-only entry acquired a resource')
socket.socket.connect = forbidden
socket.create_connection = forbidden
sqlite3.connect = sqlite3.dbapi2.connect = forbidden
import signalnest.instance_lock
signalnest.instance_lock.writer_lock = forbidden
from signalnest.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    args = (
        ["crawl-once", "--help"] if command == "help" else ["config-check", "--config", str(config)]
    )
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=config.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(config.parent.iterdir()) == [config]
