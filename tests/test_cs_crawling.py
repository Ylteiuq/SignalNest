"""Explicit source binding through the real Fetcher/archive/cache/Parser/SQLite.

HTTP is simulated. Only CS pages 1, 2 and 4 have real captures; constructed page 3
and minimal lists below are engineering inputs, not research evidence of coverage.
"""

from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from test_crawling import Clock, html, rows
from test_cs_decisions import NOW, profile
from test_cs_parsing import HOME, LAST, NOTICE, NOTICE_URL, SECOND, capture, changed

from signalnest.cache import select_cache_candidate
from signalnest.config import ConfigurationError, Settings, load_config
from signalnest.crawling import CrawlOptions, crawl_once
from signalnest.cs_parsing import LIST_URL, PARSER_VERSION, SOURCE_ID
from signalnest.errors import IngestError
from signalnest.fetching import FetchCode, FetchLimits, FetchTarget, HttpFetcher, default_profile
from signalnest.ingestion_state import read_source_state, set_cooldown_in_transaction
from signalnest.notifications.state import ActivationOptions, activate_notifications
from signalnest.schema import (
    discovered_references,
    documents,
    email_outbox,
    http_resources,
    ingestion_runs,
    notice_versions,
    notification_decisions,
    notification_events,
    notification_listing_evidence,
    notification_observations,
    raw_responses,
)
from signalnest.sources import CS, UC
from signalnest.storage import open_initialized_engine

ROOT = Path(__file__).resolve().parents[1]
URIS = (
    LIST_URL,
    str(capture(SECOND).page_url),
    LIST_URL.removesuffix(".htm") + "/2.htm",
    str(capture(LAST).page_url),
)


def settings(env):
    return Settings(
        storage=env.settings,
        source=dict(parser=CS.name, id=SOURCE_ID, list_url=LIST_URL),
        http=dict(
            connect_timeout_seconds=2.0,
            read_timeout_seconds=3.0,
            request_interval_seconds=1.0,
            user_agent="SignalNest/cs-test",
        ),
    )


def clock(at=None):
    result = Clock()
    result.epoch = at or int(NOW.timestamp())
    return result


def run(env, handler, *, at=None, full=False, pages=2, details=0, requests=30):
    return crawl_once(
        settings(env),
        CrawlOptions(
            scan_mode="full" if full else "limited",
            max_pages=pages,
            max_details=details,
            fetch_limits=FetchLimits(max_requests=requests),
        ),
        transport=httpx.MockTransport(handler),
        clock=clock(at),
    )


def listing(*, current=1, total=1, ids=(64481,), external=False):
    """Small explicit CS template, with actual controls and declared logical pages."""
    numbers = "".join(
        f'<span class="p_no_d">{i}</span>'
        if i == current
        else f'<span class="p_no"><a href="{URIS[i - 1]}">{i}</a></span>'
        for i in range(1, total + 1)
    )
    first = (
        '<span class="p_first_d">首页</span><span class="p_prev_d">上一页</span>'
        if current == 1
        else (
            f'<span class="p_first"><a href="{URIS[0]}">首页</a></span>'
            f'<span class="p_prev"><a href="{URIS[current - 2]}">上一页</a></span>'
        )
    )
    last = (
        '<span class="p_next_d">下一页</span><span class="p_last_d">尾页</span>'
        if current == total
        else (
            f'<span class="p_next"><a href="{URIS[current]}">下一页</a></span>'
            f'<span class="p_last"><a href="{URIS[total - 1]}">尾页</a></span>'
        )
    )
    items = "".join(
        f'<li><a href="/info/1074/{i}.htm"><p>助教通知{i}</p><span>2026-07-13</span></a></li>'
        for i in ids
    )
    if external:
        items += '<li><a href="https://external.example.org/apply"><p>外链</p><span>2026-07-13</span></a></li>'
    return (
        f'<div class="study under-new"><ul>{items}</ul><div class="pagebar">'
        f'<span class="p_pages">{first}{numbers}{last}</span></div></div>'
    ).encode()


