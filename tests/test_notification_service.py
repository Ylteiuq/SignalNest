"""N1 recovery semantics through real raw files, Parser, cache and SQLite transactions.

The complete-scan attestation in setup seeds an already proved coverage fact; these
tests do not claim to run a website traversal or send any mail.
"""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError

from signalnest.cache import select_cache_candidate
from signalnest.contracts import PageInput, PaginationEvidence, RequestProfile
from signalnest.ingestion import (
    IngestError,
    ResponseInput,
    import_page,
    process_cached_response,
    process_response,
    record_response,
    save_notice_in_transaction,
)
from signalnest.ingestion_state import (
    ScanCompletion,
    finish_run_in_transaction,
    record_coverage_in_transaction,
    start_run_in_transaction,
)
from signalnest.notifications.contracts import Profile
from signalnest.notifications.service import prepare_notification
from signalnest.notifications.state import ActivationOptions, activate_notifications
from signalnest.parsing import PARSER_VERSION, parse_notice
from signalnest.schema import (
    documents,
    email_outbox,
    http_resources,
    notice_versions,
    notification_activation_members,
    notification_channel_state,
    notification_decisions,
    notification_events,
    notification_listing_evidence,
    notification_observations,
    raw_responses,
)
from signalnest.storage import open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "research/fixtures"
SOURCE = "whu-undergrad-student"
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
NOTICE = "https://uc.whu.edu.cn/info/1517/128231.htm"
LEGACY = "https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=127581"
NEW_NOTICE = "https://uc.whu.edu.cn/info/1517/999999.htm"
NEW_IDENTITY = "1517:999999"
SHANGHAI = ZoneInfo("Asia/Shanghai")
ACTIVATED_AT = int(datetime(2026, 9, 4, 12, tzinfo=SHANGHAI).timestamp())
REQUEST_PROFILE = RequestProfile(user_agent="SignalNest/N1-offline-test", accept="text/html")
USER_PROFILE = Profile(
    institution="whu",
    study_level="undergraduate",
    interest_topics=("exchange", "course_enrollment"),
    high_value_topics=("exchange",),
)
OPTIONS = ActivationOptions(
    activation_id="test-activation",
    sender="sender@example.org",
    recipient="recipient@example.org",
)


def fixture(name="current-notice-detail.html"):
    return (FIXTURES / name).read_bytes()


def evidence(at, *, kind="notice", url=NOTICE, identity="1517:128231", **changes):
    values = {
        "source_id": SOURCE,
        "page_type": kind,
        "source_document_id": identity if kind == "notice" else None,
        "requested_url": url,
        "final_url": url,
        "fetched_at": at,
        "status_code": 200,
        "etag": '"test-content"',
        "request_profile": REQUEST_PROFILE,
    }
    return ResponseInput(**(values | changes))


def rows(env, table):
    with env.engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table)).mappings()]


def document(env, identity="1517:128231"):
    return next(row for row in rows(env, documents) if row["source_document_id"] == identity)


def observation(env, identity="1517:128231"):
    doc = document(env, identity)
    return next(
        row for row in rows(env, notification_observations) if row["document_id"] == doc["id"]
    )


def member(env, identity="1517:128231"):
    return next(
        row
        for row in rows(env, notification_activation_members)
        if row["source_document_id"] == identity
    )


def start_live(env, *, at=ACTIVATED_AT + 1, parser_version=PARSER_VERSION, origin="regular"):
    run_id = f"live-{at}-{len(rows(env, raw_responses))}"
    with env.engine.begin() as connection:
        start_run_in_transaction(
            connection, SOURCE, run_id, at, origin=origin, parser_version=parser_version
        )
    env.run_id = run_id
    return run_id


def discover_historical(env):
    for name, url in (
        ("student-notices-page1.html", HOME),
        ("student-notices-page2.html", "https://uc.whu.edu.cn/tzgg/xstz/23.htm"),
    ):
        import_page(
            env.engine,
            env.store,
            evidence(ACTIVATED_AT - 100, kind="list", url=url),
            fixture(name),
            ACTIVATED_AT - 99,
        )


def complete_scan_fact(env):
    with env.engine.begin() as connection:
        start_run_in_transaction(
            connection, SOURCE, "bootstrap", ACTIVATED_AT - 10, origin="bootstrap"
        )
        record_coverage_in_transaction(
            connection,
            "bootstrap",
            "complete",
            ACTIVATED_AT - 5,
            completion=ScanCompletion(
                pages=(
                    PaginationEvidence(
                        current_page=1,
                        total_pages=1,
                        is_last_page=True,
                        terminal_evidence="disabled_next_and_last",
                    ),
                ),
                home_recheck_unchanged=True,
            ),
        )
        finish_run_in_transaction(connection, "bootstrap", "partial_failure", ACTIVATED_AT - 4)


