"""Unadapted list rows remain durable without becoming detail fetch targets.

One selected sanitized capture, generated HTML, SQLite transactions and simulated HTTP are used;
fault injection here is not a process termination or power loss experiment.
"""

import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from bs4 import BeautifulSoup
from pydantic import ValidationError
from test_crawling import (
    HOME,
    SECOND,
    SOURCE,
    THIRD,
    Clock,
    CrawlOptions,
    fixture,
    html,
    listing,
    rows,
    run,
    settings,
)

from signalnest.cache import select_cache_candidate
from signalnest.contracts import ListPage, PageInput, PendingReference
from signalnest.errors import IngestError
from signalnest.fetching import default_profile
from signalnest.ingestion import (
    ResponseInput,
    discover_page,
    import_page,
    list_references,
    process_cached_response,
    process_response,
    record_response,
)
from signalnest.ingestion_state import (
    ScanCompletion,
    pending_documents,
    read_source_state,
    record_coverage_in_transaction,
    start_run_in_transaction,
)
from signalnest.parsing import PARSER_VERSION, ParseError, ParseErrorCode, parse_list
from signalnest.schema import (
    discovered_references,
    documents,
    http_resources,
    ingestion_runs,
    notice_versions,
    notification_listing_evidence,
    raw_responses,
)
from signalnest.storage import open_initialized_engine

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures/list-links-20261009"
PAGE6 = "https://uc.whu.edu.cn/tzgg/xstz/19.htm"
PAGE7 = "https://uc.whu.edu.cn/tzgg/xstz/18.htm"
SIM = "https://sim.whu.edu.cn/info/1775/107042.htm"


def capture(prefix):
    return next(FIXTURES.glob(prefix + "-*.html")).read_bytes()


def parse(content, url=HOME):
    return parse_list(PageInput(content=content, page_url=url))


def mixed(current=1, total=2, *, target=SIM, ids=(128231, 900001), position=1, **kwargs):
    soup = BeautifulSoup(listing(current, total, ids, **kwargs), "html.parser")
    soup.select(".am-list > li > a")[position]["href"] = target
    return str(soup).encode("utf-8")


def evidence(url=HOME, *, source=SOURCE, profile=None, fetched_at=100, status=200):
    return ResponseInput(
        page_type="list",
        source_id=source,
        requested_url=url,
        final_url=url,
        fetched_at=fetched_at,
        status_code=status,
        request_profile=profile,
        etag='"fixture"',
    )


@pytest.mark.parametrize(
    "prefix,url,native,references,page,total",
    [
        ("uc19", PAGE6, 24, 1, 6, 24),
    ],
)
def test_real_rows_pagination_and_full_targets(prefix, url, native, references, page, total):
    content = capture(prefix)
    result = parse(content, url)
    assert (len(result.entries), len(result.references), result.row_count) == (
        native,
        references,
        native + references,
    )
    assert (result.pagination.current_page, result.pagination.total_pages) == (page, total)
    assert result.pagination.is_last_page == (page == total)
    if references:
        ref = result.references[0]
        actual = BeautifulSoup(content, "html.parser").select(".list_txt .am-list > li > a")[4]
        assert ref.row_index == 4 and ref.reference_kind == "external"
        assert ref.raw_href == actual["href"]
        assert str(ref.resolved_url) == actual["href"]
        assert ref.title == actual.span.get_text(strip=True)
        assert ref.published_date == date.fromisoformat(actual.i.get_text(strip=True))
        assert isinstance(result.ordered_rows()[4], PendingReference)
        assert str(result.next_page_url) == (
            PAGE7 if page == 6 else "https://uc.whu.edu.cn/tzgg/xstz/17.htm"
        )


