"""Selected sanitized CS fixtures -> archive -> SQLite -> pure decision/MIME, without HTTP."""

import hashlib
import json
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser

import httpx
import pytest
import sqlalchemy as sa
from test_cs_decisions import NOW, profile
from test_cs_parsing import FIXTURES, HOME, LAST, NOTICE, NOTICE_URL, SECOND, capture, changed

from signalnest.config import HttpSettings
from signalnest.contracts import NoticeContent
from signalnest.cs_parsing import PARSER_VERSION, SOURCE_ID, parse_cs_list, parse_cs_notice
from signalnest.errors import IngestError
from signalnest.fetching import FetchCode, FetchTarget, HttpFetcher
from signalnest.ingestion import ResponseInput, import_page, process_response
from signalnest.instance_lock import writer_lock
from signalnest.mail.contracts import MailItem
from signalnest.mail.rendering import render_mail
from signalnest.notifications.contracts import EventContext
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts
from signalnest.schema import (
    discovered_references,
    documents,
    email_outbox,
    ingestion_runs,
    mail_attempts,
    mail_message_members,
    mail_messages,
    notice_versions,
    notification_channel_state,
    notification_decisions,
    notification_events,
    raw_responses,
    source_ingestion_state,
)
from signalnest.storage import open_initialized_engine


def metadata(stem):
    return json.loads((FIXTURES / f"{stem}.json").read_bytes())


def evidence(stem, identity=None):
    data = metadata(stem)
    headers = data["response_headers"]
    # Preserve the known research capture-completion clock, to contract seconds.
    # No new HTTP event or precise header-arrival timestamp is fabricated.
    return ResponseInput(
        page_type="notice" if identity is not None else "list",
        source_id=SOURCE_ID,
        source_document_id=identity,
        requested_url=data["requested_url"],
        final_url=data["final_url"],
        fetched_at=int(datetime.fromisoformat(data["finished_at"]).timestamp()),
        status_code=data["status_code"],
        body_state="complete",
        content_type=headers["content-type"],
        etag=headers["etag"],
        last_modified=headers["last-modified"],
    )


def rows(engine, table):
    with engine.connect() as connection:
        return connection.execute(sa.select(table)).mappings().all()


def no_live_results(engine):
    for table in (
        ingestion_runs,
        notification_channel_state,
        notification_events,
        notification_decisions,
        email_outbox,
        mail_messages,
        mail_message_members,
        mail_attempts,
    ):
        assert rows(engine, table) == [], table.name
    assert all(
        item["last_complete_scan_at"] is None for item in rows(engine, source_ingestion_state)
    )


def register_lists(env, at):
    results = []
    for index, stem in enumerate((HOME, SECOND, LAST)):
        results.append(
            import_page(
                env.engine,
                env.store,
                evidence(stem),
                capture(stem).content,
                at + index,
                list_parser=parse_cs_list,
                list_parser_version=PARSER_VERSION,
                processing_origin="offline",
            )
        )
    return results