@pytest.fixture
def activated(state_env):
    env = state_env
    discover_historical(env)
    complete_scan_fact(env)
    activate_notifications(env.engine, SOURCE, USER_PROFILE, OPTIONS, at=ACTIVATED_AT)
    start_live(env)
    return env


def live(
    env,
    content=None,
    *,
    at=ACTIVATED_AT + 2,
    url=NOTICE,
    identity="1517:128231",
    parser=parse_notice,
):
    response_id = record_response(
        env.engine,
        env.store,
        evidence(at, url=url, identity=identity),
        fixture() if content is None else content,
    )
    result = process_cached_response(
        env.engine,
        env.store,
        response_id,
        at + 1,
        processing_origin="live",
        ingestion_run_id=env.run_id,
        notice_parser=parser,
        next_due_at=at + 100,
    )
    return result


def opportunity():
    tree = BeautifulSoup(fixture(), "html.parser")
    tree.select_one(".title_nei > b").string = "国际交流项目报名通知"
    tree.select_one(".title_nei > i").string = "时间：2026-09-04"
    body = tree.select_one("#vsb_content > .v_news_content")
    body.clear()
    paragraph = tree.new_tag("p")
    paragraph.string = (
        "面向武汉大学本科生，现启动国际交流项目报名。"
        "报名时间：2026年9月4日9:00至2026年9月4日18:00。"
    )
    body.append(paragraph)
    return str(tree).encode()


def new_listing():
    tree = BeautifulSoup(fixture("student-notices-page1.html"), "html.parser")
    anchor = tree.select_one("div.list_txt > ul.am-list > li > a")
    anchor["href"] = "/info/1517/999999.htm"
    anchor.select_one("span").string = "国际交流项目报名通知"
    anchor.select_one("i").string = "2026-09-04"
    return str(tree).encode()


def live_discover(env, content, *, at=ACTIVATED_AT + 3, processing_origin="live"):
    response_id = record_response(
        env.engine, env.store, evidence(at, kind="list", url=HOME), content
    )
    return process_cached_response(
        env.engine,
        env.store,
        response_id,
        at + 1,
        processing_origin=processing_origin,
        ingestion_run_id=env.run_id,
    )


def test_activation_recent_once_and_duplicate_200_304_keep_actual_evidence(activated):
    env = activated
    first = live(env)
    original_decision = rows(env, notification_decisions)[0]
    second = live(env, at=ACTIVATED_AT + 5)
    assert second.version_id == first.version_id
    assert len(rows(env, notice_versions)) == 1
    assert len(rows(env, notification_events)) == 1
    assert rows(env, notification_events)[0]["kind"] == "activation_recent"
    assert member(env)["candidate_state"] == "generated"
    assert rows(env, notice_versions)[0]["raw_response_id"] == first.response_id
    assert observation(env)["body_response_id"] == second.response_id
    candidate = select_cache_candidate(
        env.engine,
        env.store,
        SOURCE,
        NOTICE,
        REQUEST_PROFILE,
        page_type="notice",
        source_document_id="1517:128231",
    ).candidate
    observed_id = record_response(
        env.engine,
        env.store,
        evidence(ACTIVATED_AT + 8, status_code=304),
        None,
        candidate=candidate,
    )
    result = process_cached_response(
        env.engine,
        env.store,
        observed_id,
        ACTIVATED_AT + 9,
        processing_origin="live",
        ingestion_run_id=env.run_id,
    )
    assert result.version_id == first.version_id
    assert observation(env)["body_response_id"] == second.response_id
    assert observation(env)["observed_response_id"] == observed_id
    assert len(rows(env, notification_events)) == len(rows(env, notification_decisions)) == 1
    assert rows(env, notification_decisions)[0] == original_decision
    response = next(row for row in rows(env, raw_responses) if row["id"] == observed_id)
    assert response["body_path"] is None and response["fetched_at"] == ACTIVATED_AT + 8
    assert (
        next(row for row in rows(env, raw_responses) if row["id"] == second.response_id)[
            "fetched_at"
        ]
        == ACTIVATED_AT + 5
    )


def test_a_b_a_reuses_content_version_but_creates_two_distinct_updates(activated):
    env = activated
    a = live(env)
    b = live(env, fixture().replace(b"2026-2027", b"2026-2028"), at=ACTIVATED_AT + 5)
    restored = live(env, at=ACTIVATED_AT + 8)
    assert a.version_id == restored.version_id != b.version_id
    assert len(rows(env, notice_versions)) == 2
    events = sorted(rows(env, notification_events), key=lambda row: row["event_seq"])
    assert [event["kind"] for event in events] == ["activation_recent", "update", "update"]
    assert [event["event_seq"] for event in events] == [1, 2, 3]
    assert events[2]["previous_version_id"] == b.version_id
    assert events[2]["version_id"] == a.version_id
    assert events[2]["body_response_id"] == restored.response_id
    assert observation(env)["event_seq"] == 3


