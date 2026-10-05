"""Offline policy checks and real-coordinator backlog service using SQLite and fixtures.

Synthetic responses and clocks measure policy, not live throughput or calendar scheduling.
"""

import ssl
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from test_crawling import HOME, SECOND, SOURCE, THIRD, Clock, fixture, html, listing, rows
from test_crawling import settings as crawl_settings

from signalnest.config import RuntimeSettings
from signalnest.crawling import CrawlOptions, crawl_once
from signalnest.errors import IngestError
from signalnest.fetching import FetchLimits
from signalnest.ingestion import ResponseInput, import_page
from signalnest.runtime_policy import (
    apply_recheck_policy,
    apply_recheck_policy_in_transaction,
    failure_due,
    select_details,
    success_due,
)
from signalnest.schema import documents, notice_versions, raw_responses

AT = int(datetime(2026, 10, 5, 4, tzinfo=UTC).timestamp())
GROUPS = ("foreground", "history", "recheck")


def candidate(identifier, *, success=None, due=10, discovered=1, status=None):
    return {
        "id": identifier,
        "source_document_id": str(identifier),
        "last_success_at": success,
        "next_due_at": due,
        "discovered_at": discovered,
        "status": status or ("processed" if success is not None else "discovered"),
    }


def clock(at=AT):
    value = Clock()
    value.epoch = at
    return value


def crawl(env, handler, *, maximum=20, at=AT, config=None, **options):
    return crawl_once(
        config or crawl_settings(env),
        CrawlOptions(
            max_pages=1,
            max_details=maximum,
            fetch_limits=FetchLimits(max_requests=40, max_retries=0, run_seconds=120),
            **options,
        ),
        clock=clock(at),
        transport=httpx.MockTransport(handler),
    )


def seed_history(env, articles, *, at=AT):
    """Persist discovered facts directly; no fake successful versions or response evidence."""
    with env.engine.begin() as connection:
        connection.execute(
            documents.insert(),
            [
                {
                    "source_id": SOURCE,
                    "source_document_id": f"1517:{article}",
                    "detail_url": f"https://uc.whu.edu.cn/info/1517/{article}.htm",
                    "discovered_title": f"历史待办 {article}",
                    "discovered_at": at - 7200,
                    "discovery_origin": "historical",
                }
                for article in articles
            ],
        )


def seed_rechecks(env, articles, *, at=AT, failed=False):
    seed_history(env, articles, at=at)
    for article in articles:
        uri = f"https://uc.whu.edu.cn/info/1517/{article}.htm"
        import_page(
            env.engine,
            env.store,
            ResponseInput(
                page_type="notice",
                source_id=SOURCE,
                source_document_id=f"1517:{article}",
                requested_url=uri,
                final_url=uri,
                fetched_at=at - 3600,
                status_code=200,
            ),
            fixture("current-notice-detail.html"),
            at - 3599,
            next_due_at=at - 1,
        )
    values = {"next_due_at": at - 1}
    if failed:
        values.update(status="failed", last_attempt_at=at - 100, last_error_code="read_timeout")
    with env.engine.begin() as connection:
        connection.execute(
            documents.update()
            .where(documents.c.source_document_id.in_([f"1517:{article}" for article in articles]))
            .values(**values)
        )


def article_id(request):
    return int(request.url.path.rsplit("/", 1)[1].removesuffix(".htm"))


@pytest.mark.parametrize(
    "age,seconds", [(0, 86400), (7, 86400), (8, 604800), (30, 604800), (31, 2592000)]
)
def test_success_age_boundaries_use_processing_time(age, seconds):
    published = date(2026, 10, 5) - timedelta(days=age)
    assert success_due(published, AT, RuntimeSettings()) == (AT + seconds, False)


def test_success_uses_shanghai_calendar_and_future_is_near_term():
    # UTC October 4 is already October 5 in Shanghai; a September 27 article is eight days old.
    at = int(datetime(2026, 10, 4, 16, 30, tzinfo=UTC).timestamp())
    policy = RuntimeSettings()
    assert success_due(date(2026, 9, 27), at, policy) == (at + 604800, False)
    assert success_due(date(2026, 10, 6), at, policy) == (at + 86400, True)


