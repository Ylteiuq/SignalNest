"""Local operational evidence commands never deploy, send or repair."""

import hashlib
import json
from pathlib import Path

from test_offline_cli import run

from signalnest.config import load_config
from signalnest.instance_lock import writer_lock

ROOT = Path(__file__).resolve().parents[1]


def config_for(tmp_path):
    config = tmp_path / "signalnest.toml"
    config.write_bytes((ROOT / "config.example.toml").read_bytes())
    return config


def command(name, config, *args):
    return [name, "--config", str(config), "--release-root", str(ROOT), "--at", "1791460800", *args]


def test_missing_instance_emits_attention_without_creating_storage(tmp_path):
    config = config_for(tmp_path)
    result = run(tmp_path, *command("rollout-check", config))
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["prepared"] is False
    assert report["externally_verified"] is False
    assert "Traceback" not in result.stderr
    assert not load_config(config).storage.data_dir.exists()
    observation = run(tmp_path, *command("observe", config))
    assert observation.returncode == 0, observation.stderr
    assert json.loads(observation.stdout)["format"] == "signalnest-observation-v1"
    assert not load_config(config).storage.data_dir.exists()


def test_ready_local_checks_never_claim_external_acceptance_or_enable_mail(tmp_path):
    config = config_for(tmp_path)
    assert run(tmp_path, "storage-init", "--config", str(config)).returncode == 0
    database = load_config(config).storage.database
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with writer_lock(database):
        result = run(
            tmp_path,
            *command("rollout-check", config, "--profile", str(ROOT / "profile.example.toml")),
        )
        assert result.returncode == 1, result.stderr
        report = json.loads(result.stdout)
        assert report["profile"]["valid"] is True
        assert report["profile"]["operator_declared_personal"] is False
        assert report["mail_locally_ready"] is False
        assert report["externally_verified"] is False
        assert report["collection"]["documents"]["total"] == 0
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_invalid_observation_time_is_a_finite_error(tmp_path):
    config = config_for(tmp_path)
    args = command("observe", config)
    args[args.index("--at") + 1] = "-1"
    result = run(tmp_path, *args)
    assert result.returncode == 1
    assert "rollout_time_invalid" in result.stderr
    assert "Traceback" not in result.stderr
    assert not load_config(config).storage.data_dir.exists()
