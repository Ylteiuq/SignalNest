"""Finite publication-date expressions resolved by the program, never by model arithmetic."""

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def _previous_month(day: date, *, same_day: bool) -> date:
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    number = min(day.day, calendar.monthrange(year, month)[1]) if same_day else 1
    return date(year, month, number)


def publication_window(question: str, at: datetime, timezone: str) -> dict:
    """Inclusive dates. Unknown/deadline-scoped expressions never become publication filters."""
    today = at.astimezone(ZoneInfo(timezone)).date()
    expressions = (
        (r"(?:最近|近|过去)(?:一个月|一月)", "last_month"),
        (r"(?:最近|近|过去)30(?:天|日)", "last_30_days"),
        (r"(?:最近|近|过去)(?:一周|7天|七天)", "last_7_days"),
        (r"本月|这个月", "this_month"),
        (r"上个月|上月", "previous_month"),
        (r"今年|本年", "this_year"),
        (r"今天|今日", "today"),
    )
    found = [(m, kind) for pattern, kind in expressions for m in re.finditer(pattern, question)]
    explicit = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:至|到|—|~)\s*(\d{4}-\d{2}-\d{2})", question)
    result = {"date_from": None, "date_to": None, "expression": None, "warning": None}
    if len(found) + bool(explicit) > 1:
        return result | {"warning": "ambiguous_date_expression"}
    if not found and not explicit:
        if re.search(r"\d{4}-\d{2}|\d{4}年|最近|近期|上周|下周|明天|去年|上季度|学期", question):
            return result | {"warning": "date_expression_not_resolved"}
        return result
    match = explicit if explicit else found[0][0]
    clause = re.split(r"[。？！?！；;，,]", question[: match.start()])[-1]
    after = re.split(r"[。？！?！；;，,]", question[match.end() :])[0][:12]
    if re.search(r"截止|到期|报名时间|报名期限", clause + after):
        return result | {"expression": match.group(), "warning": "deadline_window_not_supported"}
    if explicit:
        try:
            beginning, end = date.fromisoformat(explicit[1]), date.fromisoformat(explicit[2])
        except ValueError:
            return result | {"warning": "invalid_date_expression"}
        if beginning > end:
            return result | {"warning": "invalid_date_expression"}
    else:
        kind = found[0][1]
        end = today
        if kind == "last_month":
            beginning = _previous_month(today, same_day=True)
        elif kind in {"last_30_days", "last_7_days"}:
            beginning = today - timedelta(days=29 if kind == "last_30_days" else 6)
        elif kind == "previous_month":
            beginning = _previous_month(today, same_day=False)
            end = today.replace(day=1) - timedelta(days=1)
        elif kind == "this_month":
            beginning = today.replace(day=1)
        elif kind == "this_year":
            beginning = today.replace(month=1, day=1)
        else:
            beginning = today
    return result | {
        "date_from": beginning.isoformat(),
        "date_to": end.isoformat(),
        "expression": match.group(),
    }


def seed_keywords(question: str) -> tuple[str, ...]:
    """Small, disclosed fallback queries; this is not semantic relevance or an exhaustive search."""
    topics = (
        (("科研", "创新训练", "大创"), ("科研", "创新训练")),
        (("助教",), ("助教招聘",)),
        (("辅修",), ("辅修",)),
        (("竞赛", "大赛"), ("竞赛",)),
        (("选课",), ("选课",)),
        (("奖学金",), ("奖学金",)),
        (("保研", "推免"), ("推免",)),
    )
    for hints, terms in topics:
        if any(hint in question for hint in hints):
            return terms
    return (question[:200].strip(),)