def test_real_lists_and_detail_archive_idempotence_reparse_and_reopen(state_env):
    env = state_env
    at = int(NOW.timestamp())
    with writer_lock(env.settings.database):
        first_lists = register_lists(env, at)
        second_lists = register_lists(env, at + 10)
        listing_documents = rows(env.engine, documents)
        assert len(listing_documents) == 43
        assert all(row["source_id"] == SOURCE_ID for row in listing_documents)
        assert all(row["status"] == "discovered" for row in listing_documents)
        target = next(row for row in listing_documents if row["source_document_id"] == "1074:64481")
        assert target["detail_url"] == NOTICE_URL
        assert [(r.discovered_count, r.pagination.current_page) for r in first_lists] == [
            (15, 1),
            (15, 2),
            (13, 4),
        ]
        assert [r.pagination for r in first_lists] == [r.pagination for r in second_lists]
        assert len(rows(env.engine, discovered_references)) == 0
        assert first_lists[1].next_page_url.endswith("/2.htm")
        assert first_lists[-1].pagination.terminal_evidence == "disabled_next_and_last"
        first = import_page(
            env.engine,
            env.store,
            evidence(NOTICE, "1074:64481"),
            capture(NOTICE).content,
            at + 20,
            notice_parser=parse_cs_notice,
            processing_origin="offline",
        )
        repeated = import_page(
            env.engine,
            env.store,
            evidence(NOTICE, "1074:64481"),
            capture(NOTICE).content,
            at + 21,
            notice_parser=parse_cs_notice,
            processing_origin="offline",
        )
        reparsed = process_response(
            env.engine,
            env.store,
            first.response_id,
            at + 22,
            notice_parser=parse_cs_notice,
            expected_source_id=SOURCE_ID,
            processing_origin="maintenance",
        )
    assert first.document_id == repeated.document_id == reparsed.document_id == target["id"]
    assert first.version_id == repeated.version_id == reparsed.version_id
    assert first.response_id == reparsed.response_id != repeated.response_id
    assert (
        len(rows(env.engine, raw_responses)) == 8
    )  # Six list observations, two notice observations.
    assert len(rows(env.engine, notice_versions)) == 1
    assert len(list((env.settings.data_dir / "raw").glob("*.bin"))) == 4
    no_live_results(env.engine)
    env.engine.dispose()
    reopened = open_initialized_engine(env.settings.database)
    try:
        all_documents = rows(reopened, documents)
        assert sum(row["status"] == "discovered" for row in all_documents) == 42
        processed = next(row for row in all_documents if row["id"] == first.document_id)
        assert processed["status"] == "processed"
        assert processed["last_success_at"] == at + 22
        assert processed["current_version_id"] == first.version_id
        assert processed["first_discovery_run_id"] is None
        version = rows(reopened, notice_versions)[0]
        content = NoticeContent.model_validate(version["normalized_content"])
        assert content == parse_cs_notice(capture(NOTICE)).content
        assert version["parser_version"] == PARSER_VERSION and version["parsed_at"] == at + 20
        facts = extract_facts(content)
        decision = decide(
            profile(), facts, EventContext(next_digest_at=NOW + timedelta(days=1)), now=NOW
        )
        assert decision.action == "DIGEST" and decision.needs_review
        assert decision.eligibility == decision.time_status == "unknown"
        for response in rows(reopened, raw_responses):
            assert response["resource_id"] is None
            archived = env.store.read(response["body_path"], response["body_sha256"])
            assert hashlib.sha256(archived).hexdigest() == response["body_sha256"]
        notice_responses = [
            row for row in rows(reopened, raw_responses) if row["page_type"] == "notice"
        ]
        assert all(
            row["fetched_at"] == evidence(NOTICE, "1074:64481").fetched_at
            for row in notice_responses
        )
        no_live_results(reopened)
    finally:
        reopened.dispose()


def test_cs_list_native_and_pending_reference_remain_one_page_transaction(state_env):
    env = state_env

    def edit(tree):
        tree.select(".under-new > ul > li > a")[2]["href"] = "https://external.example.org/apply"

    page = changed(capture(HOME), edit)
    first = import_page(
        env.engine,
        env.store,
        evidence(HOME),
        page.content,
        int(NOW.timestamp()),
        list_parser=parse_cs_list,
        list_parser_version=PARSER_VERSION,
    )
    assert first.registered_row_count == 15 and first.discovered_count == 14
    assert first.reference_count == 1
    reference = rows(env.engine, discovered_references)[0]
    assert reference["source_id"] == SOURCE_ID
    assert reference["status"] == "pending_adapter"
    # A separate source keeps its own identity/reference namespace even in one database.
    assert all(row["source_id"] == SOURCE_ID for row in rows(env.engine, documents))
    assert first.pagination.current_page == 1 and first.next_page_url is not None
    no_live_results(env.engine)

    def broken(tree):
        tree.select(".under-new > ul > li > a")[10].select_one("span").string = "2026-02-30"

    before = rows(env.engine, documents)
    refs = rows(env.engine, discovered_references)
    with pytest.raises(IngestError) as caught:
        import_page(
            env.engine,
            env.store,
            evidence(HOME),
            changed(page, broken).content,
            int(NOW.timestamp()) + 1,
            list_parser=parse_cs_list,
            list_parser_version=PARSER_VERSION,
        )
    assert caught.value.code == "parse_invalid_field"
    assert rows(env.engine, documents) == before and rows(env.engine, discovered_references) == refs


def test_real_sqlite_abort_rolls_back_native_rows_and_references_together(state_env):
    env = state_env

    def edit(tree):
        tree.select(".under-new > ul > li > a")[2]["href"] = "https://external.example.org/apply"

    page = changed(capture(HOME), edit)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER injected_reference_failure BEFORE INSERT ON discovered_references "
            "BEGIN SELECT RAISE(ABORT, 'injected_cs_failure'); END"
        )
    with pytest.raises(IngestError) as caught:
        import_page(
            env.engine,
            env.store,
            evidence(HOME),
            page.content,
            int(NOW.timestamp()),
            list_parser=parse_cs_list,
            list_parser_version=PARSER_VERSION,
        )
    assert caught.value.code == "database_write_failed"
    assert rows(env.engine, documents) == []
    assert rows(env.engine, discovered_references) == []
    response = rows(env.engine, raw_responses)[0]
    assert response["last_error_code"] == "database_write_failed"
    assert env.store.read(response["body_path"], response["body_sha256"]) == page.content
    assert "injected_cs_failure" not in str(caught.value)
    no_live_results(env.engine)


