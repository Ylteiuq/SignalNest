"""Production Fetcher with offline streams, real RawStore and temporary SQLite.

Fake time verifies policy, not socket deadlines. No campus/DNS/TLS requests occur.
"""

import io
import json
import ssl
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from signalnest.config import HttpSettings
from signalnest.contracts import RequestProfile
from signalnest.errors import IngestError
from signalnest.eventlog import configure_logging
from signalnest.fetching import (
    FetchLimits,
    FetchTarget,
    HttpFetcher,
    retry_after_deadline,
)
from signalnest.ingestion import (
    ResponseInput,
    process_cached_response,
    record_response,
)
from signalnest.ingestion_state import read_source_state, set_cooldown_in_transaction
from signalnest.schema import documents, http_resources, raw_responses
from signalnest.storage import open_initialized_engine

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
SOURCE = "whu-undergrad-student"
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
SECOND = "https://uc.whu.edu.cn/tzgg/xstz/23.htm"
NOTICE = "https://uc.whu.edu.cn/info/1517/128231.htm"
LEGACY = "https://uc.whu.edu.cn/2022/show.jsp?other=keep&wbnewsid=128231&wbtreeid=1517"
PROFILE = RequestProfile(user_agent="SignalNest/test", accept="text/html")
SETTINGS = HttpSettings(
    connect_timeout_seconds=5.0,
    read_timeout_seconds=10.0,
    request_interval_seconds=3.0,
    user_agent=PROFILE.user_agent,
)
TARGET = FetchTarget(uri=HOME, page_type="list")


class FakeClock:
    def __init__(self, epoch=1000.25):
        self.epoch = epoch
        self.now = 0.0
        self.waits = []

    def time(self):
        return self.epoch + self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class TrackedStream(httpx.SyncByteStream):
    def __init__(self, actions):
        self.actions = actions
        self.closed = False
        self.read_count = 0

    def __iter__(self):
        for action in self.actions:
            self.read_count += 1
            if isinstance(action, BaseException):
                raise action
            if callable(action):
                action = action()
            yield action

    def close(self):
        self.closed = True


def html_response(content=b"<html>content</html>", *, status=200, headers=None, stream=None):
    return httpx.Response(
        status,
        headers={"Content-Type": "text/html; charset=UTF-8"} | (headers or {}),
        stream=stream if stream is not None else TrackedStream([content]),
    )


def fetcher(env, handler, *, clock=None, profile=PROFILE, **limits):
    return HttpFetcher(
        env.engine,
        env.store,
        SETTINGS,
        source_id=SOURCE,
        transport=httpx.MockTransport(handler),
        clock=clock or FakeClock(),
        profile=profile,
        limits=FetchLimits(**limits),
        jitter=lambda: 0.25,
    )


def baseline(
    env, *, uri=HOME, content=None, at=100, profile=PROFILE, etag='W/"original"', **headers
):
    metadata = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=uri,
        final_url=uri,
        fetched_at=at,
        status_code=200,
        request_profile=profile,
        etag=etag,
        last_modified="Thu, 01 Oct 2026 08:00:00 GMT",
        **headers,
    )
    content = (
        content if content is not None else (FIXTURES / "student-notices-page1.html").read_bytes()
    )
    return record_response(env.engine, env.store, metadata, content)


def register(env, result):
    return [
        record_response(
            env.engine, env.store, attempt.metadata, attempt.content, candidate=attempt.candidate
        )
        for attempt in result.attempts
        if attempt.metadata is not None
    ]


def rows(env, table):
    with env.engine.connect() as connection:
        return connection.execute(sa.select(table)).mappings().all()


def test_real_request_profile_is_exact_no_auth_cookie_and_no_construction_requests(state_env):
    requests = []
    profile = RequestProfile(user_agent="SignalNest/exact", accept="text/html;q=0.9")

    def respond(request):
        requests.append(request)
        assert state_env.engine.pool.checkedout() == 0
        assert request.headers["User-Agent"] == profile.user_agent
        assert request.headers["Accept"] == profile.accept
        assert request.headers["Accept-Encoding"] == profile.accept_encoding
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert "if-none-match" not in request.headers
        return html_response(headers={"Set-Cookie": "session=secret; Path=/"})

    with fetcher(state_env, respond, profile=profile) as worker:
        assert not requests
        worker.client.headers["Accept"] = "wrong/default"
        worker.client.headers["If-None-Match"] = '"wrong"'
        worker.client.cookies.set("old", "secret", domain="uc.whu.edu.cn")
        worker.client.auth = httpx.BasicAuth("secret", "secret")
        first = worker.fetch(TARGET)
        second = worker.fetch(TARGET)
        assert first.outcome == second.outcome == "complete"
        assert first.response.metadata.request_profile == profile
        assert len(requests) == worker.requests_sent == 2
        assert second.response.started_at - first.response.started_at == 3
        assert worker.client.cookies.get("old") is None
    assert worker.client.is_closed
    assert not rows(state_env, raw_responses)  # caller explicitly archives evidence


def test_actual_receipt_and_finish_times_not_server_date(state_env):
    clock = FakeClock()

    def respond(request):
        clock.advance(1)
        return html_response(
            headers={"Date": "Thu, 01 Jan 1970 00:00:00 GMT"},
            stream=TrackedStream([lambda: (clock.advance(2), b"html")[1]]),
        )

    with fetcher(state_env, respond, clock=clock) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "complete"
    assert result.response.started_at == 1000
    assert result.response.metadata.fetched_at == 1001
    assert result.response.finished_at == 1003
    assert result.response.content == b"html"
    assert result.response.metadata.body_state == "complete"
    assert "content=b" not in repr(result)


