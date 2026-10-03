"""Production state and composed transactions; fault injection, not process/power loss."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from signalnest.cache import pending_resources, select_cache_candidate
from signalnest.contracts import PageInput, PaginationEvidence, RequestProfile
from signalnest.ingestion import (
    IngestError,
    ResponseInput,
    discover_page_in_transaction,
    import_page,
    process_cached_response,
    record_failure_in_transaction,
    record_response,
    save_notice_in_transaction,
)
from signalnest.ingestion_state import (
    ScanCompletion,
    finish_run_in_transaction,
    pending_documents,
    read_source_state,
    record_coverage_in_transaction,
    record_list_attempt_in_transaction,
    set_cooldown_in_transaction,
    start_run_in_transaction,
)
from signalnest.parsing import PARSER_VERSION, parse_list, parse_notice
from signalnest.schema import (
    documents,
    http_resources,
    ingestion_runs,
    notice_versions,
    raw_responses,
)
from signalnest.storage import open_initialized_engine

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
SOURCE = "whu-undergrad-student"
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
NOTICE = "https://uc.whu.edu.cn/info/1517/128231.htm"
PROFILE = RequestProfile(user_agent="SignalNest/state-test", accept="text/html")


def body(name="student-notices-page1.html"):
    return (FIXTURES / name).read_bytes()


def evidence(notice=False, **overrides):
    url = NOTICE if notice else HOME
    return ResponseInput(
        **(
            dict(
                page_type="notice" if notice else "list",
                source_id=SOURCE,
                source_document_id="1517:128231" if notice else None,
                requested_url=url,
                final_url=url,
                fetched_at=100,
                status_code=200,
                request_profile=PROFILE,
                etag='"raw"',
            )
            | overrides
        )
    )


def rows(env, table):
    with env.engine.connect() as connection:
        return connection.execute(sa.select(table).order_by(table.c.id)).mappings().all()


def target(env):
    return next(row for row in rows(env, documents) if row["source_document_id"] == "1517:128231")


def completion():
    # Supplied coordinator evidence for a two-page chain; no traversal is implemented.
    return ScanCompletion(
        pages=(
            PaginationEvidence(
                current_page=1,
                total_pages=2,
                is_last_page=False,
                last_page_url="https://uc.whu.edu.cn/tzgg/xstz/1.htm",
            ),
            PaginationEvidence(
                current_page=2,
                total_pages=2,
                is_last_page=True,
                terminal_evidence="disabled_next_and_last",
            ),
        ),
        home_recheck_unchanged=True,
    )


def test_attempt_response_registration_and_scan_success_are_separate(state_env):
    env = state_env
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "run-1", 99, origin="bootstrap")
        record_list_attempt_in_transaction(connection, SOURCE, 100)
    state = read_source_state(env.engine, SOURCE)
    assert state["last_list_attempt_at"] == 100 and state["last_list_response_at"] is None
    response = record_response(env.engine, env.store, evidence(fetched_at=101), body())
    state = read_source_state(env.engine, SOURCE)
    assert state["last_list_response_at"] == 101 and state["last_list_registered_at"] is None
    result = process_cached_response(env.engine, env.store, response, 102, ingestion_run_id="run-1")
    assert result.discovered_count == 25
    state = read_source_state(env.engine, SOURCE)
    assert state["last_list_registered_at"] == 102 and state["last_complete_scan_at"] is None
    assert {row["discovery_origin"] for row in rows(env, documents)} == {"bootstrap"}
    assert {row["first_discovery_run_id"] for row in rows(env, documents)} == {"run-1"}


@pytest.mark.parametrize("coverage", ["limited", "interrupted"])
def test_incomplete_coverage_never_advances_complete_or_bootstrap(state_env, coverage):
    env = state_env
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "run-1", 100, origin="bootstrap")
        record_coverage_in_transaction(
            connection, "run-1", coverage, 101, error_code="request_budget"
        )
        finish_run_in_transaction(connection, "run-1", "partial_failure", 102)
    state = read_source_state(env.engine, SOURCE)
    assert state["last_complete_scan_at"] is state["bootstrap_completed_at"] is None
    run = rows(env, ingestion_runs)[0]
    assert run["coverage"] == coverage and run["result"] == "partial_failure"


def test_complete_scan_and_partial_detail_failure_are_independent(state_env):
    env = state_env
    import_page(env.engine, env.store, evidence(), body(), 101)
    first = import_page(
        env.engine,
        env.store,
        evidence(True, fetched_at=102),
        body("current-notice-detail.html"),
        103,
    )
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "run-1", 104, origin="bootstrap")
        record_coverage_in_transaction(
            connection, "run-1", "complete", 105, completion=completion()
        )
    bad = record_response(
        env.engine, env.store, evidence(True, fetched_at=106), b"<html>bad</html>"
    )
    with pytest.raises(IngestError, match="parse_missing_structure"):
        process_cached_response(
            env.engine, env.store, bad, 107, failure_due_at=120, ingestion_run_id="run-1"
        )
    with env.engine.begin() as connection:
        finish_run_in_transaction(
            connection, "run-1", "partial_failure", 108, error_code="detail_failed"
        )
    state = read_source_state(env.engine, SOURCE)
    assert state["last_complete_scan_at"] == state["bootstrap_completed_at"] == 105
    assert state["last_complete_scan_run_id"] == "run-1"
    assert rows(env, ingestion_runs)[0]["coverage"] == "complete"
    assert rows(env, ingestion_runs)[0]["result"] == "partial_failure"
    assert target(env)["current_version_id"] == first.version_id
    assert target(env)["next_due_at"] == 120


def test_reopen_recovers_running_state_cooldown_and_database_work(state_env):
    env = state_env
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "abandoned", 99, origin="bootstrap")
        set_cooldown_in_transaction(connection, SOURCE, 500)
        set_cooldown_in_transaction(connection, SOURCE, 300)
    import_page(env.engine, env.store, evidence(), body(), 101, ingestion_run_id="abandoned")
    response = record_response(
        env.engine, env.store, evidence(True, fetched_at=102), b"<html>bad</html>"
    )
    with pytest.raises(IngestError):
        process_cached_response(env.engine, env.store, response, 103, failure_due_at=130)
    env.engine.dispose()
    reopened = open_initialized_engine(env.settings.database)
    try:
        assert read_source_state(reopened, SOURCE)["not_before_at"] == 500
        assert len(pending_documents(reopened, SOURCE, 129)) == 24
        assert len(pending_documents(reopened, SOURCE, 130)) == 25
        assert len(pending_resources(reopened, SOURCE, PARSER_VERSION)) == 1
        with reopened.begin() as connection:
            start_run_in_transaction(connection, SOURCE, "next", 104, origin="bootstrap")
        with reopened.connect() as connection:
            previous = (
                connection.execute(
                    sa.select(ingestion_runs).where(ingestion_runs.c.id == "abandoned")
                )
                .mappings()
                .one()
            )
        assert previous["result"] == previous["coverage"] == "interrupted"
        assert read_source_state(reopened, SOURCE)["last_complete_scan_at"] is None
    finally:
        reopened.dispose()


def test_run_interruption_after_committed_complete_keeps_scan_success(state_env):
    env = state_env
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "abandoned", 100, origin="bootstrap")
        record_coverage_in_transaction(
            connection, "abandoned", "complete", 101, completion=completion()
        )
    env.engine.dispose()
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "next", 102, origin="regular")
    previous = next(row for row in rows(env, ingestion_runs) if row["id"] == "abandoned")
    assert previous["coverage"] == "complete" and previous["result"] == "interrupted"
    assert read_source_state(env.engine, SOURCE)["last_complete_scan_at"] == 101


def test_first_origin_survives_regular_rediscovery_and_old_unknown_is_not_guessed(state_env):
    env = state_env
    first = import_page(env.engine, env.store, evidence(request_profile=None), body(), 101)
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "regular", 102, origin="regular")
    import_page(
        env.engine, env.store, evidence(fetched_at=103), body(), 104, ingestion_run_id="regular"
    )
    assert {row["discovery_origin"] for row in rows(env, documents)} == {"unknown"}
    assert {row["first_discovery_run_id"] for row in rows(env, documents)} == {None}
    assert first.discovered_count == 25


@pytest.mark.parametrize("kind", ["list", "notice"])
def test_business_and_resource_marks_roll_back_together_on_real_sqlite_failure(state_env, kind):
    env = state_env
    before = None
    if kind == "notice":
        import_page(env.engine, env.store, evidence(), body(), 101)
        import_page(
            env.engine,
            env.store,
            evidence(True, fetched_at=102),
            body("current-notice-detail.html"),
            103,
            next_due_at=200,
        )
        before = target(env)
        content = body("current-notice-detail.html").replace("选课".encode(), "课程选择".encode())
        assert content != body("current-notice-detail.html")
    else:
        content = body()
    response = record_response(
        env.engine, env.store, evidence(kind == "notice", fetched_at=104), content
    )
    prior_resource = next(
        row
        for row in rows(env, http_resources)
        if row["request_uri"] == (NOTICE if kind == "notice" else HOME)
    )
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_resource BEFORE UPDATE OF last_processed_at ON http_resources "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(IngestError, match="database_write_failed"):
        process_cached_response(
            env.engine, env.store, response, 105, next_due_at=300, failure_due_at=250
        )
    resource = next(row for row in rows(env, http_resources) if row["id"] == prior_resource["id"])
    for field in (
        "last_processed_response_id",
        "last_processed_parser_version",
        "last_processed_at",
    ):
        assert resource[field] == prior_resource[field]
    assert rows(env, raw_responses)[-1]["last_error_code"] == "database_write_failed"
    if kind == "list":
        assert rows(env, documents) == []
        assert read_source_state(env.engine, SOURCE)["last_list_registered_at"] is None
    else:
        assert len(rows(env, notice_versions)) == 1
        assert target(env)["current_version_id"] == before["current_version_id"]
        assert target(env)["last_success_at"] == before["last_success_at"]
        assert target(env)["next_due_at"] == 250
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_resource")
    assert (
        process_cached_response(env.engine, env.store, response, 106, next_due_at=300).outcome
        == "processed"
    )


@pytest.mark.parametrize("kind", ["list", "notice"])
def test_connection_entry_can_compose_and_rollback_with_other_state(state_env, kind):
    env = state_env
    if kind == "notice":
        import_page(env.engine, env.store, evidence(), body(), 101)
        content = body("current-notice-detail.html")
        parsed = parse_notice(PageInput(content=content, page_url=NOTICE))
    else:
        content = body()
        parsed = parse_list(PageInput(content=content, page_url=HOME))
    response = record_response(
        env.engine, env.store, evidence(kind == "notice", fetched_at=102), content
    )
    with pytest.raises(RuntimeError, match="injected"), env.engine.begin() as connection:
        if kind == "notice":
            save_notice_in_transaction(connection, response, parsed, 103, next_due_at=200)
        else:
            discover_page_in_transaction(connection, SOURCE, parsed, 103, response_id=response)
        set_cooldown_in_transaction(connection, SOURCE, 500)
        raise RuntimeError("injected after composed writes")
    assert read_source_state(env.engine, SOURCE)["not_before_at"] is None
    assert rows(env, raw_responses)[-1]["last_attempt_at"] is None
    assert rows(env, http_resources)[-1]["last_processed_at"] is None
    if kind == "notice":
        assert rows(env, notice_versions) == [] and target(env)["current_version_id"] is None
        assert target(env)["next_due_at"] is None
    else:
        assert rows(env, documents) == []


def test_failure_registration_failure_remains_explicit_for_cached_processing(state_env):
    env = state_env
    import_page(env.engine, env.store, evidence(), body(), 101)
    response = record_response(env.engine, env.store, evidence(True, fetched_at=102), b"bad HTML")
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_failure BEFORE UPDATE OF last_error_code ON raw_responses "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(IngestError, match="failure_state_unavailable"):
        process_cached_response(env.engine, env.store, response, 103)
    assert (
        target(env)["status"] == "discovered"
        and rows(env, http_resources)[-1]["last_processed_at"] is None
    )


def test_new_cache_io_and_parser_stay_outside_transactions(state_env, monkeypatch):
    env = state_env
    active = set()

    @sa.event.listens_for(env.engine, "begin")
    def begin(connection):
        active.add(connection)

    @sa.event.listens_for(env.engine, "commit")
    @sa.event.listens_for(env.engine, "rollback")
    def end(connection):
        active.discard(connection)

    original_read, original_archive = env.store.read, env.store.archive

    def read(*args):
        assert not active
        return original_read(*args)

    def archive(*args):
        assert not active
        return original_archive(*args)

    def parser(page):
        assert not active
        return parse_list(page)

    monkeypatch.setattr(env.store, "read", read)
    monkeypatch.setattr(env.store, "archive", archive)
    original = record_response(env.engine, env.store, evidence(), body())
    candidate = select_cache_candidate(env.engine, env.store, SOURCE, HOME, PROFILE).candidate
    observed = record_response(
        env.engine, env.store, evidence(status_code=304, fetched_at=101), None, candidate=candidate
    )
    result = process_cached_response(env.engine, env.store, observed, 102, list_parser=parser)
    assert result.body_response_id == original


def test_complete_scan_requires_ordered_full_evidence_and_explicit_recheck(state_env):
    valid = completion()
    for changes in (
        {"pages": valid.pages[:1]},
        {"pages": tuple(reversed(valid.pages))},
        {"home_recheck_unchanged": False},
        {"home_recheck_unchanged": 1},
    ):
        with pytest.raises(ValidationError):
            ScanCompletion(**(valid.model_dump() | changes))
    with state_env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "run", 100, origin="bootstrap")
    with pytest.raises(IngestError), state_env.engine.begin() as connection:
        record_coverage_in_transaction(connection, "run", "complete", 101)
    assert read_source_state(state_env.engine, SOURCE)["last_complete_scan_at"] is None


def test_run_rule_snapshot_cannot_be_mixed_with_another_rule(state_env):
    env = state_env
    with env.engine.begin() as connection:
        start_run_in_transaction(
            connection, SOURCE, "run", 99, origin="bootstrap", parser_version="other-rules"
        )
    response = record_response(env.engine, env.store, evidence(), body())
    with pytest.raises(IngestError, match="run_parser_or_source_mismatch"):
        process_cached_response(env.engine, env.store, response, 101, ingestion_run_id="run")
    assert rows(env, documents) == [] and rows(env, http_resources)[0]["last_processed_at"] is None


def test_metadata_fetch_failure_and_cooldown_can_share_a_transaction(state_env):
    env = state_env
    import_page(env.engine, env.store, evidence(), body(), 101)
    prior = import_page(
        env.engine,
        env.store,
        evidence(True, fetched_at=102),
        body("current-notice-detail.html"),
        103,
        next_due_at=150,
    )
    response = record_response(
        env.engine, env.store, evidence(True, fetched_at=104, body_state="unavailable"), None
    )
    error = IngestError("http_read_failed", "fetch", response_id=response)
    with pytest.raises(RuntimeError), env.engine.begin() as connection:
        record_failure_in_transaction(connection, error, 105, failure_due_at=250)
        set_cooldown_in_transaction(connection, SOURCE, 500)
        raise RuntimeError("injected")
    assert target(env)["status"] == "processed" and target(env)["next_due_at"] == 150
    assert read_source_state(env.engine, SOURCE)["not_before_at"] is None
    with env.engine.begin() as connection:
        record_failure_in_transaction(connection, error, 106, failure_due_at=250)
        set_cooldown_in_transaction(connection, SOURCE, 500)
    assert (
        target(env)["status"] == "failed" and target(env)["current_version_id"] == prior.version_id
    )
    assert target(env)["next_due_at"] == 250
    assert read_source_state(env.engine, SOURCE)["not_before_at"] == 500
    assert rows(env, raw_responses)[-1]["last_error_code"] == "http_read_failed"