def test_real_cs_lists_then_304_use_cs_version_and_exact_profiles(state_env):
    env = state_env
    bodies = {LIST_URL: capture(HOME).content, URIS[1]: capture(SECOND).content}
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) in bodies
        if "if-none-match" in request.headers:
            return httpx.Response(304)
        return html(bodies[str(request.url)])

    first = run(env, handler)
    original = rows(env, raw_responses)
    second = run(env, handler, at=first.finished_at + 10)
    assert first.pages_committed == second.pages_committed == 2
    assert first.scanned_entries == second.scanned_entries == 30
    assert first.new_documents == 30 and second.new_documents == 0
    assert second.coverage == "limited" and not second.home_rechecked
    assert all(row["source_id"] == SOURCE_ID for row in rows(env, documents))
    assert {row["parser_version"] for row in rows(env, ingestion_runs)} == {PARSER_VERSION}
    assert {row["last_processed_parser_version"] for row in rows(env, http_resources)} == {
        PARSER_VERSION
    }
    for row in rows(env, raw_responses)[2:]:
        assert row["status_code"] == 304 and row["body_path"] is None
        body = next(r for r in original if r["id"] == row["validated_response_id"])
        assert body["fetched_at"] < row["fetched_at"]
        assert env.store.read(body["body_path"], body["body_sha256"]) in bodies.values()
    profile_value = default_profile(settings(env).http)
    assert all(row["profile_sha256"] == profile_value.sha256() for row in rows(env, http_resources))
    assert all(
        request.headers["user-agent"] == profile_value.user_agent
        and request.headers["accept"] == profile_value.accept
        and request.headers["accept-encoding"] == profile_value.accept_encoding
        for request in seen
    )
    assert rows(env, notification_events) == rows(env, email_outbox) == []


def test_full_cs_chain_including_explicitly_constructed_page_three(state_env):
    # The missing research page is not invented as a real capture. Build a template
    # for the interface proof; keep the three other original fixture bytes intact.
    bodies = {
        URIS[0]: capture(HOME).content,
        URIS[1]: capture(SECOND).content,
        URIS[2]: listing(current=3, total=4, ids=(90001,)),
        URIS[3]: capture(LAST).content,
    }
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return html(bodies[str(request.url)])

    summary = run(state_env, handler, full=True, pages=4)
    assert summary.coverage == "complete" and summary.home_rechecked
    assert summary.pages_committed == 4 and summary.scanned_entries == 44
    assert seen == [*URIS, URIS[0]]
    state = read_source_state(state_env.engine, SOURCE_ID)
    assert state["last_complete_scan_run_id"] == summary.run_id
    ledger = rows(state_env, ingestion_runs)[0]["coverage_evidence"]
    assert [p["pagination"]["current_page"] for p in ledger["registrations"]] == [1, 2, 3, 4]
    assert (
        ledger["home_recheck"]["observed_response_id"]
        != ledger["registrations"][0]["observed_response_id"]
    )
    assert len(rows(state_env, documents)) == 44


