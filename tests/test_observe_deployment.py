"""The timed entry records actual SQLite facts and an actual clock, never sends."""

import hashlib
import json
import runpy
import time
from pathlib import Path

import pytest
from test_status import seed_document

from signalnest.instance_lock import writer_lock
from signalnest.rollout import build_release_manifest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "deploy/observe_instance.py"


@pytest.fixture
def observation_config(state_env, tmp_path):
    config = tmp_path / "observe.toml"
    config.write_text(
        "[storage]\n"
        f"data_dir={json.dumps(str(state_env.settings.data_dir))}\n"
        f"database={json.dumps(str(state_env.settings.database))}\n"
        '[source]\nid="whu-undergrad-student"\n'
        'list_url="https://uc.whu.edu.cn/tzgg/xstz.htm"\n'
        "[http]\nconnect_timeout_seconds=5.0\nread_timeout_seconds=10.0\n"
        'request_interval_seconds=5.0\nuser_agent="SignalNest/observe-test"\n'
    )
    profile = tmp_path / "profile.toml"
    profile.write_text(
        'schema_version=1\nprofile_id="self"\ninstitution="whu"\n'
        'study_level="undergraduate"\ninterest_topics=["teaching_assistant"]\n'
        'high_value_topics=["teaching_assistant"]\n'
    )
    return config, profile


def run(config, profile, *, confirmed=False):
    args = [
        "--config",
        str(config),
        "--profile",
        str(profile),
        "--release-root",
        str(ROOT),
    ]
    if confirmed:
        args.append("--profile-confirmed")
    return runpy.run_path(str(ENTRY), run_name="observe_deployment_test")["main"](args)


def test_actual_time_and_failed_backlog_observed_under_writer_lock_without_writes(
    observation_config, state_env, capsys
):
    with state_env.engine.begin() as connection:
        seed_document(connection, "1517:123", failed=True, due=100)
    before_bytes = state_env.settings.database.read_bytes()
    before = int(time.time())
    with writer_lock(state_env.settings.database):
        assert run(*observation_config) == 0
    report = json.loads(capsys.readouterr().out)
    assert before <= report["at"] <= int(time.time())
    assert report["format"] == "signalnest-observation-v1"
    assert report["collection"]["documents"]["total"] == 1
    assert "parse_missing_structure" in json.dumps(report["collection"])
    assert report["externally_verified"] is False
    assert state_env.settings.database.read_bytes() == before_bytes


def test_absent_database_is_a_recorded_blocker_without_initializing(
    observation_config, tmp_path, capsys
):
    config, profile = observation_config
    missing = tmp_path / "never-created" / "db.sqlite"
    text = config.read_text()
    text = "\n".join(
        f"database={json.dumps(str(missing))}" if line.startswith("database=") else line
        for line in text.splitlines()
    )
    config.write_text(text)
    assert run(config, profile) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["prepared"] is False
    assert any(
        row["code"] == "database_initialized" and row["state"] == "blocked"
        for row in report["checks"]
    )
    assert not missing.parent.exists()


@pytest.mark.parametrize("confirmed", [False, True])
def test_personal_confirmation_is_an_explicit_operator_flag(observation_config, capsys, confirmed):
    assert run(*observation_config, confirmed=confirmed) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["profile"]["operator_declared_personal"] is confirmed
    assert report["externally_verified"] is False


def test_only_credential_presence_is_observed_no_values_or_secret_digest(
    observation_config, capsys, monkeypatch
):
    config, profile = observation_config
    with config.open("a") as stream:
        stream.write(
            '[smtp]\nhost="smtp.example.org"\nport=465\nsecurity="tls"\n'
            'username_env="OBSERVER_TEST_USERNAME"\npassword_env="OBSERVER_TEST_PASSWORD"\n'
        )
    username, password = "observer-private-user", "observer-private-password"
    monkeypatch.setenv("OBSERVER_TEST_USERNAME", username)
    monkeypatch.setenv("OBSERVER_TEST_PASSWORD", password)
    assert run(config, profile) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["smtp_credentials"]["username_present"] is True
    assert report["smtp_credentials"]["password_present"] is True
    for secret in (username, password):
        assert secret not in captured.out + captured.err
        assert hashlib.sha256(secret.encode()).hexdigest() not in captured.out + captured.err


def test_invalid_config_is_nonzero_without_a_false_observation(observation_config, capsys):
    config, profile = observation_config
    config.write_text("invalid TOML")
    assert run(config, profile) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "config_invalid" in captured.err


def test_help_does_not_require_configuration_or_storage(tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(ENTRY), run_name="observe_deployment_test")["main"](["--help"])
    assert caught.value.code == 0
    assert "--profile-confirmed" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_observer_and_instance_templates_are_in_actual_release_allowlist():
    report = build_release_manifest(ROOT)
    files = report["file_sha256"]
    assert files["deploy/observe_instance.py"] == hashlib.sha256(ENTRY.read_bytes()).hexdigest()
    for name in (
        "signalnest-cs-full.timer",
        "signalnest-cs-mail.service",
        "signalnest-cs-mail.timer",
        "signalnest-observe@.service",
        "signalnest-observe@.timer",
    ):
        assert "deploy/systemd/" + name in files
