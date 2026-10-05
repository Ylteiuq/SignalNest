"""Cross-run recovery through the real coordinator, archive, Parser and SQLite.

MockTransport and explicit connection reopening remain offline integration checks;
they are not process termination, live HTTP or power-loss experiments.
"""

import httpx
import pytest
from test_crawling import (
    HOME,
    SOURCE,
    Clock,
    baseline,
    fixture,
    html,
    listing,
    rows,
    run,
    seed,
)

from signalnest.crawling import CrawlOptions
from signalnest.fetching import FetchLimits
from signalnest.ingestion_state import read_source_state
from signalnest.schema import documents, http_resources, notice_versions, raw_responses
from signalnest.storage import open_initialized_engine

NOTICE = "https://uc.whu.edu.cn/info/1517/128231.htm"
LEGACY = "https://uc.whu.edu.cn/info/1517/127581.htm"


def reopen(env):
    env.engine.dispose()
    env.engine = open_initialized_engine(env.settings.database)


def clock_at(epoch):
    clock = Clock()
    clock.epoch = epoch
    return clock


def target(env, identity="1517:128231"):
    return next(row for row in rows(env, documents) if row["source_document_id"] == identity)


def test_detail_rate_limit_survives_reopening_and_blocks_all_network_until_recovery(state_env):
    env = state_env
    seed(env)
    original = target(env)["current_version_id"]
    with env.engine.begin() as connection:
        connection.execute(
            documents.update()
            .where(documents.c.source_document_id == "1517:128231")
            .values(next_due_at=99)
        )
        # Isolate the successful-baseline 429 first; foreground work becomes due on recovery.
        connection.execute(
            documents.update()
            .where(documents.c.source_document_id == "1517:127581")
            .values(next_due_at=1800)
        )
    calls = []
    opts = CrawlOptions(scan_mode="limited", max_pages=1, max_details=2)

    def limited(request):
        calls.append(str(request.url))
        if str(request.url) == HOME:
            return html(listing(ids=(128231, 127581)))
        return httpx.Response(429, headers={"Retry-After": "600"})

    first = run(env, limited, opts=opts)
    assert calls == [HOME, NOTICE]
    assert first.result == "interrupted" and first.details_failed == 1
    assert target(env)["status"] == "failed"
    assert target(env)["current_version_id"] == original
    assert target(env, "1517:127581")["status"] == "discovered"
    cooldown = read_source_state(env.engine, SOURCE)["not_before_at"]
    retry_due = target(env)["next_due_at"]
    assert cooldown > first.finished_at and retry_due > cooldown
    reopen(env)

    def no_requests(request):
        pytest.fail("Persisted source cooldown must prevent every HTTP request")

    blocked = run(env, no_requests, clock=clock_at(first.finished_at + 1), opts=opts)
    assert blocked.error_code == "server_cooldown"
    assert blocked.physical_requests == blocked.details_attempted == 0
    assert blocked.remaining_unprocessed == 2
    assert read_source_state(env.engine, SOURCE)["not_before_at"] == cooldown
    assert len(rows(env, raw_responses)) == 4  # Two offline seeds, list 200, metadata-only 429.
    reopen(env)
    resumed_calls = []

    def recovered(request):
        uri = str(request.url)
        resumed_calls.append(uri)
        if uri == HOME:
            return httpx.Response(304)
        return html(
            fixture("current-notice-detail.html" if uri == NOTICE else "legacy-notice-detail.html")
        )

    resumed = run(env, recovered, clock=clock_at(retry_due + 1), opts=opts)
    assert resumed.result == "succeeded" and resumed.details_succeeded == 2
    assert set(resumed_calls) == {HOME, NOTICE, LEGACY}
    assert target(env)["status"] == "processed"
    assert target(env)["current_version_id"] == original
    assert resumed.remaining_unprocessed == 0 and len(rows(env, notice_versions)) == 2


