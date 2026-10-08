"""Conservative literal WHU text facts; no network, storage, OCR or attachment parsing.

This is a fixed, deliberately small rule set. A phrase match is evidence for that
phrase, not a claim that all conditions in an arbitrary notice were understood.
"""

import re
from datetime import datetime, time, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import (
    FACTS_EXTRACTOR_VERSION,
    RULES_VERSION,
    Evidence,
    NoticeFacts,
    QualificationConstraint,
    TopicMatch,
    UnknownItem,
)

TOPIC_PHRASES = MappingProxyType(
    {
        "exchange": ("国际交流", "交换生", "交流项目", "海外交流", "出国交流", "海外研修"),
        "scholarship": ("奖学金", "助学金"),
        "research": ("创新研究", "科研训练", "创新创业训练", "大创项目", "本科生科研"),
        "competition": ("竞赛", "大赛"),
        "course_enrollment": ("选课", "课程报名"),
        "minor": ("辅修", "双学士学位"),
        "recommendation": ("推免", "推荐免试"),
    }
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})(?:年|[-/])\s*(?P<month>\d{1,2})(?:月|[-/])"
    r"\s*(?P<day>\d{1,2})(?!\d)(?:日)?"
    r"(?:\s*(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?!\d))?"
)
_TIMELESS_DATE = re.compile(
    r"(?<![\d年/-])(?P<month>\d{1,2})月\s*(?P<day>\d{1,2})日"
    r"(?:\s*(?P<hour>\d{1,2})[:：](?P<minute>\d{2})(?!\d))?"
)
_UNSUPPORTED_TIMEZONE = re.compile(
    r"\b(?:UTC|GMT|EST|EDT|PST|PDT|CST|CDT|MST|MDT|CET|CEST)\b"
    r"|纽约时间|伦敦时间|东京时间|当地时间|美国时间|英国时间"
    r"|(?:美国(?:东部|中部|西部|山地|太平洋)|美东|美中|美西|欧洲中部|中欧|东部|中部|太平洋)时间",
    re.IGNORECASE,
)
_UNSUPPORTED_TIME_SUFFIX = re.compile(
    r"\s*(?:[:：.]|[~～—–-]\s*\d{1,2}\s*[:：]|(?:至|到)\s*\d{1,2}\s*(?:[:：]|日)"
    r"|[+-]\s*\d{2}:|\b(?:AM|PM|Z)\b|时|点|秒)",
    re.IGNORECASE,
)
_UNSUPPORTED_DATE_CLOCK = re.compile(
    r"\s*(?:[，,（(]\s*)?(?:清晨|凌晨|早上|上午|中午|下午|傍晚|晚上|夜间|午夜"
    r"|[晚早]\s*\d|T\s*\d{1,2}[:：]|\d{1,2}\s*[:：时点])"
)
_LOCAL_TIMEZONES = ("北京时间", "上海时间", "中国标准时间")
_BRACKETED_TIMEZONE = re.compile(r"[（(]\s*([\u4e00-\u9fff]{2,16}(?:时间|时区))\s*[)）]")
_SUFFIX_TIMEZONE = re.compile(r"\s*([\u4e00-\u9fff]{2,16}(?:时间|时区))")
_DEADLINE = re.compile(
    r"(?:报名|申请|申报|选课)?(?:截止(?:时间|日期)?(?!后)|最迟)|(?:报名|申请|申报|选课)时间"
)
_PERIOD = re.compile(r"(?:报名|申请|申报|选课)(?:时间|期间)")
_AUDIENCE = re.compile(
    r"(?:仅面向|只面向|面向|仅限|只限|(?:报名|申请|申报|选课)对象(?:与要求)?\s*[:：]|"
    r"报名条件\s*[:：\n]\s*(?:\d+[.．]\s*)?)(?P<value>[^。；;\n]+)"
)
_CAN_PARTICIPATE = re.compile(
    r"(?:武汉大学)?(?:20\d{2}级)?(?:全日制)?(?:在校)?(?:本科生|研究生)"
    r"(?:均可|可|可以)(?:报名|申请|参加)"
)
_UNSUPPORTED_QUALIFICATIONS = (
    ("gpa", re.compile(r"GPA|绩点|平均成绩", re.IGNORECASE)),
    (
        "language",
        re.compile(r"(?:英语|语言)[^。；\n]{0,16}(?:要求|达到|不低于)|CET-[46]|雅思|托福"),
    ),
    ("grade", re.compile(r"[一二三四五六]年级|大[一二三四五六]")),
    (
        "project_membership",
        re.compile(r"(?:面向|仅限)[^。；\n]{0,20}(?:预立项项目|项目成员|项目团队)"),
    ),
    ("financial_condition", re.compile(r"(?:家庭)?经济困难|贫困(?:生|家庭)")),
    ("age", re.compile(r"年龄[^。；\n]{0,18}(?:周岁|不超过|不低于)|年满\s*\d+|\d+\s*周岁")),
    ("disciplinary_record", re.compile(r"无处分记录|无纪律处分|未受[^。；\n]{0,8}处分|无违纪记录")),
)
_QUALIFICATION_SECTION = re.compile(
    r"(?m)^\s*(?:[一二三四五六七八九十]+[、.．]\s*)?"
    r"(?:(?:报名|申请|申报|选课)(?:条件|要求)|资格(?:条件|要求))\s*[:：]?\s*"
)
_NEXT_SECTION = re.compile(
    r"(?m)^\s*[一二三四五六七八九十]+[、.．]\s*"
    r"(?:(?:报名|申请|申报|选课|提交)(?:材料|流程|方式|时间|安排|费用)|"
    r"日程|咨询|联系|附件)"
)
_NUMBERED_CONDITION = re.compile(
    r"(?:^|[。\n；;])\s*(?:\d+[.．、)）]|[（(]\s*(?:\d+|[一二三四五六七八九十]+)"
    r"\s*[)）]|[一二三四五六七八九十]+[、.．])\s*(?P<value>[^。；;\n]+)"
)
_SECTION_OR = re.compile(r"满足(?:其中)?(?:一项|任一项)|符合(?:其中)?任一|任一条件|条件之一|任选")
_UNSCOPED_OR = re.compile(
    r"满足(?:其中)?(?:一项|任一项)|符合(?:其中)?任一(?:项|条件)|任一条件|条件之一"
)
_OR = re.compile(
    r"或者|或|(?:本科生|研究生|教师)[、和及](?:本科生|研究生|教师)|含20\d{2}级"
    r"|不含|除外|除.*以外|20\d{2}(?:至|到|[-—])20\d{2}级"
)
_CRITICAL_MEDIA = re.compile(
    r"(?:报名|申请|资格|条件|对象|截止|时间|安排)[^。；\n]{0,25}"
    r"(?:见|详见|参见|如下)[^。；\n]{0,15}(?:附件|图片|图中|下图|图表)"
    r"|(?:条件|对象|资格|截止|报名时间)[^。；\n]{0,25}[（(]附件"
)
_RESULTS = re.compile(r"结题|验收结果|评审结果|获奖名单|录取名单|结果公示|公布.*结果")
_FACULTY_APPLICANT = re.compile(r"现面向(?:全校|我校|武汉大学)教师征集[^。；;\n]{0,24}选题")
_TEAM_SUBMISSION = re.compile(r"项目团队(?:完成|须|需)[^。；;\n]{0,24}(?:提交|材料)")
_ADMIN_TIME = re.compile(
    r"(?:指导教师|各项目单位|各学院)[^。；;\n]{0,32}(?:审核|评审|汇总|提交|发送)"
)
_CURRENT_APPLICATION = re.compile(r"可提出立项补报申请")
_LATER_ROUND_TIME = re.compile(
    r"^(?:辅修第一轮录取结果和)?第二轮报名时间将另行通知(?:[，,]请及时关注)?\s*$"
)
_DEFERRED_DEADLINE = re.compile(
    r"截止(?:时间|日期)?\s*(?:另行通知|(?:见|详见|以)[^，,。；;\n]{0,20}(?:通知|为准))"
)
_ENTRY_ACTION = re.compile(r"报名|申请|申报|征集|招募|招生|招收|选拔|选派|选课")
_CONTEXT_BREAK = re.compile(r"[。；;！!？?\n]")
_STEP_BREAK = re.compile(r"[，,]")
_ADMIN_ACTION = re.compile(r"缴费|收费|补缴|退费|退课|退出|成绩转换|学分转换|证书申请")
_NAVIGATION = re.compile(r"登录|菜单|点击|智慧珞珈|报名申请\s*[-→>]")
_PAST_CONTEXT = re.compile(r"去年|往年|上学期|上一年度|曾经|曾于|已于|此前|历史")
_CURRENT_CONTEXT = re.compile(r"即日起|现(?:启动|开放|开展|接受)|本次|本轮|今年")
_NEGATED_ENTRY = re.compile(r"(?:不|未|暂停|停止|取消)[^，,。；;\n]{0,10}$")
_CLOSED_ENTRY = re.compile(r"\s*(?:已(?:经)?(?:结束|截止|完成|关闭)|结束|停止|关闭)")
_UNAVAILABLE_ENTRY = re.compile(
    r"\s*(?:尚未|暂未|未|不|暂停|停止|取消)(?:正式)?(?:开放|启动|开始|开展|接受|进行)"
)
_MINOR_AFTER = re.compile(
    r"\s*(?:专业|项目|学习|课程)?\s*(?:的)?\s*"
    r"(?:(?:现|即日起)?(?:开始|启动|开放|接受))?\s*"
    r"(?P<action>报名|招生|招收|招募|申请(?:修读|攻读)?)"
)
_MINOR_BEFORE = re.compile(r"(?:报名(?:修读|参加)?|申请(?:修读|攻读)?)\s*$")


