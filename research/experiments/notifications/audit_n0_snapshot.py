"""Read-only audit of the concurrent N0 snapshot against a few frozen pages.

This is a research comparison, not human gold or a production acceptance test.
The explicit Profile below uses current N0's schema, unlike the research probe.
Run from the repository root with .venv/bin/python and this file path.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
DEST = Path(__file__).resolve().parent
SOURCES = (
    "src/signalnest/contracts.py", "src/signalnest/parsing.py",
    "src/signalnest/notifications/contracts.py",
    "src/signalnest/notifications/decision.py",
    "src/signalnest/notifications/facts.py",
)


def source_hashes():
    return {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


IMPORT_SOURCE_HASHES = source_hashes()
from signalnest.contracts import PageInput
from signalnest.parsing import parse_notice
from signalnest.notifications.contracts import EventContext, Profile
from signalnest.notifications.decision import decide
from signalnest.notifications.facts import extract_facts


def main():
    before = source_hashes()
    if before != IMPORT_SOURCE_HASHES:
        raise ValueError("source_changed_during_import")
    frozen = {case["case_id"]: case for case in json.loads((DEST / "cases.json").read_text())["cases"]}
    scenarios = (
        ("A01", "S04", "2026-09-10T09:00:00+08:00", True),
        ("A02", "S04", "2026-09-10T09:00:00+08:00", False),
        ("A03", "S04", "2026-09-28T09:00:00+08:00", False),
        ("A04", "S08", "2024-12-09T09:00:00+08:00", False),
        ("A05", "S06", "2024-02-24T09:00:00+08:00", False),
        ("A06", "S02", "2026-09-28T09:00:00+08:00", True),
        ("A07", "S07", "2022-05-14T09:00:00+08:00", False),
    )
    rows = []
    for audit_id, case_id, evaluated, save_courses in scenarios:
        case = frozen[case_id]
        body = (ROOT / case["fixture"]).read_bytes()
        if sha256(body).hexdigest() != case["sha256"]:
            raise ValueError("fixture_digest_mismatch")
        profile = Profile(
            institution="whu", study_level="undergraduate",
            interest_topics=("research", "minor"),
            high_value_topics=("research", "minor"),
            store_only_topics=("course_enrollment",) if save_courses else (),
        )
        now = datetime.fromisoformat(evaluated)
        slot = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0)
        context = EventContext(kind="new", notification_mode="hybrid", next_digest_at=slot)
        notice = parse_notice(PageInput(content=body, page_url=case["source_url"]))
        facts = extract_facts(notice.content)
        decision = decide(profile, facts, context, now=now)
        rows.append({
            "audit_id": audit_id, "fixture_case": case_id,
            "fixture_sha256": case["sha256"], "profile": profile.model_dump(mode="json"),
            "context": context.model_dump(mode="json"), "evaluated_at": evaluated,
            "topics": [match.topic for match in facts.topic_matches],
            "facts_opens_at": facts.opens_at.isoformat() if facts.opens_at else None,
            "facts_deadline_at": facts.deadline_at.isoformat() if facts.deadline_at else None,
            "decision": decision.model_dump(mode="json"),
        })
    if source_hashes() != before:
        raise ValueError("source_changed_during_research_audit")
    result = {
        "note": "Explicit current-schema fictional Profile; not S01-S11 gold evaluation or accuracy.",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": before,
        "python": sys.version.split()[0],
        "dependencies": {name: version(name) for name in ("httpx", "beautifulsoup4", "pydantic")},
        "results": rows,
    }
    (DEST / "n0-snapshot-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for row in rows:
        decision = row["decision"]
        print(row["audit_id"], decision["action"], decision["effective_route"],
              decision["eligibility"], decision["time_status"], decision["reason_codes"])


if __name__ == "__main__":
    main()