@pytest.mark.parametrize("origin", ["offline", "maintenance"])
def test_non_live_reparse_changes_business_current_without_polluting_live_baseline(
    activated, origin
):
    env = activated
    old = live(env)
    updated = live(env, fixture().replace(b"2026-2027", b"2026-2028"), at=ACTIVATED_AT + 5)
    baseline = observation(env)
    replay = process_response(
        env.engine, env.store, old.response_id, ACTIVATED_AT + 8, processing_origin=origin
    )
    assert replay.version_id == old.version_id
    assert document(env)["current_version_id"] == old.version_id
    assert observation(env) == baseline
    repeat = live(env, fixture().replace(b"2026-2027", b"2026-2028"), at=ACTIVATED_AT + 10)
    assert repeat.version_id == updated.version_id
    assert len(rows(env, notification_events)) == 2
    assert document(env)["current_version_id"] == updated.version_id


def test_initial_and_recheck_parse_failure_preserve_candidate_and_success(activated):
    env = activated
    with pytest.raises(IngestError, match="parse_missing_structure"):
        live(env, b"<html>bad page</html>")
    assert rows(env, notification_observations) == rows(env, notification_events) == []
    assert member(env)["candidate_state"] == "selected"
    success = live(env, at=ACTIVATED_AT + 5)
    previous = observation(env)
    with pytest.raises(IngestError, match="parse_missing_structure"):
        live(env, b"<html>bad recheck</html>", at=ACTIVATED_AT + 8)
    assert observation(env) == previous
    assert document(env)["current_version_id"] == success.version_id
    assert document(env)["status"] == "failed"
    assert len(rows(env, notification_events)) == 1


def test_recent_candidate_keeps_fixed_activation_window_after_long_backlog(activated):
    env = activated
    delayed_at = ACTIVATED_AT + int(timedelta(days=8).total_seconds())
    start_live(env, at=delayed_at)
    live(env, at=delayed_at + 1)
    assert member(env)["candidate_state"] == "generated"
    event = rows(env, notification_events)[0]
    assert event["kind"] == "activation_recent"
    assert event["occurred_at"] > ACTIVATED_AT + 7 * 86400


def test_historical_member_establishes_silent_baseline(activated):
    env = activated
    result = live(env, fixture("legacy-notice-detail.html"), url=LEGACY, identity="1517:127581")
    assert result.version_id
    assert observation(env, "1517:127581")["event_seq"] == 0
    assert member(env, "1517:127581")["candidate_state"] == "not_recent"
    assert (
        rows(env, notification_events)
        == rows(env, notification_decisions)
        == rows(env, email_outbox)
        == []
    )


def test_only_actual_regular_live_discovery_outside_activation_set_is_new(activated):
    env = activated
    before = len(rows(env, notification_activation_members))
    live_discover(env, new_listing())
    live(env, opportunity(), url=NEW_NOTICE, identity=NEW_IDENTITY, at=ACTIVATED_AT + 6)
    assert len(rows(env, notification_activation_members)) == before
    event = rows(env, notification_events)[0]
    assert event["kind"] == "new"
    proof = next(
        row
        for row in rows(env, notification_listing_evidence)
        if row["document_id"] == document(env, NEW_IDENTITY)["id"]
    )
    assert proof["live_discovered_run_id"] == env.run_id
    assert proof["live_discovered_at"] == ACTIVATED_AT + 4


@pytest.mark.parametrize("origin", ["offline", "bootstrap", "historical"])
def test_regular_detail_does_not_turn_unknown_historical_discovery_into_new(activated, origin):
    env = activated
    if origin == "offline":
        live_discover(env, new_listing(), processing_origin="offline")
    else:
        start_live(env, at=ACTIVATED_AT + 2, origin=origin)
        live_discover(env, new_listing())
    start_live(env, at=ACTIVATED_AT + 5)
    live(env, opportunity(), url=NEW_NOTICE, identity=NEW_IDENTITY, at=ACTIVATED_AT + 6)
    assert observation(env, NEW_IDENTITY)["event_seq"] == 0
    assert rows(env, notification_events) == []


def test_parser_upgrade_same_raw_silently_advances_live_version(activated):
    env = activated
    first = live(env)

    def newer(page):
        return parse_notice(page).model_copy(update={"parser_version": "test-parser-v2"})

    start_live(env, at=ACTIVATED_AT + 5, parser_version="test-parser-v2")
    second = live(env, at=ACTIVATED_AT + 6, parser=newer)
    assert first.version_id != second.version_id
    assert len(rows(env, notification_events)) == 1
    assert observation(env)["version_id"] == second.version_id
    assert observation(env)["comparison_error_code"] is None


