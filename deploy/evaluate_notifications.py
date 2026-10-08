"""Replay fixed public fixtures through the installed production notification rules.

This offline diagnostic does not open a database, read the personal Profile, or
send mail. The engineering expectations are regression checks, not human gold.
"""

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "docs/validation/notification-production-cases.json"
SOURCE_FILES = (
    "src/signalnest/contracts.py",
    "src/signalnest/parsing.py",
    "src/signalnest/notifications/contracts.py",
    "src/signalnest/notifications/facts.py",
    "src/signalnest/notifications/decision.py",
    "src/signalnest/notifications/profile.py",
)


class EvaluationError(ValueError):
    """A finite input or reproducibility error without raw page/exception text."""


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _fixture(path):
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise EvaluationError("evaluation_fixture_path_invalid")
    resolved = (ROOT / candidate).resolve()
    if not resolved.is_relative_to(ROOT / "research/fixtures"):
        raise EvaluationError("evaluation_fixture_path_invalid")
    return resolved


def _expectation_mismatches(decision, expectation):
    mismatches = []
    for field in ("action", "effective_route"):
        if field in expectation and decision[field] != expectation[field]:
            mismatches.append(field)
    rules = set(decision["matched_rules"])
    if any(rule not in rules for rule in expectation.get("matched_rules_present", ())):
        mismatches.append("matched_rules_present")
    if any(rule in rules for rule in expectation.get("matched_rules_absent", ())):
        mismatches.append("matched_rules_absent")
    return mismatches


def evaluate_cases(case_file=DEFAULT_CASES):
    """Return auditable production output for fixed Profiles and explicit clocks."""
    before = source_hashes()
    # Capture before import and after replay so a concurrent edit cannot silently
    # produce a report bearing source hashes from another revision.
    from pydantic import ValidationError

    from signalnest.contracts import PageInput
    from signalnest.notifications.contracts import (
        DECISION_ENGINE_VERSION,
        FACTS_EXTRACTOR_VERSION,
        ROUTING_VERSION,
        RULES_VERSION,
        EventContext,
        Profile,
        aware_time,
    )
    from signalnest.notifications.decision import decide
    from signalnest.notifications.facts import extract_facts
    from signalnest.parsing import PARSER_VERSION, ParseError, parse_notice

    if source_hashes() != before:
        raise EvaluationError("evaluation_source_changed")
    manifest_bytes = Path(case_file).read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
        if manifest["schema_version"] != 1 or not manifest["cases"]:
            raise EvaluationError("evaluation_manifest_invalid")
        profiles = {
            name: Profile.model_validate(values) for name, values in manifest["profiles"].items()
        }
    except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise EvaluationError("evaluation_manifest_invalid") from exc
    rows = []
    identifiers = set()
    for case in manifest["cases"]:
        try:
            identifier = case["case_id"]
            if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                raise EvaluationError("evaluation_case_invalid")
            identifiers.add(identifier)
            body = _fixture(case["fixture"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != case["sha256"]:
                raise EvaluationError("evaluation_fixture_digest_mismatch")
            profile = profiles[case["profile"]]
            try:
                now = aware_time(datetime.fromisoformat(case["evaluated_at"]))
            except ValueError as exc:
                raise EvaluationError("evaluation_clock_invalid") from exc
            context = EventContext.model_validate(case["context"])
            if context.next_digest_at <= now:
                raise EvaluationError("evaluation_clock_invalid")
            page = PageInput(content=body, page_url=case["source_url"])
            expectation = case["engineering_expectation"]
            description = case["description"]
        except (KeyError, TypeError, ValidationError) as exc:
            raise EvaluationError("evaluation_case_invalid") from exc
        row = {
            "case_id": identifier,
            "research_case": case.get("research_case"),
            "description": description,
            "fixture": case["fixture"],
            "fixture_sha256": case["sha256"],
            "source_url": case["source_url"],
            "profile": profile.model_dump(mode="json"),
            "context": context.model_dump(mode="json"),
            "evaluated_at": case["evaluated_at"],
            "engineering_expectation": expectation,
        }
        try:
            notice = parse_notice(page)
        except ParseError as exc:
            row.update(
                parse_error={"code": exc.code.value, "field": exc.field},
                engineering_mismatches=["parse_success"],
            )
        else:
            facts = extract_facts(notice.content)
            decision = decide(profile, facts, context, now=now).model_dump(mode="json")
            row.update(
                source_document_id=notice.source_document_id,
                parser_version=notice.parser_version,
                # Offsets/excerpts remain available without copying the full body.
                facts=facts.model_dump(mode="json", exclude={"body_text"}),
                decision=decision,
                engineering_mismatches=_expectation_mismatches(decision, expectation),
            )
        rows.append(row)
    if source_hashes() != before:
        raise EvaluationError("evaluation_source_changed")
    decisions = [row["decision"] for row in rows if "decision" in row]
    return {
        "schema_version": 1,
        "kind": "offline_production_notification_replay",
        "note": manifest["note"],
        "adaptations": manifest["adaptations"],
        "recorded_at": datetime.now(UTC).isoformat(),
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": before,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "versions": {
            "parser": PARSER_VERSION,
            "facts_extractor": FACTS_EXTRACTOR_VERSION,
            "rules": RULES_VERSION,
            "decision_engine": DECISION_ENGINE_VERSION,
            "routing": ROUTING_VERSION,
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "dependencies": {
                name: version(name) for name in ("httpx", "beautifulsoup4", "pydantic")
            },
        },
        "summary": {
            "cases": len(rows),
            "unique_pages": len({row["fixture_sha256"] for row in rows}),
            "parse_success": len(decisions),
            "actions": dict(sorted(Counter(d["action"] for d in decisions).items())),
            "routes": dict(sorted(Counter(d["effective_route"] for d in decisions).items())),
            "needs_review": sum(d["needs_review"] for d in decisions),
            "engineering_mismatch_cases": [
                row["case_id"] for row in rows if row["engineering_mismatches"]
            ],
            "human_gold": False,
        },
        "results": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, help="create a new JSON snapshot; never overwrite")
    parser.add_argument("--check", action="store_true", help="exit 1 for regression mismatches")
    args = parser.parse_args(argv)
    try:
        result = evaluate_cases(args.cases)
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            print(payload, end="")
        else:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(payload)
    except EvaluationError as exc:
        print(f"notification evaluation failed: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("notification evaluation failed: evaluation_file_unavailable", file=sys.stderr)
        return 2
    return int(args.check and bool(result["summary"]["engineering_mismatch_cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
