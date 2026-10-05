"""Configured bounded runs and explicit conservative policy application, offline."""

import json
from pathlib import Path

import httpx
import pytest
from test_crawling import HOME, SECOND, Clock, fixture, html, listing, rows, seed

from signalnest.cli import main
from signalnest.config import ConfigurationError, RuntimeSettings, load_config
from signalnest.errors import IngestError
from signalnest.eventlog import Event, JsonFormatter, configure_logging, log_event
from signalnest.ingestion import record_failure_in_transaction
from signalnest.instance_lock import writer_lock
from signalnest.runtime_policy import apply_recheck_policy
from signalnest.schema import documents
from signalnest.storage import initialize_storage

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config.example.toml").read_text()


@pytest.mark.parametrize(
    "old,new,field",
    [
        ("foreground_slots = 12", "foreground_slots = 0", "runtime.foreground_slots"),
        ("middle_days = 30", "middle_days = 7", "runtime"),
        ("max_requests = 64", "max_requests = 0", "runtime.regular.max_requests"),
        ("run_seconds = 600.0", "run_seconds = inf", "runtime.regular.run_seconds"),
        ("history_slots = 4", "history_slots = true", "runtime.history_slots"),
        ("old_recheck_seconds = 2592000", "old_recheck_seconds = 1", "runtime"),
    ],
)
def test_runtime_configuration_fails_without_creating_storage(tmp_path, old, new, field):
    path = tmp_path / "invalid.toml"
    path.write_text(EXAMPLE.replace(old, new))
    with pytest.raises(ConfigurationError, match=field):
        load_config(path)
    assert main(["config-check", "--config", str(path)]) == 2
    assert list(tmp_path.iterdir()) == [path]


def test_old_configuration_without_runtime_uses_new_policy_defaults(tmp_path):
    path = tmp_path / "legacy.toml"
    path.write_text(EXAMPLE.split("# Policies apply")[0])
    assert load_config(path).runtime == RuntimeSettings()
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("mode", ["regular", "full"])
def test_scheduled_cli_uses_configured_budget_and_real_services(
    tmp_path, monkeypatch, capsys, mode
):
    from signalnest import crawling

    path = tmp_path / "signalnest.toml"
    path.write_text(
        EXAMPLE.replace("max_pages = 2", "max_pages = 1")
        .replace("max_details = 20", "max_details = 1")
        .replace("max_requests = 64", "max_requests = 3")
    )
    settings = load_config(path)
    initialize_storage(settings.storage)
    actual = crawling.crawl_once
    observed = []

    def handler(request):
        uri = str(request.url)
        if uri == HOME:
            return html(listing())
        if uri == SECOND:
            return html(listing(2))
        return html(fixture("current-notice-detail.html"))

    def offline(configuration, options, *, run_id=None):
        observed.append(options)
        return actual(
            configuration,
            options,
            run_id=run_id,
            clock=Clock(),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(crawling, "crawl_once", offline)
    assert main(["scheduled-run", "--config", str(path), "--mode", mode]) == 0
    result = json.loads(capsys.readouterr().out)
    selected = observed[0]
    if mode == "regular":
        assert selected.max_pages == selected.max_details == 1
        assert selected.fetch_limits.max_requests == 3
        assert result["coverage"] == "limited" and result["details_succeeded"] == 1
    else:
        assert selected.max_pages == 64 and selected.max_details == 0
        assert selected.fetch_limits.max_requests == 96
        assert result["coverage"] == "complete" and result["details_attempted"] == 0


def test_policy_application_preserves_overdue_and_failed_backoff(state_env):
    env = state_env
    seed(env)
    first = next(row for row in rows(env, documents) if row["last_success_at"] is not None)
    with env.engine.begin() as connection:
        connection.execute(
            documents.update().where(documents.c.id == first["id"]).values(next_due_at=50)
        )
    assert apply_recheck_policy(env.engine, "whu-undergrad-student", 1000, RuntimeSettings()) == 0
    assert rows(env, documents)[0]["next_due_at"] == 50
    with env.engine.begin() as connection:
        record_failure_in_transaction(
            connection,
            IngestError("parse_missing_structure", "parse", document_id=first["id"]),
            1000,
            failure_due_at=99999,
        )
    assert apply_recheck_policy(env.engine, "whu-undergrad-student", 1001, RuntimeSettings()) == 0
    assert rows(env, documents)[0]["next_due_at"] == 99999
    assert rows(env, documents)[0]["current_version_id"] == first["current_version_id"]


def test_policy_cli_is_offline_repeatable_and_obeys_writer_lock(tmp_path, capsys):
    path = tmp_path / "signalnest.toml"
    path.write_text(EXAMPLE)
    settings = load_config(path)
    assert main(["apply-recheck-policy", "--config", str(path)]) == 1
    assert not settings.storage.database.exists()
    initialize_storage(settings.storage)
    with writer_lock(settings.storage.database):
        assert main(["apply-recheck-policy", "--config", str(path)]) == 1
    capsys.readouterr()
    for _ in range(2):
        assert main(["apply-recheck-policy", "--config", str(path)]) == 0
        assert json.loads(capsys.readouterr().out)["changed"] == 0


def test_group_log_includes_only_finite_nonnegative_counts():
    import io
    import logging

    stream = io.StringIO()
    logger = configure_logging(stream)
    log_event(
        logger,
        Event.DETAIL_GROUP_FINISHED,
        stage="history",
        attempted=4,
        failed=1,
        succeeded=3,
        unserved=0,
        remaining_due=96,
        oldest_overdue_seconds=72 * 3600,
    )
    result = json.loads(stream.getvalue())
    assert result["attempted"] == 4 and result["stage"] == "history"
    record = logging.LogRecord("signalnest", logging.INFO, "", 0, Event.CRAWL_FINISHED, (), None)
    record.attempted, record.failed, record.remaining_due = "secret", True, -1
    safe = json.loads(JsonFormatter().format(record))
    assert "attempted" not in safe and "failed" not in safe and "remaining_due" not in safe