@pytest.mark.parametrize(
    "content,headers,limit,expected",
    [
        (b"abcde", {}, 4, "body_too_large"),
        (b"abc", {"Content-Length": "10"}, 4, "body_too_large"),
        (b"abcde", {"Content-Length": "2"}, 4, "body_too_large"),
        (b"abc", {"Content-Length": "4"}, 4, "incomplete_body"),
        (b"abc", {"Content-Length": "2"}, 4, "incomplete_body"),
        (b"abc", {"Content-Length": "-1"}, 4, "invalid_response_headers"),
        (b"abc", {"Content-Length": "1.5"}, 4, "invalid_response_headers"),
        (b"", {}, 4, "empty_body"),
        (b"abc", {"Content-Type": "application/json"}, 4, "unsupported_content_type"),
        (
            b"abc",
            {"Content-Type": "text/html", "Content-Encoding": "gzip"},
            4,
            "unsupported_content_encoding",
        ),
        (b"abc", {"Vary": "a" * 2049}, 4, "invalid_response_headers"),
    ],
)
def test_stream_boundaries_do_not_archive_partial_or_empty_body(
    state_env, content, headers, limit, expected
):
    stream = TrackedStream([content])
    with fetcher(
        state_env,
        lambda _: html_response(headers=headers, stream=stream),
        max_body_bytes=limit,
        max_retries=0,
    ) as worker:
        result = worker.fetch(TARGET)
    assert result.error_code == expected
    assert result.response.content is None and stream.closed
    assert result.response.metadata.body_state == "unavailable"
    ids = register(state_env, result)
    assert len(ids) == 1
    assert rows(state_env, raw_responses)[0]["body_path"] is None
    assert rows(state_env, http_resources)[0]["latest_response_id"] is None
    assert not list((state_env.settings.data_dir / "raw").iterdir())
    if expected in {"unsupported_content_encoding", "unsupported_content_type"}:
        assert stream.read_count == 0


def test_limit_across_chunks_and_exact_limit(state_env):
    streams = [TrackedStream([b"ab", b"cd"]), TrackedStream([b"ab", b"cd", b"e"])]
    with fetcher(
        state_env, lambda _: html_response(stream=streams.pop(0)), max_body_bytes=4
    ) as worker:
        good = worker.fetch(TARGET)
        bad = worker.fetch(TARGET)
    assert good.outcome == "complete" and good.response.content == b"abcd"
    assert bad.error_code == "body_too_large" and bad.response.content is None


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "http_unauthorized"),
        (403, "http_forbidden"),
        (404, "http_not_found"),
        (410, "http_not_found"),
        (501, "http_status"),
        (505, "http_status"),
        (204, "http_status"),
    ],
)
def test_non_retry_statuses_close_without_reading(state_env, status, code):
    stream = TrackedStream([AssertionError("must never read a diagnostic body")])
    with fetcher(state_env, lambda _: html_response(status=status, stream=stream)) as worker:
        result = worker.fetch(TARGET)
        assert worker.requests_sent == 1
    assert result.outcome == "bodyless" and result.error_code == code
    assert result.response.content is None and stream.closed and stream.read_count == 0


@pytest.mark.parametrize(
    "kind,code,retry",
    [
        (httpx.ConnectTimeout, "connect_timeout", True),
        (httpx.ConnectError, "connect_error", True),
        (httpx.ReadTimeout, "read_timeout", True),
        (httpx.ReadError, "read_error", True),
        (httpx.WriteTimeout, "write_timeout", True),
        (httpx.WriteError, "write_error", True),
        (httpx.PoolTimeout, "pool_timeout", False),
        (httpx.LocalProtocolError, "local_protocol_error", False),
        (httpx.RemoteProtocolError, "remote_protocol_error", True),
        (httpx.DecodingError, "decoding_error", False),
    ],
)
def test_one_bounded_transport_retry_layer_without_fabricated_response(
    state_env, kind, code, retry
):
    def fail(request):
        raise kind("secret arbitrary error body", request=request)

    with fetcher(state_env, fail) as worker:
        result = worker.fetch(TARGET)
        assert worker.requests_sent == (3 if retry else 1)
    assert result.outcome == "transport_failure" and result.error_code == code
    assert all(a.metadata is None and a.content is None for a in result.attempts)
    assert "secret" not in repr(result)


def test_certificate_failure_is_not_retried_or_insecure(state_env):
    def fail(request):
        raise httpx.ConnectError(
            "private details", request=request
        ) from ssl.SSLCertVerificationError()

    with fetcher(state_env, fail) as worker:
        result = worker.fetch(TARGET)
        assert worker.requests_sent == 1
    assert result.error_code == "tls_error"


def test_partial_read_failure_discards_bytes_retries_and_closes_both_streams(state_env):
    streams = [TrackedStream([b"partial", httpx.ReadError("secret")]), TrackedStream([b"complete"])]
    original = list(streams)
    with fetcher(state_env, lambda _: html_response(stream=streams.pop(0))) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "complete" and len(result.attempts) == 2
    assert result.attempts[0].error_code == "read_error" and result.attempts[0].content is None
    assert result.attempts[0].metadata.status_code == 200
    assert result.attempts[0].metadata.body_state == "unavailable"
    assert result.response.content == b"complete" and all(s.closed for s in original)
    register(state_env, result)
    assert [r["body_state"] for r in rows(state_env, raw_responses)] == ["unavailable", "complete"]


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_transient_status_uses_only_two_extra_attempts(state_env, status):
    clock = FakeClock()
    starts = []

    def respond(request):
        starts.append(clock.monotonic())
        return html_response(status=status)

    with fetcher(state_env, respond, clock=clock) as worker:
        result = worker.fetch(TARGET)
    assert starts == [0, 3, 6]
    assert len(result.attempts) == 3 and result.error_code == "http_transient"
    assert result.outcome == "bodyless"
    assert all(a.content is None and a.metadata.status_code == status for a in result.attempts)


