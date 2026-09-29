import os
import subprocess
import sys
from pathlib import Path

import pytest

from campus_information_agent.config import ConfigurationError, load_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config.example.toml").read_text()


def run_cli(cwd, *args):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "campus_information_agent", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_paths_and_validation_have_no_storage_side_effects(tmp_path, monkeypatch):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE)
    monkeypatch.chdir(tmp_path.parent)
    settings = load_config(config)
    assert settings.storage.data_dir == tmp_path / "data"
    assert settings.storage.database == tmp_path / "data/campus.sqlite3"
    result = run_cli(tmp_path.parent, "config-check", "--config", str(config))
    assert result.returncode == 0
    assert str(tmp_path / "data/campus.sqlite3") in result.stdout
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("https://uc.whu.edu.cn/tzgg/xstz.htm", "ftp://example.org/a", "source.list_url"),
        (
            "https://uc.whu.edu.cn/tzgg/xstz.htm",
            "https://user:secret@example.org",
            "source.list_url",
        ),
        ("10.0", "0.0", "http.connect_timeout_seconds"),
        ("30.0", "-1.0", "http.read_timeout_seconds"),
        ("2.0", "inf", "http.request_interval_seconds"),
        ("2.0", "0.0", "http.request_interval_seconds"),
        ("2.0", "true", "http.request_interval_seconds"),
        ("data_dir", "data_dri", "storage.data_dir"),
        ('data_dir = "data"', 'data_dir = ""', "storage.data_dir"),
    ],
)
def test_invalid_config(tmp_path, old, new, field):
    config = tmp_path / "bad.toml"
    config.write_text(EXAMPLE.replace(old, new))
    with pytest.raises(ConfigurationError, match=field):
        load_config(config)
    result = run_cli(tmp_path, "config-check", "--config", str(config))
    assert result.returncode == 2
    assert field in result.stderr
    assert "secret" not in result.stderr
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("content", ["[broken", "\xff"])
def test_bad_toml(tmp_path, content):
    config = tmp_path / "bad.toml"
    config.write_bytes(content.encode("latin1"))
    result = run_cli(tmp_path, "config-check", "--config", str(config))
    assert result.returncode == 2
    assert "invalid TOML" in result.stderr


def test_help_missing_file_and_unknown_command(tmp_path):
    assert run_cli(tmp_path, "--help").returncode == 0
    assert run_cli(tmp_path, "collect").returncode == 2
    result = run_cli(tmp_path, "config-check", "--config", "missing.toml")
    assert result.returncode == 2
    assert "cannot read" in result.stderr
    assert not list(tmp_path.iterdir())


def test_import_and_help_do_not_access_network_or_sqlite(tmp_path):
    script = """
import socket
import sqlite3

def forbidden(*args, **kwargs):
    raise AssertionError("unexpected network or database operation")
socket.socket = forbidden
sqlite3.connect = forbidden
import campus_information_agent
import campus_information_agent.config
import campus_information_agent.cli
import campus_information_agent.__main__
campus_information_agent.cli.main(["--help"])
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_absolute_paths(tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text(EXAMPLE.replace('"data"', f'"{tmp_path}/elsewhere"'))
    assert load_config(config).storage.data_dir == tmp_path / "elsewhere"
