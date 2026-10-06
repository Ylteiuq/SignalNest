"""Offline finite rule probe, NOT the production N0 implementation or accuracy test.

Run: .venv/bin/python research/experiments/notifications/evaluate_probe.py
Reads frozen HTML with the real Parser; no network, accounts or dependency changes.
"""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from signalnest.contracts import PageInput
from signalnest.parsing import ParseError, parse_notice

DEST = Path(__file__).resolve().parent
RULES = json.loads((DEST / "rules.json").read_text())
SHANGHAI = ZoneInfo("Asia/Shanghai")
DATE = re.compile(r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})(?:[:：时])(\d{1,2})?(?:分)?)?")


def deadline(text):
    lines = text.splitlines()
    target = next((line for line in lines if "选课时间" in line), None)
    if target is None:
        target = next((line for line in lines if "报名时间" in line), None)
    if target is None:
        target = next((line for line in lines if "项目团队完成" in line and "年前" not in line), None)
    if target is None:
        return None, ["deadline_unrecognized"]
    matches = list(DATE.finditer(target))
    if not matches or not matches[0][1]:
        return None, ["deadline_year_unknown"]
    year = int(matches[0][1])
    end = matches[-1]
    year = int(end[1]) if end[1] else year
    hour, minute = int(end[4] or 0), int(end[5] or 0)
    if hour > 24 or minute > 59 or (hour == 24 and minute):
        return None, ["deadline_invalid"]
    result = datetime(year, int(end[2]), int(end[3]), hour % 24, minute, tzinfo=SHANGHAI)
    if hour == 24:
        result += timedelta(days=1)
    # Date-only/'日前' wording cannot establish an exact hour. Keep a lower
    # bound here and an uncertainty interval below, without inventing precision.
    missing = [] if end[4] else ["deadline_day_boundary"]
    return result, missing


def eligibility(title, text, profile):
    if "面向全校教师征集" in text:
        role = profile.get("role")
        return ("unknown" if role is None else "eligible" if role == "teacher" else "ineligible"), (["role"] if role is None else []), "teacher_applicant"
    year = re.search(r"(20\d{2})级新生", title)
    if year:
        actual = profile.get("entry_year")
        return ("unknown" if actual is None else "eligible" if actual == int(year[1]) else "ineligible"), (["entry_year"] if actual is None else []), "freshman_year"
    if "选课对象与要求：全日制在校本科生" in text:
        actual = profile.get("study_level")
        if actual is not None and actual != "undergraduate":
            return "ineligible", [], "undergraduate_scope"
        # This Profile has no enrollment-mode/status facts. Matching one part
        # of the explicit AND condition does not establish full eligibility.
        missing = ["full_time_enrollment", "current_enrollment", "semester_course_limit_check"]
        if actual is None:
            missing.append("study_level")
        return "unknown", missing, "undergraduate_scope"
    if "辅修专业报名" in title:
        if profile.get("study_level") == "graduate":
            return "ineligible", [], "undergraduate_scope"
        return "unknown", ["cohort_exceptions", "gpa", "major_category", "attachment_conditions"], "minor_conditions"
    if "持续自主开展科研训练团队" in text:
        return "unknown", ["team_research_state", "project_conditions"], "team_conditions"
    return "unknown", ["eligibility_scope"], "scope_unknown"


def route(action, mode, activation=False, deadline_at=None, next_digest_at=None):
    if action in ("STORE_ONLY", "IGNORE"):
        return "none"
    if action == "DIGEST" or mode == "digest_only":
        return "digest"
    if activation and not (deadline_at and next_digest_at and deadline_at <= next_digest_at):
        return "digest"
    return "immediate"