def test_backoff_and_gate_use_maximum_not_nested_sleep(state_env):
    clock = FakeClock()
    starts = []

    def respond(request):
        starts.append(clock.monotonic())
        if len(starts) == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        if len(starts) == 2:
            return html_response(status=503)
        return html_response()

    with fetcher(state_env, respond, clock=clock, backoff_seconds=2.0) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "complete"
    assert starts == [0, 3, 7.25] and clock.waits == [3, 4.25]


@pytest.mark.parametrize(
    "uri",
    [
        "http://uc.whu.edu.cn/tzgg/xstz.htm",
        "https://other.example/tzgg/xstz.htm",
        "https://uc.whu.edu.cn:444/tzgg/xstz.htm",
        "https://secret@uc.whu.edu.cn/tzgg/xstz.htm",
        "https://uc.whu.edu.cn/tzgg/xstz.htm#",
        "https://uc.whu.edu.cn/tzgg/xstz.htm?",
        "https://uc.whu.edu.cn/tzgg/another.htm",
        "https://uc.whu.edu.cn/tzgg/xstz/0.htm",
        "https://uc.whu.edu.cn/2022/show.jsp?wbnewsid=1&wbtreeid=1517",
        "https://uc.whu.edu.cn/tzgg/\\xstz.htm",
        "not a URL",
    ],
)
def test_initial_target_validation_prevents_any_request(state_env, uri):
    with fetcher(state_env, lambda _: pytest.fail("invalid target was requested")) as worker:
        result = worker.fetch(FetchTarget(uri=uri, page_type="list"))
    assert result.error_code == "invalid_target" and not result.attempts


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/tzgg/xstz.htm",
        "http://uc.whu.edu.cn/tzgg/xstz/23.htm",
        "https://uc.whu.edu.cn:444/tzgg/xstz/23.htm",
        "https://user:secret@uc.whu.edu.cn/tzgg/xstz/23.htm",
        "/login",
        "/system/_content/download.jsp?wbfileid=1",
        "/tzgg/xstz/23.htm#",
        "/tzgg/xstz/23.htm?",
        "javascript:void(0)",
        "/tzgg/\\xstz/23.htm",
        "//other.example/",
    ],
)
def test_redirect_target_rejected_before_follow(state_env, location):
    stream = TrackedStream([AssertionError("don't read redirect")])
    with fetcher(
        state_env,
        lambda _: html_response(status=302, headers={"Location": location}, stream=stream),
    ) as worker:
        result = worker.fetch(TARGET)
        assert worker.requests_sent == 1
    assert result.error_code == "redirect_invalid" and stream.closed and stream.read_count == 0


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_manual_redirect_reselects_exact_target_profile_and_drops_first_validator(
    state_env, status
):
    first_id = baseline(state_env)
    second_id = baseline(state_env, uri=SECOND, at=101, etag='"second"')
    requests = []
    streams = []

    def respond(request):
        requests.append(request)
        stream = TrackedStream([AssertionError("bodyless response")])
        streams.append(stream)
        if str(request.url) == HOME:
            assert request.headers["If-None-Match"] == 'W/"original"'
            return html_response(status=status, headers={"Location": "xstz/23.htm"}, stream=stream)
        assert str(request.url) == SECOND and request.method == "GET"
        assert request.headers["If-None-Match"] == '"second"'
        return html_response(status=304, stream=stream)

    with fetcher(state_env, respond) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "bodyless" and result.error_code is None
    assert result.response.candidate.response_id == second_id != first_id
    assert result.attempts[0].candidate is None
    assert all(s.closed and s.read_count == 0 for s in streams)
    assert [str(r.url) for r in requests] == [HOME, SECOND]
    register(state_env, result)
    assert rows(state_env, raw_responses)[-1]["validated_response_id"] == second_id


def test_redirect_to_uncached_uri_never_forwards_previous_validators(state_env):
    baseline(state_env)
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            assert request.headers["If-None-Match"] == 'W/"original"'
            return html_response(status=302, headers={"Location": SECOND})
        assert "if-none-match" not in request.headers and "if-modified-since" not in request.headers
        return html_response()

    with fetcher(state_env, respond) as worker:
        assert worker.fetch(TARGET).outcome == "complete"


def test_redirect_cycles_and_cumulative_hop_limit(state_env):
    with fetcher(
        state_env, lambda _: html_response(status=302, headers={"Location": HOME})
    ) as worker:
        assert worker.fetch(TARGET).error_code == "redirect_loop"
    starts = []

    def respond(request):
        starts.append(str(request.url))
        if len(starts) == 2:
            return html_response(status=503)
        return html_response(status=302, headers={"Location": f"/tzgg/xstz/{len(starts)}.htm"})

    with fetcher(state_env, respond, max_redirects=2) as worker:
        result = worker.fetch(TARGET)
    assert len(starts) == 4  # original + 2 hops + 1 shared retry; no counter reset
    assert result.error_code == "redirect_limit"


@pytest.mark.parametrize("headers", [{}, {"Location": ""}])
def test_redirect_missing_location_is_explicit_failure(state_env, headers):
    with fetcher(state_env, lambda _: html_response(status=302, headers=headers)) as worker:
        result = worker.fetch(TARGET)
    assert result.error_code == "redirect_invalid" and len(result.attempts) == 1