def test_cs_recruitment_real_detail_then_failed_latest_200_and_304_retry(state_env):
    env = state_env
    stage = 0
    valid = capture(NOTICE).content
    bad = changed(capture(NOTICE), lambda tree: tree.select_one("#vsb_content").decompose()).content

    def handler(request):
        if str(request.url) == LIST_URL:
            return html(listing())
        assert str(request.url) == NOTICE_URL
        if stage == 2:
            assert request.headers["if-none-match"] == '"bad"'
            return httpx.Response(304)
        return html(valid if stage == 0 else bad, etag='"good"' if stage == 0 else '"bad"')

    first = run(env, handler, pages=1, details=1)
    assert first.details_succeeded == 1
    doc = rows(env, documents)[0]
    old_version, old_success = doc["current_version_id"], doc["last_success_at"]
    assert rows(env, notice_versions)[0]["parser_version"] == PARSER_VERSION
    assert rows(env, notification_listing_evidence)[0]["parser_version"] == PARSER_VERSION
    stage = 1
    second = run(env, handler, at=doc["next_due_at"] + 1, pages=1, details=1)
    assert second.details_failed == 1 and second.error_code == "parse_missing_structure"
    failed = rows(env, documents)[0]
    assert failed["current_version_id"] == old_version and failed["last_success_at"] == old_success
    latest = [r for r in rows(env, raw_responses) if r["page_type"] == "notice"][-1]
    stage = 2
    third = run(env, handler, at=failed["next_due_at"] + 1, pages=1, details=1)
    assert third.details_failed == 1
    response = [r for r in rows(env, raw_responses) if r["page_type"] == "notice"][-1]
    assert response["status_code"] == 304 and response["validated_response_id"] == latest["id"]
    assert response["body_path"] is None
    assert rows(env, documents)[0]["current_version_id"] == old_version
    assert len(rows(env, notice_versions)) == 1


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_cs_cache_loss_requires_unconditional_fetch(state_env, damage):
    env = state_env
    body = capture(HOME).content
    first = run(env, lambda r: html(body), pages=1)
    response = rows(env, raw_responses)[0]
    path = env.settings.data_dir / response["body_path"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"damaged")
    selection = select_cache_candidate(
        env.engine,
        env.store,
        SOURCE_ID,
        LIST_URL,
        default_profile(settings(env).http),
        page_type="list",
    )
    assert selection.requires_full_fetch
    seen = []

    def handler(request):
        seen.append(request)
        assert "if-none-match" not in request.headers
        # A genuinely fresh body can be stored alongside a quarantined old hash;
        # corrupt same bytes must instead fail, as existing RawStore tests verify.
        return html(body + b"\n", etag='"fresh"')

    result = run(env, handler, at=first.finished_at + 10, pages=1)
    assert result.result == "succeeded" and len(seen) == 1


def test_failed_cs_parse_can_succeed_on_bound_304_with_new_rules(state_env, monkeypatch):
    from signalnest import cs_parsing

    env = state_env
    real = cs_parsing.parse_cs_notice

    def broken(page):
        from signalnest.parsing import ParseError, ParseErrorCode

        raise ParseError(ParseErrorCode.MISSING_STRUCTURE, field="body")

    monkeypatch.setattr(cs_parsing, "parse_cs_notice", broken)

    def handler(request):
        if "if-none-match" in request.headers:
            return httpx.Response(304)
        return html(listing() if str(request.url) == LIST_URL else capture(NOTICE).content)

    first = run(env, handler, pages=1, details=1)
    assert first.details_failed == 1
    original = [r for r in rows(env, raw_responses) if r["page_type"] == "notice"][0]
    monkeypatch.setattr(cs_parsing, "parse_cs_notice", real)
    monkeypatch.setattr(cs_parsing, "PARSER_VERSION", "cs-undergrad-notices-test-v2")

    monkeypatch.setattr(
        cs_parsing,
        "parse_cs_notice",
        lambda p: real(p).model_copy(update={"parser_version": cs_parsing.PARSER_VERSION}),
    )
    second = run(env, handler, at=rows(env, documents)[0]["next_due_at"] + 1, pages=1, details=1)
    assert second.details_succeeded == 1
    observed = [r for r in rows(env, raw_responses) if r["page_type"] == "notice"][-1]
    assert observed["status_code"] == 304 and observed["validated_response_id"] == original["id"]
    assert rows(env, notice_versions)[0]["parser_version"] == "cs-undergrad-notices-test-v2"
    assert observed["body_path"] is None
    assert len(list((env.settings.data_dir / "raw").glob("*.bin"))) == 2


def test_cs_external_references_do_not_stop_page_or_next_or_trigger_http(state_env):
    bodies = {
        URIS[0]: listing(total=2, external=True),
        URIS[1]: listing(current=2, total=2, ids=(64482,)),
    }
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return html(bodies[str(request.url)])

    result = run(state_env, handler, full=True, pages=2)
    assert result.coverage == "complete" and result.scanned_entries == 3
    assert result.new_documents == 2 and result.new_references == result.scanned_references == 1
    assert all(url.startswith("https://cs.whu.edu.cn/") for url in seen)
    assert rows(state_env, discovered_references)[0]["source_id"] == SOURCE_ID


