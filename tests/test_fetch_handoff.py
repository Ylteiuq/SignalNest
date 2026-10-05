"""Continuation of one logical fetch using real cache evidence and fake HTTP/time."""

from dataclasses import replace

import httpx
import pytest

from signalnest.config import HttpSettings
from signalnest.contracts import RequestProfile
from signalnest.errors import IngestError
from signalnest.fetching import FetchLimits, FetchTarget, HttpFetcher
from signalnest.ingestion import ResponseInput, record_response
from signalnest.ingestion_state import read_source_state, record_list_attempt_in_transaction

SOURCE = "whu-undergrad-student"
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
SECOND = "https://uc.whu.edu.cn/tzgg/xstz/23.htm"
THIRD = "https://uc.whu.edu.cn/tzgg/xstz/22.htm"
PROFILE = RequestProfile(user_agent="SignalNest/handoff", accept="text/html;q=0.9")
TARGET = FetchTarget(uri=HOME, page_type="list")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return 1000.0 + self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def response(status=200, *, body=b"<html>complete</html>", **headers):
    return httpx.Response(
        status,
        headers={"Content-Type": "text/html"} | headers,
        stream=httpx.ByteStream(body),
    )


def baseline(env, uri=HOME):
    metadata = ResponseInput(
        source_id=SOURCE,
        page_type="list",
        requested_url=uri,
        final_url=uri,
        fetched_at=100,
        status_code=200,
        request_profile=PROFILE,
        etag='"baseline"',
    )
    return record_response(env.engine, env.store, metadata, b"<html>baseline</html>")


def fetcher(env, handler, *, clock=None, before_request=None, **limits):
    return HttpFetcher(
        env.engine,
        env.store,
        HttpSettings(
            connect_timeout_seconds=5.0,
            read_timeout_seconds=10.0,
            request_interval_seconds=0.1,
            user_agent=PROFILE.user_agent,
        ),
        source_id=SOURCE,
        profile=PROFILE,
        transport=httpx.MockTransport(handler),
        clock=clock or FakeClock(),
        before_request=before_request,
        jitter=lambda: 0.0,
        limits=FetchLimits(**limits),
    )


def test_revalidate_preserves_exact_profile_on_redirect_and_retry(state_env):
    baseline(state_env, SECOND)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return response(301, Location=SECOND)
        if len(requests) == 2:
            return response(503)
        return response(304, ETag='"baseline"')

    with fetcher(state_env, handler) as worker:
        result = worker.fetch(TARGET, revalidate=True)
    assert result.outcome == "bodyless" and result.error_code is None
    assert len(requests) == 3
    for request in requests:
        assert request.headers["Cache-Control"] == "no-cache"
        assert request.headers["User-Agent"] == PROFILE.user_agent
        assert request.headers["Accept"] == PROFILE.accept
        assert request.headers["Accept-Encoding"] == PROFILE.accept_encoding
    assert "If-None-Match" not in requests[0].headers
    assert requests[1].headers["If-None-Match"] == '"baseline"'
    assert requests[2].headers["If-None-Match"] == '"baseline"'


def test_normal_fetch_does_not_add_revalidation_header(state_env):
    requests = []
    with fetcher(state_env, lambda request: (requests.append(request), response())[1]) as worker:
        assert worker.fetch(TARGET).outcome == "complete"
    assert "Cache-Control" not in requests[0].headers


def test_repair_returns_only_new_attempts_and_no_conditional_headers(state_env):
    baseline(state_env)
    requests = []

    def handler(request):
        requests.append(request)
        return response(304, ETag='"baseline"') if len(requests) == 1 else response()

    with fetcher(state_env, handler) as worker:
        original = worker.fetch(TARGET, revalidate=True)
        assert original.outcome == "bodyless" and original.error_code is None
        repaired = worker.repair(original)
        assert repaired.outcome == "complete"
        assert len(original.attempts) == len(repaired.attempts) == 1
        assert worker.requests_sent == 2
        assert original.response.metadata.status_code == 304
        assert repaired.response.metadata.status_code == 200
        with pytest.raises(ValueError, match="latest successful 304"):
            worker.repair(original)
    assert requests[0].headers["If-None-Match"] == '"baseline"'
    assert "If-None-Match" not in requests[1].headers
    assert "If-Modified-Since" not in requests[1].headers
    assert requests[1].headers["Cache-Control"] == "no-cache"


def test_repair_keeps_shared_request_budget(state_env):
    baseline(state_env)
    with fetcher(state_env, lambda request: response(304), max_requests=1) as worker:
        original = worker.fetch(TARGET)
        repaired = worker.repair(original)
        assert repaired.outcome == "deferred"
        assert repaired.error_code == "request_limit"
        assert repaired.attempts == ()
        assert worker.requests_sent == 1


@pytest.mark.parametrize(
    "resource_seconds,run_seconds,elapsed,expected",
    [(5.0, 20.0, 5.0, "resource_time_limit"), (20.0, 5.0, 5.0, "run_time_limit")],
)
def test_repair_keeps_original_resource_and_run_deadlines(
    state_env, resource_seconds, run_seconds, elapsed, expected
):
    baseline(state_env)
    clock = FakeClock()
    with fetcher(
        state_env,
        lambda request: response(304),
        clock=clock,
        resource_seconds=resource_seconds,
        run_seconds=run_seconds,
    ) as worker:
        original = worker.fetch(TARGET)
        clock.sleep(elapsed)
        repaired = worker.repair(original)
        assert repaired.outcome == "deferred"
        assert repaired.error_code == expected
        assert repaired.attempts == ()
        assert worker.requests_sent == 1


