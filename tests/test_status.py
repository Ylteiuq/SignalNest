"""Status reads real SQLite facts without repairing runs, locks or due timestamps."""

import json
import sqlite3
from datetime import date
from shutil import copyfile

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from signalnest.config import HttpSettings, Settings, SourceSettings, StorageSettings
from signalnest.contracts import PaginationEvidence
from signalnest.errors import IngestError
from signalnest.ingestion_state import (
    ScanCompletion,
    advance_source,
    finish_run_in_transaction,
    record_coverage_in_transaction,
    record_list_attempt_in_transaction,
    set_cooldown_in_transaction,
    start_run_in_transaction,
)
from signalnest.instance_lock import writer_lock
from signalnest.schema import documents, ingestion_runs, notice_versions, raw_responses
from signalnest.status import _read_only_engine, inspect_status
from signalnest.storage import StorageError

SOURCE = "whu-undergrad-student"
OTHER = "another-source"


def settings_for(storage, *, source_id=SOURCE):
    return Settings(
        storage=storage,
        source=SourceSettings(id=source_id, list_url="https://uc.whu.edu.cn/tzgg/xstz.htm"),
        http=HttpSettings(
            connect_timeout_seconds=5.0,
            read_timeout_seconds=10.0,
            request_interval_seconds=5.0,
            user_agent="SignalNest/status-test",
        ),
    )


def seed_document(
    connection,
    identity,
    *,
    discovered_at=100,
    success=None,
    failed=False,
    due=None,
    source_id=SOURCE,
):
    document_id = connection.execute(
        documents.insert().values(
            source_id=source_id,
            source_document_id=identity,
            detail_url="https://example.org/private-url",
            discovered_title="private <html>title</html>",
            discovered_at=discovered_at,
        )
    ).inserted_primary_key[0]
    if success is not None:
        response_id = connection.execute(
            raw_responses.insert().values(
                source_id=source_id,
                requested_url="https://example.org/private-url",
                final_url="https://example.org/private-url",
                fetched_at=success,
                status_code=200,
                body_state="unavailable",
                page_type="notice",
                document_id=document_id,
            )
        ).inserted_primary_key[0]
        version_id = connection.execute(
            notice_versions.insert().values(
                document_id=document_id,
                raw_response_id=response_id,
                content_sha256="a" * 64,
                parser_version="whu-test",
                parsed_at=success,
                title="private title",
                published_date=date(2026, 1, 1),
                normalized_content={"body": "private body"},
            )
        ).inserted_primary_key[0]
        connection.execute(
            documents.update()
            .where(documents.c.id == document_id)
            .values(
                status="processed",
                current_version_id=version_id,
                last_success_at=success,
                last_attempt_at=success,
                next_due_at=due,
            )
        )
    elif due is not None:
        connection.execute(
            documents.update()
            .where(documents.c.id == document_id)
            .values(
                next_due_at=due,
            )
        )
    if failed:
        connection.execute(
            documents.update()
            .where(documents.c.id == document_id)
            .values(
                status="failed",
                last_attempt_at=max(discovered_at, success or 0) + 1,
                last_error_code="parse_missing_structure",
                next_due_at=due,
            )
        )
    return document_id


def completion():
    return ScanCompletion(
        pages=(
            PaginationEvidence(
                current_page=1,
                total_pages=1,
                is_last_page=True,
                terminal_evidence="disabled_next_and_last",
            ),
        ),
        home_recheck_unchanged=True,
    )


def codes(report):
    return {item.code for item in report.diagnostics}


def test_missing_database_does_not_create_storage_or_lock(tmp_path):
    storage = StorageSettings(
        data_dir=str(tmp_path / "absent/data"), database=str(tmp_path / "absent/db.sqlite")
    )
    with pytest.raises(StorageError, match="storage-init"):
        inspect_status(settings_for(storage), at=1000)
    assert not (tmp_path / "absent").exists()


