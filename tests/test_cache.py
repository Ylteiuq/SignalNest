"""Offline production cache/service integration with real blobs and SQLite."""

from dataclasses import replace
from pathlib import Path

import pytest
import sqlalchemy as sa
from bs4 import BeautifulSoup
from pydantic import ValidationError

from signalnest.cache import pending_resources, select_cache_candidate
from signalnest.contracts import RequestProfile
from signalnest.ingestion import (
    IngestError,
    ResponseInput,
    import_page,
    process_cached_response,
    process_response,
    record_response,
)
from signalnest.parsing import PARSER_VERSION, parse_notice
from signalnest.schema import documents, http_resources, notice_versions, raw_responses

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
SOURCE = "whu-undergrad-student"
HOME = "https://uc.whu.edu.cn/tzgg/xstz.htm"
NOTICE = "https://uc.whu.edu.cn/info/1517/128231.htm"
PROFILE = RequestProfile(user_agent="SignalNest/offline-test", accept="text/html")


def body(name="student-notices-page1.html"):
    return (FIXTURES / name).read_bytes()


def evidence(**overrides):
    return ResponseInput(
        **(
            dict(
                page_type="list",
                source_id=SOURCE,
                requested_url=HOME,
                final_url=HOME,
                fetched_at=100,
                status_code=200,
                request_profile=PROFILE,
                etag='W/"home"',
                vary="User-Agent, Accept-Encoding",
                cache_control="private, max-age=600",
            )
            | overrides
        )
    )


def rows(env, table):
    with env.engine.connect() as connection:
        return connection.execute(sa.select(table).order_by(table.c.id)).mappings().all()


def select(env, uri=HOME, profile=PROFILE):
    return select_cache_candidate(env.engine, env.store, SOURCE, uri, profile)


def notice_evidence(**overrides):
    return evidence(
        **(
            dict(
                page_type="notice",
                requested_url=NOTICE,
                final_url=NOTICE,
                source_document_id="1517:128231",
                etag='"notice"',
                fetched_at=102,
            )
            | overrides
        )
    )


def discover(env):
    return import_page(env.engine, env.store, evidence(), body(), 101)


def test_bound_304_reuses_real_200_and_keeps_fetch_time_and_evidence(state_env):
    env = state_env
    first = discover(env)
    candidate = select(env).candidate
    assert candidate.response_id == first.response_id
    observed = record_response(
        env.engine, env.store, evidence(status_code=304, fetched_at=102), None, candidate=candidate
    )
    result = process_cached_response(env.engine, env.store, observed, 103)
    assert result.response_id == observed and result.body_response_id == first.response_id
    assert result.discovered_count == 25 and result.pagination.current_page == 1
    responses = rows(env, raw_responses)
    assert [row["fetched_at"] for row in responses] == [100, 102]
    assert responses[1]["body_path"] is None and responses[1]["body_sha256"] is None
    assert responses[1]["validated_response_id"] == first.response_id
    resource = rows(env, http_resources)[0]
    assert (
        resource["latest_response_id"]
        == resource["last_processed_response_id"]
        == first.response_id
    )
    assert resource["last_processed_at"] == 103
    assert len(rows(env, documents)) == 25
    assert len(list((env.settings.data_dir / "raw").iterdir())) == 1


@pytest.mark.parametrize("change", ["source", "uri", "profile", "target", "status"])
def test_binding_rejects_wrong_key_target_or_status(state_env, change):
    env = state_env
    record_response(env.engine, env.store, evidence(), body())
    candidate = select(env).candidate
    overrides = dict(status_code=304, fetched_at=102)
    if change == "source":
        overrides["source_id"] = "another-source"
    elif change == "uri":
        overrides.update(
            requested_url="https://uc.whu.edu.cn/tzgg/xstz/23.htm",
            final_url="https://uc.whu.edu.cn/tzgg/xstz/23.htm",
        )
    elif change == "profile":
        overrides["request_profile"] = PROFILE.model_copy(update={"user_agent": "another-agent"})
    elif change == "target":
        # Same URI/profile but an incompatible page type cannot reuse list evidence.
        discover(env)
        overrides.update(page_type="notice", source_document_id="1517:128231")
    else:
        bad = record_response(
            env.engine, env.store, evidence(status_code=403, fetched_at=101), None
        )
        candidate = replace(candidate, response_id=bad, etag=None, last_modified=None)
    before = len(rows(env, raw_responses))
    with pytest.raises(IngestError, match="cache_binding_invalid"):
        record_response(env.engine, env.store, evidence(**overrides), None, candidate=candidate)
    assert len(rows(env, raw_responses)) == before