@pytest.mark.parametrize(
    "uri,identity",
    [
        ("https://cs.whu.edu.cn/info/1074/64481.htm", "1074:64481"),
        ("https://cs.whu.edu.cn/content.jsp?z=1&wbnewsid=64481&wbtreeid=1074", "1074:64481"),
    ],
)
def test_http_cs_targets_require_explicit_binding(state_env, uri, identity):
    env = state_env
    target = FetchTarget(uri=uri, page_type="notice", source_document_id=identity)
    sent = []
    with HttpFetcher(
        env.engine,
        env.store,
        settings(env).http,
        source_id=SOURCE_ID,
        source_parser=CS.name,
        clock=clock(),
        transport=httpx.MockTransport(lambda r: sent.append(r) or html(capture(NOTICE).content)),
    ) as fetcher:
        result = fetcher.fetch(target)
    assert result.outcome == "complete" and str(sent[0].url) == uri
    with HttpFetcher(
        env.engine,
        env.store,
        settings(env).http,
        source_id=SOURCE_ID,
        clock=clock(),
        transport=httpx.MockTransport(lambda r: pytest.fail("default is UC")),
    ) as fetcher:
        assert fetcher.fetch(target).error_code == FetchCode.INVALID_TARGET


@pytest.mark.parametrize(
    "uri,identity,page_type",
    [
        ("https://uc.whu.edu.cn/info/1517/64481.htm", "1517:64481", "notice"),
        ("https://cs.whu.edu.cn/info/1075/64481.htm", "1075:64481", "notice"),
        (NOTICE_URL, "1074:1", "notice"),
        (
            "https://cs.whu.edu.cn/content.jsp?wbtreeid=1074&wbnewsid=64481&wbnewsid=1",
            "1074:64481",
            "notice",
        ),
        ("https://cs.whu.edu.cn/xwdt/tzgg/bkjx.htm?q=1", None, "list"),
        ("https://cs.whu.edu.cn/xwdt/tzgg/yjsjx.htm", None, "list"),
        ("http://cs.whu.edu.cn/xwdt/tzgg/bkjx.htm", None, "list"),
        ("https://cs.whu.edu.cn:444/xwdt/tzgg/bkjx.htm", None, "list"),
        ("https://cs.whu.edu.cn/system/resource/code/auth/caslogin.jsp", "1074:64481", "notice"),
        ("https://docs.qq.com/form/1", "1074:64481", "notice"),
    ],
)
def test_cs_invalid_target_never_sends(state_env, uri, identity, page_type):
    with HttpFetcher(
        state_env.engine,
        state_env.store,
        settings(state_env).http,
        source_id=SOURCE_ID,
        source_parser=CS.name,
        clock=clock(),
        transport=httpx.MockTransport(lambda r: pytest.fail("invalid target sent")),
    ) as f:
        result = f.fetch(FetchTarget(uri=uri, page_type=page_type, source_document_id=identity))
        assert result.error_code == FetchCode.INVALID_TARGET and f.requests_sent == 0


@pytest.mark.parametrize(
    "location",
    [
        "https://uc.whu.edu.cn/info/1517/64481.htm",
        "/system/resource/code/auth/caslogin.jsp",
        "/info/1074/1.htm",
    ],
)
def test_cs_redirect_login_cross_source_or_identity_is_rejected(state_env, location):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": location})

    with HttpFetcher(
        state_env.engine,
        state_env.store,
        settings(state_env).http,
        source_id=SOURCE_ID,
        source_parser=CS.name,
        clock=clock(),
        transport=httpx.MockTransport(handler),
    ) as f:
        result = f.fetch(
            FetchTarget(uri=NOTICE_URL, page_type="notice", source_document_id="1074:64481")
        )
    assert result.error_code == FetchCode.REDIRECT_INVALID and len(seen) == 1


def test_allowed_same_identity_jsp_redirect_has_separate_exact_resources(state_env):
    uri = "https://cs.whu.edu.cn/content.jsp?wbnewsid=64481&x=2&wbtreeid=1074"
    page = listing().replace(b"/info/1074/64481.htm", uri.encode().replace(b"&", b"&amp;"))
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if str(request.url) == uri:
            return httpx.Response(302, headers={"Location": NOTICE_URL})
        return html(page if str(request.url) == LIST_URL else capture(NOTICE).content)

    result = run(state_env, handler, pages=1, details=1)
    assert result.details_succeeded == 1 and seen == [LIST_URL, uri, NOTICE_URL]
    resources = {r["request_uri"] for r in rows(state_env, http_resources)}
    assert resources == {LIST_URL, uri, NOTICE_URL}
    response = [r for r in rows(state_env, raw_responses) if r["status_code"] == 302][0]
    assert response["body_path"] is None and response["requested_url"] == uri