@pytest.mark.parametrize(
    "target,kind",
    [
        (SIM, "external"),
        ("https://other.example/info/1517/128231.htm", "external"),
        ("/info/1518/128231.htm", "unsupported_column"),
        ("/2022/show.jsp?wbnewsid=128231&wbtreeid=1518&extra=x", "unsupported_column"),
        ("/another/article?id=9", "unsupported_route"),
        ("/info/1517/128231.htm#section", "unsupported_route"),
    ],
)
def test_only_unadapted_rows_are_valid_but_not_native_documents(target, kind):
    result = parse(mixed(1, 1, ids=(900001,), position=0, target=target))
    assert result.entries == () and result.row_count == 1
    assert result.pagination.is_last_page and result.next_page_url is None
    assert result.references[0].reference_kind == kind
    with pytest.raises(ValidationError):
        ListPage(entries=(), pagination=result.pagination)


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "mailto:office@example.org",
        "#menu",
        "",
        "https://",
        "https://user:secret@example.org/a",
        "https://example.org/a b",
        "https://example.org/a\\b",
        "https://example.org/%broken",
        "/info/1517/not-an-id.htm",
        "/info/1517/128231.htm?wbnewsid=999",
        "/2022/show.jsp?wbtreeid=1518",
        "/2022/show.jsp?wbtreeid=1517&wbnewsid=128231&wbnewsid=128231",
        pytest.param("./" * 4100 + "other.htm", id="oversized-relative-href"),
    ],
)
def test_bad_link_is_not_downgraded_to_pending_reference(target):
    soup = BeautifulSoup(mixed(), "html.parser")
    soup.select(".am-list > li > a")[0]["href"] = target
    with pytest.raises(ParseError) as caught:
        parse(str(soup).encode())
    assert caught.value.item_index == 0


@pytest.mark.parametrize("damage", ["date", "title", "paginator", "next"])
def test_reference_does_not_relax_fields_or_pagination(damage):
    soup = BeautifulSoup(mixed(), "html.parser")
    if damage == "date":
        soup.select(".am-list > li > a")[1].i.string = "2026-99-01"
    elif damage == "title":
        soup.select(".am-list > li > a")[1].span.decompose()
    elif damage == "paginator":
        soup.select_one(".p_pages").decompose()
    else:
        soup.select_one(".p_next a")["href"] = SIM
    with pytest.raises(ParseError):
        parse(str(soup).encode())


def test_uri_key_is_scoped_to_full_resolved_target_not_article_or_title():
    ref = parse(mixed(target="https://EXAMPLE.org:443/a?b=2&a=1#part")).references[0]
    same = ref.model_copy(update={"title": "另一个标题"})
    assert same.candidate_key() == ref.candidate_key()
    normalized = parse(mixed(target="https://example.org/a?b=2&a=1#part")).references[0]
    assert normalized.candidate_key() == ref.candidate_key()
    for target in (
        "https://example.org/a?a=1&b=2#part",
        "https://example.org/a?b=2&a=1",
        "http://example.org/a?b=2&a=1#part",
        "https://other.example/a?b=2&a=1#part",
    ):
        assert parse(mixed(target=target)).references[0].candidate_key() != ref.candidate_key()


def test_selected_sanitized_page_registration_repeat_and_reopen(state_env):
    env = state_env
    first = import_page(env.engine, env.store, evidence(PAGE6), capture("uc19"), 101)
    assert (first.discovered_count, first.reference_count, first.registered_row_count) == (
        24,
        1,
        25,
    )
    before = rows(env, discovered_references)
    assert len(rows(env, documents)) == 24 and len(before) == 1

    import_page(env.engine, env.store, evidence(PAGE6, fetched_at=102), capture("uc19"), 103)
    after = rows(env, discovered_references)
    assert len(rows(env, documents)) == 24 and len(after) == 1
    assert len(tuple((env.settings.data_dir / "raw").glob("*.bin"))) == 1
    previous, current = before[0], after[0]
    assert current["first_seen_at"] == 101 and current["last_seen_at"] == 103
    for field in (
        "first_body_response_id",
        "first_observed_response_id",
        "first_row_index",
        "first_parser_version",
    ):
        assert current[field] == previous[field]
    assert current["first_parser_version"] == current["last_parser_version"] == PARSER_VERSION
    assert current["status"] == "pending_adapter" and current["discovery_origin"] == "unknown"
    assert current["first_discovery_run_id"] is None
    response = next(
        row for row in rows(env, raw_responses) if row["id"] == current["last_body_response_id"]
    )
    assert env.store.read(response["body_path"], response["body_sha256"])
    env.engine.dispose()
    env.engine = open_initialized_engine(env.settings.database)
    assert list_references(env.engine, SOURCE, limit=1, offset=1)["total"] == 1
    assert len(pending_documents(env.engine, SOURCE, 104)) == 24


