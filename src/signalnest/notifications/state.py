"""Explicit single-channel activation and genuine site-date provenance.

Caller holds the instance writer lock for mutations. No network, file reads, or
notification policy evaluation occurs in these short database transactions.
"""

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from pydantic import Field, field_validator
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.contracts import Contract, ListPage
from signalnest.errors import IngestError, validate_time
from signalnest.ingestion_state import validate_processing_run
from signalnest.notifications.contracts import Profile, canonical_sha256
from signalnest.notifications.decision import policy_manifest
from signalnest.schema import (
    documents,
    email_outbox,
    ingestion_runs,
    notice_versions,
    notification_events,
    notification_observations,
    raw_responses,
    source_ingestion_state,
)
from signalnest.schema import (
    notification_activation_members as members,
)
from signalnest.schema import (
    notification_channel_state as channel,
)
from signalnest.schema import (
    notification_listing_evidence as listings,
)
from signalnest.schema import (
    notification_policy_revisions as policies,
)

ProcessingOrigin = Literal["live", "offline", "maintenance"]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def notification_time(at: int) -> None:
    validate_time(at)
    try:
        # The decision calendar and next daily digest must both be representable.
        datetime.fromtimestamp(at, UTC).astimezone(SHANGHAI) + timedelta(days=1)
    except (ValueError, OverflowError, OSError) as exc:
        raise IngestError("notification_time_invalid", "notification") from exc


class ActivationOptions(Contract):
    activation_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    notification_mode: Literal["hybrid", "digest_only"] = "hybrid"
    initial_recent_review: bool = Field(default=True, strict=True)
    digest_hour: int = Field(default=9, ge=0, le=23, strict=True)
    digest_minute: int = Field(default=0, ge=0, le=59, strict=True)
    sender: str
    recipient: str

    @field_validator("sender", "recipient")
    @classmethod
    def address(cls, value):
        # One ASCII addr-spec, not a header/display name or SMTP credential.
        if (
            not re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value
            )
            or len(value) > 254
            or ".." in value
        ):
            raise ValueError("must be a single ASCII email addr-spec without whitespace or headers")
        return value


def check_origin(origin: ProcessingOrigin, run_id: str | None) -> None:
    if origin not in {"live", "offline", "maintenance"}:
        raise IngestError("notification_invalid_origin", "notification")
    if origin == "live" and run_id is None:
        raise IngestError("notification_live_run_required", "notification")


def channel_on(connection: Connection):
    row = connection.execute(sa.select(channel)).mappings().one_or_none()
    return dict(row) if row is not None else None


def next_digest_at(state: dict, at: int) -> datetime:
    notification_time(at)
    now = datetime.fromtimestamp(at, UTC).astimezone(SHANGHAI)
    result = now.replace(
        hour=state["digest_hour"], minute=state["digest_minute"], second=0, microsecond=0
    )
    if result <= now:
        result += timedelta(days=1)
    return result


def candidate_values(state: dict, old: dict | None, list_evidence, notice_evidence) -> dict:
    today = datetime.fromtimestamp(state["activation_at"], UTC).astimezone(SHANGHAI).date()
    dates = [
        date.fromisoformat(e["published_date"])
        for e in (list_evidence, notice_evidence)
        if e is not None
    ]
    if old is not None and old["candidate_state"] in {"generated", "selected"}:
        candidate = old["candidate_state"]
    elif not state["initial_recent_review"]:
        candidate = "disabled"
    else:
        if any(today - timedelta(days=6) <= d <= today for d in dates):
            candidate = "selected"
        elif not dates or any(d > today for d in dates):
            candidate = "unknown"
        else:
            candidate = "not_recent"
    selection = old.get("selection_evidence") if old is not None else None
    if selection is None and candidate == "selected":
        selection = next(
            e
            for e in (list_evidence, notice_evidence)
            if e is not None
            and today - timedelta(days=6) <= date.fromisoformat(e["published_date"]) <= today
        )
    evidence = [e for e in (selection, list_evidence, notice_evidence) if e is not None]
    pair = old.get("conflict_evidence") if old is not None else None
    if pair is None:
        pair = next(
            (
                [a, b]
                for a in evidence
                for b in evidence
                if a["published_date"] != b["published_date"]
            ),
            None,
        )
    return dict(
        candidate_state=candidate,
        list_evidence=list_evidence,
        notice_evidence=notice_evidence,
        selection_evidence=selection,
        conflict_evidence=pair,
        date_conflict=pair is not None,
    )


def listing_evidence(row) -> dict | None:
    if row is None:
        return None
    return dict(
        kind="list_entry",
        published_date=row["published_date"].isoformat(),
        body_response_id=row["body_response_id"],
        observed_response_id=row["observed_response_id"],
        parser_version=row["parser_version"],
        processing_origin=row["processing_origin"],
        registered_at=row["registered_at"],
    )


def notice_evidence(row) -> dict | None:
    if row is None:
        return None
    return dict(
        kind="notice_version",
        published_date=row["published_date"].isoformat(),
        version_id=row["id"],
        body_response_id=row["raw_response_id"],
        parser_version=row["parser_version"],
    )