def _sentences(text: str):
    """Yield nonempty clauses and original Unicode offsets without rewriting text."""
    for match in re.finditer(r"[^。；;\n]+", text):
        if match.group().strip():
            yield match.group(), match.start(), match.end()


def _date_value(
    match: re.Match[str], *, deadline: bool, inherited_year: int | None = None
) -> datetime | None:
    """An inherited year is supplied only for an explicit same-clause range end."""
    year = int(match["year"]) if "year" in match.groupdict() else inherited_year
    if year is None:
        return None
    month, day = (int(match[name]) for name in ("month", "day"))
    hour, minute = match["hour"], match["minute"]
    suffix = match.string[match.end() :]
    if _UNSUPPORTED_TIME_SUFFIX.match(suffix):
        # Never truncate seconds, a clock-only range, fractional time or an explicit offset.
        return None
    if hour is None:
        if _UNSUPPORTED_DATE_CLOCK.match(suffix):
            return None
        clock = time(23, 59, 59) if deadline else time.min
    else:
        if int(hour) == 24 and int(minute) == 0:
            try:
                return datetime(year, month, day, tzinfo=SHANGHAI) + timedelta(days=1)
            except ValueError:
                return None
        try:
            clock = time(int(hour), int(minute))
        except ValueError:
            return None
    try:
        return datetime(year, month, day, clock.hour, clock.minute, clock.second, tzinfo=SHANGHAI)
    except ValueError:
        return None