def test_candidate_is_selected_before_response_and_cannot_silently_rebind(state_env):
    env = state_env
    old = record_response(env.engine, env.store, evidence(), body())
    candidate = select(env).candidate
    newer = record_response(env.engine, env.store, evidence(fetched_at=101, etag='"new"'), body())
    assert select(env).candidate.response_id == newer != old
    with pytest.raises(IngestError, match="cache_binding_invalid"):
        record_response(
            env.engine,
            env.store,
            evidence(status_code=304, fetched_at=102),
            None,
            candidate=candidate,
        )


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_raw_loss_before_selection_and_after_conditional_selection_requires_full_fetch(
    state_env, fault
):
    env = state_env
    first = discover(env)
    candidate = select(env).candidate
    raw = env.settings.data_dir / rows(env, raw_responses)[0]["body_path"]
    if fault == "missing":
        raw.unlink()
    else:
        raw.write_bytes(b"damaged")
    lookup = select(env)
    assert lookup.requires_full_fetch and lookup.candidate is None
    assert lookup.reason == ("raw_missing" if fault == "missing" else "raw_digest_mismatch")
    observed = record_response(
        env.engine, env.store, evidence(status_code=304, fetched_at=102), None, candidate=candidate
    )
    result = process_cached_response(env.engine, env.store, observed, 103)
    assert result.outcome == "full_fetch_required" and result.error_code == lookup.reason
    assert result.body_response_id == first.response_id
    assert rows(env, raw_responses)[1]["validated_response_id"] == first.response_id
    assert rows(env, http_resources)[0]["last_processed_at"] == 101


def test_latest_parse_failure_never_falls_back_to_old_success(state_env):
    env = state_env
    discover(env)
    old = import_page(
        env.engine, env.store, notice_evidence(), body("current-notice-detail.html"), 103
    )
    newer = record_response(
        env.engine,
        env.store,
        notice_evidence(fetched_at=104, etag='"new-b"'),
        b"<html>invalid B</html>",
    )
    with pytest.raises(IngestError, match="parse_missing_structure"):
        process_cached_response(env.engine, env.store, newer, 105, failure_due_at=110)
    candidate = select(env, NOTICE).candidate
    assert candidate.response_id == newer
    observed = record_response(
        env.engine,
        env.store,
        notice_evidence(status_code=304, fetched_at=106, etag=None),
        None,
        candidate=candidate,
    )
    calls = []

    def parser(page):
        calls.append(page.content)
        return parse_notice(page)

    with pytest.raises(IngestError, match="parse_missing_structure"):
        process_cached_response(
            env.engine, env.store, observed, 107, notice_parser=parser, failure_due_at=112
        )
    assert calls == [b"<html>invalid B</html>"]
    document = next(row for row in rows(env, documents) if row["id"] == old.document_id)
    assert document["current_version_id"] == old.version_id and document["status"] == "failed"
    assert document["next_due_at"] == 112 and document["last_success_at"] == 103
    resource = next(row for row in rows(env, http_resources) if row["request_uri"] == NOTICE)
    assert (
        resource["latest_response_id"] == newer
        and resource["last_processed_response_id"] == old.response_id
    )
    assert any(
        row["id"] == resource["id"] for row in pending_resources(env.engine, SOURCE, PARSER_VERSION)
    )


