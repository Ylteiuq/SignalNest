"""Actual offline CLI over an initialized, archived and parsed instance."""

import hashlib
import json

import pytest
from sqlalchemy.exc import SQLAlchemyError
from test_offline_cli import import_args, manifest, run

from signalnest.config import load_config
from signalnest.instance_lock import writer_lock
from signalnest.storage import open_initialized_engine


@pytest.fixture
def configured(tmp_path):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "signalnest.toml"
    config.write_bytes((root / "config.example.toml").read_bytes())
    assert run(tmp_path, "storage-init", "--config", str(config)).returncode == 0
    assert run(tmp_path, *import_args(config, manifest(tmp_path))).returncode == 0
    detail = run(
        tmp_path,
        *import_args(config, manifest(tmp_path, "notice"), "current-notice-detail.html", "102"),
    )
    assert detail.returncode == 0, detail.stderr
    return config, json.loads(detail.stdout)


def test_cli_search_and_detail_are_readonly_and_need_no_writer_lock(configured, tmp_path):
    config, imported = configured
    database = load_config(config).storage.database
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with writer_lock(database):
        result = run(tmp_path, "search", "--config", str(config), "--query", "本科生选课")
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output["total"] == 1
        assert output["items"][0]["document_id"] == imported["document_id"]
        assert output["items"][0]["original_url"].endswith("/1517/128231.htm")
        assert "选课" in output["items"][0]["snippet"]
        shown = run(
            tmp_path,
            "notice-show",
            "--config",
            str(config),
            "--document-id",
            str(imported["document_id"]),
        )
        assert shown.returncode == 0, shown.stderr
        assert json.loads(shown.stdout)["version_id"] == imported["version_id"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_cli_source_date_offset_and_visible_aliases(configured, tmp_path):
    config, _ = configured
    result = run(tmp_path, "search", "--config", str(config), "--query", "选课", "--offset", "1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["total"] == 1
    assert json.loads(result.stdout)["items"] == []
    for filters in (("--from", "2030-01-01"), ("--source-id", "different-source")):
        result = run(tmp_path, "search", "--config", str(config), "--query", "选课", *filters)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["total"] == 0
    result = run(tmp_path, "search", "--config", str(config), "--query", "助教招聘")
    assert result.returncode == 0, result.stderr
    assert "助教选聘" in json.loads(result.stdout)["expanded_terms"][0]


@pytest.mark.parametrize(
    "options",
    [
        ("--query", " "),
        ("--query", "选课", "--from", "wrong"),
        ("--query", "选课", "--from", "2026-10-01", "--to", "2025-01-01"),
        ("--query", "选课", "--limit", "0"),
        ("--query", "选课", "--offset", "-1"),
    ],
)
def test_invalid_queries_nonzero_without_storage_creation(tmp_path, options):
    from pathlib import Path

    config = tmp_path / "signalnest.toml"
    config.write_bytes((Path(__file__).resolve().parents[1] / "config.example.toml").read_bytes())
    result = run(tmp_path, "search", "--config", str(config), *options)
    assert result.returncode == 2
    assert "查询参数错误" in result.stderr
    assert not (tmp_path / "data").exists()


def test_cli_rebuild_repairs_stale_index_and_obeys_writer_lock(configured, tmp_path):
    config, _ = configured
    database = load_config(config).storage.database
    engine = open_initialized_engine(database)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM search_documents")
    finally:
        engine.dispose()
    result = run(tmp_path, "search", "--config", str(config), "--query", "选课")
    assert result.returncode == 1
    assert "search_index_stale" in result.stderr
    with writer_lock(database):
        result = run(tmp_path, "search-rebuild", "--config", str(config))
        assert result.returncode == 1
        assert "writer_lock_busy" in result.stderr
    result = run(tmp_path, "search-rebuild", "--config", str(config))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["indexed_count"] == 1
    assert run(tmp_path, "search", "--config", str(config), "--query", "选课").returncode == 0


def test_cli_unprocessed_and_missing_notice_have_finite_errors(configured, tmp_path):
    config, _ = configured
    for value, code in (("99999", "document_not_found"), ("1", "notice_not_processed")):
        result = run(tmp_path, "notice-show", "--config", str(config), "--document-id", value)
        assert result.returncode == 1
        assert code in result.stderr
        assert "Traceback" not in result.stderr


def test_storage_readonly_connection_cannot_write(configured):
    config, _ = configured
    engine = open_initialized_engine(load_config(config).storage.database, read_only=True)
    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            with pytest.raises(SQLAlchemyError):
                connection.exec_driver_sql("DELETE FROM documents")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "command", ["search", "notice-show", "search-rebuild", "rollout-check", "observe"]
)
def test_new_help_has_no_effect(tmp_path, command):
    result = run(tmp_path, command, "--help")
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
