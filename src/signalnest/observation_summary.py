"""Summarize recorded observations; never inspect, repair or contact an instance."""

import json
import re
from collections import defaultdict
from pathlib import Path

from pydantic import ValidationError

from signalnest.notifications.contracts import canonical_sha256
from signalnest.status import COMPLETE_SCAN_STALE_SECONDS, StatusReport

MAIL_STATES = frozenset({"pending", "sending", "retry", "uncertain", "accepted", "blocked"})
SOURCE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


class ObservationError(ValueError):
    """Finite input errors, without raw records or arbitrary exception messages."""


def _time(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ObservationError("observation_time_invalid")
    return value


def read_observations(paths: list[Path]) -> tuple[list[dict], dict]:
    """Read JSONL observations or journalctl --output=json --all exports.

    Ordinary journal messages are counted and skipped. Truncated/null MESSAGEs
    are reported as unreadable, never reconstructed into observations.
    """
    records = []
    diagnostics = {"ignored_journal_messages": 0, "unreadable_journal_messages": 0}
    for path in paths:
        try:
            with path.open("rb") as stream:
                total = 0
                while line := stream.readline(2 * 1024 * 1024 + 1):
                    total += len(line)
                    if len(line) > 2 * 1024 * 1024 or total > 64 * 1024 * 1024:
                        raise ObservationError("observation_input_too_large")
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        raise ObservationError("observation_json_invalid") from None
                    if not isinstance(value, dict):
                        raise ObservationError("observation_record_invalid")
                    if "MESSAGE" in value:
                        message = value["MESSAGE"]
                        if not isinstance(message, str):
                            diagnostics["unreadable_journal_messages"] += 1
                            continue
                        try:
                            value = json.loads(message)
                        except ValueError:
                            if "signalnest-observation-v1" in message:
                                raise ObservationError("observation_record_truncated") from None
                            diagnostics["ignored_journal_messages"] += 1
                            continue
                        if not isinstance(value, dict) or value.get("format") != (
                            "signalnest-observation-v1"
                        ):
                            diagnostics["ignored_journal_messages"] += 1
                            continue
                    if value.get("format") != "signalnest-observation-v1":
                        raise ObservationError("observation_format_unsupported")
                    records.append(value)
        except OSError:
            raise ObservationError("observation_file_unavailable") from None
    return records, diagnostics


def _point(record: dict) -> dict:
    if not isinstance(record, dict) or record.get("format") != "signalnest-observation-v1":
        raise ObservationError("observation_format_unsupported")
    source = record.get("source_id")
    if not isinstance(source, str) or not SOURCE_ID.fullmatch(source):
        raise ObservationError("observation_source_invalid")
    at = _time(record.get("at"))
    collection = record.get("collection")
    point = {"source_id": source, "at": at, "collection": None, "mail": None}
    if not isinstance(collection, dict):
        raise ObservationError("observation_record_invalid")
    if "error_code" in collection:
        point["collection_error"] = "collection_unavailable"
    else:
        try:
            status = StatusReport.model_validate(collection)
        except ValidationError:
            raise ObservationError("observation_collection_invalid") from None
        if status.source_id != source or status.observed_at != at:
            raise ObservationError("observation_binding_mismatch")
        if any(
            type(n) is not int or n < 0
            for n in (
                status.documents.total,
                status.documents.processed,
                status.documents.failed,
                status.first_processing.total,
                status.first_processing.due,
                status.first_processing.deferred,
                status.rechecks.due,
            )
        ):
            raise ObservationError("observation_collection_invalid")
        complete = status.source.last_complete_scan_at
        if complete is not None and complete > at:
            raise ObservationError("observation_binding_mismatch")
        point["collection"] = {
            "documents": status.documents.total,
            "processed": status.documents.processed,
            "failed": status.documents.failed,
            "first_backlog": status.first_processing.total,
            "first_due": status.first_processing.due,
            "first_deferred": status.first_processing.deferred,
            "recheck_due": status.rechecks.due,
            "last_complete_at": complete,
            "complete_run_id": status.source.last_complete_scan_run_id,
            "complete_age_seconds": at - complete if complete is not None else None,
            "coverage": (
                "missing"
                if complete is None
                else "stale"
                if at - complete > COMPLETE_SCAN_STALE_SECONDS
                else "fresh"
            ),
            "cooldown_active": status.source.cooldown_active,
            "unfinished_runs": status.unfinished_runs,
        }
    mail = record.get("mail")
    if not isinstance(mail, dict):
        raise ObservationError("observation_mail_invalid")
    if mail.get("activated") and "error_code" not in mail:
        counts = mail.get("counts")
        if (
            not isinstance(counts, dict)
            or set(counts) != MAIL_STATES
            or any(type(n) is not int or n < 0 for n in counts.values())
            or type(mail.get("paused")) is not bool
        ):
            raise ObservationError("observation_mail_invalid")
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in (
                mail.get("unallocated_digest"),
                mail.get("unplanned_immediate"),
                mail.get("plan_error_records"),
            )
        ):
            raise ObservationError("observation_mail_invalid")
        point["mail"] = {
            "paused": mail["paused"],
            "counts": dict(sorted(counts.items())),
            "unallocated_digest": mail.get("unallocated_digest"),
            "unplanned_immediate": mail.get("unplanned_immediate"),
            "plan_error_records": mail.get("plan_error_records"),
        }
    release_record = record.get("release", {})
    if not isinstance(release_record, dict):
        raise ObservationError("observation_release_invalid")
    release = release_record.get("content_sha256")
    if release is not None and (
        not isinstance(release, str) or not re.fullmatch(r"[0-9a-f]{64}", release)
    ):
        raise ObservationError("observation_release_invalid")
    point["release_content_sha256"] = release
    return point