def _source_fields(content: NoticeContent):
    return (("title", content.title), ("body_text", content.body_text))


def _qualification_sections(text):
    """Explicit headings; unrecognized numbered conditions cannot end the section."""
    for header in _QUALIFICATION_SECTION.finditer(text):
        following = _NEXT_SECTION.search(text[header.end() :])
        end = header.end() + following.start() if following else len(text)
        yield header.start(), header.end(), end


def _evidence(field: str, text: str, start: int, end: int) -> Evidence:
    end = min(end, start + 120)
    return Evidence(field=field, start=start, end=end, excerpt=text[start:end])


def _unknown(code, field, message, *evidence):
    return UnknownItem(code=code, field=field, message=message, evidence=tuple(evidence))


def rule_manifest() -> dict:
    """Serializable immutable-policy input; never read mutable configuration at runtime."""
    return {
        "facts_extractor_version": FACTS_EXTRACTOR_VERSION,
        "rules_version": RULES_VERSION,
        "topic_phrases": {key: list(values) for key, values in TOPIC_PHRASES.items()},
        "primary_topic": "title subject; body keyword alone is incidental, not active interest",
        "topic_context": {
            "values": ["subject", "opportunity", "incidental"],
            "entry_actions": _ENTRY_ACTION.pattern,
            "clause_break": _CONTEXT_BREAK.pattern,
            "administration_step_break": _STEP_BREAK.pattern,
            "maximum_topic_action_gap": 24,
            "minor_after_pattern": _MINOR_AFTER.pattern,
            "minor_before_pattern": _MINOR_BEFORE.pattern,
            "administration_pattern": _ADMIN_ACTION.pattern,
            "navigation_pattern": _NAVIGATION.pattern,
            "past_pattern": _PAST_CONTEXT.pattern,
            "current_pattern": _CURRENT_CONTEXT.pattern,
            "negated_entry_pattern": _NEGATED_ENTRY.pattern,
            "closed_entry_pattern": _CLOSED_ENTRY.pattern,
            "unavailable_entry_pattern": _UNAVAILABLE_ENTRY.pattern,
            "other_topic": (
                "another named topic in the gap rejects binding; research course is shared"
            ),
            "occurrences": (
                "classify every occurrence; incidental mentions cannot mask later offers"
            ),
        },
        "category": {
            "reference_title": _RESULTS.pattern,
            "administration_title": _ADMIN_ACTION.pattern,
            "administration_reference": "only when no title topic has a linked entry action",
            "opportunity_phrase": _ENTRY_ACTION.pattern,
            "opportunity_exclusions": "same entry exclusions as topic binding",
        },
        "audience_patterns": [_AUDIENCE.pattern, _CAN_PARTICIPATE.pattern],
        "supported_qualification_fields": [
            "role: explicit current faculty topic-solicitation applicant, not student beneficiary",
            "institution: explicit Wuhan University or 全校/我校 scope",
            "study_level: undergraduate/master/doctoral/faculty",
            "college: literal 学院名 in audience clause",
            "major: literal 专业为/专业： or X专业学生",
            "entry_year: explicit 20XX级",
        ],
        "unsupported_qualification_patterns": {
            field: pattern.pattern for field, pattern in _UNSUPPORTED_QUALIFICATIONS
        },
        "boolean_logic": "explicit AND only; OR and conflicting constraints remain unknown",
        "qualification_sections": {
            "heading_pattern": _QUALIFICATION_SECTION.pattern,
            "next_section_pattern": _NEXT_SECTION.pattern,
            "numbered_condition_pattern": _NUMBERED_CONDITION.pattern,
            "unparsed_numbered_conditions": (
                "unsupported; recognizing the first item is insufficient"
            ),
            "or_pattern": _SECTION_OR.pattern,
            "unscoped_or_pattern": _UNSCOPED_OR.pattern,
        },
        "time": {
            "complete_date_pattern": _DATE.pattern,
            "deadline_context_pattern": _DEADLINE.pattern,
            "timezone": "Asia/Shanghai",
            "date_only_deadline": "23:59:59 on stated date",
            "date_only_start": "00:00:00 on stated date",
            "relative_opening": "即日起/自通知发布之日起 use explicit site publication date",
            "unsupported_time_suffix_pattern": _UNSUPPORTED_TIME_SUFFIX.pattern,
            "unsupported_date_clock_pattern": _UNSUPPORTED_DATE_CLOCK.pattern,
            "unsupported_timezone_pattern": _UNSUPPORTED_TIMEZONE.pattern,
            "local_timezone_labels": list(_LOCAL_TIMEZONES),
            "bracketed_timezone_pattern": _BRACKETED_TIMEZONE.pattern,
            "suffix_timezone_pattern": _SUFFIX_TIMEZONE.pattern,
            "year_inheritance": "only YYYY date 至/到 M月D日 in one explicit application interval",
            "midnight": "24:00 exactly means next calendar day 00:00",
            "before_date": "日前 retains [day start, day end] boundary and imprecise_deadline",
            "team_submission_pattern": _TEAM_SUBMISSION.pattern,
            "administrative_deadline_pattern": _ADMIN_TIME.pattern,
            "current_application_pattern": _CURRENT_APPLICATION.pattern,
            "separate_later_round_pattern": _LATER_ROUND_TIME.pattern,
            "deferred_deadline_pattern": _DEFERRED_DEADLINE.pattern,
            "unsupported": (
                "standalone missing year, cross-year guessing, multiple applicant deadlines, "
                "seconds, clock ranges, other timezones"
            ),
        },
        "faculty_applicant_pattern": _FACULTY_APPLICANT.pattern,
        "media": "references remain unparsed; image-only or explicit critical dependency unknown",
        "cancellation": "disabled pending trustworthy positive sample",
        "limits": "literal small rule set; no OCR, attachment parsing, semantic completeness claim",
    }


