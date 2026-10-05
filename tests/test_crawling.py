"""Offline end-to-end coordinator checks with real archive, Parser and SQLite.

Synthetic HTTP and fault injection are not live HTTP, process termination or power loss.
"""

from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from signalnest.config import Settings
from signalnest.contracts import PageInput
from signalnest.crawling import CrawlOptions, crawl_once
from signalnest.errors import IngestError
from signalnest.fetching import FetchLimits, default_profile
from signalnest.ingestion import ResponseInput, import_page, record_response
from signalnest.ingestion_state import read_source_state, set_cooldown_in_transaction
from signalnest.parsing import parse_list
from signalnest.schema import (
    documents,
    http_resources,
    ingestion_runs,
    notice_versions,
    raw_responses,
)

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
SOURCE = "whu-undergrad-student"
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
SECOND = "https://uc.whu.edu.cn/tzgg/xstz/23.htm"
THIRD = "https://uc.whu.edu.cn/tzgg/xstz/7.htm"
OTHER = "https://uc.whu.edu.cn/tzgg/xstz/8.htm"
ROUTES = (HOME, SECOND, THIRD)


class Clock:
    def __init__(self):
        self.epoch, self.elapsed = 1000, 0.0

    def time(self):
        return self.epoch + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


def settings(env):
    return Settings(
        storage=env.settings,
        source=dict(id=SOURCE, list_url=HOME),
        http=dict(
            connect_timeout_seconds=2.0,
            read_timeout_seconds=3.0,
            request_interval_seconds=1.0,
            user_agent="SignalNest/crawl-test",
        ),
    )


def fixture(name):
    return (FIXTURES / name).read_bytes()


def html(body, *, etag='"stable"'):
    return httpx.Response(
        200, headers={"Content-Type": "text/html", "ETag": etag}, stream=httpx.ByteStream(body)
    )


def listing(current=1, total=2, ids=(128231,), *, routes=ROUTES, title="通知"):
    numbers = "".join(
        f'<span class="p_no_d">{i}</span>'
        if i == current
        else f'<span class="p_no"><a href="{routes[i - 1]}">{i}</a></span>'
        for i in range(1, total + 1)
    )
    controls = (
        '<span class="p_next_d">下页</span><span class="p_last_d">尾页</span>'
        if current == total
        else f'<span class="p_next"><a href="{routes[current]}">下页</a></span>'
        f'<span class="p_last"><a href="{routes[total - 1]}">尾页</a></span>'
    )
    rows = "".join(
        f'<li><a href="/info/1517/{article}.htm"><span>{title}{article}</span>'
        "<i>2026-09-24</i></a></li>"
        for article in ids
    )
    return (
        f'<div><div class="list_txt"><ul class="am-list">{rows}</ul></div>'
        f'<div class="page"><div class="p_pages">{numbers}{controls}</div></div></div>'
    ).encode()


def options(**values):
    return CrawlOptions(scan_mode="full", max_pages=3, max_details=0, **values)


def rows(env, table):
    with env.engine.connect() as connection:
        return connection.execute(sa.select(table)).mappings().all()


def run(env, handler, *, clock=None, opts=None):
    return crawl_once(
        settings(env),
        opts or options(),
        clock=clock or Clock(),
        transport=httpx.MockTransport(handler),
    )


def seed(env, *, notice=True):
    config = settings(env)
    evidence = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=HOME,
        final_url=HOME,
        fetched_at=100,
        status_code=200,
    )
    import_page(env.engine, env.store, evidence, listing(ids=(128231, 127581)), 101)
    if notice:
        uri = "https://uc.whu.edu.cn/info/1517/128231.htm"
        evidence = ResponseInput(
            page_type="notice",
            source_id=SOURCE,
            source_document_id="1517:128231",
            requested_url=uri,
            final_url=uri,
            fetched_at=102,
            status_code=200,
        )
        import_page(env.engine, env.store, evidence, fixture("current-notice-detail.html"), 103)
    return config


