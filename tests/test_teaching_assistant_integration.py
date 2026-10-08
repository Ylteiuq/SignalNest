"""Real EMS bytes, archive and SQLite; retrospective MIME preview, never live mail.

The target is explicitly registered locally, without a fabricated EMS list or
complete scan. The capture happened in 2026; only the pure decision/rendering
test uses a 2024 clock to assess the historical opportunity before its deadline.
"""

import hashlib
import json
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa

from signalnest.config import HttpSettings
from signalnest.contracts import NoticeContent, PageInput
from signalnest.ems_parsing import PARSER_VERSION, parse_ems_notice
from signalnest.fetching import FetchCode, FetchTarget, HttpFetcher
from signalnest.ingestion import ResponseInput, import_page, process_response
from signalnest.mail.contracts import MailItem
from signalnest.mail.rendering import render_mail
from signalnest.notifications.contracts import Action, EventContext, Profile
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts
from signalnest.schema import (
    documents,
    email_outbox,
    ingestion_runs,
    mail_message_members,
    mail_messages,
    notice_versions,
    notification_channel_state,
    notification_decisions,
    notification_events,
    notification_observations,
    raw_responses,
)
from signalnest.storage import open_initialized_engine

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures/teaching-assistant"
STEM = "ems-notice-250571-20261008T102823953399Z"
SOURCE = "whu-ems-notices-offline"
IDENTITY = "1588:250571"
URL = "https://ems.whu.edu.cn/info/1588/250571.htm"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _capture():
    content = (FIXTURES / f"{STEM}.html").read_bytes()
    metadata = json.loads((FIXTURES / f"{STEM}.json").read_text(encoding="utf-8"))
    assert len(content) == metadata["body_bytes"] == 24457
    assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
    assert metadata["status_code"] == 200 and metadata["body_complete"] is True
    assert metadata["requested_url"] == metadata["final_url"] == URL
    # The research capture records completion, not a separate headers-arrival
    # clock. Preserve this known observation time to integer contract precision;
    # do not invent a new fetch or move it back to the 2024 publication date.
    fetched_at = int(datetime.fromisoformat(metadata["finished_at"]).timestamp())
    return content, metadata, fetched_at


def _profile():
    # Explicit fictional engineering profile, not the user's personal settings.
    return Profile(
        institution="whu",
        role="student",
        study_level="master",
        interest_topics=("teaching_assistant",),
    )


def _evaluate(content, now):
    facts = extract_facts(content)
    decision = decide(
        _profile(), facts, EventContext(next_digest_at=now + timedelta(days=1)), now=now
    )
    return facts, decision


def _rows(engine, table):
    with engine.connect() as connection:
        return connection.execute(sa.select(table)).mappings().all()


def _assert_no_live_mail(engine):
    for table in (
        ingestion_runs,
        notification_channel_state,
        notification_observations,
        notification_events,
        notification_decisions,
        email_outbox,
        mail_messages,
        mail_message_members,
    ):
        assert _rows(engine, table) == [], table.name


def test_real_ems_capture_archive_import_reparse_and_reopen_remain_offline(state_env):
    env = state_env
    content, metadata, fetched_at = _capture()
    parsed = parse_ems_notice(PageInput(content=content, page_url=URL))
    # There is no accepted EMS list/pagination adapter. Register a known local
    # target directly instead of claiming a synthetic page proves discovery.
    with env.engine.begin() as connection:
        document_id = connection.execute(
            documents.insert().values(
                source_id=SOURCE,
                source_document_id=IDENTITY,
                detail_url=URL,
                discovered_title=parsed.content.title,
                discovered_at=fetched_at + 1,
                discovery_origin="unknown",
            )
        ).inserted_primary_key[0]
    headers = metadata["response_headers"]
    evidence = ResponseInput(
        page_type="notice",
        source_id=SOURCE,
        source_document_id=IDENTITY,
        requested_url=metadata["requested_url"],
        final_url=metadata["final_url"],
        fetched_at=fetched_at,
        status_code=metadata["status_code"],
        body_state="complete",
        content_type=headers["content-type"],
        etag=headers["etag"],
        last_modified=headers["last-modified"],
    )
    first = import_page(
        env.engine,
        env.store,
        evidence,
        content,
        fetched_at + 2,
        notice_parser=parse_ems_notice,
        processing_origin="offline",
    )
    repeated = import_page(
        env.engine,
        env.store,
        evidence,
        content,
        fetched_at + 3,
        notice_parser=parse_ems_notice,
        processing_origin="offline",
    )
    replayed = process_response(
        env.engine,
        env.store,
        first.response_id,
        fetched_at + 4,
        notice_parser=parse_ems_notice,
        processing_origin="maintenance",
    )
    assert first.document_id == repeated.document_id == replayed.document_id == document_id
    assert first.version_id == repeated.version_id == replayed.version_id
    assert repeated.response_id != first.response_id == replayed.response_id
    assert all(result.outcome == "processed" for result in (first, repeated, replayed))
    assert len(list((env.settings.data_dir / "raw").glob("*.bin"))) == 1
    _assert_no_live_mail(env.engine)

    env.engine.dispose()
    reopened = open_initialized_engine(env.settings.database)
    try:
        target = _rows(reopened, documents)[0]
        versions = _rows(reopened, notice_versions)
        responses = _rows(reopened, raw_responses)
        assert target["status"] == "processed"
        assert target["current_version_id"] == first.version_id
        assert target["last_success_at"] == fetched_at + 4
        assert target["first_discovery_run_id"] is None
        assert target["discovery_origin"] == "unknown"
        assert len(versions) == 1 and len(responses) == 2
        version = versions[0]
        assert version["parser_version"] == PARSER_VERSION
        assert version["parsed_at"] == fetched_at + 2
        assert version["raw_response_id"] == first.response_id
        restored = NoticeContent.model_validate(version["normalized_content"])
        assert restored == parsed.content
        assert version["content_sha256"] == restored.content_sha256()
        assert len(restored.attachments) == 2
        for response in responses:
            assert response["fetched_at"] == fetched_at
            assert response["body_sha256"] == metadata["sha256"]
            assert env.store.read(response["body_path"], response["body_sha256"]) == content
        # A real 2026 import never reopens this expired 2024 vacancy.
        _, current = _evaluate(restored, datetime.fromtimestamp(fetched_at + 4, SHANGHAI))
        # This is an unseen, closed event: the existing decision contract uses
        # IGNORE; STORE_ONLY remains for followed events or explicit exclusions.
        assert current.action == Action.IGNORE
        assert current.time_status == "closed" and current.effective_route == "none"
        _assert_no_live_mail(reopened)
    finally:
        reopened.dispose()