def test_parser_and_raw_change_reparse_old_actual_body_with_current_rules(activated):
    env = activated
    first = live(env)
    second_bytes = fixture().replace(b"</body>", b"<div>second response evidence</div></body>")
    assert second_bytes != fixture()
    second = live(env, second_bytes, at=ACTIVATED_AT + 5)
    # A changed exterior has identical normalized content, but different raw bytes.
    assert second.version_id == first.version_id
    assert observation(env)["body_response_id"] == second.response_id
    old_response = next(row for row in rows(env, raw_responses) if row["id"] == first.response_id)
    new_bytes = fixture().replace(b"2026-2027", b"2026-2028")
    calls = []

    def newer(page):
        calls.append(page.content)
        # Demonstrate a changed extraction rule with equal newly normalized results.
        parsed = parse_notice(page)
        text = parsed.content.body_text.replace("2026-2027", "2026-2028")
        html = parsed.content.body_html.replace("2026-2027", "2026-2028")
        title = parsed.content.title.replace("2026-2027", "2026-2028")
        return parsed.model_copy(
            update={
                "parser_version": "test-parser-v2",
                "content": parsed.content.model_copy(
                    update={"title": title, "body_text": text, "body_html": html}
                ),
            }
        )

    start_live(env, at=ACTIVATED_AT + 8, parser_version="test-parser-v2")
    result = live(env, new_bytes, at=ACTIVATED_AT + 9, parser=newer)
    assert calls == [new_bytes, second_bytes]
    assert len(rows(env, notification_events)) == 1
    assert observation(env)["version_id"] == result.version_id
    assert observation(env)["comparison_error_code"] is None
    assert (
        old_response["body_path"]
        != next(row for row in rows(env, raw_responses) if row["id"] == second.response_id)[
            "body_path"
        ]
    )


@pytest.mark.parametrize("fault", ["missing", "corrupt", "parser_rejects"])
def test_unverifiable_old_body_silently_advances_with_comparison_diagnostic(activated, fault):
    env = activated
    first = live(env)
    body_row = next(row for row in rows(env, raw_responses) if row["id"] == first.response_id)
    old_path = env.settings.data_dir / body_row["body_path"]
    if fault == "missing":
        old_path.unlink()
    elif fault == "corrupt":
        old_path.write_bytes(b"damaged")
    new_bytes = fixture().replace(b"2026-2027", b"2026-2028")

    def newer(page):
        if fault == "parser_rejects" and page.content == fixture():
            from signalnest.parsing import ParseError, ParseErrorCode

            raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="body")
        return parse_notice(page).model_copy(update={"parser_version": "test-parser-v2"})

    start_live(env, at=ACTIVATED_AT + 5, parser_version="test-parser-v2")
    result = live(env, new_bytes, at=ACTIVATED_AT + 6, parser=newer)
    baseline = observation(env)
    assert baseline["version_id"] == result.version_id
    assert baseline["comparison_error_code"].startswith("comparison_")
    assert baseline["comparison_error_at"] == ACTIVATED_AT + 7
    assert len(rows(env, notification_events)) == 1


@pytest.mark.parametrize(
    "table_name", ["notification_events", "notification_decisions", "email_outbox"]
)
def test_notification_insert_fault_rolls_back_entire_business_group(activated, table_name):
    env = activated
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER reject_notification BEFORE INSERT ON {table_name} "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError, match="database_write_failed"):
        live(env, opportunity())
    failed = document(env)
    assert failed["status"] == "failed"
    assert failed["current_version_id"] is None and failed["last_success_at"] is None
    assert rows(env, notice_versions) == []
    assert rows(env, notification_observations) == []
    assert rows(env, notification_events) == []
    assert rows(env, notification_decisions) == []
    assert rows(env, email_outbox) == []
    assert member(env)["candidate_state"] == "selected"
    resource = next(row for row in rows(env, http_resources) if row["request_uri"] == NOTICE)
    assert resource["latest_response_id"] is not None
    assert resource["last_processed_response_id"] is None
    response = next(
        row for row in rows(env, raw_responses) if row["id"] == resource["latest_response_id"]
    )
    assert (
        response["body_path"] is not None and response["last_error_code"] == "database_write_failed"
    )
    assert env.store.read(response["body_path"], response["body_sha256"]) == opportunity()


def test_live_low_level_success_cannot_bypass_prepare(activated):
    env = activated
    at = ACTIVATED_AT + 2
    response_id = record_response(env.engine, env.store, evidence(at), fixture())
    parsed = parse_notice(PageInput(content=fixture(), page_url=NOTICE))
    with (
        pytest.raises(IngestError, match="notification_prepared_required"),
        env.engine.begin() as connection,
    ):
        save_notice_in_transaction(
            connection,
            response_id,
            parsed,
            at + 1,
            processing_origin="live",
            ingestion_run_id=env.run_id,
        )
    assert rows(env, notice_versions) == rows(env, notification_events) == []
    assert document(env)["current_version_id"] is None