def test_two_real_pages_repeat_304_counts_and_body_dedup(state_env):
    env = state_env
    content = {
        HOME: fixture("student-notices-page1.html"),
        SECOND: fixture("student-notices-page2.html"),
    }
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return (
            httpx.Response(304)
            if "If-None-Match" in request.headers
            else html(content[str(request.url)])
        )

    opts = CrawlOptions(scan_mode="limited", max_pages=2, max_details=0)
    first = run(env, handler, opts=opts)
    second = run(env, handler, clock=Clock(), opts=opts)
    assert (first.scanned_entries, first.new_documents, first.remaining_due) == (50, 50, 50)
    assert (second.scanned_entries, second.new_documents, second.remaining_due) == (50, 0, 50)
    assert first.coverage == second.coverage == "limited"
    assert first.result == second.result == "succeeded"
    assert len(rows(env, documents)) == 50 and not rows(env, notice_versions)
    responses = rows(env, raw_responses)
    assert [row["status_code"] for row in responses] == [200, 200, 304, 304]
    assert responses[2]["validated_response_id"] == responses[0]["id"]
    assert responses[3]["validated_response_id"] == responses[1]["id"]
    assert len(tuple((env.settings.data_dir / "raw").glob("*.bin"))) == 2
    assert read_source_state(env.engine, SOURCE)["last_complete_scan_at"] is None
    assert calls == [HOME, SECOND, HOME, SECOND]


def test_full_proof_rechecks_home_and_processes_real_details(state_env):
    env, calls = state_env, []
    bodies = {HOME: listing(ids=(128231, 127581)), SECOND: listing(2, 2, (900001,))}

    def handler(request):
        uri = str(request.url)
        calls.append((uri, dict(request.headers)))
        if uri in bodies:
            return httpx.Response(304) if "If-None-Match" in request.headers else html(bodies[uri])
        return html(
            fixture(
                "current-notice-detail.html" if "128231" in uri else "legacy-notice-detail.html"
            )
        )

    result = run(env, handler, opts=CrawlOptions(scan_mode="full", max_pages=5, max_details=2))
    assert (result.coverage, result.result, result.home_rechecked) == (
        "complete",
        "succeeded",
        True,
    )
    assert (result.pages_committed, result.scanned_entries, result.new_documents) == (2, 3, 3)
    assert (result.details_attempted, result.details_succeeded, result.details_failed) == (2, 2, 0)
    assert result.remaining_due == result.remaining_unprocessed == 1
    assert result.physical_requests == 5
    assert calls[2][0] == HOME and calls[2][1]["cache-control"] == "no-cache"
    assert "if-none-match" in calls[2][1]
    state = read_source_state(env.engine, SOURCE)
    assert state["last_complete_scan_run_id"] == result.run_id
    assert state["bootstrap_completed_at"] is not None
    assert {row["discovery_origin"] for row in rows(env, documents)} == {"bootstrap"}
    assert all(
        row["next_due_at"] > result.finished_at
        for row in rows(env, documents)
        if row["status"] == "processed"
    )
    later = Clock()
    later.epoch = 1100
    again = run(
        env,
        handler,
        clock=later,
        opts=CrawlOptions(scan_mode="limited", max_pages=1, max_details=0),
    )
    assert again.origin == "regular" and again.new_documents == 0


def test_known_home_does_not_stop_later_new_identity(state_env):
    env = state_env
    home, tail = listing(), listing(2, 2, (900001,))
    first = run(env, lambda r: html(home), opts=CrawlOptions(max_pages=1, max_details=0))
    assert first.new_documents == 1
    second = run(env, lambda r: html(home if str(r.url) == HOME else tail))
    assert second.new_documents == 1 and second.coverage == "complete"
    assert len(rows(env, documents)) == 2