@pytest.mark.parametrize(
    "code,status,seconds",
    [
        ("connect_timeout", None, 1800),
        ("read_error", None, 1800),
        ("http_transient", 503, 1800),
        ("http_forbidden", 403, 86400),
        ("http_unauthorized", 401, 86400),
        ("tls_error", None, 86400),
        ("parse_missing_structure", 200, 21600),
        ("identity_mismatch", 200, 21600),
        ("http_not_found", 404, 604800),
        ("http_status", 410, 604800),
    ],
)
def test_failure_class_intervals(code, status, seconds):
    assert failure_due(code, status, AT, RuntimeSettings()) == AT + seconds


@pytest.mark.parametrize("name", ["foreground_slots", "history_slots", "recheck_slots"])
def test_policy_rejects_nonpositive_group_reservations(name):
    with pytest.raises(ValidationError):
        RuntimeSettings(**{name: 0})


@pytest.mark.parametrize("maximum,expected", [(1, GROUPS[:1]), (2, GROUPS[:2]), (3, GROUPS)])
def test_small_batches_reserve_service_across_nonempty_groups(maximum, expected):
    items = [candidate(1), candidate(2), candidate(3, success=1)]
    selected = select_details(items, {"1"}, maximum, RuntimeSettings())
    assert tuple(group for group, _ in selected) == expected
    assert len({item["id"] for _, item in selected}) == maximum


def test_select_partitions_by_success_baseline_and_orders_due_stably():
    items = [
        candidate(4, success=1, status="failed"),
        candidate(2, due=None, discovered=5),
        candidate(3, due=5),
        candidate(1, due=100),
    ]
    selected = select_details(items, {"1", "4"}, 4, RuntimeSettings())
    assert [(group, item["id"]) for group, item in selected] == [
        ("foreground", 1),
        ("history", 2),
        ("recheck", 4),
        ("history", 3),
    ]
    assert len(items) == 4  # Selection has no side effect on the caller's list.


@pytest.mark.parametrize("group", GROUPS)
def test_empty_groups_lend_reserved_slots_without_duplicate_ids(group):
    items = [candidate(i, success=1 if group == "recheck" else None) for i in range(1, 26)]
    foreground = {str(i) for i in range(1, 26)} if group == "foreground" else set()
    selected = select_details(items, foreground, 20, RuntimeSettings())
    assert Counter(label for label, _ in selected) == {group: 20}
    assert [item["id"] for _, item in selected] == list(range(1, 21))


def test_new_visible_notice_precedes_one_hundred_older_history_tasks(state_env):
    env, calls = state_env, []
    seed_history(env, range(200000, 200100))

    def handler(request):
        if str(request.url) == HOME:
            return html(listing(ids=(900001,)))
        calls.append(article_id(request))
        return html(fixture("current-notice-detail.html"))

    result = crawl(env, handler)
    assert calls[0] == 900001
    assert len(calls) == len(set(calls)) == 20
    assert Counter({group: result.detail_groups[group].attempted for group in GROUPS}) == {
        "foreground": 1,
        "history": 19,
        "recheck": 0,
    }
    assert result.detail_groups["history"].remaining_due == 81
    assert result.detail_groups["history"].oldest_overdue_seconds >= 7200
    assert result.details_succeeded == 20 and result.new_documents == 1
    assert result.remaining_due == 81


def test_ten_saturated_rounds_service_history_and_rechecks_each_time(state_env):
    env = state_env
    seed_history(env, range(200000, 200100))
    seed_rechecks(env, range(300000, 300040))
    totals = Counter()
    serviced = set()
    for round_number in range(10):
        foreground = tuple(range(900000 + round_number * 100, 900012 + round_number * 100))
        calls = []

        def handler(request, *, foreground=foreground, calls=calls):
            if str(request.url) == HOME:
                return html(listing(ids=foreground))
            calls.append(article_id(request))
            return html(fixture("current-notice-detail.html"))

        result = crawl(env, handler, at=AT + round_number * 1800)
        attempted = {group: result.detail_groups[group].attempted for group in GROUPS}
        assert attempted == {"foreground": 12, "history": 4, "recheck": 4}
        assert calls[:3] == [foreground[0], 200000 + round_number * 4, 300000 + round_number * 4]
        assert len(calls) == len(set(calls)) == 20
        assert not serviced.intersection(calls)
        serviced.update(calls)
        assert result.details_succeeded == 20 and result.result == "succeeded"
        totals.update(attempted)
    assert totals == {"foreground": 120, "history": 40, "recheck": 40}
    assert result.detail_groups["history"].remaining_due == 60
    assert result.detail_groups["recheck"].remaining_due == 0
    assert len(rows(env, notice_versions)) == 200