def _topics(content):
    matches = []
    for field, text in _source_fields(content):
        for topic, phrases in TOPIC_PHRASES.items():
            for phrase in phrases:
                for match in re.finditer(re.escape(phrase), text):
                    context, start, end = _topic_context(topic, field, text, match)
                    matches.append(
                        TopicMatch(
                            topic=topic,
                            evidence=_evidence(field, text, start, end),
                            primary=field == "title",
                            context=context,
                        )
                    )
    return tuple(matches)


def _clause_bounds(text, start, end):
    before = list(_CONTEXT_BREAK.finditer(text, 0, start))
    after = _CONTEXT_BREAK.search(text, end)
    return before[-1].end() if before else 0, after.start() if after else len(text)


def _entry_blocked(text, start, end, *, subject_start=None, subject_end=None):
    """Finite exclusions for witnessed administration, not general language understanding."""
    left, right = _clause_bounds(text, start, end)
    beginning = min(start, subject_start if subject_start is not None else start)
    stop = max(end, subject_end if subject_end is not None else end)
    prefix = text[max(left, beginning - 32) : beginning]
    after = text[end : min(right, end + 16)]
    if (
        _NEGATED_ENTRY.search(prefix)
        or _CLOSED_ENTRY.match(after)
        or _UNAVAILABLE_ENTRY.match(after)
    ):
        return True
    prior_steps = list(_STEP_BREAK.finditer(text, left, beginning))
    step_left = prior_steps[-1].end() if prior_steps else left
    next_step = _STEP_BREAK.search(text, stop, right)
    step_right = next_step.start() if next_step else right
    if _NAVIGATION.search(text[step_left : min(step_right, stop + 8)]):
        return True
    past = list(_PAST_CONTEXT.finditer(prefix))
    current = list(_CURRENT_CONTEXT.finditer(prefix))
    if past and (not current or past[-1].start() > current[-1].start()):
        return True
    # Do not veto an entire notice for mentioning fees. Only the proposed
    # topic/action relation and the action's immediate object are considered.
    if _ADMIN_ACTION.search(text[beginning : min(step_right, stop + 8)]):
        return True
    if _ADMIN_ACTION.search(text[max(step_left, start - 4) : start]):
        return True
    return False