def summarize_observations(
    records: list[dict],
    *,
    start: int,
    end: int,
    expected_sources: tuple[str, ...],
    interval_seconds: int = 3600,
    tolerance_seconds: int = 900,
) -> dict:
    """Report sampled facts and missing intervals, without inferring uptime.

    Counts are net snapshots, not counts of successful attempts or new documents.
    A seven-day span never becomes an automatic rollout acceptance.
    """
    _time(start)
    _time(end)
    if end < start or end - start > 366 * 86400:
        raise ObservationError("observation_window_invalid")
    if (
        type(interval_seconds) is not int
        or interval_seconds < 1
        or type(tolerance_seconds) is not int
        or not 0 <= tolerance_seconds < interval_seconds
        or not expected_sources
        or len(set(expected_sources)) != len(expected_sources)
        or any(not isinstance(s, str) or not SOURCE_ID.fullmatch(s) for s in expected_sources)
    ):
        raise ObservationError("observation_schedule_invalid")
    groups = defaultdict(dict)
    projected_evidence = []
    duplicate_count = outside_count = 0
    for record in records:
        point = _point(record)
        projected_evidence.append(point)
        if point["source_id"] not in expected_sources:
            raise ObservationError("observation_source_unexpected")
        if not start <= point["at"] <= end:
            outside_count += 1
            continue
        previous = groups[point["source_id"]].get(point["at"])
        if previous is not None:
            if previous != point:
                raise ObservationError("observation_duplicate_conflict")
            duplicate_count += 1
        groups[point["source_id"]][point["at"]] = point
    sources = {}
    for source in expected_sources:
        points = sorted(groups[source].values(), key=lambda p: p["at"])
        times = [start, *(p["at"] for p in points), end]
        gaps = [
            {"from_at": a, "to_at": b, "seconds": b - a}
            for a, b in zip(times, times[1:], strict=False)
            if b - a > interval_seconds + tolerance_seconds
        ]
        known = [p for p in points if p["collection"] is not None]
        delta = None
        if len(known) >= 2:
            first, last = known[0]["collection"], known[-1]["collection"]
            delta = {
                k: last[k] - first[k] for k in ("documents", "processed", "failed", "first_backlog")
            }
        sources[source] = {
            "sample_count": len(points),
            "points": points,
            "net_changes": delta,
            "gaps": gaps,
            "collection_unavailable_at": [p["at"] for p in points if p["collection"] is None],
            "coverage_stale_at": [p["at"] for p in known if p["collection"]["coverage"] == "stale"],
            "coverage_missing_at": [
                p["at"] for p in known if p["collection"]["coverage"] == "missing"
            ],
            "mail_paused_at": [p["at"] for p in points if p["mail"] and p["mail"]["paused"]],
            "mail_unavailable_at": [p["at"] for p in points if p["mail"] is None],
        }
    return {
        "format": "signalnest-observation-summary-v1",
        "window_start": start,
        "window_end": end,
        "window_elapsed_seconds": end - start,
        "seven_days_elapsed": end - start >= 7 * 86400,
        "rollout_acceptance": "requires_human_evidence_review",
        "sources": sources,
        "duplicate_samples": duplicate_count,
        "outside_window_samples": outside_count,
        "selected_evidence_sha256": canonical_sha256(projected_evidence),
        "limitations": [
            "Snapshots do not prove continuous uptime or mailbox receipt.",
            "Net backlog changes are not throughput or failure rates.",
            "Unobserved intervals and unavailable snapshots remain unknown.",
        ],
    }