def _scan_ready(connection: Connection, source_id: str, at: int) -> bool:
    row = (
        connection.execute(
            sa.select(source_ingestion_state).where(source_ingestion_state.c.source_id == source_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["last_complete_scan_run_id"] is None:
        return False
    run = (
        connection.execute(
            sa.select(ingestion_runs).where(ingestion_runs.c.id == row["last_complete_scan_run_id"])
        )
        .mappings()
        .one_or_none()
    )
    return bool(
        run is not None
        and run["source_id"] == source_id
        and run["coverage"] == "complete"
        and run["coverage_at"] == row["last_complete_scan_at"]
        and run["coverage_at"] <= at
        and run["origin"] != "historical"
    )


def _activation_members(connection: Connection, state: dict) -> list[dict]:
    rows = (
        connection.execute(
            sa.select(
                documents.c.source_document_id,
                listings,
                notice_versions.c.id,
                notice_versions.c.published_date,
                notice_versions.c.raw_response_id,
                notice_versions.c.parser_version,
            )
            .select_from(
                documents.outerjoin(listings, listings.c.document_id == documents.c.id).outerjoin(
                    notice_versions, notice_versions.c.id == documents.c.current_version_id
                )
            )
            .where(documents.c.source_id == state["source_id"])
        )
        .mappings()
        .all()
    )
    output = []
    # Column names overlap, so use their actual Column keys rather than string aliases.
    for row in rows:
        entry = (
            {c.name: row[c] for c in listings.c}
            if row[listings.c.document_id] is not None
            else None
        )
        version = (
            {
                c.name: row[c]
                for c in (
                    notice_versions.c.id,
                    notice_versions.c.published_date,
                    notice_versions.c.raw_response_id,
                    notice_versions.c.parser_version,
                )
            }
            if row[notice_versions.c.id] is not None
            else None
        )
        output.append(
            dict(
                installation_id=state["installation_id"],
                source_id=state["source_id"],
                source_document_id=row[documents.c.source_document_id],
                **candidate_values(state, None, listing_evidence(entry), notice_evidence(version)),
            )
        )
    return output


def _preview(
    connection: Connection, source_id: str, profile: Profile, options: ActivationOptions, at: int
) -> dict:
    existing = channel_on(connection)
    state = dict(
        installation_id="preview",
        source_id=source_id,
        activation_at=at,
        initial_recent_review=options.initial_recent_review,
    )
    candidates = _activation_members(connection, state)
    counts = {
        s: sum(m["candidate_state"] == s for m in candidates)
        for s in ("unknown", "selected", "not_recent", "disabled")
    }
    ready = existing is None and _scan_ready(connection, source_id, at)
    return dict(
        enabled=existing is not None,
        source_id=source_id,
        ready=ready,
        blocker="already_enabled" if existing else None if ready else "complete_scan_required",
        member_count=len(candidates),
        candidate_counts=counts,
        activation_at=at,
        policy_sha256=canonical_sha256(policy_manifest(profile)),
        recent_candidates=[
            dict(
                source_document_id=m["source_document_id"],
                selection_evidence=m["selection_evidence"],
                date_conflict=m["date_conflict"],
            )
            for m in sorted(candidates, key=lambda m: m["source_document_id"])
            if m["candidate_state"] == "selected"
        ][:50],
        recent_candidates_truncated=counts["selected"] > 50,
    )


def preview_activation(
    engine: Engine, source_id: str, profile: Profile, options: ActivationOptions, *, at: int
) -> dict:
    notification_time(at)
    with engine.connect() as connection:
        return _preview(connection, source_id, profile, options, at)


def activate_notifications(
    engine: Engine, source_id: str, profile: Profile, options: ActivationOptions, *, at: int
) -> dict:
    notification_time(at)
    parameters = canonical_sha256(
        dict(
            source_id=source_id,
            profile=profile.model_dump(mode="json"),
            options=options.model_dump(mode="json"),
        )
    )
    manifest = policy_manifest(profile)
    try:
        with engine.begin() as connection:
            existing = channel_on(connection)
            if existing is not None:
                if (
                    existing["activation_id"] != options.activation_id
                    or existing["parameters_sha256"] != parameters
                ):
                    raise IngestError("notification_activation_conflict", "notification")
                return dict(
                    enabled=True,
                    reused=True,
                    source_id=source_id,
                    installation_id=existing["installation_id"],
                    activation_at=existing["activation_at"],
                )
            if not _scan_ready(connection, source_id, at):
                raise IngestError("notification_complete_scan_required", "notification")
            digest = canonical_sha256(manifest)
            connection.execute(
                insert(policies)
                .values(policy_sha256=digest, manifest=manifest, created_at=at)
                .on_conflict_do_nothing()
            )
            policy_id = connection.execute(
                sa.select(policies.c.id).where(policies.c.policy_sha256 == digest)
            ).scalar_one()
            state = dict(
                id="primary",
                installation_id=str(uuid.uuid4()),
                source_id=source_id,
                activation_at=at,
                parameters_sha256=parameters,
                policy_revision_id=policy_id,
                **options.model_dump(),
            )
            connection.execute(channel.insert().values(**state))
            candidates = _activation_members(connection, state)
            if candidates:
                connection.execute(members.insert(), candidates)
            result = dict(
                enabled=True,
                reused=False,
                source_id=source_id,
                installation_id=state["installation_id"],
                activation_at=at,
                member_count=len(candidates),
            )
    except SQLAlchemyError as exc:
        raise IngestError("notification_database_write_failed", "notification") from exc
    return result


def notification_status(engine: Engine, source_id: str) -> dict:
    with engine.connect() as connection:
        state = channel_on(connection)
        if state is None or state["source_id"] != source_id:
            return dict(enabled=False, source_id=source_id)
        counts = dict(
            connection.execute(
                sa.select(members.c.candidate_state, sa.func.count())
                .where(members.c.installation_id == state["installation_id"])
                .group_by(members.c.candidate_state)
            ).all()
        )

        def count(table):
            return connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()

        return dict(
            enabled=True,
            source_id=source_id,
            activation_at=state["activation_at"],
            installation_id=state["installation_id"],
            policy_revision_id=state["policy_revision_id"],
            notification_mode=state["notification_mode"],
            paused=state["paused"],
            candidate_counts=counts,
            observations=count(notification_observations),
            events=count(notification_events),
            planned_immediate=count(email_outbox),
        )


def register_listing_evidence_in_transaction(
    connection: Connection,
    *,
    source_id: str,
    page: ListPage,
    body_response_id: int | None,
    observed_response_id: int | None,
    processed_at: int,
    parser_version: str,
    processing_origin: ProcessingOrigin,
    ingestion_run_id: str | None,
    new_document_ids: set[int],
) -> None:
    check_origin(processing_origin, ingestion_run_id)
    validate_processing_run(connection, source_id, ingestion_run_id, processed_at, parser_version)
    # Response-less convenience discovery cannot supply archived date evidence.
    if body_response_id is None:
        return
    body = (
        connection.execute(sa.select(raw_responses).where(raw_responses.c.id == body_response_id))
        .mappings()
        .one_or_none()
    )
    observed = (
        body
        if observed_response_id is None
        else connection.execute(
            sa.select(raw_responses).where(raw_responses.c.id == observed_response_id)
        )
        .mappings()
        .one_or_none()
    )
    if (
        body is None
        or observed is None
        or body["source_id"] != source_id
        or body["page_type"] != "list"
        or body["document_id"] is not None
        or body["status_code"] != 200
        or body["body_path"] is None
        or (processing_origin == "live" and body["body_state"] != "complete")
    ):
        raise IngestError("notification_list_evidence_invalid", "notification")
    if observed["id"] != body["id"] and (
        observed["status_code"] != 304
        or observed["body_path"] is not None
        or observed["validated_response_id"] != body["id"]
        or body["resource_id"] is None
        or any(
            observed[k] != body[k]
            for k in (
                "source_id",
                "page_type",
                "document_id",
                "resource_id",
                "requested_url",
                "final_url",
            )
        )
    ):
        raise IngestError("notification_list_binding_invalid", "notification")
    if max(body["fetched_at"], observed["fetched_at"]) > processed_at:
        raise IngestError("notification_time_invalid", "notification")
    observed_response_id = observed["id"]
    state = channel_on(connection)
    for entry in page.entries:
        doc_id = connection.execute(
            sa.select(documents.c.id).where(
                documents.c.source_id == source_id,
                documents.c.source_document_id == entry.source_document_id,
            )
        ).scalar_one()
        values = dict(
            document_id=doc_id,
            published_date=entry.published_date,
            body_response_id=body_response_id,
            observed_response_id=observed_response_id,
            parser_version=parser_version,
            registered_at=processed_at,
            processing_origin=processing_origin,
        )
        if processing_origin == "live" and doc_id in new_document_ids:
            values.update(live_discovered_run_id=ingestion_run_id, live_discovered_at=processed_at)
        statement = insert(listings).values(**values)
        updates = {
            key: getattr(statement.excluded, key)
            for key in values
            if key not in {"document_id", "live_discovered_run_id", "live_discovered_at"}
        }
        registered = connection.execute(
            statement.on_conflict_do_update(
                index_elements=[listings.c.document_id],
                set_=updates,
                where=listings.c.registered_at <= processed_at,
            )
        )
        if registered.rowcount != 1 or state is None or state["source_id"] != source_id:
            continue
        where = (
            members.c.installation_id == state["installation_id"],
            members.c.source_id == source_id,
            members.c.source_document_id == entry.source_document_id,
        )
        old = connection.execute(sa.select(members).where(*where)).mappings().one_or_none()
        if old is not None:
            connection.execute(
                members.update()
                .where(*where)
                .values(
                    **candidate_values(state, old, listing_evidence(values), old["notice_evidence"])
                )
            )