@pytest.mark.parametrize(
    "kind,code",
    [
        ("redirect_loop", "pagination_loop"),
        ("skipped_page", "pagination_discontinuity"),
        ("total_drift", "pagination_drift"),
        ("tail_drift", "pagination_drift"),
        ("home_changed", "home_changed"),
        ("home_final_changed", "home_changed"),
    ],
)
def test_cross_page_and_home_drift_cannot_complete(state_env, kind, code):
    home_count = 0

    def handler(request):
        nonlocal home_count
        uri = str(request.url)
        if uri == HOME:
            home_count += 1
            if home_count > 1 and kind == "home_final_changed":
                return httpx.Response(302, headers={"Location": OTHER})
            return html(
                listing(title="改变" if kind == "home_changed" and home_count > 1 else "通知")
            )
        if kind == "redirect_loop":
            return httpx.Response(302, headers={"Location": HOME})
        if kind == "skipped_page":
            return html(listing(3, 3, (900002,), routes=(HOME, OTHER, SECOND)))
        if kind == "total_drift":
            return html(listing(2, 3, (900002,)))
        if kind == "tail_drift":
            return html(listing(2, 3, (900002,), routes=(HOME, SECOND, OTHER)))
        if uri == OTHER:
            return html(listing())
        return html(listing(2, 2, (900001,)))

    if kind == "tail_drift":

        def drift_handler(request):
            return html(listing(1, 3)) if str(request.url) == HOME else handler(request)

        result = run(state_env, drift_handler)
    else:
        result = run(state_env, handler)
    assert result.coverage == "interrupted" and result.coverage_error_code == code
    assert read_source_state(state_env.engine, SOURCE)["last_complete_scan_at"] is None


def test_changed_tail_is_rejected_before_terminal_page(state_env):
    # Locally valid consecutive page declarations still cannot change the declared tail.
    def handler(request):
        uri = str(request.url)
        if uri == HOME:
            return html(listing(1, 3))
        if uri == SECOND:
            return html(listing(2, 3).replace(THIRD.encode(), OTHER.encode()))
        return html(listing(3, 3))

    result = run(state_env, handler)
    # The changed tail is detected even before visiting it.
    assert result.coverage_error_code == "pagination_drift"
    assert result.coverage != "complete"


def test_home_noise_changes_raw_bytes_but_not_scan_fingerprint(state_env):
    count = 0

    def handler(request):
        nonlocal count
        if str(request.url) == HOME:
            count += 1
            return html(listing() + f"<footer>统计{count}</footer>".encode())
        return html(listing(2, 2))

    result = run(state_env, handler)
    assert result.coverage == "complete"
    home_rows = [row for row in rows(state_env, raw_responses) if row["requested_url"] == HOME]
    assert home_rows[0]["body_sha256"] != home_rows[1]["body_sha256"]


@pytest.mark.parametrize(
    "budget,code,requests,pages",
    [
        (dict(max_pages=1), "page_limit", 1, 1),
        (dict(fetch_limits=FetchLimits(max_requests=2)), "request_limit", 2, 2),
        (dict(fetch_limits=FetchLimits(run_seconds=1)), "run_time_limit", 1, 1),
    ],
)
def test_budgets_cannot_attest_complete_scan(state_env, budget, code, requests, pages):
    opts = CrawlOptions(**(dict(scan_mode="full", max_pages=5, max_details=0) | budget))
    result = run(
        state_env, lambda r: html(listing() if str(r.url) == HOME else listing(2, 2)), opts=opts
    )
    assert result.coverage == "limited" and result.coverage_error_code == code
    assert result.result == "interrupted"
    assert result.physical_requests == requests and result.pages_committed == pages
    assert not result.home_rechecked
    assert read_source_state(state_env.engine, SOURCE)["last_complete_scan_at"] is None


def test_full_page_cap_still_allows_database_details(state_env):
    result = run(
        state_env,
        lambda r: (
            html(listing()) if str(r.url) == HOME else html(fixture("current-notice-detail.html"))
        ),
        opts=CrawlOptions(scan_mode="full", max_pages=1, max_details=1),
    )
    assert result.result == "interrupted" and result.coverage == "limited"
    assert result.details_succeeded == 1 and result.physical_requests == 2