def test_duplicate_rows_update_latest_without_losing_first_evidence(state_env):
    env = state_env
    body = mixed(1, 1, ids=(900001, 900002), position=0)
    soup = BeautifulSoup(body, "html.parser")
    soup.select(".am-list > li > a")[1]["href"] = SIM
    both = str(soup).encode()
    first = import_page(env.engine, env.store, evidence(), both, 101)
    assert first.reference_count == first.registered_row_count == 2
    assert first.discovered_count == 0
    row = rows(env, discovered_references)[0]
    assert (row["first_row_index"], row["last_row_index"]) == (0, 1)
    assert len(rows(env, discovered_references)) == 1
    soup.select(".am-list > li > a")[1].span.string = "新标题"
    soup.select(".am-list > li > a")[1].i.string = "2026-10-01"
    second = import_page(env.engine, env.store, evidence(fetched_at=200), str(soup).encode(), 201)
    after = rows(env, discovered_references)[0]
    assert after["title"] == "新标题" and after["published_date"] == date(2026, 10, 1)
    assert after["first_body_response_id"] == first.response_id
    assert after["last_body_response_id"] == second.response_id
    # Replay an old listing with an earlier processing timestamp: no latest-label rollback.
    import_page(env.engine, env.store, evidence(fetched_at=100), both, 150)
    assert rows(env, discovered_references)[0] == after
    # The same URI in another source has independent provenance.
    import_page(env.engine, env.store, evidence(source="another-source"), both, 101)
    assert len(rows(env, discovered_references)) == 2


def test_reference_needs_archived_list_evidence_and_rolls_back_native_rows(state_env):
    env = state_env
    with pytest.raises(IngestError, match="list_reference_evidence_required"):
        discover_page(env.engine, SOURCE, parse(mixed()), 100)
    assert not rows(env, documents) and not rows(env, discovered_references)


