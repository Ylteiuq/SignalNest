"""Installed entry point and validation from unrelated working directories, fully offline."""

import json
from pathlib import Path

import pytest
from test_offline_cli import run
from test_query_agent import AT, digests
from test_query_agent import campus as campus

from signalnest.config import ConfigurationError
from signalnest.querying.config import load_query_config, load_query_inputs

ROOT = Path(__file__).resolve().parents[1]


def write_config(tmp_path, extra=""):
    (tmp_path / "uc.toml").write_bytes((ROOT / "config.example.toml").read_bytes())
    config = tmp_path / "query.toml"
    config.write_text('[[instances]]\nid="uc"\nconfig="uc.toml"\n' + extra, encoding="utf-8")
    return config


def test_example_check_and_help_have_no_storage_or_model_side_effects(tmp_path):
    config = write_config(tmp_path)
    for name in ("ask", "ask-config-check"):
        result = run(tmp_path, name, "--help")
        assert result.returncode == 0
    result = run(tmp_path, "ask-config-check", "--config", str(config))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "valid": True,
        "instances": ["uc"],
        "model_configured": False,
    }
    assert not (tmp_path / "data").exists()
    root_check = run(tmp_path, "ask-config-check", "--config", str(ROOT / "ask.example.toml"))
    assert root_check.returncode == 0, root_check.stderr


def test_cli_queries_both_real_instances_under_writer_locks(campus, tmp_path):
    from signalnest.instance_lock import writer_lock

    before = digests(campus)
    with (
        writer_lock(campus.instances["uc"].storage.database),
        writer_lock(campus.instances["cs"].storage.database),
    ):
        result = run(
            tmp_path.parent,
            "ask",
            "--config",
            str(campus.query_file),
            "--question",
            "助教招聘",
            "--local",
            "--json",
            "--at",
            AT.isoformat(),
        )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "local" and report["model_usage"]["http_requests"] == 0
    assert report["search_results"][0]["instance_id"] == "cs"
    assert {s["instance_id"] for s in report["source_status"]} == {"uc", "cs"}
    assert report["at"] == AT.isoformat()
    assert digests(campus) == before
    logs = [json.loads(line) for line in result.stderr.splitlines()]
    assert [log["event"] for log in logs] == ["ask_started", "ask_finished"]
    assert "助教招聘" not in result.stderr and "profile" not in result.stderr
    text = run(
        tmp_path.parent, "ask", "--config", str(campus.query_file), "--question", "选课", "--local"
    )
    assert text.returncode == 0 and "https://uc.whu.edu.cn" in text.stdout
    assert "未由模型阅读" in text.stdout


def test_missing_model_returns_visible_retrieval_and_nonzero(campus, tmp_path):
    result = run(
        tmp_path, "ask", "--config", str(campus.query_file), "--question", "选课", "--json"
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "fallback" and report["error_code"] == "model_not_configured"
    assert report["search_results"] and report["model_usage"]["http_requests"] == 0


def test_uninitialized_instance_is_not_implicitly_created(tmp_path):
    config = write_config(tmp_path)
    result = run(
        tmp_path, "ask", "--config", str(config), "--question", "科研", "--local", "--json"
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["error_code"] == "query_storage_unavailable"
    assert not (tmp_path / "data").exists()
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "question,at",
    [
        (" ", AT.isoformat()),
        ("科研", "not-a-date"),
        ("科研", "2026-10-12T09:00:00"),
        ("x" * 1001, AT.isoformat()),
        ("x\x01", AT.isoformat()),
    ],
)
def test_invalid_questions_exit_before_storage_access(tmp_path, question, at):
    result = run(
        tmp_path, "ask", "--config", str(write_config(tmp_path)), "--question", question, "--at", at
    )
    assert result.returncode == 2 and "参数错误" in result.stderr
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    "extra",
    [
        "\n[limits]\nmax_tool_calls=0\n",
        "\n[limits]\nmax_requests=true\n",
        "\n[limits]\nrun_seconds=nan\n",
        '\n[limits]\nunknown_secret="must-not-be-echoed"\n',
        '\n[[instances]]\nid="uc"\nconfig="uc.toml"\n',
        '\n[model]\nmodel="chosen"\ninput_usd_per_million="-1"\noutput_usd_per_million="4"\n',
        '\n[model]\nmodel="chosen"\ninput_usd_per_million="1"\noutput_usd_per_million="4"\napi_key="must-not-be-echoed"\n',
        '\n[model]\nprovider="unsupported"\nmodel="chosen"\ninput_usd_per_million="1"\noutput_usd_per_million="4"\n',
    ],
)
def test_bad_config_is_finite_and_never_echoes_values(tmp_path, extra):
    result = run(tmp_path, "ask-config-check", "--config", str(write_config(tmp_path, extra)))
    assert result.returncode == 2 and "配置" in result.stderr
    assert "must-not-be-echoed" not in result.stderr
    assert not (tmp_path / "data").exists()


def test_query_paths_are_relative_to_config_and_instance_paths_to_their_config(campus):
    settings = load_query_config(campus.query_file)
    assert settings.instances[0].config == (campus.query_file.parent / "uc/instance.toml").resolve()
    assert settings.profile == (campus.query_file.parent / "profile.toml").resolve()
    instances, profile = load_query_inputs(settings)
    assert instances["uc"].storage.database == campus.instances["uc"].storage.database
    assert profile.study_level == "undergraduate" and profile.entry_year is None


def test_duplicate_database_alias_and_invalid_timezone_fail_without_migration(tmp_path):
    config = write_config(tmp_path, '\n[[instances]]\nid="alias"\nconfig="uc.toml"\n')
    with pytest.raises(ConfigurationError, match="distinct databases"):
        load_query_inputs(load_query_config(config))
    config.write_text('timezone="Not/AZone"\n[[instances]]\nid="uc"\nconfig="uc.toml"\n')
    with pytest.raises(ConfigurationError, match="timezone"):
        load_query_config(config)
    assert not (tmp_path / "data").exists()


def test_config_check_does_not_require_or_read_model_credentials(tmp_path, monkeypatch):
    extra = '\n[model]\nmodel="test-model"\ninput_usd_per_million="1"\noutput_usd_per_million="4"\n'
    config = write_config(tmp_path, extra)

    def forbidden(*args, **kwargs):
        raise AssertionError("credentials cannot be read by configuration checks")

    monkeypatch.setattr("signalnest.model_client.os.environ.get", forbidden)
    instances, profile = load_query_inputs(load_query_config(config))
    assert profile is None and list(instances) == ["uc"]