def test_duplicate_location_is_not_guessed(state_env):
    response = httpx.Response(302, headers=[("Location", SECOND), ("Location", HOME)])
    with fetcher(state_env, lambda _: response) as worker:
        assert worker.fetch(TARGET).error_code == "redirect_invalid"
    assert response.is_closed


def test_notice_redirect_old_route_keeps_identity_and_exact_query_order(state_env):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return (
            html_response(status=302, headers={"Location": LEGACY})
            if len(requests) == 1
            else html_response()
        )

    target = FetchTarget(uri=NOTICE, page_type="notice", source_document_id="1517:128231")
    with fetcher(state_env, respond) as worker:
        result = worker.fetch(target)
    assert result.outcome == "complete" and requests == [NOTICE, LEGACY]
    assert str(result.response.metadata.requested_url) == LEGACY
    assert result.response.metadata.source_document_id == "1517:128231"


@pytest.mark.parametrize(
    "url",
    [
        "https://uc.whu.edu.cn/info/1517/128232.htm",
        "https://uc.whu.edu.cn/info/1518/128231.htm",
        "https://uc.whu.edu.cn/2022/show.jsp?wbnewsid=128231&wbtreeid=1517&wbtreeid=1517",
        "https://uc.whu.edu.cn/2022/show.jsp?wbnewsid=128231&wbtreeid=1518",
        "https://uc.whu.edu.cn/2022/show.jsp?wbnewsid=128231",
        "https://uc.whu.edu.cn/2022/show.jsp?wbnewsid=128231&wbtreeid=1517&urltype=other",
        "https://uc.whu.edu.cn/info/1517/128231.htm?wbnewsid=another",
    ],
)
def test_notice_identity_cannot_change_during_redirect(state_env, url):
    target = FetchTarget(uri=NOTICE, page_type="notice", source_document_id="1517:128231")
    with fetcher(
        state_env, lambda _: html_response(status=302, headers={"Location": url})
    ) as worker:
        result = worker.fetch(target)
    assert result.error_code == "redirect_invalid" and len(result.attempts) == 1


def test_physical_request_budget_shared_by_retry_redirect_and_next_fetch(state_env):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return (
            html_response(status=503)
            if len(calls) == 1
            else html_response(status=302, headers={"Location": SECOND})
        )

    with fetcher(state_env, respond, max_requests=2) as worker:
        result = worker.fetch(TARGET)
        again = worker.fetch(TARGET)
    assert len(calls) == 2 and len(result.attempts) == 2
    assert result.outcome == again.outcome == "deferred"
    assert result.error_code == again.error_code == "request_limit" and not again.attempts


def test_run_budget_does_not_reset_on_next_fetch(state_env):
    clock = FakeClock()
    with fetcher(state_env, lambda _: html_response(), clock=clock, run_seconds=4.0) as worker:
        assert worker.fetch(TARGET).outcome == "complete"
        clock.advance(4)
        result = worker.fetch(TARGET)
        assert worker.requests_sent == 1
    assert result.outcome == "deferred" and result.error_code == "run_time_limit"


def test_all_timeouts_tightened_by_remaining_budget_before_each_hop(state_env):
    clock = FakeClock()
    timeouts = []

    def respond(request):
        timeouts.append(request.extensions["timeout"])
        return (
            html_response(status=302, headers={"Location": SECOND})
            if len(timeouts) == 1
            else html_response()
        )

    with fetcher(state_env, respond, clock=clock, resource_seconds=5.0) as worker:
        assert worker.fetch(TARGET).outcome == "complete"
    assert timeouts == [
        dict(connect=5.0, read=5.0, write=5.0, pool=5.0),
        dict(connect=2.0, read=2.0, write=2.0, pool=2.0),
    ]


def test_gate_wait_spends_resource_budget_without_sending_too_late(state_env):
    with fetcher(state_env, lambda _: html_response(status=503), resource_seconds=2.0) as worker:
        result = worker.fetch(TARGET)
        assert worker.requests_sent == 1
    assert result.outcome == "deferred" and result.error_code == "resource_time_limit"
    assert len(result.attempts) == 1


@pytest.mark.parametrize(
    "budget,code",
    [
        ({"resource_seconds": 5.0}, "resource_time_limit"),
        ({"run_seconds": 5.0}, "run_time_limit"),
    ],
)
def test_stream_checks_cooperative_deadline_each_raw_chunk(state_env, budget, code):
    clock = FakeClock()
    stream = TrackedStream([lambda: (clock.advance(2), b"chunk")[1] for _ in range(5)])
    with fetcher(
        state_env, lambda _: html_response(stream=stream), clock=clock, **budget
    ) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "deferred" and result.error_code == code
    assert result.response.content is None and result.response.metadata.body_state == "unavailable"
    assert stream.closed and stream.read_count == 3 and clock.monotonic() == 6
    register(state_env, result)
    assert rows(state_env, raw_responses)[0]["body_path"] is None


def test_response_received_after_deadline_keeps_metadata_without_reading(state_env):
    clock = FakeClock()
    stream = TrackedStream([AssertionError("late body")])

    def respond(request):
        clock.advance(10)
        return html_response(stream=stream)

    with fetcher(state_env, respond, clock=clock, resource_seconds=5.0) as worker:
        result = worker.fetch(TARGET)
    assert (
        result.error_code == "resource_time_limit" and result.response.metadata.fetched_at == 1010
    )
    assert stream.closed and stream.read_count == 0