@pytest.mark.parametrize("target", ["reference", "processing_marker"])
def test_page_fault_rolls_back_native_reference_and_marks_together(state_env, target):
    env = state_env
    profile = default_profile(settings(env).http)
    trigger = (
        "CREATE TRIGGER fail_page BEFORE INSERT ON discovered_references "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        if target == "reference"
        else "CREATE TRIGGER fail_page BEFORE UPDATE ON http_resources "
        "WHEN NEW.last_processed_response_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with env.engine.begin() as connection:
        connection.exec_driver_sql(trigger)
    with pytest.raises(IngestError, match="database_write_failed") as caught:
        import_page(env.engine, env.store, evidence(profile=profile), mixed(), 101)
    response_id = caught.value.response_id
    assert not rows(env, documents) and not rows(env, discovered_references)
    assert not rows(env, notification_listing_evidence)
    assert rows(env, http_resources)[0]["last_processed_response_id"] is None
    assert read_source_state(env.engine, SOURCE)["last_list_registered_at"] is None
    response = rows(env, raw_responses)[0]
    assert response["last_error_code"] == "database_write_failed"
    assert env.store.read(response["body_path"], response["body_sha256"]) == mixed()
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER fail_page")
    result = process_cached_response(env.engine, env.store, response_id, 102)
    assert result.registered_row_count == 2 and len(rows(env, discovered_references)) == 1
    assert rows(env, http_resources)[0]["last_processed_response_id"] == response_id


def test_parse_failure_durable_body_304_recovery_and_parser_trace(state_env):
    env = state_env
    profile = default_profile(settings(env).http)
    body_id = record_response(env.engine, env.store, evidence(profile=profile), mixed())

    def old_failure(page):
        raise ParseError(ParseErrorCode.UNSUPPORTED_IDENTITY, field="identity", item_index=1)

    with pytest.raises(IngestError, match="parse_unsupported_identity"):
        process_response(env.engine, env.store, body_id, 101, list_parser=old_failure)
    assert not rows(env, documents) and not rows(env, discovered_references)
    candidate = select_cache_candidate(env.engine, env.store, SOURCE, HOME, profile)
    assert not candidate.requires_full_fetch
    validation_id = record_response(
        env.engine,
        env.store,
        evidence(profile=profile, fetched_at=102, status=304),
        None,
        candidate=candidate.candidate,
    )
    recovered = process_cached_response(env.engine, env.store, validation_id, 103)
    assert recovered.body_response_id == body_id and recovered.reference_count == 1
    ref = rows(env, discovered_references)[0]
    assert ref["first_body_response_id"] == body_id
    assert ref["first_observed_response_id"] == validation_id
    assert rows(env, raw_responses)[0]["fetched_at"] == 100
    assert rows(env, raw_responses)[1]["body_path"] is None
    assert len(tuple((env.settings.data_dir / "raw").glob("*.bin"))) == 1
    process_response(
        env.engine, env.store, validation_id, 104, list_parser_version="test-new-list-rules"
    )
    after = rows(env, discovered_references)[0]
    assert after["first_parser_version"] == PARSER_VERSION
    assert after["last_parser_version"] == "test-new-list-rules"
    assert len(rows(env, raw_responses)) == 2 and len(rows(env, discovered_references)) == 1


def test_full_scan_with_references_proves_all_rows_and_only_fetches_native_details(state_env):
    env = state_env
    content = {
        HOME: mixed(1, 3),
        SECOND: mixed(2, 3, target="https://example.org/view?b=2&a=1"),
        THIRD: listing(3, 3, (127581,)),
    }
    calls = []

    def handler(request):
        uri = str(request.url)
        calls.append(uri)
        assert request.url.host == "uc.whu.edu.cn"
        if uri in content:
            return httpx.Response(304) if "If-None-Match" in request.headers else html(content[uri])
        return html(
            fixture(
                "current-notice-detail.html" if "128231" in uri else "legacy-notice-detail.html"
            )
        )

    first = run(env, handler, opts=CrawlOptions(scan_mode="full", max_pages=3, max_details=2))
    assert first.coverage == "complete" and first.result == "succeeded"
    assert (
        first.scanned_entries,
        first.scanned_references,
        first.new_documents,
        first.new_references,
    ) == (5, 2, 2, 2)
    assert first.remaining_unadapted_references == 2 and first.remaining_due == 0
    assert first.details_succeeded == 2
    assert len(rows(env, documents)) == 2 and len(rows(env, notice_versions)) == 2
    ledger = rows(env, ingestion_runs)[0]["coverage_evidence"]
    proof = ScanCompletion.model_validate(ledger)
    assert len(proof.registrations) == 3
    assert [kind for kind, _ in proof.registrations[0].rows] == ["notice", "reference"]
    assert proof.home_recheck.rows == proof.registrations[0].rows
    assert proof.home_recheck.body_response_id == proof.registrations[0].body_response_id
    assert proof.home_recheck.observed_response_id != proof.home_recheck.body_response_id
    assert read_source_state(env.engine, SOURCE)["last_complete_scan_run_id"] == first.run_id
    assert {row["discovery_origin"] for row in rows(env, discovered_references)} == {"bootstrap"}
    assert {row["first_discovery_run_id"] for row in rows(env, discovered_references)} == {
        first.run_id
    }
    later = Clock()
    later.epoch = 1100
    repeated = run(env, handler, clock=later)
    assert (
        repeated.coverage == "complete" and repeated.new_documents == repeated.new_references == 0
    )
    assert repeated.scanned_entries == 5 and repeated.scanned_references == 2
    assert repeated.details_attempted == 0
    assert len(rows(env, discovered_references)) == len(rows(env, notice_versions)) == 2
    assert ledger == rows(env, ingestion_runs)[0]["coverage_evidence"]
    assert len(calls) == 10  # First chain + home + two details; repeat chain + home.


def test_all_reference_page_completes_listing_without_fetching_any_body(state_env):
    body = mixed(1, 1, ids=(900001,), position=0)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return html(body)

    result = run(
        state_env, handler, opts=CrawlOptions(scan_mode="full", max_pages=1, max_details=10)
    )
    assert result.coverage == "complete" and result.new_documents == 0
    assert result.new_references == result.remaining_unadapted_references == 1
    assert result.details_attempted == result.remaining_due == 0
    assert calls == [HOME, HOME]
    assert not rows(state_env, documents) and not rows(state_env, notice_versions)


def test_reference_change_in_home_recheck_blocks_complete_coverage(state_env):
    home_calls = 0

    def handler(request):
        nonlocal home_calls
        if str(request.url) == HOME:
            home_calls += 1
            return html(mixed(target=SIM if home_calls == 1 else SIM + "?changed=1"))
        return html(listing(2, 2, (127581,)))

    result = run(state_env, handler)
    assert result.coverage == "interrupted" and result.error_code == "home_changed"
    assert result.new_references == 2
    assert read_source_state(state_env.engine, SOURCE)["last_complete_scan_at"] is None
    assert rows(state_env, ingestion_runs)[0]["coverage_evidence"] is None


def test_valid_redirect_is_retained_separately_from_page_chain_request(state_env):
    alias = "https://uc.whu.edu.cn/tzgg/xstz/1.htm"

    def handler(request):
        return (
            httpx.Response(302, headers={"Location": alias})
            if str(request.url) == HOME
            else html(mixed(1, 1))
        )

    result = run(state_env, handler)
    assert result.coverage == "complete"
    ledger = ScanCompletion.model_validate(rows(state_env, ingestion_runs)[0]["coverage_evidence"])
    assert str(ledger.registrations[0].requested_url) == HOME
    assert str(ledger.registrations[0].response_requested_url) == alias
    assert str(ledger.registrations[0].final_url) == alias


@pytest.mark.parametrize("mode", ["budget", "loop", "bad_row"])
def test_reference_does_not_mask_incomplete_scan(state_env, mode):
    home = mixed()
    if mode == "loop":
        tail = mixed(2, 3, target=SIM + "?second", routes=(HOME, SECOND, HOME))
    else:
        tail = listing(2, 2, (127581,))
    if mode == "bad_row":
        tail = tail.replace(b"2026-09-24", b"2026-99-24")
    result = run(
        state_env,
        lambda request: html(home if str(request.url) == HOME else tail),
        opts=CrawlOptions(scan_mode="full", max_pages=1 if mode == "budget" else 3, max_details=0),
    )
    assert result.coverage != "complete" and result.new_references >= 1
    assert read_source_state(state_env.engine, SOURCE)["last_complete_scan_at"] is None
    assert rows(state_env, ingestion_runs)[0]["coverage_evidence"] is None


def test_registration_contract_cannot_omit_reference_in_unchanged_home(state_env):
    run(state_env, lambda request: html(mixed(1, 1)))
    ledger = rows(state_env, ingestion_runs)[0]["coverage_evidence"]
    changed = json.loads(json.dumps(ledger))
    changed["home_recheck"]["rows"] = [["notice", "1517:128231"]]
    with pytest.raises(ValidationError):
        ScanCompletion.model_validate(changed)
    assert hashlib.sha256(mixed(1, 1)).hexdigest() != ledger["registrations"][0]["listing_sha256"]


def test_scan_finish_failure_keeps_page_and_reference_but_not_complete_clock(state_env):
    env = state_env
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_coverage BEFORE UPDATE ON ingestion_runs "
            "WHEN NEW.coverage = 'complete' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sa.exc.SQLAlchemyError):
        run(env, lambda request: html(mixed(1, 1)))
    assert len(rows(env, documents)) == len(rows(env, discovered_references)) == 1
    interrupted = rows(env, ingestion_runs)[0]
    assert interrupted["result"] == "failed" and interrupted["coverage"] == "interrupted"
    assert interrupted["coverage_evidence"] is None
    assert read_source_state(env.engine, SOURCE)["last_complete_scan_at"] is None
    first = rows(env, discovered_references)[0]
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER fail_coverage")
    clock = Clock()
    clock.epoch = 1100
    resumed = run(env, lambda request: html(mixed(1, 1)), clock=clock)
    assert resumed.coverage == "complete" and resumed.new_references == 0
    assert (
        rows(env, discovered_references)[0]["first_discovery_run_id"]
        == first["first_discovery_run_id"]
    )
    assert read_source_state(env.engine, SOURCE)["last_complete_scan_run_id"] == resumed.run_id


@pytest.mark.parametrize("damage", ["member", "source", "response_url"])
def test_coverage_commit_validates_real_response_and_member_registration(state_env, damage):
    env = state_env
    run(env, lambda request: html(mixed(1, 1)))
    ledger = ScanCompletion.model_validate(rows(env, ingestion_runs)[0]["coverage_evidence"])
    old_clock = read_source_state(env.engine, SOURCE)["last_complete_scan_at"]
    with env.engine.begin() as connection:
        start_run_in_transaction(connection, SOURCE, "verify-registration", 1200, origin="regular")
    for response_id in {
        ledger.registrations[0].observed_response_id,
        ledger.home_recheck.observed_response_id,
    }:
        process_response(
            env.engine, env.store, response_id, 1201, ingestion_run_id="verify-registration"
        )
    with (
        pytest.raises(IngestError, match="scan_registration_invalid"),
        env.engine.begin() as connection,
    ):
        if damage == "member":
            connection.execute(discovered_references.delete())
        else:
            connection.execute(
                raw_responses.update()
                .where(raw_responses.c.id == ledger.registrations[0].body_response_id)
                .values(
                    **(
                        {"source_id": "another-source"}
                        if damage == "source"
                        else {"requested_url": SECOND}
                    )
                )
            )
        record_coverage_in_transaction(
            connection, "verify-registration", "complete", 1202, completion=ledger
        )
    assert read_source_state(env.engine, SOURCE)["last_complete_scan_at"] == old_clock
    new_run = next(row for row in rows(env, ingestion_runs) if row["id"] == "verify-registration")
    assert new_run["coverage"] == "pending" and new_run["coverage_evidence"] is None
    assert len(rows(env, discovered_references)) == 1