def test_failed_foreground_waits_for_due_and_successful_failure_stays_recheck(state_env):
    env, calls = state_env, []
    seed_history(env, (900001, 200001))
    seed_rechecks(env, (300001,), failed=True)
    baseline = next(
        row for row in rows(env, documents) if row["source_document_id"] == "1517:300001"
    )
    with env.engine.begin() as connection:
        connection.execute(
            documents.update()
            .where(documents.c.source_document_id == "1517:900001")
            .values(
                status="failed",
                last_attempt_at=AT - 1,
                last_error_code="parse_missing_structure",
                next_due_at=AT + 21600,
            )
        )

    def handler(request):
        if str(request.url) == HOME:
            return html(listing(ids=(900001,)))
        calls.append(article_id(request))
        return (
            httpx.Response(403)
            if article_id(request) == 300001
            else html(fixture("current-notice-detail.html"))
        )

    result = crawl(env, handler)
    assert calls == [200001, 300001] and result.details_failed == 1
    assert {group: result.detail_groups[group].attempted for group in GROUPS} == {
        "foreground": 0,
        "history": 1,
        "recheck": 1,
    }
    after = next(row for row in rows(env, documents) if row["id"] == baseline["id"])
    assert after["current_version_id"] == baseline["current_version_id"]
    assert after["last_success_at"] == baseline["last_success_at"]
    assert after["next_due_at"] == after["last_attempt_at"] + 86400


def test_only_first_two_registered_pages_supply_foreground(state_env):
    env, calls = state_env, []
    routes = (HOME, SECOND, THIRD)
    bodies = {
        HOME: listing(1, 3, (900001,), routes=routes),
        SECOND: listing(2, 3, (900002,), routes=routes),
        THIRD: listing(3, 3, (900003,), routes=routes),
    }

    def handler(request):
        uri = str(request.url)
        if uri in bodies:
            return httpx.Response(304) if "If-None-Match" in request.headers else html(bodies[uri])
        calls.append(article_id(request))
        return html(fixture("current-notice-detail.html"))

    result = crawl_once(
        crawl_settings(env),
        CrawlOptions(scan_mode="full", max_pages=3, max_details=3),
        clock=clock(),
        transport=httpx.MockTransport(handler),
    )
    assert result.coverage == "complete" and result.home_rechecked
    assert calls == [900001, 900003, 900002]
    assert result.detail_groups["foreground"].attempted == 2
    assert result.detail_groups["history"].attempted == 1


def test_home_304_rebuilds_foreground_from_bound_body(state_env):
    env, calls = state_env, []
    seed_history(env, range(200000, 200010))

    def handler(request):
        if str(request.url) == HOME:
            return (
                httpx.Response(304)
                if "If-None-Match" in request.headers
                else html(listing(ids=(900001,)))
            )
        calls.append(article_id(request))
        return html(fixture("current-notice-detail.html"))

    initial = crawl(env, handler, maximum=0)
    recovered = crawl(env, handler, maximum=2, at=AT + 60)
    assert initial.new_documents == 1 and recovered.new_documents == 0
    assert calls == [900001, 200000]
    assert recovered.detail_groups["foreground"].attempted == 1
    assert recovered.detail_groups["history"].attempted == 1


def test_bad_home_produces_no_foreground_but_existing_groups_continue(state_env):
    env, calls = state_env, []
    seed_history(env, (200001,))
    seed_rechecks(env, (300001,))

    def handler(request):
        if str(request.url) == HOME:
            return html(b"<html><title>Temporary unavailable</title></html>")
        calls.append(article_id(request))
        return html(fixture("current-notice-detail.html"))

    result = crawl(env, handler, maximum=2)
    assert result.coverage == "interrupted" and result.result == "partial_failure"
    assert result.detail_groups["foreground"].attempted == 0
    assert calls == [200001, 300001] and result.details_succeeded == 2