def test_repair_cannot_reset_retry_budget_already_spent(state_env):
    baseline(state_env)
    requests = []

    def handler(request):
        requests.append(request)
        return response(503) if len(requests) == 1 else response(304)

    with fetcher(state_env, handler, max_retries=1) as worker:
        original = worker.fetch(TARGET)
        assert original.error_code is None
        repaired = worker.repair(original)
        assert repaired.outcome == "bodyless"
        assert repaired.error_code == "cache_repair_required"
        assert repaired.attempts == ()
        assert worker.requests_sent == 2


def test_repair_and_transport_retry_share_single_retry_allowance(state_env):
    baseline(state_env)
    requests = []

    def handler(request):
        requests.append(request)
        return response(304) if len(requests) == 1 else response(503)

    with fetcher(state_env, handler, max_retries=2) as worker:
        original = worker.fetch(TARGET)
        repaired = worker.repair(original)
        assert repaired.outcome == "bodyless"
        assert repaired.error_code == "http_transient"
        assert len(repaired.attempts) == 2
        assert worker.requests_sent == 3
    assert all("If-None-Match" not in request.headers for request in requests[1:])


def test_repair_keeps_final_uri_and_redirect_allowance(state_env):
    baseline(state_env, SECOND)
    requests = []

    def handler(request):
        requests.append(request)
        return [response(301, Location=SECOND), response(304), response(301, Location=THIRD)][
            len(requests) - 1
        ]

    with fetcher(state_env, handler, max_redirects=1) as worker:
        original = worker.fetch(TARGET)
        repaired = worker.repair(original)
        assert repaired.error_code == "redirect_limit"
        assert len(repaired.attempts) == 1
        assert worker.requests_sent == 3
    assert str(requests[2].url) == SECOND
    assert "If-None-Match" not in requests[2].headers


def test_repair_full_mode_stays_unconditional_after_redirect(state_env):
    baseline(state_env)
    baseline(state_env, SECOND)
    requests = []

    def handler(request):
        requests.append(request)
        return [response(304), response(301, Location=SECOND), response()][len(requests) - 1]

    with fetcher(state_env, handler) as worker:
        repaired = worker.repair(worker.fetch(TARGET, revalidate=True))
        assert repaired.outcome == "complete"
    for request in requests[1:]:
        assert "If-None-Match" not in request.headers
        assert request.headers["Cache-Control"] == "no-cache"


def test_repair_rejects_foreign_copied_non304_and_superseded_results(state_env):
    baseline(state_env)
    with (
        fetcher(state_env, lambda request: response(304)) as first,
        fetcher(state_env, lambda request: response()) as second,
    ):
        original = first.fetch(TARGET)
        with pytest.raises(ValueError):
            first.repair(replace(original))
        with pytest.raises(ValueError):
            second.repair(original)
        complete = second.fetch(TARGET)
        with pytest.raises(ValueError):
            second.repair(complete)
        first.fetch(TARGET)
        with pytest.raises(ValueError):
            first.repair(original)


def test_repair_rejects_closed_fetcher(state_env):
    baseline(state_env)
    worker = fetcher(state_env, lambda request: response(304))
    original = worker.fetch(TARGET)
    worker.close()
    with pytest.raises(ValueError):
        worker.repair(original)


def test_before_request_commits_attempt_intent_before_http_and_keeps_receipt_time(state_env):
    clock = FakeClock()
    observed = []

    def before_request(target, started_at):
        assert state_env.engine.pool.checkedout() == 0
        with state_env.engine.begin() as connection:
            record_list_attempt_in_transaction(connection, SOURCE, started_at)
        observed.append((target, started_at))
        clock.sleep(2.0)

    def handler(request):
        assert state_env.engine.pool.checkedout() == 0
        assert read_source_state(state_env.engine, SOURCE)["last_list_attempt_at"] == 1000
        return response()

    with fetcher(state_env, handler, clock=clock, before_request=before_request) as worker:
        result = worker.fetch(TARGET)
        assert worker.requests_sent == 1
    assert observed == [(TARGET, 1000)]
    assert result.response.started_at == 1002
    assert result.response.metadata.fetched_at == 1002


def test_before_request_failure_rolls_back_and_does_not_send_or_count_request(state_env):
    requests = []

    def before_request(target, started_at):
        with state_env.engine.begin() as connection:
            record_list_attempt_in_transaction(connection, SOURCE, started_at)
            raise IngestError("database_write_failed", "list_attempt")

    with fetcher(
        state_env,
        lambda request: (requests.append(request), response())[1],
        before_request=before_request,
    ) as worker:
        with pytest.raises(IngestError) as caught:
            worker.fetch(TARGET)
        assert caught.value.code == "database_write_failed"
        assert worker.requests_sent == 0
    assert requests == []
    assert read_source_state(state_env.engine, SOURCE) is None


def test_before_request_is_not_called_when_request_gate_denies_send(state_env):
    attempts = []
    with fetcher(
        state_env,
        lambda request: response(),
        before_request=lambda target, started_at: attempts.append(started_at),
        max_requests=1,
    ) as worker:
        assert worker.fetch(TARGET).outcome == "complete"
        assert worker.fetch(TARGET).error_code == "request_limit"
    assert attempts == [1000]


def test_before_request_time_is_charged_without_creating_fake_physical_attempt(state_env):
    clock = FakeClock()
    requests = []
    with fetcher(
        state_env,
        lambda request: (requests.append(request), response())[1],
        before_request=lambda target, started_at: clock.sleep(5.0),
        clock=clock,
        resource_seconds=5.0,
    ) as worker:
        result = worker.fetch(TARGET)
        assert result.outcome == "deferred"
        assert result.error_code == "resource_time_limit"
        assert result.attempts == ()
        assert worker.requests_sent == 0
    assert requests == []
