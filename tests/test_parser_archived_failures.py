"""Real redacted topology, and actual archive/SQLite retry without new fetching."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from bs4 import BeautifulSoup

from signalnest.contracts import PageInput
from signalnest.cs_parsing import parse_cs_notice
from signalnest.ingestion import IngestError, ResponseInput, import_page, process_response
from signalnest.parsing import ParseError, _one, html_tree, parse_notice
from signalnest.schema import documents, notice_versions, raw_responses

FIXTURES = Path(__file__).parent / "fixtures/processing-failures"
CASES = json.loads((FIXTURES / "manifest.json").read_text())["cases"]


def page(case):
    return PageInput(content=(FIXTURES / case["fixture"]).read_bytes(), page_url=case["source_url"])


def parser(case):
    return parse_cs_notice if case["source"] == "cs" else parse_notice


def old_wrapper_validation(case, input_page):
    """Exact prior body-selection branch, not a simulated HTTP failure."""
    tree = html_tree(input_page)
    if case["source"] == "uc":
        region = _one(tree, "div.news_show:not(.news_show .news_show)", "notice")
        _one(region, "div#vsb_content, div#vsb_content_501", "body")
    else:
        article = _one(tree, 'div.article > form[name="_newscontent_fromname"]', "notice")
        region = _one(article, ":scope > div.content", "body")
        _one(region, ":scope > div#vsb_content, :scope > div#vsb_content_501", "body")
    raise AssertionError("The recorded previous selector must reject this fixture")


@pytest.mark.parametrize("case", CASES, ids=lambda r: r["fixture"])
def test_original_selector_failure_and_current_real_structure_success(case):
    content = page(case)
    assert hashlib.sha256(content.content).hexdigest() == case["fixture_sha256"]
    with pytest.raises(ParseError) as exc:
        old_wrapper_validation(case, content)
    assert exc.value.code.value == case["old_replay"]["code"] == "missing_structure"
    assert exc.value.field == "body"
    parsed = parser(case)(content)
    assert parsed.source_document_id == case["source_document_id"]
    assert parsed.content.title == case["title"]
    assert parsed.content.published_date.isoformat() == case["published_date"]
    assert parsed.content.body_text
    assert parsed.content.content_sha256() == case["sanitized_content_sha256"]
    assert parser(case)(content) == parsed


@pytest.mark.parametrize("case", CASES, ids=lambda r: r["fixture"])
def test_failed_evidence_can_be_reparsed_idempotently_without_new_response(state_env, case):
    source = "whu-cs-undergrad-teaching" if case["source"] == "cs" else "whu-undergrad-student"
    with state_env.engine.begin() as connection:
        document_id = connection.execute(
            documents.insert().values(
                source_id=source,
                source_document_id=case["source_document_id"],
                detail_url=case["source_url"],
                discovered_title=case["title"],
                discovered_at=90,
            )
        ).inserted_primary_key[0]
    metadata = ResponseInput(
        source_id=source,
        source_document_id=case["source_document_id"],
        page_type="notice",
        requested_url=case["source_url"],
        final_url=case["source_url"],
        fetched_at=100,
        status_code=200,
        body_state="complete",
    )
    with pytest.raises(IngestError) as exc:
        import_page(
            state_env.engine,
            state_env.store,
            metadata,
            page(case).content,
            101,
            notice_parser=lambda p: old_wrapper_validation(case, p),
        )
    assert exc.value.code == "parse_missing_structure"
    with state_env.engine.connect() as connection:
        response = connection.execute(sa.select(raw_responses)).mappings().one()
        failed = connection.execute(sa.select(documents)).mappings().one()
        assert failed["status"] == "failed" and failed["current_version_id"] is None
        assert response["body_path"] and response["last_error_code"] == "parse_missing_structure"
    result = process_response(
        state_env.engine, state_env.store, response["id"], 102, notice_parser=parser(case)
    )
    repeated = process_response(
        state_env.engine, state_env.store, response["id"], 103, notice_parser=parser(case)
    )
    assert result.outcome == "processed" and repeated.version_id == result.version_id
    with state_env.engine.connect() as connection:
        current = connection.execute(sa.select(documents)).mappings().one()
        assert current["id"] == document_id and current["status"] == "processed"
        assert (
            current["last_error_code"] is None
            and current["current_version_id"] == result.version_id
        )
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(notice_versions)).scalar_one()
            == 1
        )
        retained = connection.execute(sa.select(raw_responses)).mappings().one()
        assert retained["fetched_at"] == 100 and retained["body_sha256"] == response["body_sha256"]


@pytest.mark.parametrize("source", ["uc", "cs"])
def test_supported_wrapper_still_requires_unique_article_body_title_and_date(source):
    case = next(r for r in CASES if r["source"] == source)
    for kind in (
        "unknown_wrapper",
        "duplicate_wrapper",
        "missing_body",
        "missing_title",
        "bad_date",
    ):
        tree = BeautifulSoup(page(case).content, "html.parser")
        body = tree.select_one(".v_news_content")
        wrapper = body.parent
        if kind == "unknown_wrapper":
            wrapper["id"] = "vsb_content_999999"
        elif kind == "duplicate_wrapper":
            wrapper.parent.append(copy.copy(wrapper))
        elif kind == "missing_body":
            body.decompose()
        elif kind == "missing_title":
            tree.select_one(".title_nei b" if source == "uc" else "h2.title").decompose()
        else:
            tree.select_one(
                ".title_nei i" if source == "uc" else "h4.information"
            ).string = "2026-02-30"
        with pytest.raises(ParseError):
            parser(case)(PageInput(content=str(tree).encode(), page_url=case["source_url"]))