def test_failed_list_304_still_processes_pending_details(state_env):
    env = state_env
    seed(env, notice=False)
    evidence = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=HOME,
        final_url=HOME,
        fetched_at=104,
        status_code=200,
        request_profile=default_profile(settings(env).http),
        etag='"invalid"',
    )
    bad_id = record_response(env.engine, env.store, evidence, b"<html>error template</html>")
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(304)
            if str(request.url) == HOME
            else html(fixture("current-notice-detail.html"))
        )

    result = run(env, handler, opts=CrawlOptions(max_pages=1, max_details=1))
    assert result.coverage_error_code == "parse_missing_structure"
    assert result.details_succeeded == 1 and result.result == "partial_failure"
    assert calls[0].headers["If-None-Match"] == '"invalid"'
    response = next(row for row in rows(env, raw_responses) if row["status_code"] == 304)
    assert response["validated_response_id"] == bad_id
    assert response["last_error_code"] == "parse_missing_structure"


def test_first_online_due_enrollment_preserves_old_success_on_failure_and_continues(state_env):
    env = state_env
    seed(env)
    previous = rows(env, notice_versions)[0]["id"]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == HOME:
            return httpx.Response(403)
        if "128231" in str(request.url):
            return html(b"<html>not a notice</html>")
        return html(fixture("legacy-notice-detail.html"))

    result = run(env, handler, opts=CrawlOptions(max_pages=1, max_details=2))
    assert result.legacy_rechecks_scheduled == 1
    assert (result.details_attempted, result.details_succeeded, result.details_failed) == (2, 1, 1)
    assert result.remaining_due == 0 and result.remaining_unprocessed == 1
    first = next(row for row in rows(env, documents) if row["source_document_id"] == "1517:128231")
    assert first["current_version_id"] == previous and first["status"] == "failed"
    assert first["next_due_at"] > result.finished_at
    assert first["discovery_origin"] == "unknown"
    assert len(calls) == 3
    later = Clock()
    later.epoch = first["next_due_at"] + 1
    retry = run(
        env,
        lambda r: (
            httpx.Response(403)
            if str(r.url) == HOME
            else html(fixture("current-notice-detail.html"))
        ),
        clock=later,
        opts=CrawlOptions(max_details=2),
    )
    assert retry.details_succeeded == 1
    first = next(row for row in rows(env, documents) if row["source_document_id"] == "1517:128231")
    assert first["current_version_id"] == previous and first["status"] == "processed"
    assert len(rows(env, notice_versions)) == 2


def test_complete_list_and_partial_detail_failure_are_independent(state_env):
    def handler(request):
        uri = str(request.url)
        if uri == HOME:
            return html(listing(ids=(128231, 127581)))
        if uri == SECOND:
            return html(listing(2, 2, (128231,)))
        return (
            httpx.Response(404) if "128231" in uri else html(fixture("legacy-notice-detail.html"))
        )

    result = run(
        state_env, handler, opts=CrawlOptions(scan_mode="full", max_pages=4, max_details=2)
    )
    assert result.coverage == "complete" and result.result == "partial_failure"
    assert result.details_failed == result.details_succeeded == 1
    assert result.scanned_entries == 3 and result.new_documents == 2
    assert read_source_state(state_env.engine, SOURCE)["last_complete_scan_at"] is not None


def test_detail_limit_does_not_call_remaining_targets(state_env):
    env = state_env
    seed(env, notice=False)
    result = run(
        env,
        lambda r: (
            httpx.Response(403)
            if str(r.url) == HOME
            else html(fixture("current-notice-detail.html"))
        ),
        opts=CrawlOptions(max_details=1),
    )
    assert result.details_attempted == 1 and result.remaining_due == 1


def test_429_stops_details_and_persists_cooldown_for_next_run(state_env):
    env = state_env
    seed(env, notice=False)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "300"})

    first = run(env, handler)
    second = run(env, handler)
    assert calls == [HOME]
    assert first.result == second.result == "interrupted"
    assert first.details_attempted == second.details_attempted == 0
    assert second.physical_requests == 0 and second.error_code == "server_cooldown"
    assert second.remaining_due == 2
    assert read_source_state(env.engine, SOURCE)["not_before_at"] == 1300
    assert rows(env, raw_responses)[-1]["last_error_code"] == "http_rate_limited"


