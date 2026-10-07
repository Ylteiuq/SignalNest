"""Prepare notification decisions outside a transaction; commit verified facts inside it.

An observation refers to the actual successful body response, not a deduplicated
notice version's first raw response. Historical replay never advances this live
baseline. Planned intents are not frozen messages and cannot be sent.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, Engine

from signalnest.contracts import NoticeContent, PageInput, ParsedNotice
from signalnest.errors import IngestError
from signalnest.ingestion_state import validate_processing_run
from signalnest.notifications.contracts import (
    Decision,
    EventContext,
    Profile,
    canonical_json,
    canonical_sha256,
)
from signalnest.notifications.decision import decide, policy_manifest
from signalnest.notifications.facts import extract_facts
from signalnest.notifications.state import (
    ProcessingOrigin,
    candidate_values,
    channel_on,
    check_origin,
    next_digest_at,
    notification_time,
)
from signalnest.parsing import ParseError, parse_notice
from signalnest.rawstore import RawStore, RawStoreError
from signalnest.schema import (
    documents,
    email_outbox,
    ingestion_runs,
    notice_versions,
    raw_responses,
)
from signalnest.schema import (
    notification_activation_members as members,
)
from signalnest.schema import (
    notification_decisions as decisions,
)
from signalnest.schema import (
    notification_events as events,
)
from signalnest.schema import (
    notification_listing_evidence as listings,
)
from signalnest.schema import (
    notification_observations as observations,
)
from signalnest.schema import (
    notification_policy_revisions as policies,
)


def _row(connection, table, *where):
    row = connection.execute(sa.select(table).where(*where)).mappings().one_or_none()
    return dict(row) if row is not None else None


def _jsonable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _snapshot(connection: Connection, document_id: int) -> dict:
    state = channel_on(connection)
    if state is None:
        return dict(channel=None)
    doc = _row(connection, documents, documents.c.id == document_id)
    if doc is None or state["source_id"] != doc["source_id"]:
        raise IngestError("notification_source_mismatch", "notification")
    member = _row(
        connection,
        members,
        members.c.installation_id == state["installation_id"],
        members.c.source_id == doc["source_id"],
        members.c.source_document_id == doc["source_document_id"],
    )
    observation = _row(
        connection,
        observations,
        observations.c.installation_id == state["installation_id"],
        observations.c.document_id == document_id,
    )
    prior = None
    if observation is not None and observation["event_seq"]:
        event = _row(
            connection,
            events,
            events.c.installation_id == state["installation_id"],
            events.c.document_id == document_id,
            events.c.event_seq == observation["event_seq"],
        )
        if event is None or event["selected_decision_id"] is None:
            raise IngestError("notification_event_incomplete", "notification")
        selected = _row(
            connection,
            decisions,
            decisions.c.id == event["selected_decision_id"],
            decisions.c.event_id == event["id"],
        )
        if selected is None:
            raise IngestError("notification_decision_missing", "notification")
        prior = dict(event=event, selected=selected)
    listing = _row(connection, listings, listings.c.document_id == document_id)
    discovery_run = (
        _row(connection, ingestion_runs, ingestion_runs.c.id == listing["live_discovered_run_id"])
        if listing is not None and listing["live_discovered_run_id"] is not None
        else None
    )
    policy = _row(connection, policies, policies.c.id == state["policy_revision_id"])
    return dict(
        channel=state,
        member=member,
        observation=observation,
        prior=prior,
        listing=listing,
        discovery_run=discovery_run,
        policy=policy,
    )


def _token(snapshot: dict) -> str:
    return canonical_json(_jsonable(snapshot))


def _validate_evidence(
    connection: Connection,
    *,
    body_response_id: int,
    observed_response_id: int,
    document_id: int,
    notice: ParsedNotice,
    ingestion_run_id: str | None,
    at: int,
) -> dict:
    doc = _row(connection, documents, documents.c.id == document_id)
    body = _row(connection, raw_responses, raw_responses.c.id == body_response_id)
    observed = _row(connection, raw_responses, raw_responses.c.id == observed_response_id)
    if doc is None or body is None or observed is None:
        raise IngestError("notification_evidence_missing", "notification")
    if (
        body["status_code"] != 200
        or body["body_state"] != "complete"
        or body["body_path"] is None
        or body["body_sha256"] is None
    ):
        raise IngestError("notification_body_not_complete", "notification")
    if (
        body["page_type"] != "notice"
        or body["document_id"] != document_id
        or body["source_id"] != doc["source_id"]
        or notice.source_document_id != doc["source_document_id"]
        or str(notice.page_url) != body["final_url"]
    ):
        raise IngestError("notification_identity_mismatch", "notification")
    if observed["id"] != body["id"] and (
        observed["status_code"] != 304
        or observed["body_path"] is not None
        or observed["validated_response_id"] != body["id"]
        or body["resource_id"] is None
        or any(
            observed[k] != body[k]
            for k in (
                "resource_id",
                "source_id",
                "requested_url",
                "final_url",
                "page_type",
                "document_id",
            )
        )
    ):
        raise IngestError("notification_binding_invalid", "notification")
    if max(body["fetched_at"], observed["fetched_at"]) > at:
        raise IngestError("notification_time_invalid", "notification")
    validate_processing_run(
        connection, doc["source_id"], ingestion_run_id, at, notice.parser_version
    )
    return body


@dataclass(frozen=True)
class PreparedNotification:
    document_id: int
    source_document_id: str
    body_response_id: int
    observed_response_id: int
    ingestion_run_id: str | None
    evaluated_at: int
    content_sha256: str
    parser_version: str
    snapshot_token: str
    kind: str | None = None
    silent_reason: str | None = None
    comparison_error_code: str | None = None
    member_values_json: str | None = None
    decision: Decision | None = None
    facts_json: str | None = None
    context_json: str | None = None


@dataclass(frozen=True)
class NotificationCommitResult:
    baseline_advanced: bool
    event_id: int | None = None
    decision_id: int | None = None
    outbox_id: int | None = None
    silent_reason: str | None = None
    comparison_error_code: str | None = None


def _version_content(version) -> NoticeContent:
    try:
        content = NoticeContent.model_validate(version["normalized_content"])
    except ValidationError as exc:
        raise IngestError("notification_version_invalid", "notification") from exc
    if content.content_sha256() != version["content_sha256"]:
        raise IngestError("notification_version_digest", "notification")
    return content


def prepare_notification(
    engine: Engine,
    raw_store: RawStore,
    *,
    notice: ParsedNotice,
    body_response_id: int,
    observed_response_id: int,
    processing_origin: ProcessingOrigin,
    ingestion_run_id: str | None,
    evaluated_at: int,
    notice_parser: Callable[[PageInput], ParsedNotice] = parse_notice,
) -> PreparedNotification:
    """No writes. File reads/reparse/N0 evaluation finish before success BEGIN."""
    notification_time(evaluated_at)
    check_origin(processing_origin, ingestion_run_id)
    with engine.connect() as connection:
        body = _row(connection, raw_responses, raw_responses.c.id == body_response_id)
        if body is None or body["document_id"] is None:
            raise IngestError("notification_evidence_missing", "notification")
        doc_id = body["document_id"]
        snapshot = (
            _snapshot(connection, doc_id) if processing_origin == "live" else dict(channel=None)
        )
        if processing_origin == "live":
            body = _validate_evidence(
                connection,
                body_response_id=body_response_id,
                observed_response_id=observed_response_id,
                document_id=doc_id,
                notice=notice,
                ingestion_run_id=ingestion_run_id,
                at=evaluated_at,
            )
        previous_version = None
        previous_body = None
        old = snapshot.get("observation")
        if old is not None:
            previous_version = _row(
                connection,
                notice_versions,
                notice_versions.c.id == old["version_id"],
                notice_versions.c.document_id == doc_id,
            )
            previous_body = _row(
                connection, raw_responses, raw_responses.c.id == old["body_response_id"]
            )
            if (
                previous_version is None
                or previous_body is None
                or previous_body["document_id"] != doc_id
                or previous_body["source_id"] != body["source_id"]
                or previous_body["page_type"] != "notice"
                or previous_body["status_code"] != 200
                or previous_body["body_state"] != "complete"
            ):
                raise IngestError("notification_baseline_invalid", "notification")
    base = dict(
        document_id=doc_id,
        source_document_id=notice.source_document_id,
        body_response_id=body_response_id,
        observed_response_id=observed_response_id,
        ingestion_run_id=ingestion_run_id,
        evaluated_at=evaluated_at,
        content_sha256=notice.content.content_sha256(),
        parser_version=notice.parser_version,
        snapshot_token=_token(snapshot),
    )
    state = snapshot["channel"]
    if processing_origin != "live" or state is None:
        return PreparedNotification(
            **base, silent_reason="not_live" if processing_origin != "live" else "not_enabled"
        )
    if evaluated_at < state["activation_at"] or (
        old is not None and evaluated_at < old["observed_at"]
    ):
        raise IngestError("notification_time_invalid", "notification")
    policy = snapshot["policy"]
    if policy is None:
        raise IngestError("notification_policy_missing", "notification")
    try:
        profile = Profile.model_validate(policy["manifest"]["profile"])
    except (ValidationError, KeyError) as exc:
        raise IngestError("notification_policy_invalid", "notification") from exc
    if (
        canonical_sha256(policy["manifest"]) != policy["policy_sha256"]
        or canonical_sha256(policy_manifest(profile)) != policy["policy_sha256"]
    ):
        raise IngestError("notification_policy_outdated", "notification")
    member = snapshot["member"]
    member_values = None
    if member is not None:
        evidence = dict(
            kind="notice_version",
            published_date=notice.content.published_date.isoformat(),
            body_response_id=body_response_id,
            observed_response_id=observed_response_id,
            parser_version=notice.parser_version,
        )
        member_values = candidate_values(state, member, member["list_evidence"], evidence)
    kind = None
    reason = "historical_baseline"
    comparison_error = None
    previous_content = None
    if old is not None:
        previous_content = _version_content(previous_version)
        if previous_version["parser_version"] == notice.parser_version:
            changed = previous_content.content_sha256() != base["content_sha256"]
        elif previous_body["body_sha256"] == body["body_sha256"]:
            changed = False
            reason = "parser_changed_same_raw"
        else:
            try:
                previous_bytes = raw_store.read(
                    previous_body["body_path"], previous_body["body_sha256"]
                )
                reparsed = notice_parser(
                    PageInput(content=previous_bytes, page_url=previous_body["final_url"])
                )
                if (
                    reparsed.parser_version != notice.parser_version
                    or reparsed.source_document_id != notice.source_document_id
                    or str(reparsed.page_url) != previous_body["final_url"]
                ):
                    raise IngestError("notification_comparison_parser_mismatch", "notification")
                previous_content = reparsed.content
                changed = previous_content.content_sha256() != base["content_sha256"]
            except (RawStoreError, ParseError, ValidationError) as exc:
                comparison_error = ("comparison_" + getattr(exc, "code", "input_invalid"))[:64]
                changed = False
                reason = "comparison_unknown"
        if changed:
            kind = "update"
        elif reason == "historical_baseline":
            reason = "unchanged"
    if (
        member_values is not None
        and member_values["candidate_state"] == "selected"
        and (old is None or old["event_seq"] == 0)
    ):
        kind = "activation_recent"
    elif old is None and member is None:
        proof, run = snapshot["listing"], snapshot["discovery_run"]
        if (
            proof is not None
            and run is not None
            and run["origin"] == "regular"
            and run["source_id"] == state["source_id"]
            and proof["live_discovered_at"] >= state["activation_at"]
        ):
            kind = "new"
    extra = dict(
        kind=kind,
        silent_reason=reason if kind is None else None,
        comparison_error_code=comparison_error,
        member_values_json=canonical_json(member_values) if member_values is not None else None,
    )
    if kind is None:
        return PreparedNotification(**base, **extra)
    facts = extract_facts(notice.content)
    previous_facts = extract_facts(previous_content) if previous_content is not None else None
    prior = snapshot["prior"]
    old_decision = (
        Decision.model_validate(prior["selected"]["decision"]) if prior is not None else None
    )
    context = EventContext(
        kind=kind,
        notification_mode=state["notification_mode"],
        previous_facts=previous_facts,
        previous_action=old_decision.action if old_decision else None,
        previous_effective_route=prior["event"]["effective_route"] if prior else "none",
        comparison_known=comparison_error is None,
        activation_date_conflict=member_values["date_conflict"]
        if member_values is not None
        else False,
        next_digest_at=next_digest_at(state, evaluated_at),
    )
    decision = decide(profile, facts, context, now=datetime.fromtimestamp(evaluated_at, UTC))
    compact_context = context.model_dump(mode="json", exclude={"previous_facts"})
    compact_context["previous_facts"] = (
        previous_facts.model_dump(mode="json", exclude={"body_text"})
        if previous_facts is not None
        else None
    )
    return PreparedNotification(
        **base,
        **extra,
        decision=decision,
        facts_json=canonical_json(facts.model_dump(mode="json", exclude={"body_text"})),
        context_json=canonical_json(compact_context),
    )


def commit_notification_in_transaction(
    connection: Connection, prepared: PreparedNotification, *, version_id: int
) -> NotificationCommitResult:
    """No commit, file reads, parsing or rule evaluation. Caller rolls back on errors."""
    snapshot = _snapshot(connection, prepared.document_id)
    if _token(snapshot) != prepared.snapshot_token:
        raise IngestError("notification_prepare_stale", "notification")
    state = snapshot["channel"]
    if state is None:
        return NotificationCommitResult(False, silent_reason=prepared.silent_reason)
    version = _row(
        connection,
        notice_versions,
        notice_versions.c.id == version_id,
        notice_versions.c.document_id == prepared.document_id,
    )
    if (
        version is None
        or version["content_sha256"] != prepared.content_sha256
        or version["parser_version"] != prepared.parser_version
    ):
        raise IngestError("notification_version_mismatch", "notification")
    notice = ParsedNotice(
        source_document_id=prepared.source_document_id,
        page_url=_row(connection, raw_responses, raw_responses.c.id == prepared.body_response_id)[
            "final_url"
        ],
        parser_version=prepared.parser_version,
        content=_version_content(version),
    )
    _validate_evidence(
        connection,
        body_response_id=prepared.body_response_id,
        observed_response_id=prepared.observed_response_id,
        document_id=prepared.document_id,
        notice=notice,
        ingestion_run_id=prepared.ingestion_run_id,
        at=prepared.evaluated_at,
    )
    old = snapshot["observation"]
    seq = old["event_seq"] if old is not None else 0
    event_id = decision_id = outbox_id = None
    if prepared.kind is not None:
        decision = prepared.decision
        if (
            decision is None
            or prepared.facts_json is None
            or prepared.context_json is None
            or decision.policy_sha256 != snapshot["policy"]["policy_sha256"]
            or decision.content_sha256 != prepared.content_sha256
        ):
            raise IngestError("notification_decision_invalid", "notification")
        seq += 1
        event_id = connection.execute(
            events.insert().values(
                installation_id=state["installation_id"],
                document_id=prepared.document_id,
                event_seq=seq,
                kind=prepared.kind,
                previous_version_id=old["version_id"] if old else None,
                version_id=version_id,
                previous_body_response_id=old["body_response_id"] if old else None,
                body_response_id=prepared.body_response_id,
                observed_response_id=prepared.observed_response_id,
                occurred_at=prepared.evaluated_at,
            )
        ).inserted_primary_key[0]
        decision_id = connection.execute(
            decisions.insert().values(
                event_id=event_id,
                policy_revision_id=state["policy_revision_id"],
                evaluation_key="initial",
                evaluated_at=prepared.evaluated_at,
                decision=decision.model_dump(mode="json"),
                facts=json.loads(prepared.facts_json),
                context=json.loads(prepared.context_json),
            )
        ).inserted_primary_key[0]
        if decision.effective_route == "immediate":
            delivery_key = canonical_sha256(
                dict(
                    installation_id=state["installation_id"],
                    source_id=state["source_id"],
                    source_document_id=prepared.source_document_id,
                    event_seq=seq,
                    recipient_key="primary",
                )
            )
            outbox_id = connection.execute(
                email_outbox.insert().values(
                    event_id=event_id,
                    decision_id=decision_id,
                    delivery_key=delivery_key,
                    sender=state["sender"],
                    recipient=state["recipient"],
                    created_at=prepared.evaluated_at,
                )
            ).inserted_primary_key[0]
        connection.execute(
            events.update()
            .where(events.c.id == event_id)
            .values(
                selected_decision_id=decision_id,
                effective_route=decision.effective_route,
                delivery_intent_registered_at=prepared.evaluated_at
                if decision.effective_route != "none"
                else None,
                outbox_id=outbox_id,
            )
        )
    values = dict(
        installation_id=state["installation_id"],
        document_id=prepared.document_id,
        version_id=version_id,
        body_response_id=prepared.body_response_id,
        observed_response_id=prepared.observed_response_id,
        observed_at=prepared.evaluated_at,
        event_seq=seq,
        comparison_error_code=prepared.comparison_error_code
        or (old["comparison_error_code"] if old else None),
        comparison_error_at=prepared.evaluated_at
        if prepared.comparison_error_code
        else (old["comparison_error_at"] if old else None),
        comparison_evidence=dict(
            previous_version_id=old["version_id"],
            previous_body_response_id=old["body_response_id"],
            body_response_id=prepared.body_response_id,
            observed_response_id=prepared.observed_response_id,
            parser_version=prepared.parser_version,
        )
        if prepared.comparison_error_code
        else (old["comparison_evidence"] if old else None),
    )
    connection.execute(
        insert(observations)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[observations.c.installation_id, observations.c.document_id],
            set_={k: v for k, v in values.items() if k not in {"installation_id", "document_id"}},
        )
    )
    if prepared.member_values_json is not None:
        updates = json.loads(prepared.member_values_json)
        updates["notice_evidence"]["version_id"] = version_id
        evidence = [updates["selection_evidence"], *(updates["conflict_evidence"] or [])]
        for item in evidence:
            if (
                item is not None
                and item["kind"] == "notice_version"
                and item["body_response_id"] == prepared.body_response_id
                and item["parser_version"] == prepared.parser_version
            ):
                item["version_id"] = version_id
        if event_id is not None and snapshot["member"]["candidate_state"] != "generated":
            updates.update(candidate_state="generated", generated_event_id=event_id)
        connection.execute(
            members.update()
            .where(
                members.c.installation_id == state["installation_id"],
                members.c.source_id == state["source_id"],
                members.c.source_document_id == prepared.source_document_id,
            )
            .values(**updates)
        )
    return NotificationCommitResult(
        True,
        event_id,
        decision_id,
        outbox_id,
        prepared.silent_reason,
        prepared.comparison_error_code,
    )