def test_cs_identity_mismatch_and_bad_recheck_keep_evidence_and_recent_success(state_env):
    env = state_env
    at = int(NOW.timestamp())
    register_lists(env, at)
    wrong_identity = parse_cs_list(capture(HOME)).entries[0].source_document_id
    with pytest.raises(IngestError) as caught:
        import_page(
            env.engine,
            env.store,
            evidence(NOTICE, wrong_identity),
            capture(NOTICE).content,
            at + 10,
            notice_parser=parse_cs_notice,
        )
    assert caught.value.code == "identity_mismatch"
    wrong = next(
        row for row in rows(env.engine, documents) if row["source_document_id"] == wrong_identity
    )
    assert wrong["status"] == "failed" and wrong["current_version_id"] is None
    success = import_page(
        env.engine,
        env.store,
        evidence(NOTICE, "1074:64481"),
        capture(NOTICE).content,
        at + 11,
        notice_parser=parse_cs_notice,
    )
    bad_page = changed(capture(NOTICE), lambda tree: tree.select_one("#vsb_content").decompose())
    with pytest.raises(IngestError) as caught:
        import_page(
            env.engine,
            env.store,
            evidence(NOTICE, "1074:64481"),
            bad_page.content,
            at + 12,
            notice_parser=parse_cs_notice,
        )
    assert caught.value.code == "parse_missing_structure"
    response_id = caught.value.response_id
    for time in (at + 13, at + 14):
        with pytest.raises(IngestError) as retried:
            process_response(
                env.engine, env.store, response_id, time, notice_parser=parse_cs_notice
            )
        assert retried.value.code == "parse_missing_structure"
    target = next(row for row in rows(env.engine, documents) if row["id"] == success.document_id)
    assert target["status"] == "failed" and target["current_version_id"] == success.version_id
    assert target["last_success_at"] == at + 11
    assert target["last_error_code"] == "parse_missing_structure"
    raw = next(row for row in rows(env.engine, raw_responses) if row["id"] == response_id)
    assert env.store.read(raw["body_path"], raw["body_sha256"]) == bad_page.content
    recovered = process_response(
        env.engine,
        env.store,
        success.response_id,
        at + 15,
        notice_parser=parse_cs_notice,
        processing_origin="maintenance",
    )
    assert recovered.version_id == success.version_id and recovered.outcome == "processed"
    no_live_results(env.engine)


def test_pure_cs_decision_can_render_reviewable_digest_without_persisting_or_sending(state_env):
    notice = parse_cs_notice(capture(NOTICE))
    facts = extract_facts(notice.content)
    decision = decide(
        profile(), facts, EventContext(next_digest_at=NOW + timedelta(days=1)), now=NOW
    )
    item = MailItem(
        event_id=1,
        decision_id=1,
        document_id=1,
        source_document_id=notice.source_document_id,
        kind="new",
        occurred_at=int(NOW.timestamp()),
        content=notice.content,
        page_url=str(notice.page_url),
        decision=decision,
    )
    arguments = dict(
        kind="digest",
        sender="signalnest@example.invalid",
        recipient="preview@example.invalid",
        message_id="<cs-offline-preview@example.invalid>",
        date_at=int(NOW.timestamp()),
        digest_slot=int((NOW + timedelta(days=1)).timestamp()),
    )
    rendered = render_mail((item,), **arguments)
    assert render_mail((item,), **arguments) == rendered
    message = BytesParser(policy=policy.default).parsebytes(rendered.payload)
    assert message.get_content_type() == "text/plain"
    assert not message.is_multipart()
    text = message.get_content()
    assert "待核对信息" in text and "不代表已经符合报名条件" in text
    assert notice.content.title in text and NOTICE_URL in text
    assert "DIGEST" in text and "分支" in text
    assert rendered.payload_sha256 == hashlib.sha256(rendered.payload).hexdigest()
    assert rows(state_env.engine, raw_responses) == []
    no_live_results(state_env.engine)


def test_cs_offline_adapter_does_not_change_fetch_permission_or_configuration(state_env):
    def unexpected(request):
        raise AssertionError("CS offline adapter must not permit network collection")

    settings = HttpSettings(
        connect_timeout_seconds=5,
        read_timeout_seconds=10,
        request_interval_seconds=3,
        user_agent="SignalNest/offline-cs",
    )
    with HttpFetcher(
        state_env.engine,
        state_env.store,
        settings,
        source_id=SOURCE_ID,
        transport=httpx.MockTransport(unexpected),
    ) as fetcher:
        result = fetcher.fetch(
            FetchTarget(uri=NOTICE_URL, page_type="notice", source_document_id="1074:64481")
        )
        assert result.error_code == FetchCode.INVALID_TARGET and result.attempts == ()
        assert fetcher.requests_sent == 0
    assert rows(state_env.engine, raw_responses) == []
    no_live_results(state_env.engine)