def test_cache_file_check_is_outside_transaction_and_spends_budget(state_env, monkeypatch):
    baseline(state_env)
    clock = FakeClock()
    read = state_env.store.read

    def slow_read(*args):
        assert state_env.engine.pool.checkedout() == 0
        clock.advance(61)
        return read(*args)

    monkeypatch.setattr(state_env.store, "read", slow_read)
    with fetcher(
        state_env, lambda _: pytest.fail("cache verification consumed budget"), clock=clock
    ) as worker:
        result = worker.fetch(TARGET)
    assert result.error_code == "resource_time_limit" and not result.attempts


@pytest.mark.parametrize(
    "values,expected",
    [
        (["120"], 1121),
        (["0"], 1001),
        (["0002"], 1003),
        (["Thu, 01 Jan 1970 00:18:40 GMT"], 1120),
        (["Thu, 01 Jan 1970 00:00:01 GMT"], 1001),
        (["Thursday, 01-Jan-70 00:18:40 GMT"], 1120),
        (["Thu Jan  1 00:18:40 1970"], 1120),
        ([], None),
        (["-1"], None),
        (["1.5"], None),
        (["invalid"], None),
        ([""], None),
        (["120", "121"], None),
        (["١٢٠"], None),
        (["Thu, 01 Jan 1970 00:18:40 +0800"], None),
        (["120\r\nsecret"], None),
    ],
)
def test_retry_after_is_full_utc_deadline_never_invalid_zero(values, expected):
    deadline, overflow = retry_after_deadline(values, 1000.25)
    assert deadline == expected and overflow is False


@pytest.mark.parametrize("values", [["9" * 30], [str(2**63 - 1)]])
def test_retry_after_overflow_never_wraps_or_shortens(values):
    assert retry_after_deadline(values, 1000.25) == (2**63 - 1, True)


@pytest.mark.parametrize("status", [429, 503, 302])
def test_long_server_wait_persisted_whole_and_applies_to_other_uri(state_env, status):
    headers = {"Retry-After": "120", "Location": SECOND}
    with fetcher(state_env, lambda _: html_response(status=status, headers=headers)) as worker:
        first = worker.fetch(TARGET)
        other = worker.fetch(FetchTarget(uri=SECOND, page_type="list"))
        assert worker.requests_sent == 1
    assert first.outcome == other.outcome == "deferred"
    assert first.not_before_at == other.not_before_at == 1121
    assert read_source_state(state_env.engine, SOURCE)["not_before_at"] == 1121
    assert not other.attempts
    assert first.response.content is None


@pytest.mark.parametrize("retry_after", [None, "-1", "1.5", "not a date"])
def test_invalid_or_missing_429_waits_default_not_zero(state_env, retry_after):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    with fetcher(state_env, lambda _: html_response(status=429, headers=headers)) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "deferred" and result.error_code == "http_rate_limited"
    assert (
        result.not_before_at == read_source_state(state_env.engine, SOURCE)["not_before_at"] == 2801
    )


def test_duplicate_retry_after_is_conservative_default(state_env):
    response = httpx.Response(429, headers=[("Retry-After", "0"), ("Retry-After", "120")])
    with fetcher(state_env, lambda _: response) as worker:
        result = worker.fetch(TARGET)
    assert result.not_before_at == 2801 and response.is_closed


def test_short_retry_after_waits_without_shortening_and_keeps_no_transaction(state_env):
    clock = FakeClock()
    starts = []
    sleep = clock.sleep

    def checked_sleep(seconds):
        assert state_env.engine.pool.checkedout() == 0
        sleep(seconds)

    clock.sleep = checked_sleep

    def respond(request):
        assert state_env.engine.pool.checkedout() == 0
        starts.append(clock.monotonic())
        return (
            html_response(status=503, headers={"Retry-After": "6"})
            if len(starts) == 1
            else html_response()
        )

    with fetcher(state_env, respond, clock=clock) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "complete" and starts == [0, 6.75]
    assert read_source_state(state_env.engine, SOURCE)["not_before_at"] == 1007
    assert result.attempts[0].not_before_at == 1007


def test_absolute_http_date_cooldown(state_env):
    headers = {"Retry-After": format_datetime(datetime.fromtimestamp(1100, UTC), usegmt=True)}
    with fetcher(state_env, lambda _: html_response(status=503, headers=headers)) as worker:
        result = worker.fetch(TARGET)
    assert result.not_before_at == 1100


def test_cooldown_survives_reopen_then_expires_without_new_migration(state_env):
    clock = FakeClock()
    with fetcher(
        state_env, lambda _: html_response(status=429, headers={"Retry-After": "120"}), clock=clock
    ) as worker:
        first = worker.fetch(TARGET)
    state_env.engine.dispose()
    state_env.engine = open_initialized_engine(state_env.settings.database)
    try:
        with fetcher(
            state_env, lambda _: pytest.fail("must not bypass persistent cooldown"), clock=clock
        ) as worker:
            result = worker.fetch(TARGET)
        assert result.outcome == "deferred" and result.not_before_at == first.not_before_at == 1121
        clock.advance(121)
        with fetcher(state_env, lambda _: html_response(), clock=clock) as worker:
            assert worker.fetch(TARGET).outcome == "complete"
    finally:
        state_env.engine.dispose()


def test_cooldown_write_failure_is_system_error_and_response_is_closed(state_env):
    with state_env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_cooldown BEFORE INSERT ON source_ingestion_state "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    stream = TrackedStream([AssertionError("mustn't read rate-limit body")])
    with fetcher(state_env, lambda _: html_response(status=429, stream=stream)) as worker:
        with pytest.raises(IngestError, match="cooldown_state_unavailable"):
            worker.fetch(TARGET)
    assert stream.closed and read_source_state(state_env.engine, SOURCE) is None