def test_prepared_snapshot_rejects_changed_mode_before_business_commit(activated):
    env = activated
    at = ACTIVATED_AT + 2
    response_id = record_response(env.engine, env.store, evidence(at), fixture())
    parsed = parse_notice(PageInput(content=fixture(), page_url=NOTICE))
    prepared = prepare_notification(
        env.engine,
        env.store,
        notice=parsed,
        body_response_id=response_id,
        observed_response_id=response_id,
        processing_origin="live",
        ingestion_run_id=env.run_id,
        evaluated_at=at + 1,
    )
    with env.engine.begin() as connection:
        connection.execute(
            notification_channel_state.update().values(notification_mode="digest_only")
        )
    with (
        pytest.raises(IngestError, match="notification_prepare_stale"),
        env.engine.begin() as connection,
    ):
        save_notice_in_transaction(
            connection,
            response_id,
            parsed,
            at + 1,
            processing_origin="live",
            prepared_notification=prepared,
            ingestion_run_id=env.run_id,
        )
    assert rows(env, notice_versions) == rows(env, notification_events) == []
    assert document(env)["current_version_id"] is None


def test_selected_decision_and_outbox_cannot_reference_another_event(activated):
    env = activated
    live(env, opportunity())
    live(
        env, opportunity().replace("交流项目".encode(), "交流项目二".encode()), at=ACTIVATED_AT + 5
    )
    events = sorted(rows(env, notification_events), key=lambda row: row["event_seq"])
    assert len(events) == 2
    assert events[0]["outbox_id"] is not None
    with pytest.raises(IntegrityError), env.engine.begin() as connection:
        connection.execute(
            notification_events.update()
            .where(notification_events.c.id == events[1]["id"])
            .values(selected_decision_id=events[0]["selected_decision_id"])
        )
    with pytest.raises(IntegrityError), env.engine.begin() as connection:
        connection.execute(
            email_outbox.update()
            .where(email_outbox.c.id == events[0]["outbox_id"])
            .values(decision_id=events[1]["selected_decision_id"])
        )


def test_reopen_preserves_live_baseline_decisions_and_pending_candidate(activated):
    env = activated
    first = live(env)
    baseline = observation(env)
    decisions = rows(env, notification_decisions)
    env.engine.dispose()
    env.engine = open_initialized_engine(env.settings.database)
    assert observation(env) == baseline
    assert rows(env, notification_decisions) == decisions
    repeat = live(env, at=ACTIVATED_AT + 5)
    assert repeat.version_id == first.version_id
    assert len(rows(env, notification_events)) == 1


def test_run_finish_failure_does_not_erase_committed_event(activated):
    env = activated
    live(env)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_run_finish BEFORE UPDATE ON ingestion_runs "
            "WHEN NEW.result != 'running' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IntegrityError), env.engine.begin() as connection:
        finish_run_in_transaction(connection, env.run_id, "failed", ACTIVATED_AT + 5)
    assert document(env)["status"] == "processed"
    assert len(rows(env, notification_events)) == len(rows(env, notification_decisions)) == 1
    assert member(env)["candidate_state"] == "generated"


def test_digest_route_is_qualified_without_immediate_outbox(activated):
    env = activated
    # Course information is not silently converted to an immediate email intent.
    live(env)
    event = rows(env, notification_events)[0]
    decision = rows(env, notification_decisions)[0]
    assert event["selected_decision_id"] == decision["id"]
    assert event["effective_route"] == decision["decision"]["effective_route"]
    assert event["effective_route"] == "digest"
    assert event["delivery_intent_registered_at"] == ACTIVATED_AT + 3
    assert event["outbox_id"] is None
    assert "body_text" not in decision["facts"]
    assert decision["context"]["previous_facts"] is None


def test_failed_complete_200_can_first_succeed_via_bound_304(activated):
    env = activated
    at = ACTIVATED_AT + 2
    body_id = record_response(env.engine, env.store, evidence(at), fixture())

    def rejected(page):
        from signalnest.parsing import ParseError, ParseErrorCode

        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="body")

    with pytest.raises(IngestError, match="parse_missing_structure"):
        process_cached_response(
            env.engine,
            env.store,
            body_id,
            at + 1,
            processing_origin="live",
            ingestion_run_id=env.run_id,
            notice_parser=rejected,
        )
    candidate = select_cache_candidate(
        env.engine, env.store, SOURCE, NOTICE, REQUEST_PROFILE
    ).candidate
    observed_id = record_response(
        env.engine, env.store, evidence(at + 3, status_code=304), None, candidate=candidate
    )
    process_cached_response(
        env.engine,
        env.store,
        observed_id,
        at + 4,
        processing_origin="live",
        ingestion_run_id=env.run_id,
    )
    assert len(rows(env, notification_events)) == 1
    assert observation(env)["body_response_id"] == body_id
    assert observation(env)["observed_response_id"] == observed_id
    original = next(row for row in rows(env, raw_responses) if row["id"] == body_id)
    assert original["fetched_at"] == at
    assert member(env)["candidate_state"] == "generated"