def test_304_detail_new_rule_version_uses_original_raw_and_due(state_env):
    env = state_env
    discover(env)
    first = import_page(
        env.engine, env.store, notice_evidence(), body("current-notice-detail.html"), 103
    )
    candidate = select(env, NOTICE).candidate
    observed = record_response(
        env.engine,
        env.store,
        notice_evidence(status_code=304, fetched_at=104, etag='W/"notice"'),
        None,
        candidate=candidate,
    )

    def newer_rules(page):
        return parse_notice(page).model_copy(update={"parser_version": "test-v3"})

    result = process_cached_response(
        env.engine, env.store, observed, 105, notice_parser=newer_rules, next_due_at=200
    )
    assert result.version_id != first.version_id
    versions = rows(env, notice_versions)
    assert len(versions) == 2 and versions[0]["content_sha256"] == versions[1]["content_sha256"]
    assert {row["raw_response_id"] for row in versions} == {first.response_id}
    assert [row["fetched_at"] for row in rows(env, raw_responses)] == [100, 102, 104]
    assert (
        next(row for row in rows(env, documents) if row["id"] == first.document_id)["next_due_at"]
        == 200
    )
    assert len(list((env.settings.data_dir / "raw").iterdir())) == 2


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"vary": "*"}, "vary_unsupported"),
        ({"vary": "Cookie"}, "vary_unsupported"),
        ({"cache_control": "private, no-store"}, "cache_no_store"),
        ({"content_encoding": "gzip"}, "encoding_unsupported"),
        ({"etag": "unquoted"}, "validator_invalid"),
        ({"etag": None}, "validator_missing"),
        ({"last_modified": "not-a-date"}, "validator_invalid"),
        ({"final_url": "https://uc.whu.edu.cn/tzgg/xstz/23.htm"}, "redirect_requires_full_fetch"),
    ],
)
def test_new_disallowed_200_blocks_old_candidate_without_losing_evidence(
    state_env, overrides, reason
):
    env = state_env
    old = record_response(env.engine, env.store, evidence(), body())
    new = record_response(env.engine, env.store, evidence(fetched_at=101, **overrides), body())
    selection = select(env)
    assert selection.requires_full_fetch and selection.reason == reason
    assert rows(env, http_resources)[0]["latest_response_id"] == new != old
    assert len(rows(env, raw_responses)) == 2


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"etag": '"other"'}, "validator_changed"),
        ({"vary": "User-Agent"}, "vary_changed"),
        ({"vary": "*"}, "vary_changed"),
        ({"cache_control": "no-store"}, "cache_no_store"),
    ],
)
def test_conflicting_304_is_preserved_but_requires_full_fetch(state_env, overrides, reason):
    env = state_env
    first = discover(env)
    candidate = select(env).candidate
    observed = record_response(
        env.engine,
        env.store,
        evidence(status_code=304, fetched_at=102, **overrides),
        None,
        candidate=candidate,
    )
    result = process_cached_response(env.engine, env.store, observed, 103)
    assert result.outcome == "full_fetch_required" and result.error_code == reason
    env.engine.dispose()
    assert select(env).requires_full_fetch and select(env).reason == "cache_validation_blocked"
    assert rows(env, raw_responses)[-1]["validated_response_id"] == first.response_id
    assert rows(env, http_resources)[0]["last_processed_at"] == 101


def test_complete_200_clears_persistent_validation_block(state_env):
    env = state_env
    discover(env)
    candidate = select(env).candidate
    record_response(
        env.engine,
        env.store,
        evidence(status_code=304, fetched_at=102, vary="*"),
        None,
        candidate=candidate,
    )
    assert select(env).requires_full_fetch
    # Historical import does not undo a newer server prohibition.
    record_response(env.engine, env.store, evidence(fetched_at=101), body())
    assert select(env).requires_full_fetch
    new = record_response(env.engine, env.store, evidence(fetched_at=103, etag='"new"'), body())
    assert select(env).candidate.response_id == new
    assert rows(env, http_resources)[0]["blocked_by_response_id"] is None


