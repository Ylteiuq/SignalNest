"""Local rendering exercises real frozen N0 decisions and RFC 5322 bytes."""

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

import pytest

from signalnest.contracts import AttachmentReference, ImageReference, NoticeContent
from signalnest.mail.contracts import RENDERING_VERSION, MailError, MailItem
from signalnest.mail.rendering import render_mail
from signalnest.notifications.contracts import EventContext, Profile, UnknownItem
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts

NOW = datetime(2026, 10, 7, 1, tzinfo=UTC)
AT = int(NOW.timestamp())
SLOT = AT - 3600
BODY = "武汉大学本科生可报名。即日起开放报名，申请截止日期为2026年10月9日。"


def item(
    *, title="本科生国际交流报名通知", body=BODY, unknown=False, event_id=1, **content_options
):
    content = NoticeContent(
        title=title,
        published_date=date(2026, 10, 5),
        body_html="<p>" + body + "</p>",
        body_text=body,
        **content_options,
    )
    decision = decide(
        Profile(
            institution="whu",
            study_level=None if unknown else "undergraduate",
            interest_topics=("exchange",),
        ),
        extract_facts(content),
        EventContext(next_digest_at=NOW + timedelta(days=1)),
        now=NOW,
    )
    return MailItem(
        event_id=event_id,
        decision_id=event_id,
        document_id=event_id,
        source_document_id=f"1517:{18000 + event_id}",
        kind="new",
        occurred_at=AT - 86400,
        content=content,
        page_url=f"https://uc.whu.edu.cn/info/1517/{18000 + event_id}.htm",
        decision=decision,
    )


def render(items=None, **options):
    values = dict(
        kind="immediate",
        sender="sender@example.com",
        recipient="self@example.com",
        message_id="<signalnest.frozen-1@example.com>",
        date_at=AT,
    )
    values.update(options)
    return render_mail(items or (item(),), **values)


def parsed(mail):
    return BytesParser(policy=policy.default).parsebytes(mail.payload)


def test_utf8_plain_text_exact_headers_and_fixed_date():
    mail = render()
    message = parsed(mail)
    assert message["From"] == "sender@example.com"
    assert message["To"] == "self@example.com"
    assert str(message["Subject"]) == mail.subject
    assert message["Message-ID"] == "<signalnest.frozen-1@example.com>"
    assert parsedate_to_datetime(message["Date"]) == NOW
    assert message.get_content_type() == "text/plain"
    assert message.get_content_charset() == "utf-8"
    assert message["Content-Transfer-Encoding"] == "quoted-printable"
    assert message.get_content().replace("\r\n", "\n") == mail.body_text
    assert not message.is_multipart()
    assert mail.rendering_version == RENDERING_VERSION
    assert mail.payload_sha256 == hashlib.sha256(mail.payload).hexdigest()
    assert b"\n" not in mail.payload.replace(b"\r\n", b"")


def test_identical_frozen_inputs_produce_identical_bytes():
    current = item()
    assert render((current,)) == render((current,))
    assert (
        render((current,)).payload_sha256
        != render((item(body=BODY + "报名需提交成绩单。"),)).payload_sha256
    )


def test_body_has_notice_and_frozen_decision_provenance_without_refreshing_dates():
    current = item()
    mail = render((current,), date_at=AT + 20 * 86400)
    assert current.content.title in mail.body_text
    assert "站点发布日期：2026-10-05" in mail.body_text
    assert "新通知" in mail.body_text
    assert "2026-10-06T01:00:00+00:00" in mail.body_text
    assert "规则判断时间：2026-10-07T01:00:00+00:00" in mail.body_text
    assert current.decision.action.value in mail.body_text
    assert all(reason in mail.body_text for reason in current.decision.reasons)
    assert current.page_url in mail.body_text
    assert "积压补计划" in mail.body_text


def test_pending_review_immediate_is_prominent_and_keeps_frozen_action():
    current = item(unknown=True)
    assert current.decision.needs_review
    mail = render((current,))
    assert "【待核对】" in mail.subject
    assert "【待核对】" in mail.body_text
    assert "提醒不代表已经符合报名条件" in mail.body_text
    assert all(unknown.message in mail.body_text for unknown in current.decision.unknowns)
    assert current.decision.action.value in mail.body_text


def test_digest_has_separate_review_group_and_preserves_within_group_order():
    review = item(unknown=True, title="需核对资格的交流报名", event_id=3)
    regular1 = item(title="常规交流报名甲", event_id=1)
    regular2 = item(title="常规交流报名乙", event_id=2)
    mail = render((review, regular1, regular2), kind="digest", digest_slot=SLOT, part=2)
    text = mail.body_text
    assert "第 2 份" in mail.subject
    assert text.index("=== 常规摘要 ===") < text.index(regular1.content.title)
    assert text.index(regular1.content.title) < text.index(regular2.content.title)
    assert text.index("=== 待核对信息 ===") < text.index(review.content.title)
    assert text.index(regular2.content.title) < text.index("=== 待核对信息 ===")
    assert all(current.page_url in text for current in (review, regular1, regular2))


def test_digest_with_only_pending_items_has_no_empty_regular_section():
    mail = render((item(unknown=True),), kind="digest", digest_slot=SLOT)
    assert "=== 常规摘要 ===" not in mail.body_text
    assert "=== 待核对信息 ===" in mail.body_text


def test_summary_retains_visible_dates_and_numbers_and_reports_truncation():
    text = "2026年10月9日截止，申请名额25个，GPA要求3.5。" + "正文内容。" * 120
    mail = render((item(body=text),))
    assert "2026年10月9日" in mail.body_text
    assert "申请名额25个" in mail.body_text
    assert "GPA要求3.5" in mail.body_text
    assert "摘录已截断" in mail.body_text
    assert text not in mail.body_text
    assert len(mail.body_text) < 3000