def test_unrepresentable_wait_fails_closed_with_persistent_maximum(state_env):
    with fetcher(
        state_env, lambda _: html_response(status=429, headers={"Retry-After": "9" * 30})
    ) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "deferred" and result.error_code == "retry_after_out_of_range"
    assert (
        result.not_before_at
        == read_source_state(state_env.engine, SOURCE)["not_before_at"]
        == 2**63 - 1
    )


def test_preexisting_cooldown_never_shortened_and_budget_can_refuse_short_wait(state_env):
    with state_env.engine.begin() as connection:
        set_cooldown_in_transaction(connection, SOURCE, 1010)
        set_cooldown_in_transaction(connection, SOURCE, 1001)
    with fetcher(
        state_env,
        lambda _: pytest.fail("waiting would spend entire resource budget"),
        resource_seconds=5.0,
    ) as worker:
        result = worker.fetch(TARGET)
    assert result.error_code == "resource_time_limit" and result.not_before_at == 1010
    assert (
        not result.attempts and read_source_state(state_env.engine, SOURCE)["not_before_at"] == 1010
    )


def test_valid_304_uses_bound_archived_bytes_without_forging_new_body_or_fetch_time(state_env):
    first_id = baseline(state_env)
    requests = []
    stream = TrackedStream([AssertionError("304 has no body")])

    def respond(request):
        requests.append(request)
        assert request.headers["If-None-Match"] == 'W/"original"'
        assert request.headers["If-Modified-Since"] == "Thu, 01 Oct 2026 08:00:00 GMT"
        return html_response(status=304, stream=stream)

    with fetcher(state_env, respond) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "bodyless" and result.error_code is None and len(requests) == 1
    assert result.response.content is None and result.response.candidate.response_id == first_id
    observed_id = register(state_env, result)[0]
    processed = process_cached_response(state_env.engine, state_env.store, observed_id, 1002)
    assert processed.discovered_count == 25 and processed.body_response_id == first_id
    responses = rows(state_env, raw_responses)
    assert [r["fetched_at"] for r in responses] == [100, 1000]
    assert responses[1]["validated_response_id"] == first_id
    assert responses[1]["body_path"] is None and stream.closed and stream.read_count == 0
    assert len(list((state_env.settings.data_dir / "raw").iterdir())) == 1
    process_cached_response(
        state_env.engine, state_env.store, observed_id, 1003, list_parser_version="test-new-rules"
    )
    assert rows(state_env, http_resources)[0]["last_processed_parser_version"] == "test-new-rules"
    assert [r["fetched_at"] for r in rows(state_env, raw_responses)] == [100, 1000]


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_304_file_fault_after_conditional_send_repairs_once_with_full_get(state_env, fault):
    baseline(state_env)
    path = state_env.settings.data_dir / rows(state_env, raw_responses)[0]["body_path"]
    requests = []
    full_body = (FIXTURES / "student-notices-page1.html").read_bytes()
    if fault == "corrupt":
        full_body += b"<!--new raw evidence-->"  # preserve corrupt file, never overwrite silently

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            assert "if-none-match" in request.headers
            path.unlink() if fault == "missing" else path.write_bytes(b"corrupt")
            return html_response(status=304)
        assert "if-none-match" not in request.headers and "if-modified-since" not in request.headers
        return html_response(full_body, headers={"ETag": '"refetched"'})

    with fetcher(state_env, respond) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "complete" and len(requests) == 2
    assert result.attempts[0].error_code == "cache_repair_required"
    assert result.attempts[0].candidate is not None and result.attempts[0].content is None
    ids = register(state_env, result)
    assert (
        process_cached_response(state_env.engine, state_env.store, ids[-1], 1010).discovered_count
        == 25
    )
    assert (
        rows(state_env, raw_responses)[1]["validated_response_id"]
        == result.attempts[0].candidate.response_id
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"ETag": '"different"'},
        {"Vary": "*"},
        {"Vary": "Cookie"},
        {"Cache-Control": "no-store"},
        {"Last-Modified": "Thu, 01 Oct 2026 08:00:01 GMT"},
    ],
)
def test_conflicting_304_headers_full_refetch_and_concrete_evidence_binding(state_env, headers):
    first_id = baseline(state_env)
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            return html_response(status=304, headers=headers)
        assert "if-none-match" not in request.headers and "if-modified-since" not in request.headers
        return html_response(headers={"ETag": '"fresh"'})

    with fetcher(state_env, respond) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "complete" and count == 2
    ids = register(state_env, result)
    assert rows(state_env, raw_responses)[1]["validated_response_id"] == first_id
    resource = rows(state_env, http_resources)[0]
    assert resource["latest_response_id"] == ids[-1] and resource["blocked_by_response_id"] is None


def test_unconditional_304_once_repaired_no_infinite_loop(state_env):
    requests = []

    def respond(request):
        requests.append(request)
        assert "if-none-match" not in request.headers
        return html_response(status=304)

    with fetcher(state_env, respond) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "bodyless" and result.error_code == "unexpected_304"
    assert len(requests) == 2 and all(a.content is None for a in result.attempts)
    register(state_env, result)
    assert all(r["body_path"] is None for r in rows(state_env, raw_responses))
    assert not list((state_env.settings.data_dir / "raw").iterdir())


def test_304_repair_spends_same_retry_budget_as_transport_failure(state_env):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return html_response(status=304)

    with fetcher(state_env, respond, max_retries=1) as worker:
        result = worker.fetch(TARGET)
    assert result.error_code == "unexpected_304" and len(requests) == 2