def test_short_existing_cooldown_is_waited_before_any_request(state_env):
    with state_env.engine.begin() as connection:
        set_cooldown_in_transaction(connection, SOURCE, 1005)
    clock, starts = Clock(), []

    def handler(request):
        starts.append(clock.time())
        return html(listing())

    result = run(state_env, handler, clock=clock, opts=CrawlOptions(max_pages=1, max_details=0))
    assert starts == [1005] and result.result == "succeeded"


def test_sql_commit_failure_aborts_and_rolls_back_business_resource_markers(state_env):
    env = state_env
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_discovery BEFORE INSERT ON documents "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return html(listing())

    with pytest.raises(IngestError, match="database_write_failed"):
        run(env, handler, opts=CrawlOptions(max_pages=2, max_details=2))
    assert calls == [HOME] and not rows(env, documents)
    response = rows(env, raw_responses)[0]
    assert response["body_path"] and response["last_error_code"] == "database_write_failed"
    assert rows(env, http_resources)[0]["last_processed_response_id"] is None
    assert rows(env, ingestion_runs)[0]["result"] == "failed"


def test_failure_registration_unavailable_stops_instead_of_continuing(state_env):
    env = state_env
    seed(env, notice=False)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_failure BEFORE UPDATE ON documents WHEN NEW.status='failed' "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return (
            html(listing(ids=(128231, 127581))) if str(request.url) == HOME else httpx.Response(403)
        )

    with pytest.raises(IngestError, match="failure_state_unavailable"):
        run(env, handler, opts=CrawlOptions(max_pages=1, max_details=2))
    assert len(calls) == 2 and rows(env, ingestion_runs)[0]["result"] == "failed"


def test_run_finalization_failure_is_explicit_and_not_reported_success(state_env):
    with state_env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_finish BEFORE UPDATE ON ingestion_runs "
            "WHEN NEW.result!='running' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError, match="run_finalization_unavailable"):
        run(state_env, lambda r: html(listing()), opts=CrawlOptions(max_pages=1, max_details=0))
    assert rows(state_env, ingestion_runs)[0]["result"] == "running"
    assert len(rows(state_env, documents)) == 1


def test_synthetic_page_helpers_have_real_parser_valid_evidence():
    for current in (1, 2, 3):
        parsed = parse_list(PageInput(content=listing(current, 3), page_url=ROUTES[current - 1]))
        assert parsed.pagination.current_page == current
        assert parsed.pagination.is_last_page == (current == 3)


def baseline(env, body=None):
    evidence = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=HOME,
        final_url=HOME,
        fetched_at=104,
        status_code=200,
        request_profile=default_profile(settings(env).http),
        etag='"baseline"',
    )
    response_id = record_response(env.engine, env.store, evidence, body or listing())
    row = next(row for row in rows(env, raw_responses) if row["id"] == response_id)
    return response_id, env.settings.data_dir / row["body_path"]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_invalid_cached_file_forces_full_fetch_not_old_success(state_env, damage):
    env = state_env
    _, path = baseline(env)
    if damage == "missing":
        path.unlink()
        returned = listing()
    else:
        path.write_bytes(b"corrupt")
        # A changed server response is safely archived under a different digest.
        returned = listing(title="新版")
    calls = []

    def handler(request):
        calls.append(request)
        return html(returned)

    result = run(env, handler, opts=CrawlOptions(max_pages=1, max_details=0))
    assert result.pages_committed == 1 and result.result == "succeeded"
    assert "If-None-Match" not in calls[0].headers
    assert rows(env, raw_responses)[-1]["status_code"] == 200
    if damage == "corrupt":
        assert path.read_bytes() == b"corrupt"  # Never silently overwrite corrupt evidence.


def test_identical_full_response_cannot_silently_overwrite_corrupt_raw(state_env):
    _, path = baseline(state_env)
    path.write_bytes(b"corrupt")
    with pytest.raises(IngestError, match="raw_digest_mismatch"):
        run(
            state_env,
            lambda request: html(listing()),
            opts=CrawlOptions(max_pages=1, max_details=0),
        )
    assert path.read_bytes() == b"corrupt" and len(rows(state_env, raw_responses)) == 1