def test_later_true_list_date_promotes_silent_member_even_if_body_unchanged(activated):
    env = activated
    live(env, fixture("legacy-notice-detail.html"), url=LEGACY, identity="1517:127581")
    assert observation(env, "1517:127581")["event_seq"] == 0
    tree = BeautifulSoup(fixture("student-notices-page2.html"), "html.parser")
    matching = next(
        anchor
        for anchor in tree.select("div.list_txt > ul.am-list > li > a")
        if "127581" in anchor["href"]
    )
    matching.select_one("i").string = "2026-09-04"
    at = ACTIVATED_AT + 5
    url = "https://uc.whu.edu.cn/tzgg/xstz/23.htm"
    response_id = record_response(
        env.engine, env.store, evidence(at, kind="list", url=url), str(tree).encode()
    )
    process_cached_response(
        env.engine,
        env.store,
        response_id,
        at + 1,
        processing_origin="live",
        ingestion_run_id=env.run_id,
    )
    assert member(env, "1517:127581")["candidate_state"] == "selected"
    assert member(env, "1517:127581")["date_conflict"]
    live(
        env,
        fixture("legacy-notice-detail.html"),
        url=LEGACY,
        identity="1517:127581",
        at=at + 3,
    )
    event = rows(env, notification_events)[0]
    assert event["kind"] == "activation_recent"
    assert observation(env, "1517:127581")["event_seq"] == 1
    decision = rows(env, notification_decisions)[0]
    assert decision["context"]["activation_date_conflict"]
    assert member(env, "1517:127581")["candidate_state"] == "generated"


@pytest.mark.parametrize(
    "table_name", ["notification_events", "notification_decisions", "email_outbox"]
)
def test_notification_recheck_commit_fault_preserves_all_previous_success(activated, table_name):
    env = activated
    first = live(env, opportunity())
    baseline = observation(env)
    original_event = rows(env, notification_events)
    original_decision = rows(env, notification_decisions)
    original_intent = rows(env, email_outbox)
    assert original_intent
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER reject_notification BEFORE INSERT ON {table_name} "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    changed = opportunity().replace(b"18:00", b"17:00")
    with pytest.raises(IngestError, match="database_write_failed"):
        live(env, changed, at=ACTIVATED_AT + 5)
    assert document(env)["current_version_id"] == first.version_id
    assert document(env)["last_success_at"] == ACTIVATED_AT + 3
    assert document(env)["status"] == "failed"
    assert observation(env) == baseline
    assert len(rows(env, notice_versions)) == 1
    assert rows(env, notification_events) == original_event
    assert rows(env, notification_decisions) == original_decision
    assert rows(env, email_outbox) == original_intent
    resource = next(row for row in rows(env, http_resources) if row["request_uri"] == NOTICE)
    assert resource["last_processed_response_id"] == first.response_id
    assert resource["latest_response_id"] != first.response_id


def test_absent_channel_prepare_token_rejects_activation_race(state_env):
    env = state_env
    discover_historical(env)
    complete_scan_fact(env)
    start_live(env)
    at = ACTIVATED_AT + 2
    response_id = record_response(env.engine, env.store, evidence(at), fixture())
    parsed = parse_notice(PageInput(content=fixture(), page_url=NOTICE))
    prepared = prepare_notification(
        env.engine,
        env.store,
        notice=parsed,
        body_response_id=response_id,
        observed_response_id=response_id,
        processing_origin="live",
        ingestion_run_id=env.run_id,
        evaluated_at=at + 1,
    )
    assert prepared.silent_reason == "not_enabled"
    activate_notifications(env.engine, SOURCE, USER_PROFILE, OPTIONS, at=at + 1)
    with (
        pytest.raises(IngestError, match="notification_prepare_stale"),
        env.engine.begin() as connection,
    ):
        save_notice_in_transaction(
            connection,
            response_id,
            parsed,
            at + 1,
            processing_origin="live",
            prepared_notification=prepared,
            ingestion_run_id=env.run_id,
        )
    assert rows(env, notice_versions) == rows(env, notification_observations) == []