def _topic_context(topic, field, text, match):
    start, end = match.span()
    left, right = _clause_bounds(text, start, end)
    for action in _ENTRY_ACTION.finditer(text, max(left, start - 32), min(right, end + 40)):
        if action.end() <= start:
            gap = text[action.end() : start]
        elif end <= action.start():
            gap = text[end : action.start()]
        else:
            gap = ""
        if len(gap) > 24 or _entry_blocked(
            text, action.start(), action.end(), subject_start=start, subject_end=end
        ):
            continue
        if topic == "minor":
            after = _MINOR_AFTER.match(text[end:right])
            before = _MINOR_BEFORE.search(text[left:start])
            if not (
                (after is not None and end + after.start("action") == action.start())
                or (before is not None and left + before.start() == action.start())
            ):
                continue
        if any(
            phrase in gap
            for other, phrases in TOPIC_PHRASES.items()
            if other != topic and not (topic == "research" and other == "course_enrollment")
            for phrase in phrases
        ):
            continue
        return "opportunity", min(start, action.start()), max(end, action.end())
    return ("subject" if field == "title" else "incidental"), start, end


def _opportunities(content):
    return tuple(
        _evidence(field, text, match.start(), match.end())
        for field, text in _source_fields(content)
        for match in _ENTRY_ACTION.finditer(text)
        if not _entry_blocked(text, match.start(), match.end())
    )