def test_uninitialized_database_is_not_migrated(tmp_path):
    database = tmp_path / "empty.sqlite"
    sqlite3.connect(database).close()
    before = database.read_bytes()
    mtime = database.stat().st_mtime_ns
    storage = StorageSettings(data_dir=str(tmp_path / "data"), database=str(database))
    with pytest.raises(StorageError, match="storage-init"):
        inspect_status(settings_for(storage), at=1000)
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == mtime
    assert set(tmp_path.iterdir()) == {database}


def test_old_or_unknown_revision_is_rejected_without_upgrade(state_env):
    with state_env.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num='0002_offline_ingestion'"
        )
    before = state_env.settings.database.read_bytes()
    with pytest.raises(StorageError, match="storage-init"):
        inspect_status(settings_for(state_env.settings), at=1000)
    assert state_env.settings.database.read_bytes() == before


def test_multiple_migration_heads_fail_clearly_without_modifying_storage(state_env):
    with state_env.engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('unrecognized_head')")
    before = state_env.settings.database.read_bytes()
    with pytest.raises(StorageError, match="storage-init"):
        inspect_status(settings_for(state_env.settings), at=1000)
    assert state_env.settings.database.read_bytes() == before


def test_invalid_sqlite_file_fails_without_replacement(tmp_path):
    database = tmp_path / "damaged.sqlite"
    database.write_bytes(b"not a SQLite database")
    storage = StorageSettings(data_dir=str(tmp_path / "absent"), database=str(database))
    with pytest.raises(StorageError, match="storage-init"):
        inspect_status(settings_for(storage), at=1000)
    assert database.read_bytes() == b"not a SQLite database"
    assert not storage.data_dir.exists()


def test_read_only_engine_rejects_real_database_write(state_env):
    engine = _read_only_engine(state_env.settings.database)
    try:
        with pytest.raises(OperationalError, match="readonly"), engine.begin() as connection:
            connection.execute(
                documents.insert().values(
                    source_id=SOURCE,
                    source_document_id="1517:1",
                    detail_url="https://example.org",
                    discovered_title="title",
                    discovered_at=1,
                )
            )
        with state_env.engine.connect() as connection:
            assert connection.execute(sa.select(documents)).first() is None
    finally:
        engine.dispose()


def test_initialized_empty_database_reports_unknown_source_without_creating_it(state_env):
    before = state_env.settings.database.read_bytes()
    report = inspect_status(settings_for(state_env.settings), at=1000)
    assert report.source.known is False
    assert report.documents.total == report.first_processing.total == report.rechecks.due == 0
    assert report.latest_run is None and report.recent_runs == ()
    assert codes(report) == {"complete_scan_missing"}
    assert state_env.settings.database.read_bytes() == before


def test_backlogs_use_success_baseline_and_due_not_failed_status(state_env):
    at = 500_000
    with state_env.engine.begin() as connection:
        oldest = seed_document(connection, "1517:1", discovered_at=100)
        seed_document(connection, "1517:2", failed=True, due=at + 1)
        seed_document(connection, "1517:3", discovered_at=300, due=at + 1)
        overdue = seed_document(connection, "1517:4", success=1000, failed=True, due=at - 90_000)
        seed_document(connection, "1517:5", success=1000, due=at)
        seed_document(connection, "1517:6", success=1000, due=at + 1)
        seed_document(connection, "1517:7", success=1000)
        seed_document(connection, "1517:8", success=1000, failed=True)
    report = inspect_status(settings_for(state_env.settings), at=at)
    assert report.documents.model_dump() == {
        "total": 8,
        "discovered": 2,
        "processed": 3,
        "failed": 3,
        "failed_without_success": 1,
        "failed_with_success": 2,
    }
    assert report.first_processing.total == 3
    assert report.first_processing.due == 1
    assert report.first_processing.deferred == 2
    assert report.first_processing.failed == 1
    assert report.first_processing.oldest_document_id == oldest
    assert report.first_processing.oldest_age_seconds == at - 100
    assert report.rechecks.total_with_success == 5
    assert report.rechecks.due == 3
    assert report.rechecks.failed_due == 2
    assert report.rechecks.deferred == 1
    assert report.rechecks.unscheduled_successful == 1
    assert report.rechecks.due_without_deadline == 1
    assert report.rechecks.oldest_due_document_id == overdue
    assert report.rechecks.oldest_overdue_seconds == 90_000
    assert report.rechecks.oldest_error_code == "parse_missing_structure"
    assert {
        "first_processing_backlog_stale",
        "rechecks_overdue",
        "successful_rechecks_unscheduled",
    } <= codes(report)