@pytest.mark.parametrize(
    "headers",
    [
        {"vary": "Cookie"},
        {"vary": "*"},
        {"cache_control": "no-store"},
        {"content_encoding": "gzip"},
    ],
)
def test_ineligible_baseline_never_sends_conditional_headers(state_env, headers):
    baseline(state_env, **headers)

    def respond(request):
        assert "if-none-match" not in request.headers and "if-modified-since" not in request.headers
        return html_response()

    with fetcher(state_env, respond) as worker:
        assert worker.fetch(TARGET).outcome == "complete"


def test_changed_profile_cannot_reuse_old_validators(state_env):
    baseline(state_env)
    changed = RequestProfile(user_agent="SignalNest/new-profile", accept="text/html;q=0.9")

    def respond(request):
        assert request.headers["User-Agent"] == changed.user_agent
        assert request.headers["Accept"] == changed.accept
        assert "if-none-match" not in request.headers
        return html_response()

    with fetcher(state_env, respond, profile=changed) as worker:
        assert worker.fetch(TARGET).outcome == "complete"


def test_failed_latest_200_is_valid_transport_baseline_not_old_success(state_env):
    first_id = baseline(state_env)
    process_cached_response(state_env.engine, state_env.store, first_id, 101)
    metadata = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=HOME,
        final_url=HOME,
        fetched_at=102,
        status_code=200,
        request_profile=PROFILE,
        etag='"failed-latest"',
    )
    bad_id = record_response(
        state_env.engine, state_env.store, metadata, b"<html>wrong template</html>"
    )
    with pytest.raises(IngestError, match="parse_missing_structure"):
        process_cached_response(state_env.engine, state_env.store, bad_id, 103)

    def respond(request):
        assert request.headers["If-None-Match"] == '"failed-latest"'
        return html_response(status=304)

    with fetcher(state_env, respond) as worker:
        result = worker.fetch(TARGET)
    observed_id = register(state_env, result)[0]
    assert result.response.candidate.response_id == bad_id != first_id
    with pytest.raises(IngestError, match="parse_missing_structure"):
        process_cached_response(state_env.engine, state_env.store, observed_id, 1001)
    resource = rows(state_env, http_resources)[0]
    assert (
        resource["latest_response_id"] == bad_id
        and resource["last_processed_response_id"] == first_id
    )
    assert len(rows(state_env, documents)) == 25


def test_explicit_unconditional_request_ignores_eligible_candidate(state_env):
    baseline(state_env)

    def respond(request):
        assert "if-none-match" not in request.headers and "if-modified-since" not in request.headers
        return html_response()

    with fetcher(state_env, respond) as worker:
        assert worker.fetch(TARGET, unconditional=True).outcome == "complete"


def test_latin1_etag_sent_verbatim_including_weak_prefix(state_env):
    metadata = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=HOME,
        final_url=HOME,
        fetched_at=100,
        status_code=200,
        request_profile=PROFILE,
        etag='W/"\xff"',
    )
    record_response(state_env.engine, state_env.store, metadata, b"raw")

    def respond(request):
        assert (b"If-None-Match", b'W/"\xff"') in request.headers.raw
        return html_response(status=304, headers={"ETag": b'W/"\xff"'})

    with fetcher(state_env, respond) as worker:
        assert worker.fetch(TARGET).error_code is None


@pytest.mark.parametrize("field", ["user_agent", "accept"])
def test_profile_rejects_values_httpx_cannot_send_exactly(field):
    with pytest.raises(ValidationError):
        RequestProfile.model_validate(
            {"user_agent": "SignalNest", "accept": "text/html", field: "中文"}
        )


def test_program_defect_is_not_converted_to_ordinary_fetch_failure(state_env):
    def defect(request):
        raise AssertionError("implementation defect")

    with fetcher(state_env, defect) as worker:
        with pytest.raises(AssertionError, match="implementation defect"):
            worker.fetch(TARGET)


def test_fetch_logs_only_finite_events_and_context_not_url_headers_or_body(state_env):
    log = io.StringIO()
    logger = configure_logging(log)
    try:
        with fetcher(
            state_env, lambda _: html_response(status=403, headers={"Set-Cookie": "private=secret"})
        ) as worker:
            worker.fetch(TARGET)
        payloads = [json.loads(line) for line in log.getvalue().splitlines()]
        assert [p["event"] for p in payloads] == ["fetch_started", "fetch_finished"]
        assert payloads[-1]["error_code"] == "http_forbidden"
        assert all(p["source_id"] == SOURCE and "time" in p and "level" in p for p in payloads)
        assert "secret" not in log.getvalue() and "http" not in payloads[0]["stage"]
        assert "uc.whu.edu.cn" not in log.getvalue() and "<html>" not in log.getvalue()
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


@pytest.mark.parametrize("name", ["Content-Type", "Content-Encoding", "ETag", "Last-Modified"])
def test_duplicate_singleton_response_headers_are_rejected(state_env, name):
    values = {
        "Content-Type": "text/html",
        "Content-Encoding": "identity",
        "ETag": '"one"',
        "Last-Modified": "Thu, 01 Oct 2026 08:00:00 GMT",
    }
    stream = TrackedStream([AssertionError("invalid headers must not produce baseline")])
    headers = [("Content-Type", "text/html")] if name != "Content-Type" else []
    headers += [(name, values[name]), (name, values[name])]
    response = httpx.Response(200, headers=headers, stream=stream)
    with fetcher(state_env, lambda _: response) as worker:
        result = worker.fetch(TARGET)
    assert result.error_code == "invalid_response_headers" and result.response.content is None
    assert stream.closed and stream.read_count == 0