def _qualifications(content):
    constraints, unknowns = [], []
    audience_declared = False
    incomplete = False
    for field, text in _source_fields(content):
        faculty_applicant = _FACULTY_APPLICANT.search(text)
        audience_matches = sorted(
            (*_AUDIENCE.finditer(text), *_CAN_PARTICIPATE.finditer(text)),
            key=lambda match: match.start(),
        )
        for match in audience_matches:
            # This witnessed template explicitly solicits topics from teachers.
            # Its descriptions of the eventual student beneficiaries are not a
            # competing declaration about who submits the current application.
            if faculty_applicant is not None and not (
                match.start() <= faculty_applicant.start() + 1 < match.end()
                or faculty_applicant.start() <= match.start() < faculty_applicant.end()
            ):
                continue
            clause = match.groupdict().get("value") or match.group()
            evidence = _evidence(field, text, match.start(), match.end())
            audience_declared = True
            negated = re.search(
                r"不|非", text[max(0, match.start() - 3) : match.start()]
            ) or re.search(r"非(?:本科生|研究生|教师)|不包括|并非", clause)
            ambiguous = _OR.search(clause) is not None or negated is not None
            if ambiguous:
                incomplete = True
                unknowns.append(
                    _unknown(
                        "unsupported_negation" if negated else "unsupported_or",
                        "eligibility",
                        "资格含未支持的否定、OR 或例外条件。",
                        evidence,
                    )
                )
            extracted = []
            if any(scope in clause for scope in ("武汉大学", "全校", "我校")):
                extracted.append(("institution", ("whu",)))
            levels = []
            if "本科生" in clause:
                levels.append("undergraduate")
            if "硕士研究生" in clause:
                levels.append("master")
            if "博士研究生" in clause:
                levels.append("doctoral")
            if "研究生" in clause and not any(word in clause for word in ("硕士", "博士")):
                levels.extend(("master", "doctoral"))
            if "教师" in clause and faculty_applicant is not None:
                extracted.append(("role", ("faculty",)))
            elif "教师" in clause:
                levels.append("faculty")
            if levels:
                extracted.append(("study_level", tuple(levels)))
            years = tuple(int(value) for value in re.findall(r"(20\d{2})级", clause))
            if years:
                extracted.append(("entry_year", years))
            # Narrow separators keep 全校各学院 and 辅修专业 from becoming personal requirements.
            local_clause = clause
            for scope in ("武汉大学", "全校", "我校"):
                local_clause = local_clause.removeprefix(scope)
            college_match = re.search(
                r"(?:^|[:：，,\s]|大学)([\u4e00-\u9fff]{2,15}学院)(?=学生|本科生|研究生|20\d{2}级|[，,\s]|$)",
                local_clause,
            )
            if college_match and college_match[1] not in {"各学院", "全校各学院"}:
                extracted.append(("college", (college_match[1],)))
            major_match = re.search(r"专业(?:为|[:：])\s*([^，,。；;\s]+)", local_clause)
            if major_match is None:
                major_match = re.search(
                    r"(?:^|[:：，,\s]|大学)([\u4e00-\u9fff]{2,20})专业(?:的)?(?:学生|本科生|研究生)",
                    local_clause,
                )
            if major_match:
                extracted.append(("major", (major_match[1],)))
            if "全日制" in clause or "在校" in clause:
                incomplete = True
                status = "全日制" if "全日制" in clause else "在校"
                constraints.append(
                    QualificationConstraint(
                        field="student_status",
                        values=(status,),
                        operator="unsupported",
                        evidence=evidence,
                    )
                )
                unknowns.append(
                    _unknown(
                        "unsupported_condition",
                        "student_status",
                        "Profile 未保存学籍状态。",
                        evidence,
                    )
                )
            for constraint_field, values in extracted:
                constraints.append(
                    QualificationConstraint(
                        field=constraint_field,
                        values=values,
                        operator="unsupported" if ambiguous else "one_of",
                        evidence=evidence,
                    )
                )
            # Completeness is restricted to fully consumed small audience expressions.
            # Recognizing 本科生 alone must not erase a comma followed by extra requirements.
            residue = clause
            for constraint_field, values in extracted:
                if constraint_field in {"college", "major"}:
                    for value in values:
                        residue = residue.replace(str(value), "")
                elif constraint_field == "entry_year":
                    for value in values:
                        residue = residue.replace(f"{value}级", "")
            residue = re.sub(
                r"武汉大学|全校|我校|全体|本科生|硕士研究生|博士研究生|研究生|教师|"
                r"专业为|专业[:：]|学生|均可|可以|报名|申请|参加|征集|可|且|为|的|[，,\s]",
                "",
                residue,
            )
            if residue:
                incomplete = True
                constraints.append(
                    QualificationConstraint(
                        field="other", operator="unsupported", evidence=evidence
                    )
                )
                unknowns.append(
                    _unknown(
                        "unrecognized_condition",
                        "eligibility",
                        "对象声明含未完整理解的附加内容。",
                        evidence,
                    )
                )
            if not extracted:
                incomplete = True
                unknowns.append(
                    _unknown(
                        "unrecognized_audience",
                        "eligibility",
                        "对象声明不属于支持的资格表达式。",
                        evidence,
                    )
                )
                constraints.append(
                    QualificationConstraint(
                        field="other", operator="unsupported", evidence=evidence
                    )
                )
        for constraint_field, pattern in _UNSUPPORTED_QUALIFICATIONS:
            for match in pattern.finditer(text):
                incomplete = True
                evidence = _evidence(field, text, match.start(), match.end())
                stored_field = (
                    constraint_field
                    if constraint_field in {"gpa", "language", "grade"}
                    else "other"
                )
                constraints.append(
                    QualificationConstraint(
                        field=stored_field, operator="unsupported", evidence=evidence
                    )
                )
                unknowns.append(
                    _unknown(
                        "unsupported_condition",
                        constraint_field,
                        "正文有当前 Profile 无法核对的条件，不默认为符合。",
                        evidence,
                    )
                )
        # Unknown additional mandatory conditions cannot be erased by recognizing a student label.
        extra = re.search(
            r"(?:还须|还需|必须|应当|须具备|(?:报名|申请|选课|参与)[^。；;\n]{0,8}"
            r"(?:须|需|应|要求))[^。；;\n]+",
            text,
        )
        if extra:
            incomplete = True
            evidence = _evidence(field, text, extra.start(), extra.end())
            constraints.append(
                QualificationConstraint(field="other", operator="unsupported", evidence=evidence)
            )
            unknowns.append(
                _unknown(
                    "unsupported_condition", "other", "有额外强制条件，首版未完整理解。", evidence
                )
            )
        sections = tuple(_qualification_sections(text))
        if not sections and audience_matches and (alternative := _UNSCOPED_OR.search(text)):
            incomplete = True
            constraints = [
                item.model_copy(update={"operator": "unsupported"})
                if item.evidence.field == field
                else item
                for item in constraints
            ]
            proof = _evidence(field, text, alternative.start(), alternative.end())
            unknowns.append(
                _unknown("unsupported_or", "eligibility", "跨句的资格 OR 无法可靠分组。", proof)
            )
        for section_start, body_start, section_end in sections:
            section = text[body_start:section_end]
            for item in _NUMBERED_CONDITION.finditer(section):
                start = body_start + item.start("value")
                end = body_start + item.end("value")
                if any(match.start() <= start and end <= match.end() for match in audience_matches):
                    continue
                incomplete = True
                evidence = _evidence(field, text, start, end)
                constraints.append(
                    QualificationConstraint(
                        field="other", operator="unsupported", evidence=evidence
                    )
                )
                unknowns.append(
                    _unknown(
                        "unparsed_condition", "eligibility", "编号资格条件尚未完整理解。", evidence
                    )
                )
            alternative = _SECTION_OR.search(section)
            if alternative:
                incomplete = True
                constraints = [
                    item.model_copy(update={"operator": "unsupported"})
                    if item.evidence.field == field
                    and section_start <= item.evidence.start < section_end
                    else item
                    for item in constraints
                ]
                proof = _evidence(
                    field, text, body_start + alternative.start(), body_start + alternative.end()
                )
                unknowns.append(
                    _unknown(
                        "unsupported_or",
                        "eligibility",
                        "资格章节有跨条目的 OR，不能按全部条件合取。",
                        proof,
                    )
                )
    # Multiple declarations on one field can be contradictory, not just an AND mismatch.
    for constraint_field in sorted(
        {item.field for item in constraints if item.operator != "unsupported"}
    ):
        related = [
            item
            for item in constraints
            if item.field == constraint_field and item.operator != "unsupported"
        ]
        if len(related) > 1 and not set.intersection(*(set(item.values) for item in related)):
            incomplete = True
            unknowns.append(
                _unknown(
                    "conflicting_evidence",
                    "eligibility",
                    "同一资格字段有互相冲突的声明。",
                    *(item.evidence for item in related),
                )
            )
            constraints = [
                item.model_copy(update={"operator": "unsupported"}) if item in related else item
                for item in constraints
            ]
    return tuple(constraints), audience_declared, audience_declared and not incomplete, unknowns