def test_cs_failed_detail_does_not_block_next_and_due_survives_reopen(state_env):
    env = state_env

    def handler(request):
        if str(request.url) == LIST_URL:
            return html(listing(ids=(64481, 64482)))
        if request.url.path.endswith("64481.htm"):
            return html(b"<html>not a notice</html>")
        return html(capture(NOTICE).content)

    first = run(env, handler, pages=1, details=2)
    assert first.details_failed == first.details_succeeded == 1
    reopened = open_initialized_engine(env.settings.database)
    try:
        with reopened.connect() as c:
            failed = (
                c.execute(sa.select(documents).where(documents.c.status == "failed"))
                .mappings()
                .one()
            )
            assert (
                failed["current_version_id"] is None and failed["next_due_at"] > first.finished_at
            )
    finally:
        reopened.dispose()


def test_cs_cooldown_is_source_specific_and_survives_reopen(state_env):
    env = state_env
    at = int(NOW.timestamp())
    with env.engine.begin() as c:
        set_cooldown_in_transaction(c, SOURCE_ID, at + 3600)
    result = run(env, lambda r: pytest.fail("cooldown must suppress all HTTP"), at=at, details=1)
    assert result.error_code == "server_cooldown" and result.physical_requests == 0
    reopened = open_initialized_engine(env.settings.database)
    try:
        assert read_source_state(reopened, SOURCE_ID)["not_before_at"] == at + 3600
        assert read_source_state(reopened, UC.source_id) is None
    finally:
        reopened.dispose()


def test_cs_explicit_activation_and_live_recheck_use_cs_notice_parser(state_env):
    env = state_env
    first = run(env, lambda r: html(listing()), at=1784000000, pages=1, full=True)
    activate_notifications(
        env.engine,
        SOURCE_ID,
        profile(),
        ActivationOptions(
            activation_id="cs-test", sender="sender@example.invalid", recipient="me@example.invalid"
        ),
        at=first.finished_at + 10,
    )

    def handler(request):
        if "if-none-match" in request.headers:
            return httpx.Response(304)
        return html(listing() if str(request.url) == LIST_URL else capture(NOTICE).content)

    second = run(env, handler, at=first.finished_at + 20, pages=1, details=1)
    assert second.details_succeeded == 1
    assert len(rows(env, notification_events)) == 1
    decision = rows(env, notification_decisions)[0]["decision"]
    assert decision["action"] == "DIGEST" and decision["needs_review"]
    before = rows(env, notification_observations)[0]
    third = run(env, handler, at=rows(env, documents)[0]["next_due_at"] + 1, pages=1, details=1)
    assert third.details_succeeded == 1 and len(rows(env, notification_events)) == 1
    after = rows(env, notification_observations)[0]
    assert after["body_response_id"] == before["body_response_id"]
    assert after["observed_response_id"] != before["observed_response_id"]
    assert len(rows(env, notice_versions)) == 1


def test_wrong_active_notification_source_stops_before_run_or_network(state_env):
    from test_notification_crawl import activate_after_full

    activate_after_full(state_env)
    before = rows(state_env, ingestion_runs)
    with pytest.raises(IngestError, match="notification_source_mismatch"):
        run(state_env, lambda r: pytest.fail("must not send"))
    assert rows(state_env, ingestion_runs) == before