def main():
    rows = []
    for case in json.loads((DEST / "cases.json").read_text())["cases"]:
        body = (ROOT / case["fixture"]).read_bytes()
        if sha256(body).hexdigest() != case["sha256"]:
            raise ValueError("fixture_digest_mismatch")
        common = dict(case_id=case["case_id"], source_url=case["source_url"], annotation_status=case["annotation_status"])
        try:
            notice = parse_notice(PageInput(content=body, page_url=case["source_url"]))
        except ParseError as exc:
            raw = body.decode("utf-8", errors="replace")
            rows.append({**common, "parse_status": "failed", "parse_error_type": type(exc).__name__,
                         "parse_error_code": exc.code.value, "parse_error_field": exc.field,
                         "action": None, "raw_has_cancel_qualification": "取消参赛资格" in raw,
                         "opportunity_cancellation_confirmed": False})
            continue
        title, text, profile = notice.content.title, notice.content.body_text, case["profile"]
        now = datetime.fromisoformat(case["evaluated_at"])
        def interest_hit(phrases):
            if any(phrase in title for phrase in phrases):
                return True
            if not RULES.get("body_interest_requires_action_sentence"):
                return any(phrase in text for phrase in phrases)
            return any(any(phrase in line for phrase in phrases) and
                       re.search(r"报名|申报|招募|招聘|补报|申请", line)
                       for line in text.splitlines() if "→" not in line and "登录" not in line)
        topics = [topic for topic, phrases in RULES["topics"].items() if interest_hit(phrases)]
        if re.search(RULES["assistant_recruitment_pattern"], title + "\n" + text):
            topics.append("teaching_assistant")
        reference = any(phrase in title for phrase in RULES["reference_title_phrases"])
        is_archive = bool(set(topics) & set(profile["store_only_topics"])) and not bool(set(topics) & set(profile["interest_topics"]))
        interested = bool(set(topics) & set(profile["interest_topics"]))
        end, time_missing = deadline(text)
        # A date-only boundary is not evidence of an exact hour. Use an upper
        # bound for expiry, keeping uncertainty visible rather than dropping early.
        latest_end = end + timedelta(days=1) if end and "deadline_day_boundary" in time_missing else end
        eligible, missing, qual_rule = eligibility(title, text, profile)
        if reference:
            missing, time_missing = [], []
        if notice.content.images and not text.strip():
            missing.append("image_content")
        if "具体安排如下" in text and notice.content.images:
            missing.append("time_table_image")
        if "选课说明见附件" in text:
            missing.append("course_requirements_attachments")
        missing = sorted(set(missing + time_missing))
        soon = latest_end is not None and 0 <= (latest_end - now).total_seconds() <= 72 * 3600
        recent = 0 <= (now.date() - notice.content.published_date).days < 7
        high = bool(set(topics) & set(profile["high_value_topics"]))
        if reference or is_archive or eligible == "ineligible" or (latest_end and latest_end <= now):
            action = "STORE_ONLY" if interested or is_archive else "IGNORE"
        elif not interested:
            action = "STORE_ONLY" if notice.content.images or notice.content.attachments else "IGNORE"
        elif high and (soon or recent):
            action = "PUSH_NOW"
        else:
            action = "DIGEST"
        rows.append({**common, "parse_status": "ok", "title": title, "topics": topics, "interested": interested,
                     "naive_research_hit": "科研" in text, "naive_assistant_hit": "助教" in text,
                     "stage": "reference" if reference else "candidate", "eligibility": eligible,
                     "qualification_rule": qual_rule, "deadline_at": end.isoformat() if end else None,
                     "deadline_latest_at": latest_end.isoformat() if latest_end else None,
                     "needs_review": bool(missing), "missing_fields": missing, "action": action,
                     "effective_route": route(action, case["mode"])})
    ok = [row for row in rows if row["parse_status"] == "ok"]
    opportunity_cases = [row for row in ok if row["interested"] and row["stage"] != "reference"]
    summary = {"cases": len(rows), "parsed_cases": len(ok), "parser_failures": len(rows)-len(ok),
               "needs_review_cases": sum(row["needs_review"] for row in ok),
               "eligibility_unknown_cases": sum(row["eligibility"] == "unknown" for row in ok),
               "interested_opportunity_cases": len(opportunity_cases),
               "interested_opportunity_unknown_eligibility": sum(row["eligibility"] == "unknown" for row in opportunity_cases),
               "human_confirmed_labels": 0, "accuracy": None,
               "note": "Curated repeated pages/historical clocks; not representative coverage or gold accuracy."}
    routing_checks = {
        "urgent_unknown_stays_immediate": route("PUSH_NOW", "hybrid") == "immediate",
        "digest_only_before_intent": route("PUSH_NOW", "digest_only") == "digest",
        "activation_urgent_escape": route("PUSH_NOW", "hybrid", True, datetime(2024,12,9,23,59,tzinfo=SHANGHAI), datetime(2024,12,10,8,tzinfo=SHANGHAI)) == "immediate",
        "activation_default_cap": route("PUSH_NOW", "hybrid", True) == "digest",
    }
    if not all(routing_checks.values()):
        raise ValueError("routing_probe_failure")
    result = dict(rule_revision=RULES["revision"], summary=summary, routing_checks=routing_checks, results=rows)
    (DEST / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))
    for row in rows:
        print(row["case_id"], row.get("action"), row.get("needs_review"), row.get("eligibility"), row.get("deadline_at"))


if __name__ == "__main__":
    main()
