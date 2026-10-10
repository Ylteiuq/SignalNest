"""Current application evidence without an invented start date.

These earlier engineering boundaries remain separate from the subsequently
frozen original-phrase misses and from human-labelled pages. Q01/Q02 are unchanged.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import Action, EventContext, Profile
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads(
    (ROOT / "docs/validation/notification-current-instruction-cases.json").read_text()
)
NOW = datetime(2026, 10, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
DEADLINE = "截止2026年10月8日23:59。"
PROFILE = Profile(
    institution="whu", study_level="undergraduate", role="student", interest_topics=("research",)
)
CONTEXT = EventContext(next_digest_at=NOW + timedelta(hours=23))


def evaluate(body, *, title="科研训练项目通知", profile=PROFILE):
    content = NoticeContent(
        title=title,
        published_date=date(2026, 10, 8),
        body_text=body,
        body_html=f"<p>{body}</p>",
    )
    facts = extract_facts(content)
    return content, facts, decide(profile, facts, CONTEXT, now=NOW)


@pytest.mark.parametrize("row", CASES["cases"], ids=lambda row: row["case_id"])
def test_bound_current_application_is_open_without_adding_opening_words(row):
    content = NoticeContent.model_validate(row["synthetic_notice_content"])
    assert "即日起" not in content.body_text
    assert content.content_sha256() == row["content_sha256"]
    facts = extract_facts(content)
    decision = decide(
        Profile.model_validate(CASES["profiles"][row["profile"]]),
        facts,
        EventContext.model_validate(row["context"]),
        now=datetime.fromisoformat(row["evaluated_at"]),
    )
    assert facts.opening_confirmed and facts.opens_at is None
    assert decision.action == Action.PUSH_NOW
    assert decision.time_status == "open" and decision.effective_route == "immediate"
    assert decision.reason_codes == ("deadline_soon",)
    assert not any(item.code == "opening_unknown" for item in facts.unknowns)
    proofs = [proof for proof in facts.time_evidence if "请" in (proof.excerpt or "")]
    assert proofs
    for proof in proofs:
        assert proof.field == "body_text"
        assert content.body_text[proof.start : proof.end] == proof.excerpt
        assert "完成" in proof.excerpt and "报名" in proof.excerpt
    assert facts == extract_facts(content)
    assert decision == decide(
        Profile.model_validate(CASES["profiles"][row["profile"]]),
        extract_facts(content),
        EventContext.model_validate(row["context"]),
        now=datetime.fromisoformat(row["evaluated_at"]),
    )


@pytest.mark.parametrize(
    "body",
    [
        "菜单路径：请登录系统完成科研训练报名→查看。",
        "请登录系统完成科研训练报名查看已有记录。",
        "请登录系统查看科研训练报名记录。",
        "历史记录：去年请登录系统完成科研训练报名。",
        "请完成科研训练报名缴费。",
        "请登录系统完成科研训练退课申请。",
        "科研训练报名已结束，请登录系统查看记录。",
        "科研训练报名尚未开放，请查看历史记录。",
    ],
)
def test_menu_history_and_administration_cannot_confirm_opening(body):
    _, facts, decision = evaluate(body + DEADLINE, title="本科生查询说明")
    assert not facts.opening_confirmed
    assert decision.action not in {Action.PUSH_NOW, Action.DIGEST}
    assert decision.effective_route == "none"


@pytest.mark.parametrize(
    "title,body",
    [
        ("科研训练项目通知", "面向武汉大学本科生。报名截止2026年10月8日23:59。"),
        ("请完成科研训练报名", "面向武汉大学本科生。" + DEADLINE),
        (
            "科研训练与辅修专业安排通知",
            "面向武汉大学本科生。请完成报名，" + DEADLINE,
        ),
    ],
)
def test_deadline_title_or_uncertain_topic_cannot_supply_current_application(title, body):
    _, facts, decision = evaluate(body, title=title)
    assert not facts.opening_confirmed
    assert decision.action != Action.PUSH_NOW
    assert any(item.code == "opening_unknown" for item in facts.unknowns)


def test_future_opening_still_precedes_current_instruction_in_time_decision():
    _, facts, decision = evaluate(
        "面向武汉大学本科生。报名时间：2026年10月9日至2026年10月10日。请完成科研训练报名。"
    )
    assert facts.opening_confirmed
    assert facts.opens_at > NOW
    assert decision.time_status == "not_started"
    assert decision.action == Action.DIGEST and decision.effective_route == "digest"


def test_directive_does_not_borrow_other_topics_action_for_research_title():
    _, facts, decision = evaluate("面向武汉大学本科生。请完成辅修专业报名。" + DEADLINE)
    assert not any(
        match.topic == "research" and match.context == "opportunity"
        for match in facts.topic_matches
    )
    assert decision.action != Action.PUSH_NOW


def test_current_research_instruction_keeps_cross_topic_retention_priority():
    _, facts, decision = evaluate(
        "面向武汉大学本科生。请登录系统完成科研训练课程报名，" + DEADLINE,
        title="科研训练课程选课通知",
        profile=PROFILE.model_copy(update={"store_only_topics": ("course_enrollment",)}),
    )
    assert facts.opening_confirmed
    assert decision.action == Action.PUSH_NOW
    assert "interest.topic.research" in decision.matched_rules
    assert "retention.topic.course_enrollment" in decision.matched_rules


def test_supported_directive_does_not_make_unknown_qualification_eligible():
    _, facts, decision = evaluate(
        "面向武汉大学本科生，绩点不低于3.5。请完成科研训练报名，" + DEADLINE
    )
    assert facts.opening_confirmed
    assert decision.action == Action.PUSH_NOW
    assert decision.eligibility == "unknown" and decision.needs_review
    assert any(item.field == "gpa" for item in facts.unknowns)


def test_current_instruction_does_not_make_unrecognized_deadline_precise():
    _, facts, decision = evaluate(
        "面向武汉大学本科生。请完成科研训练报名。截止2026年10月8日下午5点。"
    )
    assert facts.opening_confirmed and facts.deadline_at is None
    assert decision.action == Action.DIGEST and decision.time_status == "unknown"
    assert any(item.code == "invalid_time" for item in facts.unknowns)