@pytest.mark.parametrize(
    "kind,seconds,code",
    [
        (401, 86400, "http_unauthorized"),
        (403, 86400, "http_forbidden"),
        (404, 604800, "http_not_found"),
        (410, 604800, "http_not_found"),
        (503, 1800, "http_transient"),
        ("parse", 21600, "parse_missing_structure"),
        ("timeout", 1800, "connect_timeout"),
        ("tls", 86400, "tls_error"),
    ],
)
def test_coordinator_persists_failure_due_by_actual_error(kind, seconds, code, state_env):
    def handler(request):
        if str(request.url) == HOME:
            return html(listing(ids=(900001,)))
        if kind == "timeout":
            raise httpx.ConnectTimeout("synthetic timeout", request=request)
        if kind == "tls":
            raise httpx.ConnectError(
                "synthetic TLS failure", request=request
            ) from ssl.SSLCertVerificationError()
        if kind == "parse":
            return html(b"<html>Not a notice template</html>")
        return httpx.Response(kind)

    result = crawl(state_env, handler, maximum=1)
    document = rows(state_env, documents)[0]
    assert result.details_failed == result.detail_groups["foreground"].failed == 1
    assert document["status"] == "failed" and document["current_version_id"] is None
    assert document["last_error_code"] == code
    assert document["next_due_at"] == document["last_attempt_at"] + seconds


@pytest.mark.parametrize(
    "age,seconds", [(7, 86400), (8, 604800), (30, 604800), (31, 2592000), (-1, 86400)]
)
def test_coordinator_success_due_uses_parsed_site_date_in_commit(age, seconds, state_env):
    published = (date(2026, 10, 5) - timedelta(days=age)).isoformat().encode()
    detail = fixture("current-notice-detail.html").replace(b"2026-09-04", published)
    result = crawl(
        state_env,
        lambda request: html(listing(ids=(900001,)) if str(request.url) == HOME else detail),
        maximum=1,
    )
    document = rows(state_env, documents)[0]
    assert document["next_due_at"] == document["last_success_at"] + seconds
    assert result.future_dates == int(age < 0)
    assert result.detail_groups["foreground"].succeeded == 1


def test_global_budget_exhaustion_reports_unserved_reservations(state_env):
    env = state_env
    seed_history(env, (200001,))
    seed_rechecks(env, (300001,))
    result = crawl_once(
        crawl_settings(env),
        CrawlOptions(
            max_pages=1,
            max_details=3,
            fetch_limits=FetchLimits(max_requests=2, max_retries=0),
        ),
        clock=clock(),
        transport=httpx.MockTransport(
            lambda request: html(
                listing(ids=(900001,))
                if str(request.url) == HOME
                else fixture("current-notice-detail.html")
            )
        ),
    )
    assert result.result == "interrupted" and result.error_code == "request_limit"
    assert result.details_attempted == result.detail_groups["foreground"].attempted == 1
    assert result.detail_groups["history"].unserved == 1
    assert result.detail_groups["recheck"].unserved == 1
    assert (
        result.detail_groups["history"].remaining_due
        == result.detail_groups["recheck"].remaining_due
        == 1
    )


def test_custom_reservations_control_logical_service():
    policy = RuntimeSettings(foreground_slots=3, history_slots=2, recheck_slots=1)
    items = [candidate(i) for i in range(1, 9)] + [candidate(i, success=1) for i in range(9, 13)]
    selected = select_details(items, {str(i) for i in range(1, 5)}, 6, policy)
    assert [group for group, _ in selected] == [
        "foreground",
        "history",
        "recheck",
        "foreground",
        "history",
        "foreground",
    ]