def test_file_loss_after_bound_304_resumes_same_fetch_and_retains_evidence(state_env, monkeypatch):
    import signalnest.crawling as crawling

    env, calls = state_env, []
    old_id, path = baseline(env)
    real_record = crawling.record_response

    def record(*args, **kwargs):
        response_id = real_record(*args, **kwargs)
        if args[2].status_code == 304:
            path.unlink()  # Fault injection precisely after evidence commit, before read/Parser.
        return response_id

    monkeypatch.setattr(crawling, "record_response", record)

    def handler(request):
        calls.append(request)
        return httpx.Response(304) if len(calls) == 1 else html(listing())

    result = run(env, handler, opts=CrawlOptions(max_pages=1, max_details=0))
    assert result.result == "succeeded" and result.physical_requests == 2
    assert "If-None-Match" in calls[0].headers and "If-None-Match" not in calls[1].headers
    responses = rows(env, raw_responses)
    assert [row["status_code"] for row in responses] == [200, 304, 200]
    assert responses[1]["validated_response_id"] == old_id
    assert responses[1]["body_path"] is None and responses[1]["last_error_code"] == "raw_missing"
    assert responses[0]["fetched_at"] == 104 and path.exists()
    assert len(tuple((env.settings.data_dir / "raw").glob("*.bin"))) == 1


def test_committed_200_whose_business_failed_is_retried_on_304(state_env):
    env = state_env
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_list BEFORE INSERT ON documents "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError, match="database_write_failed"):
        run(env, lambda r: html(listing()), opts=CrawlOptions(max_pages=1, max_details=0))
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER fail_list")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(304)

    result = run(env, handler, opts=CrawlOptions(max_pages=1, max_details=0))
    assert result.result == "succeeded" and result.new_documents == 1
    assert "If-None-Match" in calls[0].headers
    assert (
        rows(env, raw_responses)[-1]["validated_response_id"] == rows(env, raw_responses)[0]["id"]
    )
    assert (
        rows(env, http_resources)[0]["last_processed_response_id"]
        == rows(env, raw_responses)[0]["id"]
    )


def test_retry_within_one_page_is_not_cross_page_loop(state_env):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(500)
        return html(listing() if str(request.url) == HOME else listing(2, 2))

    result = run(state_env, handler)
    assert result.coverage == "complete" and result.result == "succeeded"
    assert result.physical_requests == 4
    assert rows(state_env, raw_responses)[0]["last_error_code"] == "http_transient"


def test_list_resource_time_limit_does_not_skip_details(state_env):
    env, clock = state_env, Clock()
    seed(env, notice=False)

    def handler(request):
        if str(request.url) == HOME:
            clock.sleep(2)
            return html(listing())
        return html(fixture("current-notice-detail.html"))

    result = run(
        env,
        handler,
        clock=clock,
        opts=CrawlOptions(max_details=1, fetch_limits=FetchLimits(resource_seconds=1)),
    )
    assert result.coverage == "limited" and result.coverage_error_code == "resource_time_limit"
    assert result.details_succeeded == 1 and result.result == "partial_failure"