def test_full_query_order_and_profile_are_distinct_keys(state_env):
    env = state_env
    uri = "https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=127581&other=1"
    record_response(env.engine, env.store, evidence(requested_url=uri, final_url=uri), body())
    assert not select(env, uri).requires_full_fetch
    assert select(
        env, "https://uc.whu.edu.cn/2022/show.jsp?wbnewsid=127581&wbtreeid=1517&other=1"
    ).requires_full_fetch
    assert select(env, uri.replace("&other=1", "")).requires_full_fetch
    assert select(
        env, uri, PROFILE.model_copy(update={"accept": "text/html,*/*"})
    ).requires_full_fetch
    assert rows(env, http_resources)[0]["request_uri"] == uri


def test_offline_profile_unknown_and_unbound_304_have_no_guessed_cache(state_env):
    env = state_env
    first = import_page(env.engine, env.store, evidence(request_profile=None), body(), 101)
    assert select(env).requires_full_fetch
    assert rows(env, raw_responses)[0]["resource_id"] is None
    assert process_response(env.engine, env.store, first.response_id, 102).discovered_count == 25
    observed = record_response(
        env.engine, env.store, evidence(status_code=304, fetched_at=103), None
    )
    result = process_cached_response(env.engine, env.store, observed, 104)
    assert result.outcome == "full_fetch_required" and result.error_code == "cache_binding_missing"


@pytest.mark.parametrize("status", [200, 302, 403, 503])
def test_metadata_only_responses_do_not_publish_partial_or_empty_files(state_env, status):
    env = state_env
    observed = record_response(
        env.engine, env.store, evidence(status_code=status, body_state="unavailable"), None
    )
    response = rows(env, raw_responses)[0]
    assert response["body_state"] == "unavailable" and response["body_path"] is None
    assert list((env.settings.data_dir / "raw").iterdir()) == []
    with pytest.raises(IngestError):
        process_response(env.engine, env.store, observed, 101)
    assert rows(env, documents) == [] and rows(env, http_resources)[0]["latest_response_id"] is None
    with pytest.raises(IngestError, match="body_completeness_mismatch"):
        record_response(
            env.engine,
            env.store,
            evidence(status_code=status, body_state="unavailable"),
            b"partial",
        )


def test_automatic_cache_cannot_revert_body_from_another_profile_but_history_can(state_env):
    env = state_env
    discover(env)
    old = import_page(
        env.engine, env.store, notice_evidence(), body("current-notice-detail.html"), 103
    )
    profile = PROFILE.model_copy(update={"user_agent": "new representation"})
    tree = BeautifulSoup(body("current-notice-detail.html"), "html.parser")
    paragraph = tree.new_tag("p")
    paragraph.string = "实际修改正文"
    tree.select_one(".v_news_content").append(paragraph)
    updated = str(tree).encode()
    assert updated != body("current-notice-detail.html")
    latest = import_page(
        env.engine,
        env.store,
        notice_evidence(request_profile=profile, fetched_at=104, etag='"changed"'),
        updated,
        105,
    )
    result = process_cached_response(env.engine, env.store, old.response_id, 106)
    assert result.outcome == "full_fetch_required" and result.error_code == "obsolete_document_body"
    document = next(row for row in rows(env, documents) if row["id"] == old.document_id)
    assert document["current_version_id"] == latest.version_id
    historical = process_response(env.engine, env.store, old.response_id, 107)
    assert historical.version_id == old.version_id


def test_profile_headers_and_fragment_are_validated():
    with pytest.raises(ValidationError):
        RequestProfile(user_agent="agent\r\nCookie:secret", accept="text/html")
    with pytest.raises(ValidationError):
        RequestProfile(user_agent="agent", accept="text/html", accept_encoding="gzip")
    with pytest.raises(ValidationError):
        evidence(requested_url=HOME + "#fragment")


def test_empty_profiled_200_must_be_metadata_failure_not_a_complete_blob(state_env):
    env = state_env
    with pytest.raises(IngestError, match="empty_complete_body"):
        record_response(env.engine, env.store, evidence(), b"")
    assert rows(env, raw_responses) == []
    assert list((env.settings.data_dir / "raw").iterdir()) == []
    response = record_response(env.engine, env.store, evidence(body_state="unavailable"), None)
    assert rows(env, raw_responses)[0]["id"] == response
    assert select(env).requires_full_fetch