def test_status_is_source_specific_and_keeps_attempt_response_and_registration_distinct(state_env):
    with state_env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "limited-run", 100, origin="bootstrap")
        record_list_attempt_in_transaction(connection, SOURCE, 101)
        advance_source(connection, SOURCE, "last_list_response_at", 102)
        advance_source(connection, SOURCE, "last_list_registered_at", 103)
        record_coverage_in_transaction(
            connection, "limited-run", "limited", 104, error_code="page_limit"
        )
        finish_run_in_transaction(connection, "limited-run", "succeeded", 105)
        start_run_in_transaction(connection, OTHER, "other-run", 106, origin="historical")
        seed_document(connection, "1517:1", source_id=SOURCE)
        seed_document(connection, "1517:1", source_id=OTHER)
        set_cooldown_in_transaction(connection, OTHER, 500)
    report = inspect_status(settings_for(state_env.settings), at=200)
    assert report.documents.total == 1
    assert report.source.last_list_attempt_at == 101
    assert report.source.last_list_response_at == 102
    assert report.source.last_list_registered_at == 103
    assert report.source.last_complete_scan_at is report.source.bootstrap_completed_at is None
    assert report.source.not_before_at is None
    assert report.latest_run.run_id == "limited-run"
    assert report.latest_run.coverage == "limited" and report.latest_run.result == "succeeded"
    assert report.unfinished_runs == 0


def test_cooldown_survives_reopen_and_is_observed_without_clearing(state_env):
    with state_env.engine.begin() as connection:
        set_cooldown_in_transaction(connection, SOURCE, 2000)
    state_env.engine.dispose()
    active = inspect_status(settings_for(state_env.settings), at=1000)
    assert active.source.cooldown_active
    assert active.source.not_before_at == 2000
    assert active.source.cooldown_remaining_seconds == 1000
    assert "source_cooldown_active" in codes(active)
    expired = inspect_status(settings_for(state_env.settings), at=2000)
    assert expired.source.cooldown_active is False
    assert expired.source.not_before_at == 2000
    assert expired.source.cooldown_remaining_seconds == 0
    assert "source_cooldown_active" not in codes(expired)


@pytest.mark.parametrize("extra,stale", [(0, False), (1, True)])
def test_exact_diagnostic_thresholds_and_partial_failure_complete_scan(state_env, extra, stale):
    at = 1_000_000
    complete_at = at - 26 * 3600 - extra
    with state_env.engine.begin() as connection:
        start_run_in_transaction(
            connection, SOURCE, "complete-with-detail-failure", complete_at - 1, origin="bootstrap"
        )
        record_coverage_in_transaction(
            connection,
            "complete-with-detail-failure",
            "complete",
            complete_at,
            completion=completion(),
        )
        finish_run_in_transaction(
            connection,
            "complete-with-detail-failure",
            "partial_failure",
            complete_at + 1,
            error_code="detail_failed",
        )
        seed_document(connection, "1517:1", discovered_at=at - 72 * 3600 - extra)
        seed_document(connection, "1517:2", success=100, due=at - 24 * 3600 - extra)
    report = inspect_status(settings_for(state_env.settings), at=at)
    assert report.source.last_complete_scan_at == complete_at
    assert report.source.bootstrap_completed_at == complete_at
    assert (
        report.latest_run.result == "partial_failure" and report.latest_run.coverage == "complete"
    )
    for code in ("complete_scan_stale", "first_processing_backlog_stale", "rechecks_overdue"):
        assert (code in codes(report)) is stale