def _times(content, category):
    openings, deadlines, evidence, unknowns, lower_bounds = [], [], [], [], []
    for field, text in _source_fields(content):
        for clause, start, end in _sentences(text):
            team_deadline = bool(
                _TEAM_SUBMISSION.search(clause)
                and any(
                    re.match(r"\s*前", clause[match.end() :]) for match in _DATE.finditer(clause)
                )
            )
            time_context = (
                _DEADLINE.search(clause)
                or team_deadline
                or re.search(r"报名开始|开放报名|开始报名|即日起|自通知发布之日起", clause)
            )
            if not time_context:
                continue
            proof = _evidence(field, text, start, end)
            evidence.append(proof)
            if _ADMIN_TIME.search(clause) and not team_deadline:
                # These are review/submission steps for teachers and units in
                # the witnessed project template, not the applicant's deadline.
                continue
            if _LATER_ROUND_TIME.search(clause):
                unknowns.append(
                    _unknown(
                        "secondary_time_unknown",
                        "time",
                        "另行通知的第二轮报名不是已声明的首轮区间，仍保留未知。",
                        proof,
                    )
                )
                continue
            if _DEFERRED_DEADLINE.search(clause):
                unknowns.append(
                    _unknown(
                        "time_unrecognized",
                        "time",
                        "截止指向后续通知，不能把同句开始日期当截止。",
                        proof,
                    )
                )
                continue
            dates = list(_DATE.finditer(clause))
            timeless_dates = list(_TIMELESS_DATE.finditer(clause))
            inherited_range = (
                bool(_PERIOD.search(clause))
                and len(dates) == 1
                and len(timeless_dates) == 1
                and dates[0].end() <= timeless_dates[0].start()
                and re.fullmatch(
                    r"\s*(?:至|到)\s*", clause[dates[0].end() : timeless_dates[0].start()]
                )
                is not None
            )
            declared_zones = _BRACKETED_TIMEZONE.findall(clause)
            declared_zones.extend(
                zone[1]
                for date in dates + timeless_dates
                if (zone := _SUFFIX_TIMEZONE.match(clause[date.end() :])) is not None
            )
            if _UNSUPPORTED_TIMEZONE.search(clause) or any(
                zone not in _LOCAL_TIMEZONES for zone in declared_zones
            ):
                unknowns.append(
                    _unknown(
                        "unsupported_timezone", "time", "时间不属于支持的上海时间表达式。", proof
                    )
                )
                continue
            if timeless_dates and not inherited_range:
                unknowns.append(
                    _unknown("year_missing", "time", "月日缺少年份，不从发布日期推测。", proof)
                )
                continue
            relative_start = re.search(r"自通知发布之日起|即日起|现(?:开始|开放)报名", clause)
            if relative_start:
                openings.append(datetime.combine(content.published_date, time.min, SHANGHAI))
            if inherited_range:
                opens = _date_value(dates[0], deadline=False)
                deadline = _date_value(
                    timeless_dates[0], deadline=True, inherited_year=int(dates[0]["year"])
                )
                if opens is not None and deadline is not None and opens <= deadline:
                    openings.append(opens)
                    deadlines.append(deadline)
                else:
                    unknowns.append(
                        _unknown("invalid_time", "time", "报名区间日期或顺序无效。", proof)
                    )
            elif _PERIOD.search(clause) and len(dates) == 2:
                opens = _date_value(dates[0], deadline=False)
                deadline = _date_value(dates[1], deadline=True)
                if opens is not None and deadline is not None and opens <= deadline:
                    openings.append(opens)
                    deadlines.append(deadline)
                else:
                    unknowns.append(
                        _unknown("invalid_time", "time", "报名区间日期或顺序无效。", proof)
                    )
            elif len(dates) == 1:
                opening_only = re.search(
                    r"报名开始|开放报名|开始报名", clause
                ) and not _DEADLINE.search(clause)
                value = _date_value(dates[0], deadline=not opening_only)
                if value is None:
                    unknowns.append(_unknown("invalid_time", "time", "日期或时刻无效。", proof))
                elif opening_only:
                    openings.append(value)
                elif (_DEADLINE.search(clause) or relative_start or team_deadline) and (
                    not _PERIOD.search(clause) or re.search(r"至|前|截止", clause)
                ):
                    deadlines.append(value)
                    if dates[0]["hour"] is None and re.match(r"\s*前", clause[dates[0].end() :]):
                        lower_bounds.append(datetime.combine(value.date(), time.min, SHANGHAI))
                        unknowns.append(
                            _unknown(
                                "imprecise_deadline",
                                "deadline",
                                "“日前”未声明精确时刻；保留当日日初至日末边界，请提前核对。",
                                proof,
                            )
                        )
                else:
                    unknowns.append(
                        _unknown(
                            "ambiguous_time", "time", "日期用途不明确，不能猜测截止时间。", proof
                        )
                    )
            elif len(dates) > 2:
                unknowns.append(
                    _unknown(
                        "ambiguous_time", "time", "同句有多个日期，首版不猜测报名区间。", proof
                    )
                )
            elif not relative_start:
                unknowns.append(
                    _unknown("time_unrecognized", "time", "时间声明没有支持的完整年份日期。", proof)
                )
    opens_at = next(iter(set(openings))) if len(set(openings)) == 1 else None
    deadline_at = next(iter(set(deadlines))) if len(set(deadlines)) == 1 else None
    if len(set(openings)) > 1 or len(set(deadlines)) > 1:
        unknowns.append(
            _unknown(
                "conflicting_evidence",
                "time",
                "存在不同的开始或截止日期，不能挑选其一。",
                *evidence,
            )
        )
    if any(
        item.code
        in {
            "unsupported_timezone",
            "year_missing",
            "invalid_time",
            "ambiguous_time",
            "time_unrecognized",
        }
        for item in unknowns
    ):
        # A recognized date must not mask a competing unsupported time declaration.
        opens_at, deadline_at = None, None
    if opens_at is not None and deadline_at is not None and opens_at > deadline_at:
        unknowns.append(_unknown("conflicting_evidence", "time", "开始晚于截止。", *evidence))
        opens_at, deadline_at = None, None
    if category == "opportunity":
        if opens_at is None:
            unknowns.append(
                _unknown("opening_unknown", "time", "报名是否开始尚不能从可见正文核实。")
            )
        if deadline_at is None:
            unknowns.append(
                _unknown("deadline_unknown", "time", "没有唯一可核对的完整年份截止时间。")
            )
    lower = next(iter(set(lower_bounds))) if len(set(lower_bounds)) == 1 else None
    if deadline_at is None:
        lower = None
    return opens_at, deadline_at, lower, tuple(evidence), unknowns