def test_prior_selected_decision_change_invalidates_prepare_even_if_baseline_same(activated):
    env = activated
    first = live(env)
    at = ACTIVATED_AT + 5
    changed_bytes = fixture().replace(b"2026-2027", b"2026-2028")
    response_id = record_response(env.engine, env.store, evidence(at), changed_bytes)
    parsed = parse_notice(PageInput(content=changed_bytes, page_url=NOTICE))
    prepared = prepare_notification(
        env.engine,
        env.store,
        notice=parsed,
        body_response_id=response_id,
        observed_response_id=response_id,
        processing_origin="live",
        ingestion_run_id=env.run_id,
        evaluated_at=at + 1,
    )
    old = rows(env, notification_decisions)[0]
    values = {key: value for key, value in old.items() if key != "id"}
    values["evaluation_key"] = "test-explicit-selection"
    with env.engine.begin() as connection:
        new_id = connection.execute(
            notification_decisions.insert().values(**values)
        ).inserted_primary_key[0]
        connection.execute(
            notification_events.update()
            .where(notification_events.c.id == old["event_id"])
            .values(selected_decision_id=new_id)
        )
    previous = observation(env)
    with (
        pytest.raises(IngestError, match="notification_prepare_stale"),
        env.engine.begin() as connection,
    ):
        save_notice_in_transaction(
            connection,
            response_id,
            parsed,
            at + 1,
            processing_origin="live",
            prepared_notification=prepared,
            ingestion_run_id=env.run_id,
        )
    assert observation(env) == previous
    assert document(env)["current_version_id"] == first.version_id
    assert len(rows(env, notice_versions)) == len(rows(env, notification_events)) == 1


def test_paused_channel_still_registers_content_event_and_intent(activated):
    env = activated
    with env.engine.begin() as connection:
        connection.execute(notification_channel_state.update().values(paused=True))
    live(env, opportunity())
    assert len(rows(env, notification_events)) == len(rows(env, email_outbox)) == 1
    assert rows(env, email_outbox)[0]["state"] == "planned"
    assert rows(env, notification_channel_state)[0]["paused"]


def test_digest_only_routes_urgent_opportunity_without_immediate_intent(activated):
    env = activated
    with env.engine.begin() as connection:
        connection.execute(
            notification_channel_state.update().values(notification_mode="digest_only")
        )
    live(env, opportunity())
    event = rows(env, notification_events)[0]
    decision = rows(env, notification_decisions)[0]
    assert decision["decision"]["action"] == "PUSH_NOW"
    assert event["effective_route"] == "digest"
    assert event["delivery_intent_registered_at"] is not None
    assert rows(env, email_outbox) == []


def test_ignored_content_event_is_retained_with_decision_evidence(state_env):
    env = state_env
    discover_historical(env)
    complete_scan_fact(env)
    activate_notifications(
        env.engine,
        SOURCE,
        Profile(interest_topics=("exchange",), exclude_topics=("course_enrollment",)),
        OPTIONS,
        at=ACTIVATED_AT,
    )
    start_live(env)
    live(env)
    event = rows(env, notification_events)[0]
    decision = rows(env, notification_decisions)[0]
    assert decision["decision"]["action"] == "IGNORE"
    assert event["effective_route"] == "none"
    assert event["delivery_intent_registered_at"] is None
    assert rows(env, email_outbox) == []
    live(env, fixture().replace(b"2026-2027", b"2026-2028"), at=ACTIVATED_AT + 5)
    assert [event["kind"] for event in rows(env, notification_events)] == [
        "activation_recent",
        "update",
    ]
    assert all(row["decision"]["action"] == "IGNORE" for row in rows(env, notification_decisions))


def test_archive_reads_parser_and_rules_run_outside_business_transactions(activated, monkeypatch):
    env = activated
    live(env)
    active_transactions = 0

    def started(connection):
        nonlocal active_transactions
        active_transactions += 1

    def finished(connection):
        nonlocal active_transactions
        active_transactions -= 1

    from signalnest.notifications import service

    original_read = env.store.read
    original_extract = service.extract_facts

    def read(*args, **kwargs):
        assert active_transactions == 0
        return original_read(*args, **kwargs)

    def facts(*args, **kwargs):
        assert active_transactions == 0
        return original_extract(*args, **kwargs)

    def newer(page):
        assert active_transactions == 0
        return parse_notice(page).model_copy(update={"parser_version": "test-parser-v2"})

    start_live(env, at=ACTIVATED_AT + 5, parser_version="test-parser-v2")
    monkeypatch.setattr(env.store, "read", read)
    monkeypatch.setattr(service, "extract_facts", facts)
    listeners = [("begin", started), ("commit", finished), ("rollback", finished)]
    for name, callback in listeners:
        sa.event.listen(env.engine, name, callback)
    try:
        live(env, fixture().replace(b"2026-2027", b"2026-2028"), at=ACTIVATED_AT + 6, parser=newer)
    finally:
        for name, callback in listeners:
            sa.event.remove(env.engine, name, callback)
    assert active_transactions == 0
    assert len(rows(env, notification_events)) == 2