def test_recent_runs_are_bounded_and_running_row_is_not_repaired_or_called_active(state_env):
    with state_env.engine.begin() as connection:
        for index in range(12):
            run_id = f"run-{index}"
            start_run_in_transaction(connection, SOURCE, run_id, 100 + index * 3, origin="regular")
            if index < 11:
                record_coverage_in_transaction(connection, run_id, "limited", 101 + index * 3)
                finish_run_in_transaction(
                    connection, run_id, "interrupted", 102 + index * 3, error_code="request_limit"
                )
    report = inspect_status(settings_for(state_env.settings), at=1000)
    assert len(report.recent_runs) == 10
    assert report.latest_run.run_id == "run-11" and report.latest_run.result == "running"
    assert report.unfinished_runs == 1
    assert "unfinished_runs" in codes(report)
    assert "recent_budget_truncation" in codes(report)
    with state_env.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(ingestion_runs.c.result).where(ingestion_runs.c.id == "run-11")
            ).scalar_one()
            == "running"
        )


def test_json_report_omits_html_urls_and_unfiltered_error_text(state_env):
    with state_env.engine.begin() as connection:
        seed_document(connection, "1517:1", failed=True)
        start_run_in_transaction(connection, SOURCE, "safe-run", 1000, origin="regular")
        record_coverage_in_transaction(connection, "safe-run", "interrupted", 1001)
        finish_run_in_transaction(connection, "safe-run", "failed", 1002)
        connection.execute(
            ingestion_runs.update()
            .where(ingestion_runs.c.id == "safe-run")
            .values(error_code="https://example.org/private?secret=value <html>")
        )
    report = inspect_status(settings_for(state_env.settings), at=2000)
    value = json.loads(report.model_dump_json())
    assert value["latest_run"]["error_code"] == "unrecognized_error_code"
    assert report.model_dump(mode="json") == value
    encoded = report.model_dump_json()
    assert "http" not in encoded and "<html>" not in encoded and "private" not in encoded
    assert "foreground" not in encoded and "history" not in encoded and "scan_mode" not in encoded


def test_read_while_writer_lock_held_is_safe_and_does_not_mutate_database(state_env):
    with state_env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "unfinished", 100, origin="regular")
    with writer_lock(state_env.settings.database):
        before = state_env.settings.database.read_bytes()
        entries = set(state_env.settings.database.parent.iterdir())
        report = inspect_status(settings_for(state_env.settings), at=1000)
        assert report.unfinished_runs == 1
        assert state_env.settings.database.read_bytes() == before
        assert set(state_env.settings.database.parent.iterdir()) == entries


def test_future_facts_do_not_produce_negative_ages(state_env):
    with state_env.engine.begin() as connection:
        seed_document(connection, "1517:1", discovered_at=2000)
    report = inspect_status(settings_for(state_env.settings), at=1000)
    assert report.first_processing.oldest_age_seconds == 0


def test_read_only_uri_preserves_path_special_characters(state_env, tmp_path):
    database = tmp_path / "status # ? % space.sqlite"
    copyfile(state_env.settings.database, database)
    storage = StorageSettings(data_dir=str(tmp_path / "absent"), database=str(database))
    report = inspect_status(settings_for(storage), at=1000)
    assert report.documents.total == 0
    assert not storage.data_dir.exists()
    assert not database.with_name(database.name + ".lock").exists()


@pytest.mark.parametrize("at", [-1, True, 1.0, "1"])
def test_invalid_observation_time_fails_before_accessing_storage(tmp_path, at):
    storage = StorageSettings(data_dir=str(tmp_path / "data"), database=str(tmp_path / "db.sqlite"))
    with pytest.raises(IngestError, match="invalid_processing_time"):
        inspect_status(settings_for(storage), at=at)
    assert not list(tmp_path.iterdir())
