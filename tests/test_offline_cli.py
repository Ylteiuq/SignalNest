import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from signalnest.config import load_config
from signalnest.schema import documents, notice_versions, raw_responses
from signalnest.storage import open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "research/fixtures"


def run(cwd, *args):
    # Forbid network in child processes, including accidental future imports.
    script = """
import socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('offline CLI must not access the network')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
from signalnest.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=cwd,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "signalnest.toml"
    path.write_bytes((ROOT / "config.example.toml").read_bytes())
    return path


def manifest(tmp_path, kind="list", **overrides):
    url = (
        "https://uc.whu.edu.cn/tzgg/xstz.htm"
        if kind == "list"
        else "https://uc.whu.edu.cn/info/1517/128231.htm"
    )
    metadata = {
        "page_type": kind,
        "source_id": "whu-undergrad-student",
        "requested_url": url,
        "final_url": url,
        "fetched_at": 100,
        "status_code": 200,
    }
    if kind == "notice":
        metadata["source_document_id"] = "1517:128231"
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata | overrides))
    return path


def import_args(config, metadata, name="student-notices-page1.html", timestamp="101"):
    return [
        "import-page",
        "--config",
        str(config),
        "--metadata",
        str(metadata),
        "--file",
        str(FIXTURES / name),
        "--processed-at",
        timestamp,
    ]


def test_real_cli_import_repeat_and_reparse_from_other_directory(config, tmp_path):
    result = run(tmp_path, "storage-init", "--config", str(config))
    assert result.returncode == 0, result.stderr
    listing = run(tmp_path.parent, *import_args(config, manifest(tmp_path)))
    assert listing.returncode == 0, listing.stderr
    assert json.loads(listing.stdout)["discovered_count"] == 25
    metadata = manifest(tmp_path, "notice")
    detail = run(
        tmp_path.parent, *import_args(config, metadata, "current-notice-detail.html", "102")
    )
    assert detail.returncode == 0, detail.stderr
    first = json.loads(detail.stdout)
    repeat = run(tmp_path, *import_args(config, metadata, "current-notice-detail.html", "103"))
    assert repeat.returncode == 0, repeat.stderr
    assert json.loads(repeat.stdout)["version_id"] == first["version_id"]
    replay = run(
        tmp_path.parent,
        "reparse",
        "--config",
        str(config),
        "--response-id",
        str(first["response_id"]),
        "--processed-at",
        "110",
    )
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["response_id"] == first["response_id"]
    engine = open_initialized_engine(load_config(config).storage.database)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(documents)).scalar() == 25
            )
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(notice_versions)).scalar()
                == 1
            )
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(raw_responses)).scalar()
                == 3
            )
            response = (
                connection.execute(
                    sa.select(raw_responses).where(raw_responses.c.id == first["response_id"])
                )
                .mappings()
                .one()
            )
            assert response["fetched_at"] == 100
            assert response["last_attempt_at"] == 110
    finally:
        engine.dispose()
    events = [json.loads(line) for line in detail.stderr.splitlines()]
    assert [event["event"] for event in events] == [
        "raw_archived",
        "response_recorded",
        "page_processed",
    ]
    assert events[-1]["stage"] == "success_commit"
    assert events[-1]["document_id"] == first["document_id"]


def test_uninitialized_database_is_not_created(config, tmp_path):
    result = run(tmp_path, *import_args(config, manifest(tmp_path)))
    assert result.returncode == 1
    assert "storage-init" in result.stderr
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("command", ["import-page", "reparse"])
def test_subcommand_help_is_side_effect_free(tmp_path, command):
    result = run(tmp_path, command, "--help")
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "overrides",
    [
        {"fetched_at": None},
        {"page_type": "unknown"},
        {"source_id": "different-source"},
        {"final_url": "https://user:SECRET@example.org/"},
        {"cookie": "SECRET"},
    ],
)
def test_metadata_errors_do_not_initialize_or_leak_inputs(config, tmp_path, overrides):
    result = run(tmp_path, *import_args(config, manifest(tmp_path, **overrides)))
    assert result.returncode == 2
    assert "元数据错误" in result.stderr
    assert "SECRET" not in result.stderr
    assert not (tmp_path / "data").exists()


def test_cli_parse_failure_is_nonzero_and_reports_evidence(config, tmp_path):
    assert run(tmp_path, "storage-init", "--config", str(config)).returncode == 0
    assert run(tmp_path, *import_args(config, manifest(tmp_path))).returncode == 0
    bad = tmp_path / "bad.html"
    bad.write_bytes(b"<html>PRIVATE_BODY</html>")
    metadata = manifest(tmp_path, "notice")
    args = import_args(config, metadata, "current-notice-detail.html", "102")
    args[args.index("--file") + 1] = str(bad)
    result = run(tmp_path, *args)
    assert result.returncode == 1
    assert not result.stdout
    assert "parse_missing_structure" in result.stderr
    assert "response_id=2" in result.stderr
    assert "PRIVATE_BODY" not in result.stderr
    assert "Traceback" not in result.stderr
    retry = run(
        tmp_path,
        "reparse",
        "--config",
        str(config),
        "--response-id",
        "2",
        "--processed-at",
        "103",
    )
    assert retry.returncode == 1
    assert "parse_missing_structure" in retry.stderr


def test_missing_files_response_and_304(config, tmp_path):
    assert run(tmp_path, "storage-init", "--config", str(config)).returncode == 0
    result = run(tmp_path, "reparse", "--config", str(config), "--response-id", "999")
    assert result.returncode == 1
    assert "response_missing" in result.stderr
    metadata = manifest(tmp_path, status_code=304)
    result = run(tmp_path, *import_args(config, metadata))
    assert result.returncode == 2
    result = run(tmp_path, "import-page", "--config", str(config), "--metadata", str(metadata))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["outcome"] == "evidence_only"
    metadata = manifest(tmp_path)
    args = import_args(config, metadata)
    args[args.index("--file") + 1] = str(tmp_path / "missing.html")
    result = run(tmp_path, *args)
    assert result.returncode == 1
    assert "无法读取" in result.stderr
    assert not list((tmp_path / "data" / "raw").iterdir())


def test_unexpected_cli_errors_are_nonzero_and_not_echoed(config, tmp_path, monkeypatch, capsys):
    from signalnest import ingestion
    from signalnest.cli import main

    assert run(tmp_path, "storage-init", "--config", str(config)).returncode == 0

    def defect(*args, **kwargs):
        raise RuntimeError("PRIVATE_EXCEPTION <html>whole body</html>")

    monkeypatch.setattr(ingestion, "import_page", defect)
    assert main(import_args(config, manifest(tmp_path))) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "unexpected_error" in output.err
    assert "PRIVATE_EXCEPTION" not in output.err
    assert "whole body" not in output.err
    assert "未确认失败状态已保存" in output.err
