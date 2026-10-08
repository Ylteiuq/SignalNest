"""SMTP configuration stays optional, secret-free and inert until explicit sending."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from signalnest.config import ConfigurationError, SmtpSettings, load_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config.example.toml").read_text()
SMTP = """
[smtp]
host = "smtp.example.org"
port = 587
security = "starttls"
timeout_seconds = 30.0
username_env = "SIGNALNEST_SMTP_USERNAME"
password_env = "SIGNALNEST_SMTP_PASSWORD"
"""


@pytest.mark.parametrize("host", ["smtp.example.org", "localhost", "127.0.0.1", "::1"])
def test_smtp_host_and_defaults(host):
    smtp = SmtpSettings(host=host, port=587)
    assert smtp.host == host
    assert smtp.port == 587
    assert smtp.security == "starttls"
    assert smtp.timeout_seconds == 30.0
    assert smtp.username_env is None
    assert smtp.password_env is None


def test_tls_and_credentials_environment_names():
    smtp = SmtpSettings(
        host="smtp.example.org",
        port=465,
        security="tls",
        timeout_seconds=5.0,
        username_env="SIGNALNEST_SMTP_USERNAME",
        password_env="SIGNALNEST_SMTP_PASSWORD",
    )
    assert smtp.security == "tls"
    assert smtp.username_env == "SIGNALNEST_SMTP_USERNAME"
    assert smtp.password_env == "SIGNALNEST_SMTP_PASSWORD"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "smtp://example.org",
        "smtp.example.org:587",
        "user:secret@example.org",
        "example.org/path",
        "smtp.example.org\r\nRCPT TO:bad",
        " example.org",
        "example.org ",
        "smtp..example.org",
        "-smtp.example.org",
        "smtp-.example.org",
        "[::1]",
        "fe80::1%eth0",
        "例子.example.org",
        "a" * 64 + ".example.org",
    ],
)
def test_invalid_smtp_hosts(host):
    with pytest.raises(ValidationError):
        SmtpSettings(host=host, port=587)


@pytest.mark.parametrize(
    "changes",
    [
        {"port": 0},
        {"port": 65536},
        {"port": "587"},
        {"port": True},
        {"port": 587.0},
        {"security": "plain"},
        {"security": "ssl"},
        {"timeout_seconds": 0.0},
        {"timeout_seconds": 120.1},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": "30.0"},
        {"timeout_seconds": True},
        {"username_env": "SMTP_USER"},
        {"password_env": "SMTP_PASS"},
        {"username_env": "SMTP_USER", "password_env": ""},
        {"username_env": "1_USER", "password_env": "SMTP_PASS"},
        {"username_env": "SMTP_USER", "password_env": "SMTP-PASS"},
        {"username_env": "SMTP_USER", "password_env": "SMTP_PASS\n"},
        {"username_env": "SMTP_USER", "password_env": True},
        {"username": "real-user"},
        {"password": "real-secret"},
        {"sender": "sender@example.org"},
        {"recipient": "recipient@example.org"},
        {"retries": 3},
    ],
)
def test_invalid_smtp_settings(changes):
    with pytest.raises(ValidationError):
        SmtpSettings.model_validate({"host": "smtp.example.org", "port": 587, **changes})


def test_port_is_explicitly_required():
    with pytest.raises(ValidationError, match="port"):
        SmtpSettings(host="smtp.example.org")


def test_old_config_remains_valid_and_creates_nothing(tmp_path):
    config = tmp_path / "signalnest.toml"
    config.write_text(EXAMPLE)
    settings = load_config(config)
    assert settings.smtp is None
    assert list(tmp_path.iterdir()) == [config]


def test_loads_smtp_without_reading_credentials(tmp_path, monkeypatch):
    config = tmp_path / "signalnest.toml"
    config.write_text(EXAMPLE + SMTP)
    monkeypatch.setenv("SIGNALNEST_SMTP_USERNAME", "must-not-be-read")
    monkeypatch.setenv("SIGNALNEST_SMTP_PASSWORD", "must-not-be-read")
    settings = load_config(config)
    assert settings.smtp is not None
    assert settings.smtp.username_env == "SIGNALNEST_SMTP_USERNAME"
    assert "must-not-be-read" not in settings.model_dump_json()
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ('host = "smtp.example.org"', 'host = "user:top-secret@example.org"', "smtp.host"),
        ("port = 587", "port = 0", "smtp.port"),
        ('security = "starttls"', 'security = "plain"', "smtp.security"),
        ("timeout_seconds = 30.0", "timeout_seconds = inf", "smtp.timeout_seconds"),
        ('password_env = "SIGNALNEST_SMTP_PASSWORD"', 'password = "top-secret"', "smtp"),
    ],
)
def test_load_config_has_finite_secret_free_errors(tmp_path, old, new, field):
    config = tmp_path / "signalnest.toml"
    config.write_text(EXAMPLE + SMTP.replace(old, new))
    with pytest.raises(ConfigurationError, match=field) as error:
        load_config(config)
    assert "top-secret" not in str(error.value)
    assert list(tmp_path.iterdir()) == [config]


def test_config_check_does_not_access_env_network_database_or_lock(tmp_path):
    config = tmp_path / "signalnest.toml"
    config.write_text(EXAMPLE + SMTP)
    script = """
import os
import socket
import sqlite3
import sys

from signalnest.cli import main
import signalnest.instance_lock

def forbidden(*args, **kwargs):
    raise AssertionError("unexpected SMTP or storage side effect")

class EnvironmentGuard(dict):
    def __getitem__(self, key):
        if key in {"SIGNALNEST_SMTP_USERNAME", "SIGNALNEST_SMTP_PASSWORD"}:
            forbidden()
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in {"SIGNALNEST_SMTP_USERNAME", "SIGNALNEST_SMTP_PASSWORD"}:
            forbidden()
        return super().get(key, default)

os.environ = EnvironmentGuard(os.environ)
socket.create_connection = forbidden
socket.socket.connect = forbidden
sqlite3.connect = forbidden
sqlite3.dbapi2.connect = forbidden
signalnest.instance_lock.writer_lock = forbidden
raise SystemExit(main(["config-check", "--config", sys.argv[1]]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(config)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr)["event"] == "config_validated"
    assert list(tmp_path.iterdir()) == [config]