def extract_facts(content: NoticeContent) -> NoticeFacts:
    """Extract deterministic visible evidence. Each invocation performs full extraction."""
    media = tuple(
        Evidence(field=field, index=index, url=reference.url)
        for field in ("images", "attachments")
        for index, reference in enumerate(getattr(content, field))
    )
    topics = _topics(content)
    opportunities = _opportunities(content)
    if _RESULTS.search(content.title) or (
        _ADMIN_ACTION.search(content.title)
        and not any(item.context == "opportunity" and item.primary for item in topics)
    ):
        category = "reference"
    elif opportunities:
        category = "opportunity"
    elif content.body_text.strip():
        category = "information"
    else:
        category = "unknown"
    if category != "opportunity":
        opportunities = ()
    constraints, audience, complete, unknowns = _qualifications(content)
    opens_at, deadline_at, deadline_lower_at, time_evidence, time_unknowns = _times(
        content, category
    )
    current_application = _CURRENT_APPLICATION.search(content.body_text)
    if current_application is not None and category == "opportunity":
        # This explicit present-tense permission confirms the witnessed team's
        # application is open; it does not supply an invented start timestamp.
        time_evidence += (
            _evidence(
                "body_text",
                content.body_text,
                current_application.start(),
                current_application.end(),
            ),
        )
        time_unknowns = [item for item in time_unknowns if item.code != "opening_unknown"]
    unknowns.extend(time_unknowns)
    incomplete = bool(media and not content.body_text.strip())
    critical = _CRITICAL_MEDIA.search(content.body_text)
    if critical is not None:
        incomplete = True
        unknowns.append(
            _unknown(
                "media_required",
                "media",
                "关键条件或时间指向未解析的媒体。",
                _evidence("body_text", content.body_text, critical.start(), critical.end()),
            )
        )
    if content.images and re.search(
        r"(?:选课|报名|时间|资格)[^。；\n]{0,35}安排如下|(?:时间|资格|条件)[^。；\n]{0,12}如下",
        content.body_text,
    ):
        incomplete = True
        unknowns.append(
            _unknown("media_required", "media", "安排指向图片，首版不读取图片内容。", *media)
        )
    if incomplete:
        complete = False
        if not any(item.code == "media_required" for item in unknowns):
            unknowns.append(
                _unknown("media_required", "media", "有意义的正文仅在未解析媒体中。", *media)
            )
    if category == "opportunity" and not audience:
        unknowns.append(
            _unknown(
                "audience_unknown", "eligibility", "没有明确支持的报名对象，不能视为面向全体。"
            )
        )
    return NoticeFacts(
        content_sha256=content.content_sha256(),
        title=content.title,
        published_date=content.published_date,
        body_text=content.body_text,
        topic_matches=topics,
        category=category,
        opportunity_evidence=opportunities,
        constraints=constraints,
        audience_declared=audience,
        eligibility_complete=complete,
        opens_at=opens_at,
        deadline_at=deadline_at,
        deadline_lower_at=deadline_lower_at,
        opening_confirmed=opens_at is not None or current_application is not None,
        time_evidence=time_evidence,
        information_incomplete=incomplete,
        media=media,
        unknowns=tuple(unknowns),
    )
