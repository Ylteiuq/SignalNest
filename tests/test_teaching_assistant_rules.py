"""Applicant-aware recruitment rules replay immutable official pages offline.

The EMS page is parsed by its own production parser.  The university news page
is an explicit negative content input, not a claim that news is a crawl source.
Profiles and replay clocks below are fictional engineering expectations, not
human relevance labels or evidence of present-day vacancy availability.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from signalnest.contracts import NoticeContent, PageInput
from signalnest.ems_parsing import parse_ems_notice
from signalnest.notifications.contracts import Action, EventContext, Profile
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts
from signalnest.parsing import parse_notice

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "research/fixtures"
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2024, 9, 20, 9, tzinfo=SHANGHAI)
TA = "teaching_assistant"
EMS_PATH = FIXTURES / "teaching-assistant/ems-notice-250571-20261008T102823953399Z.html"


def profile(**changes):
    values = dict(
        institution="whu",
        role="student",
        study_level="master",
        interest_topics=(TA,),
    )
    return Profile(**(values | changes))


def notice(body, *, title="课程助教招聘通知"):
    return NoticeContent(
        title=title,
        published_date=NOW.date(),
        body_text=body,
        body_html=f"<p>{body}</p>",
    )


def evaluate(content, *, user=None, now=NOW):
    facts = extract_facts(content)
    result = decide(
        user or profile(),
        facts,
        EventContext(next_digest_at=now + timedelta(days=1)),
        now=now,
    )
    return facts, result


def recruitment_matches(facts):
    return tuple(
        item for item in facts.topic_matches if item.topic == TA and item.context == "opportunity"
    )


def assert_original_evidence(content, proofs):
    for proof in proofs:
        if proof.field in {"title", "body_text"}:
            assert getattr(content, proof.field)[proof.start : proof.end] == proof.excerpt
        else:
            assert str(getattr(content, proof.field)[proof.index].url) == str(proof.url)


@pytest.mark.parametrize("field", ["interest_topics", "high_value_topics", "store_only_topics"])
def test_profile_has_an_explicit_strict_teaching_assistant_topic(field):
    user = profile(**{field: (TA,)})
    assert TA in getattr(user, field)
    with pytest.raises(ValidationError):
        profile(**{field: ("teaching_assistants",)})


@pytest.mark.parametrize("study_level", ["master", "undergraduate"])
def test_real_ems_recruitment_is_urgent_without_claiming_all_conditions_met(study_level):
    parsed = parse_ems_notice(
        PageInput(
            content=EMS_PATH.read_bytes(), page_url="https://ems.whu.edu.cn/info/1588/250571.htm"
        )
    )
    facts, result = evaluate(parsed.content, user=profile(study_level=study_level))
    assert recruitment_matches(facts)
    assert facts.deadline_at == datetime(2024, 9, 20, 16, tzinfo=SHANGHAI)
    assert result.action == Action.PUSH_NOW
    assert result.reason_codes == ("deadline_soon",)
    assert result.effective_route == "immediate"
    assert result.time_status == "open"
    assert result.eligibility == "unknown" and result.needs_review
    assert not any(item.result == "mismatch" for item in result.constraint_results)
    # Neither undergraduate teaching beneficiaries nor 原则上 graduates create
    # an unconditional, fully-known applicant match or exclusion.
    assert not any(
        item.field == "study_level" and item.operator != "unsupported" for item in facts.constraints
    )
    unknown_proofs = tuple(proof for item in facts.unknowns for proof in item.evidence)
    excerpts = "\n".join(proof.excerpt for proof in unknown_proofs)
    for condition in ("本院", "全日制", "原则上", "签署", "附件"):
        assert condition in excerpts
    assert_original_evidence(parsed.content, (*unknown_proofs, *result.evidence))


def test_real_ems_historical_vacancy_is_not_reopened_by_present_day_evaluation():
    parsed = parse_ems_notice(
        PageInput(
            content=EMS_PATH.read_bytes(), page_url="https://ems.whu.edu.cn/info/1588/250571.htm"
        )
    )
    _, result = evaluate(parsed.content, now=datetime(2026, 10, 8, 9, tzinfo=SHANGHAI))
    assert result.time_status == "closed"
    assert result.action == Action.IGNORE and result.effective_route == "none"
    assert "deadline_passed" in result.reason_codes


def test_real_research_course_existing_assistants_are_not_recruitment():
    path = FIXTURES / "notifications/notice-128291-20261005T133533Z.html"
    content = parse_notice(
        PageInput(content=path.read_bytes(), page_url="https://uc.whu.edu.cn/info/1517/128291.htm")
    ).content
    assert "助教" in content.body_text
    facts, result = evaluate(content, now=datetime(2026, 9, 28, 9, tzinfo=SHANGHAI))
    assert not recruitment_matches(facts)
    assert result.action not in {Action.PUSH_NOW, Action.DIGEST}
    assert result.effective_route == "none"


def test_real_news_profile_with_selection_history_is_not_a_current_vacancy():
    path = FIXTURES / "teaching-assistant/whu-news-45084-negative-20261008T102828089357Z.html"
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    body = soup.select_one("#vsb_content .v_news_content")
    assert body is not None
    content = NoticeContent(
        title="【学在武大】我怎样当助教",
        published_date=date(2016, 1, 6),
        body_html=str(body),
        body_text=body.get_text("\n", strip=True),
    )
    assert "选聘助教" in content.body_text
    facts, result = evaluate(content, now=datetime(2016, 1, 6, 17, tzinfo=SHANGHAI))
    assert not recruitment_matches(facts)
    assert result.action not in {Action.PUSH_NOW, Action.DIGEST}
    assert result.effective_route == "none"


def test_research_opportunity_remains_active_when_another_topic_mentions_existing_assistants():
    path = FIXTURES / "notifications/notice-128291-20261005T133533Z.html"
    content = parse_notice(
        PageInput(content=path.read_bytes(), page_url="https://uc.whu.edu.cn/info/1517/128291.htm")
    ).content
    facts, result = evaluate(
        content,
        user=profile(
            study_level="undergraduate",
            interest_topics=(TA, "research"),
            store_only_topics=("course_enrollment",),
        ),
        now=datetime(2026, 9, 28, 9, tzinfo=SHANGHAI),
    )
    assert not recruitment_matches(facts)
    assert result.action == Action.PUSH_NOW and result.reason_codes == ("deadline_soon",)


@pytest.mark.parametrize("action", ["招聘", "招募", "选聘"])
def test_current_direct_recruitment_action_can_reach_urgent_rule(action):
    content = notice(
        f"面向武汉大学硕士研究生。即日起{action}课程助教，申请截止2024年9月20日16:00。",
        title=f"课程助教{action}通知",
    )
    facts, result = evaluate(content)
    assert recruitment_matches(facts)
    assert result.action == Action.PUSH_NOW and "deadline_soon" in result.reason_codes
    assert_original_evidence(content, result.evidence)


def test_recruitment_title_and_generic_body_application_remain_linked():
    content = notice("申请对象：武汉大学硕士研究生。请登录系统完成报名，截止2024年9月20日16:00。")
    facts, result = evaluate(content)
    assert recruitment_matches(facts)
    assert result.action == Action.PUSH_NOW
    assert result.effective_route == "immediate" and result.time_status == "open"
    assert_original_evidence(content, result.evidence)


def test_pay_information_does_not_suppress_a_current_application_instruction():
    content = notice(
        "申请对象：武汉大学硕士研究生。请登录系统完成报名，"
        "截止2024年9月20日16:00。助教岗位按规定发放工资。"
    )
    facts, result = evaluate(content)
    assert recruitment_matches(facts)
    assert result.action == Action.PUSH_NOW and result.effective_route == "immediate"


@pytest.mark.parametrize(
    "other,entry", [("research", "科研训练即日起报名"), ("minor", "辅修专业现开放报名")]
)
@pytest.mark.parametrize("also_follows_other", [False, True])
def test_a_recruitment_title_cannot_borrow_another_topics_current_application(
    other, entry, also_follows_other
):
    content = notice(f"{entry}，截止2024年9月20日16:00。")
    topics = (TA, other) if also_follows_other else (TA,)
    facts, result = evaluate(content, user=profile(interest_topics=topics))
    assert not recruitment_matches(facts)
    if also_follows_other:
        assert result.action == Action.PUSH_NOW
        assert result.effective_route == "immediate"
    else:
        assert result.action == Action.STORE_ONLY and result.effective_route == "none"
        assert result.relevance == "unknown" and result.needs_review
        assert any(item.topic == TA and item.context == "uncertain" for item in facts.topic_matches)
    assert_original_evidence(content, result.evidence)


def test_unexplained_recruitment_title_keeps_uncertain_evidence_without_mail():
    content = notice("有关安排见后续说明。")
    facts, result = evaluate(content)
    assert not recruitment_matches(facts)
    assert any(item.topic == TA and item.context == "uncertain" for item in facts.topic_matches)
    assert result.action == Action.STORE_ONLY and result.effective_route == "none"
    assert result.relevance == "unknown" and result.needs_review
    assert_original_evidence(content, result.evidence)


@pytest.mark.parametrize(
    "body",
    [
        "助教招聘尚未开放。请登录系统完成报名，截止2024年9月20日16:00。",
        "本轮不招聘助教。请登录系统完成报名，截止2024年9月20日16:00。",
        "往年流程举例：请各课程助教于2024年9月20日（周五）下午16点前"
        "将岗位申请表提交到办公室。本次只供历史查阅。",
    ],
)
def test_unavailable_or_historical_recruitment_cannot_borrow_a_generic_current_instruction(body):
    facts, result = evaluate(notice(body))
    assert not recruitment_matches(facts)
    assert result.action not in {Action.PUSH_NOW, Action.DIGEST}
    assert result.effective_route == "none"


@pytest.mark.parametrize("study_level", ["master", "undergraduate"])
def test_explicit_but_soft_applicant_level_never_becomes_a_hard_inclusion_or_exclusion(study_level):
    content = notice(
        "申请对象：原则上武汉大学硕士研究生。即日起招聘课程助教，截止2024年9月20日16:00。"
    )
    facts, result = evaluate(content, user=profile(study_level=study_level))
    assert recruitment_matches(facts)
    assert result.action == Action.PUSH_NOW
    assert result.eligibility == "unknown" and result.needs_review
    assert not any(item.result == "mismatch" for item in result.constraint_results)
    assert any("原则上" in proof.excerpt for item in facts.unknowns for proof in item.evidence)


@pytest.mark.parametrize(
    "audience,study_level", [("本科生", "master"), ("硕士研究生", "undergraduate")]
)
def test_unqualified_strict_applicant_level_can_exclude_a_known_mismatch(audience, study_level):
    content = notice(
        f"申请对象：武汉大学{audience}。现招募课程助教。即日起报名，截止2024年9月20日16:00。"
    )
    _, result = evaluate(content, user=profile(study_level=study_level))
    assert result.eligibility == "ineligible"
    assert result.action == Action.IGNORE and result.effective_route == "none"
    assert "audience_mismatch" in result.reason_codes


def test_ununderstood_conditions_and_deadline_stay_unknown_without_fabricated_urgency():
    content = notice("现招募课程助教。申请须符合附件规定并经任课教师推荐。报名截止另行通知。")
    facts, result = evaluate(content)
    assert recruitment_matches(facts)
    assert facts.deadline_at is None
    assert result.action == Action.DIGEST
    assert result.time_status == "unknown" and result.eligibility == "unknown"
    assert result.needs_review
    assert result.effective_route == "digest"
    assert "deadline_soon" not in result.reason_codes


@pytest.mark.parametrize(
    "unsupported_clock",
    [
        "2024年9月20日（周五）下午4点前",
        "2024年9月20日（周五）下午16点30分前",
        "2024年9月20日（周五）下午16时前",
        "2024年9月31日（周五）下午16点前",
        "2024年9月20日（周四）下午16点前",
    ],
)
def test_real_form_deadline_variants_remain_unknown_instead_of_truncating_a_clock(
    unsupported_clock,
):
    parsed = parse_ems_notice(
        PageInput(
            content=EMS_PATH.read_bytes(), page_url="https://ems.whu.edu.cn/info/1588/250571.htm"
        )
    ).content
    body = parsed.body_text.replace("2024年9月20日（周五）下午16点前", unsupported_clock)
    assert body != parsed.body_text
    changed = parsed.model_copy(update={"body_text": body, "body_html": f"<p>{body}</p>"})
    facts, result = evaluate(changed)
    assert facts.deadline_at is None
    assert result.time_status == "unknown" and result.needs_review
    assert result.action == Action.DIGEST
    assert "deadline_soon" not in result.reason_codes
    assert any(item.field in {"time", "deadline"} for item in facts.unknowns)


def test_real_later_publication_and_appraisal_dates_cannot_replace_application_deadline():
    parsed = parse_ems_notice(
        PageInput(
            content=EMS_PATH.read_bytes(), page_url="https://ems.whu.edu.cn/info/1588/250571.htm"
        )
    ).content
    body = (
        parsed.body_text.replace("2024年9月27日", "2024年9月28日")
        .replace("10月31日", "10月15日")
        .replace("12月30日", "12月10日")
    )
    assert body != parsed.body_text
    changed = parsed.model_copy(update={"body_text": body, "body_html": f"<p>{body}</p>"})
    facts, result = evaluate(changed)
    assert facts.deadline_at == datetime(2024, 9, 20, 16, tzinfo=SHANGHAI)
    assert result.action == Action.PUSH_NOW
    assert changed.content_sha256() != parsed.content_sha256()


@pytest.mark.parametrize(
    "title,body",
    [
        ("课程教学安排", "本课程配备两名助教，负责课后答疑。"),
        ("课程教学安排", "课程报名现已开放，每班配有一名现任助教。"),
        ("课程教学安排", "请登录课程系统完成选课。授课团队包含助教。"),
        ("课程助教津贴发放通知", "现任助教请申请本月津贴。"),
        ("课程助教工资缴费说明", "助教报酬单独计算，学生单独缴费。"),
        ("课程助教退出申请通知", "请现任助教提交退出申请。"),
        ("课程助教考核通知", "已聘任助教请提交考核表。"),
        ("课程助教任职介绍", "张同学去年获选聘为助教，目前负责答疑。"),
        ("课程助教工作回顾", "去年面向研究生招募助教，报名已经结束。"),
        ("课程助教招聘通知", "去年已完成助教招聘，报名已经结束。本次只核对历史报名记录。"),
        ("课程助教栏目使用说明", "点击菜单“课程助教→岗位申请”查看历史记录。"),
        ("课程助教相关安排", "本轮不招聘助教，不接受申请。"),
        ("课程助教相关安排", "助教招聘尚未开放，具体报名时间另行通知。"),
    ],
)
def test_incidental_administration_history_or_unavailable_mentions_are_not_recruitment(title, body):
    facts, result = evaluate(notice(body, title=title))
    assert not recruitment_matches(facts)
    assert result.action not in {Action.PUSH_NOW, Action.DIGEST}
    assert result.effective_route == "none"


def test_beneficiary_undergraduates_do_not_override_explicit_graduate_applicants():
    content = notice(
        "本岗位服务本科教学，帮助本科生完成课程学习。"
        "申请对象：武汉大学硕士研究生。即日起招募课程助教，"
        "申请截止2024年9月20日16:00。"
    )
    facts, result = evaluate(content)
    assert result.action == Action.PUSH_NOW
    assert not any(item.result == "mismatch" for item in result.constraint_results)
    assert not any(
        item.field == "study_level" and "undergraduate" in item.values for item in facts.constraints
    )


def test_repeated_real_page_evaluation_has_identical_evidence_and_decision_hashes():
    content = parse_ems_notice(
        PageInput(
            content=EMS_PATH.read_bytes(), page_url="https://ems.whu.edu.cn/info/1588/250571.htm"
        )
    ).content
    first_facts, first_decision = evaluate(content)
    second_facts, second_decision = evaluate(content)
    assert first_facts.model_dump(mode="json") == second_facts.model_dump(mode="json")
    assert first_facts.sha256() == second_facts.sha256()
    assert first_decision.model_dump(mode="json") == second_decision.model_dump(mode="json")
    assert first_decision.input_sha256 == second_decision.input_sha256