def test_newest_invalid_detail_200_is_reprocessed_on_304_without_old_success_fallback(state_env):
    env = state_env
    seed(env)
    original = target(env)["current_version_id"]
    opts = CrawlOptions(scan_mode="limited", max_pages=1, max_details=2)

    def first_handler(request):
        uri = str(request.url)
        if uri == HOME:
            return html(listing(ids=(128231, 127581)))
        if uri == NOTICE:
            return html(b"<html><main>upstream error template</main></html>", etag='"broken"')
        return html(fixture("legacy-notice-detail.html"))

    first = run(env, first_handler, opts=opts)
    assert (first.details_succeeded, first.details_failed) == (1, 1)
    broken = next(
        row
        for row in rows(env, raw_responses)
        if row["requested_url"] == NOTICE and row["etag"] == '"broken"'
    )
    assert broken["last_error_code"] == "parse_missing_structure"
    original_fetch = broken["fetched_at"]
    before_files = set((env.settings.data_dir / "raw").glob("*.bin"))
    first_due = target(env)["next_due_at"]
    reopen(env)
    calls = []

    def unchanged_invalid(request):
        calls.append(request)
        return httpx.Response(304)

    retried = run(env, unchanged_invalid, clock=clock_at(first_due + 1), opts=opts)
    assert [str(request.url) for request in calls] == [HOME, NOTICE]
    assert calls[1].headers["If-None-Match"] == '"broken"'
    assert retried.details_failed == 1 and retried.details_succeeded == 0
    assert target(env)["status"] == "failed" and target(env)["current_version_id"] == original
    assert set((env.settings.data_dir / "raw").glob("*.bin")) == before_files
    observed = rows(env, raw_responses)[-1]
    assert observed["status_code"] == 304 and observed["body_path"] is None
    assert observed["validated_response_id"] == broken["id"]
    assert observed["last_error_code"] == "parse_missing_structure"
    resource = next(row for row in rows(env, http_resources) if row["request_uri"] == NOTICE)
    assert resource["latest_response_id"] == broken["id"]
    assert resource["last_processed_response_id"] is None
    assert (
        next(row for row in rows(env, raw_responses) if row["id"] == broken["id"])["fetched_at"]
        == original_fetch
    )
    second_due = target(env)["next_due_at"]
    reopen(env)

    def repaired(request):
        return (
            httpx.Response(304)
            if str(request.url) == HOME
            else html(fixture("current-notice-detail.html"), etag='"repaired"')
        )

    recovered = run(env, repaired, clock=clock_at(second_due + 1), opts=opts)
    assert recovered.details_succeeded == 1 and recovered.details_failed == 0
    assert target(env)["status"] == "processed" and target(env)["current_version_id"] == original
    assert len(rows(env, notice_versions)) == 2  # Identical successful content reuses its version.


def test_cross_run_a_b_a_and_bound_304_restore_current_without_duplicate_versions(state_env):
    env = state_env
    a = fixture("current-notice-detail.html")
    b = a.replace(b"2026-2027", b"2026-2028")
    assert a != b
    opts = CrawlOptions(scan_mode="limited", max_pages=1, max_details=1)
    clock = Clock()
    notice_calls = []

    def run_body(body, etag):
        def handler(request):
            if str(request.url) == HOME:
                return (
                    httpx.Response(304) if "If-None-Match" in request.headers else html(listing())
                )
            notice_calls.append(request)
            return html(body, etag=etag) if body is not None else httpx.Response(304)

        return run(env, handler, clock=clock, opts=opts)

    first = run_body(a, '"a"')
    a_version = target(env)["current_version_id"]
    clock = clock_at(target(env)["next_due_at"] + 1)
    reopen(env)
    second = run_body(b, '"b"')
    b_version = target(env)["current_version_id"]
    assert b_version != a_version
    clock = clock_at(target(env)["next_due_at"] + 1)
    reopen(env)
    restored = run_body(a, '"a-returned"')
    assert target(env)["current_version_id"] == a_version < b_version
    assert len(rows(env, notice_versions)) == 2
    returned_response = rows(env, raw_responses)[-1]
    clock = clock_at(target(env)["next_due_at"] + 1)
    reopen(env)
    unchanged = run_body(None, None)
    assert all(result.details_succeeded == 1 for result in (first, second, restored, unchanged))
    assert [result.new_documents for result in (first, second, restored, unchanged)] == [1, 0, 0, 0]
    assert len(rows(env, documents)) == 1 and len(rows(env, notice_versions)) == 2
    assert target(env)["current_version_id"] == a_version
    assert notice_calls[-1].headers["If-None-Match"] == '"a-returned"'
    observed = rows(env, raw_responses)[-1]
    assert observed["status_code"] == 304 and observed["body_sha256"] is None
    assert observed["validated_response_id"] == returned_response["id"]
    assert (
        next(row for row in rows(env, raw_responses) if row["id"] == returned_response["id"])[
            "fetched_at"
        ]
        == returned_response["fetched_at"]
    )
    assert len(tuple((env.settings.data_dir / "raw").glob("*.bin"))) == 3


