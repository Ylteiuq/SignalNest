"""Finite processing choices, independent of HTTP, Parser and database transactions."""

from collections import deque
from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from signalnest.config import RuntimeSettings
from signalnest.contracts import Contract
from signalnest.errors import validate_time
from signalnest.schema import documents, notice_versions

Group = Literal["foreground", "history", "recheck"]
GROUPS: tuple[Group, ...] = ("foreground", "history", "recheck")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class GroupSummary(Contract):
    model_config = {"extra": "forbid", "frozen": False}

    allocated: int = 0
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    unserved: int = 0
    remaining_due: int = 0
    oldest_overdue_seconds: int = 0


def group_for(document, foreground_ids: set[str] | frozenset[str]) -> Group:
    if document["last_success_at"] is not None:
        return "recheck"
    return "foreground" if document["source_document_id"] in foreground_ids else "history"


def due_order(document):
    return (
        document["next_due_at"]
        if document["next_due_at"] is not None
        else document["discovered_at"],
        document["id"],
    )


def select_details(candidates, foreground_ids, max_details: int, policy: RuntimeSettings):
    """Reserve then borrow slots; interleave groups. Caller supplies due-gated rows.

    Smaller batches truncate reserved slots in F/H/R rotation. With sufficient batch
    and network budgets, occupied groups get their reservation; no such promise is
    made when a run stops early. Each database identity is selected at most once.
    """
    queues = {group: deque() for group in GROUPS}
    seen = set()
    for document in sorted(candidates, key=due_order):
        if document["id"] not in seen:
            queues[group_for(document, foreground_ids)].append(document)
            seen.add(document["id"])
    quotas = dict(
        foreground=policy.foreground_slots,
        history=policy.history_slots,
        recheck=policy.recheck_slots,
    )
    reserved = {group: min(len(queues[group]), quotas[group]) for group in GROUPS}
    allocated = dict.fromkeys(GROUPS, 0)
    remaining = max_details
    while remaining and any(allocated[g] < reserved[g] for g in GROUPS):
        for group in GROUPS:
            if remaining and allocated[group] < reserved[group]:
                allocated[group] += 1
                remaining -= 1
    for group in GROUPS:
        extra = min(remaining, len(queues[group]) - allocated[group])
        allocated[group] += extra
        remaining -= extra
    batch = []
    while any(allocated.values()):
        for group in GROUPS:
            if allocated[group]:
                batch.append((group, queues[group].popleft()))
                allocated[group] -= 1
    return tuple(batch)


def success_due(published_date: date, at: int, policy: RuntimeSettings) -> tuple[int, bool]:
    validate_time(at)
    age = (datetime.fromtimestamp(at, UTC).astimezone(SHANGHAI).date() - published_date).days
    interval = (
        policy.recent_recheck_seconds
        if age <= policy.recent_days
        else policy.middle_recheck_seconds
        if age <= policy.middle_days
        else policy.old_recheck_seconds
    )
    due = at + interval
    validate_time(due)
    return due, age < 0


def failure_due(code: str, status: int | None, at: int, policy: RuntimeSettings) -> int:
    """Finite categories; global server cooldown is separately applied by the coordinator."""
    if status in {404, 410} or code == "http_not_found":
        interval = policy.missing_failure_seconds
    elif status in {401, 403} or code in {"tls_error", "http_unauthorized", "http_forbidden"}:
        interval = policy.access_failure_seconds
    elif code.startswith("parse_") or code in {
        "identity_mismatch",
        "invalid_target",
        "unsupported_content_type",
        "unsupported_content_encoding",
        "empty_body",
        "body_too_large",
        "invalid_response_headers",
        "redirect_invalid",
        "redirect_loop",
        "redirect_limit",
    }:
        interval = policy.parse_failure_seconds
    else:
        interval = policy.transient_failure_seconds
    due = at + interval
    validate_time(due)
    return due


def apply_recheck_policy_in_transaction(connection, source_id: str, at: int, policy):
    """Explicit upgrade: preserve failed delays and never postpone existing due dates.

    Success age is measured at its last successful processing time, not upgrade time.
    NULL successful due is enrolled for immediate first-online recheck.
    """
    validate_time(at)
    rows = connection.execute(
        sa.select(documents, notice_versions.c.published_date)
        .join(notice_versions, documents.c.current_version_id == notice_versions.c.id)
        .where(documents.c.source_id == source_id, documents.c.status == "processed")
    ).mappings()
    changed = 0
    for row in rows:
        proposed, _ = success_due(row["published_date"], row["last_success_at"], policy)
        old = row["next_due_at"]
        due = at if old is None else min(old, proposed)
        if old != due:
            connection.execute(
                documents.update().where(documents.c.id == row["id"]).values(next_due_at=due)
            )
            changed += 1
    return changed


def apply_recheck_policy(engine: Engine, source_id: str, at: int, policy: RuntimeSettings) -> int:
    """Caller holds the instance writer lock. This function owns one short transaction."""
    with engine.begin() as connection:
        return apply_recheck_policy_in_transaction(connection, source_id, at, policy)