def test_real_historical_recruitment_produces_deterministic_reviewable_mime_preview():
    content, _, _ = _capture()
    parsed = parse_ems_notice(PageInput(content=content, page_url=URL))
    now = datetime(2024, 9, 20, 9, tzinfo=SHANGHAI)
    facts, decision = _evaluate(parsed.content, now)
    assert facts.deadline_at == datetime(2024, 9, 20, 16, tzinfo=SHANGHAI)
    assert decision.action == Action.PUSH_NOW and decision.effective_route == "immediate"
    assert decision.reason_codes == ("deadline_soon",)
    assert decision.eligibility == "unknown" and decision.needs_review
    proofs = [proof for unknown in facts.unknowns for proof in unknown.evidence]
    excerpts = "\n".join(proof.excerpt for proof in proofs)
    for condition in ("本院", "全日制", "原则上", "签署", "附件"):
        assert condition in excerpts
    # These IDs only satisfy the pure renderer's explicit input contract. No
    # event, decision, installation, scan or sendable task is persisted here.
    item = MailItem(
        event_id=1,
        decision_id=1,
        document_id=1,
        source_document_id=parsed.source_document_id,
        kind="new",
        occurred_at=int(now.timestamp()),
        content=parsed.content,
        page_url=str(parsed.page_url),
        decision=decision,
    )
    arguments = dict(
        kind="immediate",
        sender="signalnest@example.invalid",
        recipient="preview@example.invalid",
        message_id="<offline-teaching-assistant-preview@example.invalid>",
        date_at=int(now.timestamp()),
    )
    first = render_mail((item,), **arguments)
    second = render_mail((item,), **arguments)
    assert first == second
    assert first.payload_sha256 == hashlib.sha256(first.payload).hexdigest()
    message = BytesParser(policy=policy.default).parsebytes(first.payload)
    text = message.get_content()
    assert parsed.content.title in str(message["Subject"])
    assert message["Message-ID"] == arguments["message_id"]
    assert message.get_content_type() == "text/plain"
    assert not message.is_multipart()
    assert "待核对" in text and "不能默认符合" in text
    # The existing renderer preserves the original Chinese deadline expression
    # in its excerpt; it does not serialize every structured fact as an ISO time.
    assert "2024年9月20日" in text and "下午16点前" in text
    assert URL in text and "PUSH_NOW" in text
    assert "图片与附件仅保留引用" in text
    assert "本邮件没有下载或附带文件" in text
    assert all(attachment.access == "not_checked" for attachment in parsed.content.attachments)


def test_offline_ems_parser_does_not_expand_production_fetch_targets(state_env):
    def unexpected_send(request):
        raise AssertionError("EMS offline parser must not enable an HTTP request")

    settings = HttpSettings(
        connect_timeout_seconds=5.0,
        read_timeout_seconds=10.0,
        request_interval_seconds=3.0,
        user_agent="SignalNest/offline-integration",
    )
    with HttpFetcher(
        state_env.engine,
        state_env.store,
        settings,
        source_id=SOURCE,
        transport=httpx.MockTransport(unexpected_send),
    ) as fetcher:
        result = fetcher.fetch(
            FetchTarget(uri=URL, page_type="notice", source_document_id=IDENTITY)
        )
        assert result.outcome == "bodyless" and result.error_code == FetchCode.INVALID_TARGET
        assert result.attempts == () and fetcher.requests_sent == 0
    assert _rows(state_env.engine, raw_responses) == []
    _assert_no_live_mail(state_env.engine)
