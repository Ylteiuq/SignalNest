"""Real status/help CLI paths remain offline and leave persisted state untouched."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from signalnest.cli import main
from signalnest.config import load_config
from signalnest.ingestion_state import set_cooldown_in_transaction, start_run_in_transaction
from signalnest.instance_lock import writer_lock
from signalnest.schema import documents, ingestion_runs
from signalnest.storage import initialize_storage, open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "signalnest.toml"
    path.write_bytes((ROOT / "config.example.toml").read_bytes())
    return path


def forbid_http_and_lock(monkeypatch):
    import httpx

    from signalnest import instance_lock

    def forbidden(*args, **kwargs):
        raise AssertionError("status must not create an HTTP client or acquire a writer lock")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(instance_lock, "writer_lock", forbidden)


def test_status_json_and_event_use_actual_database_without_http_or_writer_lock(
    config, tmp_path, monkeypatch, capsys
):
    settings = load_config(config)
    initialize_storage(settings.storage)
    engine = open_initialized_engine(settings.storage.database)
    try:
        with engine.begin() as connection:
            start_run_in_transaction(
                connection, settings.source.id, "run-waiting", 100, origin="bootstrap"
            )
            set_cooldown_in_transaction(connection, settings.source.id, 9_000_000_000)
            connection.execute(
                documents.insert().values(
                    source_id=settings.source.id,
                    source_document_id="1517:1",
                    detail_url="https://example.org/private-url",
                    discovered_title="private title",
                    discovered_at=100,
                )
            )
        before = settings.storage.database.read_bytes()
        entries = set(settings.storage.database.parent.iterdir())
        forbid_http_and_lock(monkeypatch)
        assert main(["status", "--config", str(config)]) == 0
        output = capsys.readouterr()
        value = json.loads(output.out)
        assert value["source_id"] == settings.source.id
        assert value["first_processing"]["total"] == value["first_processing"]["due"] == 1
        assert value["rechecks"]["due"] == 0
        assert value["source"]["cooldown_active"] is True
        assert value["latest_run"]["run_id"] == "run-waiting"
        assert value["latest_run"]["result"] == "running"
        assert value["unfinished_runs"] == 1
        assert "private" not in output.out
        event = json.loads(output.err)
        assert event["event"] == "status_read"
        assert event["source_id"] == settings.source.id
        assert settings.storage.database.read_bytes() == before
        assert set(settings.storage.database.parent.iterdir()) == entries
        assert (tmp_path / "data/raw").is_dir()
        assert not list((tmp_path / "data/raw").iterdir())
    finally:
        engine.dispose()


def test_status_missing_database_returns_one_without_creating_database_directory_or_lock(
    config, tmp_path, monkeypatch, capsys
):
    forbid_http_and_lock(monkeypatch)
    assert main(["status", "--config", str(config)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "database_unavailable" in output.err
    assert "初始化" in output.err
    assert "Traceback" not in output.err
    assert set(tmp_path.iterdir()) == {config}


def test_status_uninitialized_database_returns_one_without_migration_or_lock(
    config, monkeypatch, capsys
):
    settings = load_config(config)
    settings.storage.database.parent.mkdir()
    sqlite3.connect(settings.storage.database).close()
    before = settings.storage.database.read_bytes()
    entries = set(settings.storage.database.parent.iterdir())
    forbid_http_and_lock(monkeypatch)
    assert main(["status", "--config", str(config)]) == 1
    output = capsys.readouterr()
    assert not output.out and "database_unavailable" in output.err
    assert settings.storage.database.read_bytes() == before
    assert set(settings.storage.database.parent.iterdir()) == entries
    assert not settings.storage.data_dir.joinpath("raw").exists()


def test_status_cli_can_read_while_writer_lock_held_without_repairing_running_row(
    config, monkeypatch, capsys
):
    settings = load_config(config)
    initialize_storage(settings.storage)
    engine = open_initialized_engine(settings.storage.database)
    try:
        with engine.begin() as connection:
            start_run_in_transaction(
                connection, settings.source.id, "unfinished", 100, origin="regular"
            )
        with writer_lock(settings.storage.database):
            before = settings.storage.database.read_bytes()
            forbid_http_and_lock(monkeypatch)
            assert main(["status", "--config", str(config)]) == 0
            value = json.loads(capsys.readouterr().out)
            assert value["latest_run"]["result"] == "running"
            assert value["latest_run"]["finished_at"] is None
            assert value["unfinished_runs"] == 1
            assert settings.storage.database.read_bytes() == before
        with engine.connect() as connection:
            row = connection.execute(sa.select(ingestion_runs)).mappings().one()
            assert row["result"] == "running" and row["finished_at"] is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("command", ["status", "scheduled-run", "apply-recheck-policy"])
def test_new_command_help_does_not_open_database_http_or_writer_lock(command, tmp_path):
    script = """
import socket, sqlite3, sys, httpx
def forbidden(*args, **kwargs):
    raise AssertionError('help attempted database, HTTP or writer lock')
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
    assert not result.stderr
    assert not list(tmp_path.iterdir())