def test_detail_database_failure_preserves_complete_coverage_and_previous_version(state_env):
    env = state_env
    seed(env)
    previous = rows(env, notice_versions)[0]["id"]
    with env.engine.begin() as connection:
        connection.execute(
            documents.update()
            .where(documents.c.source_document_id == "1517:128231")
            .values(next_due_at=99)
        )
        # Keep unrelated first-processing work deferred while measuring the recheck rollback.
        connection.execute(
            documents.update()
            .where(documents.c.source_document_id == "1517:127581")
            .values(next_due_at=1000000)
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_version BEFORE INSERT ON notice_versions "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    calls = []

    def handler(request):
        uri = str(request.url)
        calls.append(uri)
        if uri == HOME:
            return html(listing())
        if uri == SECOND:
            return html(listing(2, 2))
        return html(fixture("current-notice-detail.html"))

    with pytest.raises(IngestError, match="database_write_failed"):
        run(env, handler, opts=CrawlOptions(scan_mode="full", max_pages=3, max_details=2))
    assert len(calls) == 4  # Next detail is not fetched after the systemic failure.
    assert read_source_state(env.engine, SOURCE)["last_complete_scan_at"] is not None
    assert rows(env, ingestion_runs)[0]["coverage"] == "complete"
    assert rows(env, ingestion_runs)[0]["result"] == "failed"
    first = next(row for row in rows(env, documents) if row["source_document_id"] == "1517:128231")
    assert first["current_version_id"] == previous and first["status"] == "failed"
    assert len(rows(env, notice_versions)) == 1
    resource = next(row for row in rows(env, http_resources) if "128231" in row["request_uri"])
    assert resource["last_processed_response_id"] is None


def test_home_recheck_parsing_time_counts_toward_run_budget(state_env, monkeypatch):
    import signalnest.crawling as crawling

    clock, parses = Clock(), 0
    real_parse = crawling.parse_list

    def parse(page):
        nonlocal parses
        parses += 1
        result = real_parse(page)
        if parses == 3:
            clock.sleep(5)
        return result

    monkeypatch.setattr(crawling, "parse_list", parse)
    result = run(
        state_env,
        lambda request: html(listing() if str(request.url) == HOME else listing(2, 2)),
        clock=clock,
        opts=CrawlOptions(
            scan_mode="full", max_pages=4, max_details=0, fetch_limits=FetchLimits(run_seconds=5)
        ),
    )
    assert result.home_rechecked and result.pages_committed == 2
    assert result.coverage == "limited" and result.result == "interrupted"
    assert result.error_code == "run_time_limit"
    assert read_source_state(state_env.engine, SOURCE)["last_complete_scan_at"] is None


def test_next_locked_run_recovers_unfinished_run_from_database_facts(state_env):
    from signalnest.ingestion_state import start_run_in_transaction

    env = state_env
    seed(env, notice=False)
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "unfinished", 104, origin="bootstrap")
    result = run(
        env,
        lambda request: (
            httpx.Response(403)
            if str(request.url) == HOME
            else html(fixture("current-notice-detail.html"))
        ),
        opts=CrawlOptions(max_details=1),
    )
    old = next(row for row in rows(env, ingestion_runs) if row["id"] == "unfinished")
    assert old["result"] == old["coverage"] == "interrupted"
    assert old["error_code"] == "previous_run_unfinished"
    assert result.details_succeeded == 1 and result.remaining_due == 1


def test_due_order_does_not_starve_waiting_detail_behind_low_id_recheck(state_env):
    env = state_env
    seed(env, notice=False)
    first = run(
        env,
        lambda request: (
            httpx.Response(403)
            if str(request.url) == HOME
            else html(fixture("current-notice-detail.html"))
        ),
        opts=CrawlOptions(max_details=1),
    )
    assert first.details_succeeded == 1
    clock, calls = Clock(), []
    clock.epoch = 90000  # Both the first notice's recheck and the unprocessed second are due.

    def handler(request):
        calls.append(str(request.url))
        return (
            httpx.Response(403)
            if str(request.url) == HOME
            else html(fixture("legacy-notice-detail.html"))
        )

    second = run(env, handler, clock=clock, opts=CrawlOptions(max_details=1))
    assert second.details_succeeded == 1 and second.remaining_due == 1
    assert "127581" in calls[-1]
    assert len(rows(env, notice_versions)) == 2


def test_cannot_persist_list_attempt_means_no_http_send(state_env):
    from signalnest.schema import source_ingestion_state

    env, calls = state_env, []
    seed(env, notice=False)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_list_attempt BEFORE UPDATE ON source_ingestion_state "
            "WHEN NEW.last_list_attempt_at IS NOT OLD.last_list_attempt_at "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )

    def handler(request):
        calls.append(request)
        return html(listing())

    with pytest.raises(IngestError, match="list_attempt_state_unavailable"):
        run(env, handler)
    assert not calls and rows(env, ingestion_runs)[0]["result"] == "failed"
    assert rows(env, source_ingestion_state)[0]["last_list_attempt_at"] is None