def test_explicit_policy_upgrade_preserves_overdue_and_failed_retry_times(state_env):
    env = state_env
    seed_rechecks(env, (300001, 300002, 300003, 300004))
    due_by_article = {300001: AT - 100, 300002: AT + 86400, 300003: None, 300004: AT + 100000}
    with env.engine.begin() as connection:
        for article, due in due_by_article.items():
            values = {"next_due_at": due}
            if article == 300004:
                values.update(
                    status="failed", last_attempt_at=AT - 1, last_error_code="http_forbidden"
                )
            connection.execute(
                documents.update()
                .where(documents.c.source_document_id == f"1517:{article}")
                .values(**values)
            )
    before = {row["id"]: row for row in rows(env, documents)}
    policy = RuntimeSettings(
        recent_recheck_seconds=600, middle_recheck_seconds=1800, old_recheck_seconds=3600
    )
    assert apply_recheck_policy(env.engine, SOURCE, AT, policy) == 2
    after = {row["source_document_id"]: row for row in rows(env, documents)}
    assert {identity: row["next_due_at"] for identity, row in after.items()} == {
        "1517:300001": AT - 100,
        "1517:300002": AT + 1,  # Last success was AT - 3599; policy does not fabricate a new one.
        "1517:300003": AT,
        "1517:300004": AT + 100000,
    }
    for row in after.values():
        assert row["current_version_id"] == before[row["id"]]["current_version_id"]
        assert row["last_success_at"] == before[row["id"]]["last_success_at"]
    assert apply_recheck_policy(env.engine, SOURCE, AT, policy) == 0


def test_policy_upgrade_can_share_a_real_transaction_and_roll_back(state_env):
    env = state_env
    seed_rechecks(env, (300001,))
    with env.engine.begin() as connection:
        connection.execute(documents.update().values(next_due_at=None))
    with pytest.raises(RuntimeError, match="injected interruption"):
        with env.engine.begin() as connection:
            assert (
                apply_recheck_policy_in_transaction(connection, SOURCE, AT, RuntimeSettings()) == 1
            )
            raise RuntimeError("injected interruption")
    assert rows(env, documents)[0]["next_due_at"] is None


def test_304_success_due_uses_current_processing_not_original_fetch_time(state_env):
    env = state_env

    def handler(request):
        if "If-None-Match" in request.headers:
            return httpx.Response(304)
        return html(
            listing(ids=(900001,))
            if str(request.url) == HOME
            else fixture("current-notice-detail.html")
        )

    first = crawl(env, handler, maximum=1)
    prior = rows(env, documents)[0]
    with env.engine.begin() as connection:
        connection.execute(documents.update().values(next_due_at=AT + 100))
    second = crawl(env, handler, maximum=1, at=AT + 200)
    after = rows(env, documents)[0]
    assert second.detail_groups["recheck"].succeeded == 1
    assert after["next_due_at"] == after["last_success_at"] + 2592000
    assert after["next_due_at"] > prior["next_due_at"]
    assert second.details_succeeded == first.details_succeeded == 1
    assert len(rows(env, notice_versions)) == 1
    evidence = rows(env, raw_responses)
    notice_200 = next(
        row for row in evidence if row["page_type"] == "notice" and row["status_code"] == 200
    )
    notice_304 = next(
        row for row in evidence if row["page_type"] == "notice" and row["status_code"] == 304
    )
    assert notice_200["fetched_at"] == prior["last_success_at"]
    assert notice_304["validated_response_id"] == notice_200["id"]
    assert notice_304["body_path"] is None


def test_failed_success_transaction_preserves_version_and_never_commits_success_due(state_env):
    env = state_env
    seed_rechecks(env, (300001,))
    prior = rows(env, documents)[0]
    detail = fixture("current-notice-detail.html").replace(b"2026-2027", b"2026-2028")
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_runtime_due BEFORE UPDATE ON documents "
            "WHEN NEW.status = 'processed' AND NEW.last_success_at IS NOT OLD.last_success_at "
            "BEGIN SELECT RAISE(ABORT, 'injected policy commit failure'); END"
        )
    with pytest.raises(IngestError):
        crawl(
            env,
            lambda request: html(listing(ids=(300001,)) if str(request.url) == HOME else detail),
            maximum=1,
        )
    after = rows(env, documents)[0]
    assert after["current_version_id"] == prior["current_version_id"]
    assert after["last_success_at"] == prior["last_success_at"]
    assert len(rows(env, notice_versions)) == 1
    assert after["status"] == "failed"
    assert after["next_due_at"] != after["last_attempt_at"] + 2592000