def test_comparison_unknown_survives_unchanged_bound_304_with_original_time(activated):
    env = activated
    first = live(env)
    old_body = next(row for row in rows(env, raw_responses) if row["id"] == first.response_id)
    (env.settings.data_dir / old_body["body_path"]).unlink()

    def newer(page):
        return parse_notice(page).model_copy(update={"parser_version": "test-parser-v2"})

    start_live(env, at=ACTIVATED_AT + 5, parser_version="test-parser-v2")
    updated = live(
        env,
        fixture().replace(b"2026-2027", b"2026-2028"),
        at=ACTIVATED_AT + 6,
        parser=newer,
    )
    before = observation(env)
    assert before["comparison_error_code"] == "comparison_raw_missing"
    assert before["comparison_error_at"] == ACTIVATED_AT + 7
    assert before["comparison_evidence"] == {
        "previous_version_id": first.version_id,
        "previous_body_response_id": first.response_id,
        "body_response_id": updated.response_id,
        "observed_response_id": updated.response_id,
        "parser_version": "test-parser-v2",
    }
    candidate = select_cache_candidate(
        env.engine, env.store, SOURCE, NOTICE, REQUEST_PROFILE
    ).candidate
    observed_id = record_response(
        env.engine,
        env.store,
        evidence(ACTIVATED_AT + 9, status_code=304),
        None,
        candidate=candidate,
    )
    result = process_cached_response(
        env.engine,
        env.store,
        observed_id,
        ACTIVATED_AT + 10,
        processing_origin="live",
        ingestion_run_id=env.run_id,
        notice_parser=newer,
    )
    after = observation(env)
    assert result.version_id == updated.version_id
    assert after["body_response_id"] == updated.response_id
    assert after["observed_response_id"] == observed_id
    assert after["observed_at"] == ACTIVATED_AT + 10
    assert after["comparison_error_code"] == before["comparison_error_code"]
    assert after["comparison_error_at"] == before["comparison_error_at"]
    assert after["comparison_evidence"] == before["comparison_evidence"]
    assert len(rows(env, notification_events)) == 1


def test_activation_selection_conflict_keep_exact_version_and_initial_event_refs(state_env):
    env = state_env
    discover_historical(env)
    # A future list date is not an activation candidate on its own.
    tree = BeautifulSoup(fixture("student-notices-page1.html"), "html.parser")
    anchor = next(
        item
        for item in tree.select("div.list_txt > ul.am-list > li > a")
        if "128231" in item["href"]
    )
    anchor.select_one("i").string = "2026-09-05"
    imported = import_page(
        env.engine,
        env.store,
        evidence(ACTIVATED_AT - 20, kind="list", url=HOME),
        str(tree).encode(),
        ACTIVATED_AT - 19,
    )
    complete_scan_fact(env)
    activate_notifications(env.engine, SOURCE, USER_PROFILE, OPTIONS, at=ACTIVATED_AT)
    assert member(env)["candidate_state"] == "unknown"
    start_live(env)
    first = live(env)
    selected = member(env)
    event_id = rows(env, notification_events)[0]["id"]
    assert selected["candidate_state"] == "generated"
    assert selected["selection_evidence"]["kind"] == "notice_version"
    assert selected["selection_evidence"]["published_date"] == "2026-09-04"
    assert selected["selection_evidence"]["version_id"] == first.version_id
    assert selected["selection_evidence"]["body_response_id"] == first.response_id
    assert selected["generated_event_id"] == event_id
    conflict = selected["conflict_evidence"]
    assert {item["published_date"] for item in conflict} == {"2026-09-04", "2026-09-05"}
    detail_evidence = next(item for item in conflict if item["kind"] == "notice_version")
    list_evidence = next(item for item in conflict if item["kind"] == "list_entry")
    assert detail_evidence["version_id"] == first.version_id
    assert list_evidence["body_response_id"] == imported.response_id
    live_discover(env, fixture("student-notices-page1.html"), at=ACTIVATED_AT + 5)
    replayed = member(env)
    assert replayed["list_evidence"]["published_date"] == "2026-09-04"
    assert replayed["date_conflict"]
    assert replayed["selection_evidence"] == selected["selection_evidence"]
    assert replayed["conflict_evidence"] == conflict
    second = live(
        env,
        fixture().replace(b"2026-2027", b"2026-2028"),
        at=ACTIVATED_AT + 8,
    )
    updated = member(env)
    assert second.version_id != first.version_id
    assert len(rows(env, notification_events)) == 2
    assert updated["generated_event_id"] == event_id
    assert updated["notice_evidence"]["version_id"] == second.version_id
    assert updated["selection_evidence"]["version_id"] == first.version_id
    assert updated["conflict_evidence"] == conflict