@pytest.mark.parametrize("failure", ["loop", "drift", "page_budget", "request_budget"])
def test_later_incomplete_run_preserves_previous_complete_scan_checkpoint(state_env, failure):
    env = state_env

    def valid(request):
        return html(listing() if str(request.url) == HOME else listing(2, 2))

    completed = run(env, valid)
    previous = read_source_state(env.engine, SOURCE)
    assert completed.coverage == "complete"
    reopen(env)
    opts = CrawlOptions(scan_mode="full", max_pages=3, max_details=0)
    if failure == "page_budget":
        opts = opts.model_copy(update={"max_pages": 1})
    elif failure == "request_budget":
        opts = opts.model_copy(update={"fetch_limits": FetchLimits(max_requests=2)})

    def interrupted(request):
        if str(request.url) == HOME:
            return html(listing())
        if failure == "loop":
            return httpx.Response(302, headers={"Location": HOME})
        if failure == "drift":
            return html(listing(2, 3, (900001,)))
        return html(listing(2, 2))

    result = run(env, interrupted, clock=clock_at(3000), opts=opts)
    assert result.origin == "regular" and result.coverage != "complete"
    expected = {
        "loop": "pagination_loop",
        "drift": "pagination_drift",
        "page_budget": "page_limit",
        "request_budget": "request_limit",
    }
    assert result.coverage_error_code == expected[failure]
    latest = read_source_state(env.engine, SOURCE)
    assert latest["last_complete_scan_at"] == previous["last_complete_scan_at"]
    assert latest["last_complete_scan_run_id"] == completed.run_id
    assert latest["bootstrap_completed_at"] == previous["bootstrap_completed_at"]
    if failure == "drift":
        assert result.new_documents == 1  # Committed observations survive invalid coverage.


def test_corruption_after_bound_304_uses_full_fetch_without_overwriting_evidence(
    state_env, monkeypatch
):
    import signalnest.crawling as crawling

    env = state_env
    old_id, old_path = baseline(env)
    real_record = crawling.record_response

    def corrupt_after_evidence(*args, **kwargs):
        response_id = real_record(*args, **kwargs)
        if args[2].status_code == 304:
            old_path.write_bytes(b"corrupt retained evidence")
        return response_id

    monkeypatch.setattr(crawling, "record_response", corrupt_after_evidence)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(304) if len(calls) == 1 else html(listing(title="修复后通知"))

    result = run(env, handler, opts=CrawlOptions(max_pages=1, max_details=0))
    assert result.result == "succeeded" and result.physical_requests == 2
    assert "If-None-Match" in calls[0].headers and "If-None-Match" not in calls[1].headers
    responses = rows(env, raw_responses)
    assert [row["status_code"] for row in responses] == [200, 304, 200]
    assert responses[1]["validated_response_id"] == old_id
    assert responses[1]["last_error_code"] == "raw_digest_mismatch"
    assert responses[1]["body_path"] is None and responses[0]["fetched_at"] == 104
    assert old_path.read_bytes() == b"corrupt retained evidence"
    assert rows(env, http_resources)[0]["latest_response_id"] == responses[2]["id"]
    assert len(tuple((env.settings.data_dir / "raw").glob("*.bin"))) == 2


def test_observed_vary_subset_repair_spends_budget_and_next_run_continues(state_env):
    """Live observation replay: 200 varies on UA/encoding, 304 only on UA.

    Keep the conservative policy. Full recovery GET spends the shared request budget;
    an unfetched detail remains pending, and the next bounded run processes it.
    """
    env = state_env

    def handler(request):
        if str(request.url) == HOME:
            if "If-None-Match" in request.headers:
                return httpx.Response(304, headers={"Vary": "User-Agent", "ETag": '"stable"'})
            response = html(listing(ids=(128231, 127581)))
            response.headers["Vary"] = "User-Agent,Accept-Encoding"
            return response
        return html(
            fixture(
                "current-notice-detail.html"
                if str(request.url) == NOTICE
                else "legacy-notice-detail.html"
            )
        )

    seeded = run(env, handler, opts=CrawlOptions(max_pages=1, max_details=0))
    assert seeded.new_documents == 2 and seeded.physical_requests == 1
    opts = CrawlOptions(max_pages=1, max_details=2, fetch_limits=FetchLimits(max_requests=3))
    stopped = run(env, handler, clock=clock_at(2000), opts=opts)
    assert stopped.result == "interrupted" and stopped.error_code == "request_limit"
    assert stopped.details_succeeded == 1 and stopped.remaining_due == 1
    assert stopped.physical_requests == 3 and stopped.new_documents == 0
    reopen(env)
    resumed = run(
        env, handler, clock=clock_at(3000), opts=opts.model_copy(update={"max_details": 1})
    )
    assert resumed.result == "succeeded" and resumed.details_succeeded == 1
    assert resumed.remaining_due == resumed.remaining_unprocessed == 0
    assert resumed.physical_requests == 3 and resumed.new_documents == 0
    assert len(rows(env, documents)) == len(rows(env, notice_versions)) == 2
    observations = [row for row in rows(env, raw_responses) if row["status_code"] == 304]
    assert len(observations) == 2
    assert all(row["last_error_code"] == "cache_repair_required" for row in observations)
    assert all(row["body_path"] is None and row["validated_response_id"] for row in observations)
    assert len(tuple((env.settings.data_dir / "raw").glob("*.bin"))) == 3
    assert read_source_state(env.engine, SOURCE)["last_complete_scan_at"] is None