def test_encoded_304_is_not_accepted_for_identity_baseline(state_env):
    baseline(state_env)
    calls = []

    def respond(request):
        calls.append(request)
        return (
            html_response(status=304, headers={"Content-Encoding": "gzip"})
            if len(calls) == 1
            else html_response()
        )

    with fetcher(state_env, respond) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "complete" and len(calls) == 2
    assert "if-none-match" not in calls[-1].headers
    register(state_env, result)


@pytest.mark.parametrize("validator", ["etag", "last_modified"])
def test_single_validator_sent_exactly_without_fabricating_other_header(state_env, validator):
    value = '"only-tag"' if validator == "etag" else "Thu, 01 Oct 2026 08:00:00 GMT"
    metadata = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=HOME,
        final_url=HOME,
        fetched_at=100,
        status_code=200,
        request_profile=PROFILE,
        **{validator: value},
    )
    record_response(state_env.engine, state_env.store, metadata, b"raw")

    def respond(request):
        assert request.headers.get("If-None-Match") == (value if validator == "etag" else None)
        assert request.headers.get("If-Modified-Since") == (
            value if validator == "last_modified" else None
        )
        return html_response(status=304)

    with fetcher(state_env, respond) as worker:
        assert worker.fetch(TARGET).error_code is None


def test_offline_response_without_profile_does_not_gain_cache_eligibility(state_env):
    metadata = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=HOME,
        final_url=HOME,
        fetched_at=100,
        status_code=200,
        etag='"offline"',
    )
    record_response(state_env.engine, state_env.store, metadata, b"raw")

    def respond(request):
        assert "if-none-match" not in request.headers and "if-modified-since" not in request.headers
        return html_response()

    with fetcher(state_env, respond) as worker:
        assert worker.fetch(TARGET).outcome == "complete"


def test_corrupt_file_before_request_requires_full_get_without_validator(state_env):
    baseline(state_env)
    path = state_env.settings.data_dir / rows(state_env, raw_responses)[0]["body_path"]
    path.write_bytes(b"damaged")

    def respond(request):
        assert "if-none-match" not in request.headers and "if-modified-since" not in request.headers
        return html_response()

    with fetcher(state_env, respond) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "complete" and len(result.attempts) == 1
    assert path.read_bytes() == b"damaged"  # Fetcher never repairs/overwrites archives itself.


def test_304_repair_respects_global_request_limit(state_env):
    with fetcher(state_env, lambda _: html_response(status=304), max_requests=1) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "deferred" and result.error_code == "request_limit"
    assert len(result.attempts) == 1 and result.response.content is None


def test_304_post_response_cache_verification_also_spends_budget(state_env, monkeypatch):
    baseline(state_env)
    clock = FakeClock()
    calls = 0
    read = state_env.store.read

    def slow_second_read(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            clock.advance(61)
        return read(*args)

    monkeypatch.setattr(state_env.store, "read", slow_second_read)
    with fetcher(state_env, lambda _: html_response(status=304), clock=clock) as worker:
        result = worker.fetch(TARGET)
    assert result.outcome == "deferred" and result.error_code == "resource_time_limit"
    assert len(result.attempts) == 1 and result.response.content is None


def test_oversized_content_length_header_is_classified_not_python_int_error(state_env):
    stream = TrackedStream([AssertionError("invalid huge header")])
    with fetcher(
        state_env, lambda _: html_response(headers={"Content-Length": "0" * 5000}, stream=stream)
    ) as worker:
        result = worker.fetch(TARGET)
    assert result.error_code == "invalid_response_headers" and stream.closed


def test_cache_target_mismatch_never_sent_as_notice_validator(state_env):
    first = baseline(state_env)
    process_cached_response(state_env.engine, state_env.store, first, 101)
    wrong_type = ResponseInput(
        page_type="list",
        source_id=SOURCE,
        requested_url=NOTICE,
        final_url=NOTICE,
        fetched_at=102,
        status_code=200,
        request_profile=PROFILE,
        etag='"wrong-target"',
    )
    record_response(state_env.engine, state_env.store, wrong_type, b"raw")
    requests = []

    def respond(request):
        requests.append(request)
        assert "if-none-match" not in request.headers
        return html_response(status=304) if len(requests) == 1 else html_response()

    target = FetchTarget(uri=NOTICE, page_type="notice", source_document_id="1517:128231")
    with fetcher(state_env, respond) as worker:
        result = worker.fetch(target)
    assert result.outcome == "complete" and result.attempts[0].candidate is None
    register(state_env, result)  # anomalous 304 remains recordable as unbound evidence


def test_default_hop_plus_shared_retry_bound_is_six_physical_attempts(state_env):
    clock = FakeClock()
    starts = []
    streams = []

    def respond(request):
        starts.append(clock.monotonic())
        count = len(starts)
        if count == 2:
            raise httpx.ReadTimeout("temporary", request=request)
        stream = TrackedStream([AssertionError("no body from redirects/transient status")])
        streams.append(stream)
        if count in {4, 6}:
            return html_response(status=503, stream=stream)
        return html_response(
            status=302, headers={"Location": f"/tzgg/xstz/{count}.htm"}, stream=stream
        )

    with fetcher(state_env, respond, clock=clock) as worker:
        result = worker.fetch(TARGET)
        assert worker.requests_sent == 6
    assert result.error_code == "http_transient" and len(result.attempts) == 6
    assert starts == [0, 3, 6, 9, 12, 15]
    assert all(stream.closed and stream.read_count == 0 for stream in streams)