@pytest.mark.parametrize("same_raw", [True, False])
def test_cs_parser_upgrade_reprocesses_without_false_live_update(state_env, monkeypatch, same_raw):
    from signalnest import cs_parsing

    env = state_env
    first = run(env, lambda r: html(listing()), at=1784000000, pages=1, full=True)
    activate_notifications(
        env.engine,
        SOURCE_ID,
        profile(),
        ActivationOptions(
            activation_id="cs-upgrade",
            sender="sender@example.invalid",
            recipient="me@example.invalid",
        ),
        at=first.finished_at + 10,
    )
    body, stage = capture(NOTICE).content, 0

    def handler(request):
        if str(request.url) == LIST_URL:
            return html(listing())
        if stage and same_raw:
            return httpx.Response(304)
        return html(body + (b"\n<!-- outside content -->" if stage else b""))

    second = run(env, handler, at=first.finished_at + 20, pages=1, details=1)
    assert second.details_succeeded == 1
    original = rows(env, notification_observations)[0]
    real, calls = cs_parsing.parse_cs_notice, []
    monkeypatch.setattr(cs_parsing, "PARSER_VERSION", "cs-undergrad-notices-test-v2")

    def newer(page):
        calls.append(page.content)
        return real(page).model_copy(update={"parser_version": cs_parsing.PARSER_VERSION})

    monkeypatch.setattr(cs_parsing, "parse_cs_notice", newer)
    stage = 1
    third = run(env, handler, at=rows(env, documents)[0]["next_due_at"] + 1, pages=1, details=1)
    assert third.details_succeeded == 1 and len(rows(env, notification_events)) == 1
    versions = rows(env, notice_versions)
    assert len(versions) == 2 and len({v["content_sha256"] for v in versions}) == 1
    assert rows(env, documents)[0]["current_version_id"] == versions[-1]["id"]
    after = rows(env, notification_observations)[0]
    if same_raw:
        assert after["body_response_id"] == original["body_response_id"] and len(calls) == 1
    else:
        # Different raw bytes under a new Parser invoke that same CS Parser on
        # the earlier successful raw body, not the default UC Parser.
        assert after["body_response_id"] != original["body_response_id"]
        assert len(calls) == 2 and calls[-1] == body


@pytest.mark.parametrize("failure", ["list", "notice"])
def test_cs_business_commit_failure_rolls_back_resource_marker_and_stops(state_env, failure):
    env = state_env
    if failure == "list":
        trigger = "CREATE TRIGGER fail_cs BEFORE INSERT ON discovered_references "
    else:
        trigger = "CREATE TRIGGER fail_cs BEFORE UPDATE OF current_version_id ON documents "
    with env.engine.begin() as connection:
        connection.exec_driver_sql(trigger + "BEGIN SELECT RAISE(ABORT, 'injected_cs_abort'); END")

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return html(
            listing(external=failure == "list")
            if str(request.url) == LIST_URL
            else capture(NOTICE).content
        )

    with pytest.raises(IngestError, match="database_write_failed"):
        run(env, handler, pages=1, details=2)
    assert rows(env, ingestion_runs)[0]["result"] == "failed"
    if failure == "list":
        assert len(seen) == 1
        assert rows(env, documents) == rows(env, discovered_references) == []
        resource = rows(env, http_resources)[0]
    else:
        assert len(seen) == 2
        doc = rows(env, documents)[0]
        assert doc["current_version_id"] is None and doc["last_success_at"] is None
        assert rows(env, notice_versions) == []
        resource = next(r for r in rows(env, http_resources) if r["request_uri"] == NOTICE_URL)
    assert resource["last_processed_response_id"] is None
    assert resource["last_processed_at"] is None
    assert rows(env, raw_responses)[-1]["last_error_code"] == "database_write_failed"
    assert rows(env, raw_responses)[-1]["body_path"] is not None


@pytest.mark.parametrize(
    "edit",
    [
        lambda s: s.replace('parser = "cs-undergrad-notices"', 'parser = "whu-student-notices"'),
        lambda s: s.replace('parser = "cs-undergrad-notices"', 'parser = "unknown"'),
        lambda s: s.replace('id = "whu-cs-undergrad-teaching"', 'id = "whu-undergrad-student"'),
        lambda s: s.replace("bkjx.htm", "bkjx/3.htm"),
        lambda s: s.replace("bkjx.htm", "bkjx.htm?x=1"),
    ],
)
def test_cs_configuration_cannot_guess_or_cross_binding(tmp_path, edit):
    path = tmp_path / "cs.toml"
    path.write_text(edit((ROOT / "config.cs.example.toml").read_text()))
    with pytest.raises(ConfigurationError):
        load_config(path)
    assert list(tmp_path.iterdir()) == [path]