def test_html_and_remote_media_are_never_embedded_or_downloaded():
    current = item(
        images=(ImageReference(url="https://example.com/secret.png"),),
        attachments=(
            AttachmentReference(name="申请书.docx", url="https://example.com/private.docx"),
        ),
    )
    content = current.content.model_copy(
        update={
            "body_html": "<script>unsafe-html-marker</script><iframe src='secret'>正文</iframe>"
        }
    )
    # Rebuild a legitimate frozen decision for the changed NoticeContent hash.
    current = replace(
        current,
        content=content,
        decision=current.decision.model_copy(update={"content_sha256": content.content_sha256()}),
    )
    mail = render((current,))
    assert "unsafe-html-marker" not in mail.body_text
    assert "<iframe" not in mail.body_text
    assert "secret.png" not in mail.body_text
    assert "private.docx" not in mail.body_text
    assert "没有下载或附带文件" in mail.body_text
    assert parsed(mail).get_content_type() == "text/plain"


def test_image_only_body_points_to_official_page():
    current = item(body="", images=(ImageReference(url="https://example.com/notice.jpg"),))
    mail = render((current,))
    assert "正文没有可摘录的文字" in mail.body_text
    assert current.page_url in mail.body_text


def test_header_and_body_notice_text_controls_are_flattened():
    current = item(
        title="交流报名\r\nBcc: victim@example.com\u2028\u202e隐藏", body=BODY + "\x00\n额外要求"
    )
    mail = render((current,))
    assert "\r" not in mail.subject
    assert "\n" not in mail.subject
    assert "\u202e" not in mail.subject
    assert "\u2028" not in mail.subject
    assert parsed(mail)["Bcc"] is None
    assert "\x00" not in mail.body_text
    assert "额外要求" in mail.body_text


def test_long_titles_reasons_and_unknowns_are_bounded_with_omission_notes():
    current = item(title="交流报名" + "长标题" * 100, unknown=True)
    decision = current.decision.model_copy(
        update={
            "reasons": tuple("规则理由" * 100 for _ in range(10)),
            "unknowns": tuple(
                UnknownItem(code=f"unknown_{i}", field="资格", message="资格信息仍需核对。")
                for i in range(11)
            ),
        }
    )
    mail = render((replace(current, decision=decision),))
    assert len(mail.subject) < 210
    assert "理由已截断" in mail.body_text
    assert "另有 2 条理由" in mail.body_text
    assert "另有 3 项" in mail.body_text
    assert mail.body_text.count("资格信息仍需核对。") == 8


@pytest.mark.parametrize(
    "options",
    [
        {"sender": "Sender <sender@example.com>"},
        {"recipient": "self@example.com\r\nBcc: other@example.com"},
        {"recipient": "a@example.com,b@example.com"},
        {"sender": "你好@example.com"},
        {"sender": "a..b@example.com"},
        {"message_id": "missing-brackets@example.com"},
        {"message_id": "<a@example.com>\r\nBcc: victim@example.com"},
        {"date_at": -1},
        {"date_at": True},
        {"date_at": 10**100},
        {"kind": "smtp"},
        {"kind": "digest"},
        {"digest_slot": SLOT},
        {"part": 0},
        {"part": True},
    ],
)
def test_invalid_explicit_inputs_have_finite_error(options):
    with pytest.raises(MailError) as error:
        render(**options)
    assert error.value.code == "render_input_invalid"
    assert "victim" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "mailto:self@example.com",
        "javascript:evil()",
        "https://user:secret@example.com/n",
        "https://example.com/\r\nn",
    ],
)
def test_official_url_requires_http_without_credentials_or_controls(url):
    with pytest.raises(MailError, match="render_input_invalid"):
        render((replace(item(), page_url=url),))


def test_nonempty_membership_and_single_immediate_are_enforced():
    with pytest.raises(MailError, match="render_input_invalid"):
        render_mail(
            (),
            kind="digest",
            sender="a@example.com",
            recipient="b@example.com",
            message_id="<a@example.com>",
            date_at=AT,
            digest_slot=SLOT,
        )
    with pytest.raises(MailError, match="render_input_invalid"):
        render((item(), item(event_id=2)))


def test_mismatched_content_and_frozen_decision_are_rejected():
    current = item()
    changed = current.content.model_copy(update={"body_text": BODY + "真实变化"})
    with pytest.raises(MailError) as error:
        render((replace(current, content=changed),))
    assert error.value.code == "render_content_mismatch"
    assert error.value.event_id == current.event_id


def test_long_valid_address_domain_allows_a_larger_generated_message_id():
    domain = ".".join(("a" * 60, "b" * 60, "c" * 60, "example", "com"))
    message_id = "<signalnest." + "f" * 64 + "@" + domain + ">"
    assert len(message_id) > 254
    mail = render(sender="sender@" + domain, message_id=message_id)
    assert str(parsed(mail)["Message-ID"]) == message_id


@pytest.mark.parametrize("changes", [{"content": None}, {"decision": None}, {"event_id": False}])
def test_invalid_dataclass_members_have_finite_validation_errors(changes):
    with pytest.raises(MailError, match="render_input_invalid"):
        render((replace(item(), **changes),))


def test_renderer_does_not_use_wall_clock_or_network(monkeypatch):
    import socket
    import time

    def forbidden(*args, **kwargs):
        pytest.fail("pure rendering attempted external work")

    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    mail = render()
    assert parsedate_to_datetime(parsed(mail)["Date"]) == NOW
