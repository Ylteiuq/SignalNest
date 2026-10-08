"""Bounded local planning, atomic membership allocation and immutable byte readback.

The caller holds the instance writer lock for plan_mail. Reads and rendering finish
before the write transaction; SMTP and raw-file reads are absent from this module.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from itertools import zip_longest

import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.contracts import NoticeContent, WebUrl
from signalnest.errors import IngestError
from signalnest.mail.contracts import (
    RENDERING_VERSION,
    FrozenMessage,
    MailError,
    MailItem,
    PlanOptions,
    RenderedMail,
)
from signalnest.mail.rendering import render_mail
from signalnest.notifications.contracts import Decision, canonical_json, canonical_sha256
from signalnest.notifications.state import SHANGHAI, channel_on, notification_time
from signalnest.schema import (
    documents,
    email_outbox,
    mail_delivery,
    notice_versions,
    raw_responses,
)
from signalnest.schema import (
    mail_message_members as members,
)
from signalnest.schema import (
    mail_messages as messages,
)
from signalnest.schema import (
    mail_plan_errors as errors,
)
from signalnest.schema import (
    notification_decisions as decisions,
)
from signalnest.schema import (
    notification_events as events,
)

# Limits preparation work even when rendering errors or future events dominate.
CANDIDATE_LIMIT = 1000


def _time(at: int) -> None:
    try:
        notification_time(at)
    except IngestError as exc:
        raise MailError("mail_time_invalid") from exc


def _row(connection, table, *where):
    row = connection.execute(sa.select(table).where(*where)).mappings().one_or_none()
    return dict(row) if row is not None else None


def _jsonable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _token(value) -> str:
    return canonical_json(_jsonable(value))


def _channel(connection, source_id):
    state = channel_on(connection)
    if state is None:
        raise MailError("mail_not_enabled")
    if state["source_id"] != source_id:
        raise MailError("mail_source_mismatch")
    # Current Profile/mode/pause is not a planning input or a re-routing token.
    return {
        k: state[k]
        for k in (
            "installation_id",
            "source_id",
            "sender",
            "recipient",
            "digest_hour",
            "digest_minute",
        )
    }


def _elapsed_slot(state, at):
    now = datetime.fromtimestamp(at, UTC).astimezone(SHANGHAI)
    slot = now.replace(
        hour=state["digest_hour"], minute=state["digest_minute"], second=0, microsecond=0
    )
    if slot > now:
        slot -= timedelta(days=1)
    return int(slot.timestamp())


def _last_part(connection, state, slot):
    return connection.execute(
        sa.select(sa.func.coalesce(sa.func.max(messages.c.part), 0)).where(
            messages.c.installation_id == state["installation_id"], messages.c.digest_slot == slot
        )
    ).scalar_one()


def _pending(connection, state):
    return (
        connection.execute(
            sa.select(
                events.c.id,
                events.c.effective_route,
                events.c.occurred_at,
                events.c.selected_decision_id,
                decisions.c.context,
                decisions.c.evaluated_at,
                email_outbox.c.created_at.label("intent_at"),
                errors.c.decision_id.label("error_decision"),
                errors.c.rendering_version.label("error_renderer"),
                errors.c.max_bytes.label("error_limit"),
            )
            .select_from(
                events.join(documents, documents.c.id == events.c.document_id)
                .outerjoin(decisions, decisions.c.id == events.c.selected_decision_id)
                .outerjoin(email_outbox, email_outbox.c.id == events.c.outbox_id)
                .outerjoin(members, members.c.event_id == events.c.id)
                .outerjoin(errors, errors.c.event_id == events.c.id)
            )
            .where(
                events.c.installation_id == state["installation_id"],
                documents.c.source_id == state["source_id"],
                events.c.effective_route != "none",
                members.c.event_id.is_(None),
            )
            .order_by(events.c.occurred_at, events.c.id)
        )
        .mappings()
        .all()
    )


def _digest_due(row) -> int:
    try:
        dt = datetime.fromisoformat(row["context"]["next_digest_at"])
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("offset required")
        due = int(dt.timestamp())
        _time(due)
        if due <= row["evaluated_at"]:
            raise ValueError("digest must follow evaluation")
    except (KeyError, TypeError, ValueError, OverflowError, OSError, MailError) as exc:
        raise MailError("mail_event_context_invalid", event_id=row["id"]) from exc
    return due


def _eligible(row, at):
    if row["selected_decision_id"] is None or row["evaluated_at"] is None:
        raise MailError("mail_event_decision_missing", event_id=row["id"])
    if row["effective_route"] == "immediate":
        if row["intent_at"] is None:
            raise MailError("mail_immediate_intent_missing", event_id=row["id"])
        return max(row["intent_at"], row["occurred_at"]) <= at
    return max(_digest_due(row), row["occurred_at"]) <= at


def _snapshot(connection, event_id):
    event = _row(connection, events, events.c.id == event_id)
    if event is None:
        raise MailError("mail_event_missing", event_id=event_id)
    doc = _row(connection, documents, documents.c.id == event["document_id"])
    version = _row(connection, notice_versions, notice_versions.c.id == event["version_id"])
    decision = _row(connection, decisions, decisions.c.id == event["selected_decision_id"])
    body = _row(connection, raw_responses, raw_responses.c.id == event["body_response_id"])
    if any(row is None for row in (doc, version, decision, body)):
        raise MailError("mail_event_evidence_missing", event_id=event_id)
    intent = (
        _row(connection, email_outbox, email_outbox.c.id == event["outbox_id"])
        if event["outbox_id"] is not None
        else None
    )
    return dict(
        event=event,
        document=dict(
            id=doc["id"], source_id=doc["source_id"], source_document_id=doc["source_document_id"]
        ),
        version=version,
        decision=decision,
        body={
            k: body[k]
            for k in (
                "id",
                "source_id",
                "document_id",
                "page_type",
                "status_code",
                "body_state",
                "body_path",
                "body_sha256",
                "final_url",
            )
        },
        intent=intent,
        membership=_row(connection, members, members.c.event_id == event_id),
    )


def _item(snapshot, state):
    event, doc, version, decision, body, intent = (
        snapshot[k] for k in ("event", "document", "version", "decision", "body", "intent")
    )
    event_id = event["id"]
    if snapshot["membership"] is not None:
        raise MailError("mail_prepare_stale", event_id=event_id)
    if (
        event["installation_id"] != state["installation_id"]
        or doc["source_id"] != state["source_id"]
        or version["document_id"] != doc["id"]
        or decision["event_id"] != event_id
        or body["document_id"] != doc["id"]
        or body["source_id"] != doc["source_id"]
        or body["page_type"] != "notice"
        or body["status_code"] != 200
        or body["body_state"] != "complete"
        or body["body_path"] is None
        or event["delivery_intent_registered_at"] is None
    ):
        raise MailError("mail_event_evidence_invalid", event_id=event_id)
    try:
        content = NoticeContent.model_validate(version["normalized_content"])
        selected = Decision.model_validate(decision["decision"])
        # Use the actual event response URL, independent of the version's first raw.
        page_url = str(TypeAdapter(WebUrl).validate_python(body["final_url"]))
    except (ValidationError, ValueError, TypeError) as exc:
        raise MailError("mail_event_evidence_invalid", event_id=event_id) from exc
    if (
        content.content_sha256() != version["content_sha256"]
        or selected.content_sha256 != version["content_sha256"]
        or selected.effective_route != event["effective_route"]
        or int(selected.evaluated_at.timestamp()) != decision["evaluated_at"]
    ):
        raise MailError("mail_event_decision_invalid", event_id=event_id)
    if event["effective_route"] == "immediate":
        if (
            intent is None
            or intent["event_id"] != event_id
            or intent["decision_id"] != decision["id"]
            or intent["sender"] != state["sender"]
            or intent["recipient"] != state["recipient"]
        ):
            raise MailError("mail_intent_address_or_decision_mismatch", event_id=event_id)
    elif event["effective_route"] != "digest" or intent is not None:
        raise MailError("mail_event_route_invalid", event_id=event_id)
    return MailItem(
        event_id=event_id,
        decision_id=decision["id"],
        document_id=doc["id"],
        source_document_id=doc["source_document_id"],
        kind=event["kind"],
        occurred_at=event["occurred_at"],
        content=content,
        page_url=page_url,
        decision=selected,
    )


def _member(snapshot):
    return dict(
        event_id=snapshot["event"]["id"],
        decision_id=snapshot["decision"]["id"],
        document_id=snapshot["document"]["id"],
        source_id=snapshot["document"]["source_id"],
        source_document_id=snapshot["document"]["source_document_id"],
        version_id=snapshot["version"]["id"],
        content_sha256=snapshot["version"]["content_sha256"],
        parser_version=snapshot["version"]["parser_version"],
        body_response_id=snapshot["event"]["body_response_id"],
        observed_response_id=snapshot["event"]["observed_response_id"],
        page_url=snapshot["body"]["final_url"],
        event_kind=snapshot["event"]["kind"],
        occurred_at=snapshot["event"]["occurred_at"],
        decision=snapshot["decision"]["decision"],
        policy_revision_id=snapshot["decision"]["policy_revision_id"],
    )


@dataclass(frozen=True)
class PreparedMail:
    kind: str
    delivery_key: str
    immediate_intent_id: int | None
    digest_slot: int | None
    part: int
    rendered: RenderedMail = field(repr=False)
    event_tokens: tuple[tuple[int, str], ...] = field(repr=False)
    members_json: str = field(repr=False)


@dataclass(frozen=True)
class PreparedError:
    event_id: int
    decision_id: int
    token: str = field(repr=False)
    code: str


@dataclass(frozen=True)
class PreparedPlan:
    source_id: str
    at: int
    options: PlanOptions
    channel_token: str = field(repr=False)
    digest_slot: int
    last_part: int
    messages: tuple[PreparedMail, ...] = field(repr=False)
    blocked: tuple[PreparedError, ...]
    candidate_limit_reached: bool
    counts_json: str


def _build(items, snapshots, state, *, kind, slot, part):
    if kind == "immediate":
        intent = snapshots[0]["intent"]
        key = canonical_sha256(dict(kind=kind, intent_key=intent["delivery_key"]))
        date_at = intent["created_at"]
        intent_id = intent["id"]
    else:
        key = canonical_sha256(
            dict(
                kind=kind,
                installation_id=state["installation_id"],
                recipient_key="primary",
                slot=slot,
                part=part,
            )
        )
        date_at, intent_id = slot, None
    message_id = f"<signalnest.{key}@{state['sender'].rsplit('@', 1)[-1]}>"
    rendered = render_mail(
        tuple(items),
        kind=kind,
        sender=state["sender"],
        recipient=state["recipient"],
        message_id=message_id,
        date_at=date_at,
        digest_slot=slot if kind == "digest" else None,
        part=part,
    )
    return PreparedMail(
        kind=kind,
        delivery_key=key,
        immediate_intent_id=intent_id,
        digest_slot=slot if kind == "digest" else None,
        part=part,
        rendered=rendered,
        event_tokens=tuple(
            (item.event_id, _token(snapshot))
            for item, snapshot in zip(items, snapshots, strict=True)
        ),
        members_json=canonical_json([_member(snapshot) for snapshot in snapshots]),
    )


def _counts(connection, state, at):
    pending = _pending(connection, state)
    immediate = sum(r["effective_route"] == "immediate" for r in pending)
    due = sum(r["effective_route"] == "digest" and _eligible(r, at) for r in pending)
    total_digest = len(pending) - immediate
    blocked = connection.execute(
        sa.select(sa.func.count())
        .select_from(
            errors.join(events, events.c.id == errors.c.event_id).outerjoin(
                members, members.c.event_id == errors.c.event_id
            )
        )
        .where(events.c.installation_id == state["installation_id"], members.c.event_id.is_(None))
    ).scalar_one()
    return dict(
        remaining_immediate=immediate,
        remaining_digest=total_digest,
        due_digest=due,
        deferred_digest=total_digest - due,
        blocked_count=blocked,
    )


def prepare_plan(engine: Engine, source_id: str, options: PlanOptions, *, at: int) -> PreparedPlan:
    _time(at)
    try:
        with engine.connect() as connection:
            state = _channel(connection, source_id)
            slot = _elapsed_slot(state, at)
            last_part = _last_part(connection, state, slot)
            counts = _counts(connection, state, at)
            pending = _pending(connection, state)
            candidates = [
                r
                for r in pending
                if _eligible(r, at)
                and not (
                    r["error_decision"] == r["selected_decision_id"]
                    and r["error_renderer"] == RENDERING_VERSION
                    and r["error_limit"] == options.max_bytes
                )
            ]
            # Alternate queues to keep bounded reads useful for both routes.
            immediates = [r for r in candidates if r["effective_route"] == "immediate"]
            digests = [r for r in candidates if r["effective_route"] == "digest"]
            selected = [
                row for pair in zip_longest(immediates, digests) for row in pair if row is not None
            ][:CANDIDATE_LIMIT]
            snapshots = [_snapshot(connection, r["id"]) for r in selected]
    except SQLAlchemyError as exc:
        raise MailError("mail_database_read_failed") from exc
    # No DB transaction is open below. Pure rendering/packing only.
    immediate = []
    digest = []
    for snapshot in snapshots:
        item = _item(snapshot, state)
        (immediate if snapshot["event"]["effective_route"] == "immediate" else digest).append(
            (item, snapshot)
        )
    planned = []
    blocked = []

    def blocked_item(item, snapshot, code):
        blocked.append(PreparedError(item.event_id, item.decision_id, _token(snapshot), code))

    capacity = options.max_messages - int(bool(digest))
    for item, snapshot in immediate:
        if len(planned) >= capacity:
            break
        try:
            mail = _build([item], [snapshot], state, kind="immediate", slot=None, part=1)
            if len(mail.rendered.payload) > options.max_bytes:
                blocked_item(item, snapshot, "mail_item_too_large")
            else:
                planned.append(mail)
        except MailError as exc:
            if exc.code not in {"render_input_invalid", "render_content_mismatch"}:
                raise
            blocked_item(item, snapshot, exc.code)
    current_items = []
    current_snapshots = []
    current_mail = None
    part = last_part + 1
    for item, snapshot in digest:
        if len(planned) >= options.max_messages:
            break
        try:
            single = _build([item], [snapshot], state, kind="digest", slot=slot, part=part)
        except MailError as exc:
            if exc.code not in {"render_input_invalid", "render_content_mismatch"}:
                raise
            blocked_item(item, snapshot, exc.code)
            continue
        if len(single.rendered.payload) > options.max_bytes:
            blocked_item(item, snapshot, "mail_item_too_large")
            continue
        # The renderer accepts at most 100 members; split before constructing 101.
        combined = (
            current_mail
            if len(current_items) >= options.max_events
            else _build(
                [*current_items, item],
                [*current_snapshots, snapshot],
                state,
                kind="digest",
                slot=slot,
                part=part,
            )
        )
        if current_items and (
            len(current_items) >= options.max_events
            or len(combined.rendered.payload) > options.max_bytes
        ):
            planned.append(current_mail)
            part += 1
            current_items, current_snapshots, current_mail = [], [], None
            if len(planned) >= options.max_messages:
                break
            single = _build([item], [snapshot], state, kind="digest", slot=slot, part=part)
            if len(single.rendered.payload) > options.max_bytes:
                blocked_item(item, snapshot, "mail_item_too_large")
                continue
            combined = single
        current_items.append(item)
        current_snapshots.append(snapshot)
        current_mail = combined
    if current_mail is not None and len(planned) < options.max_messages:
        planned.append(current_mail)
    # Unused reserved digest capacity can service immediates if all digest items block.
    assigned = {member[0] for mail in planned for member in mail.event_tokens}
    rejected = {error.event_id for error in blocked}
    for item, snapshot in immediate:
        if len(planned) >= options.max_messages:
            break
        if item.event_id in assigned or item.event_id in rejected:
            continue
        try:
            mail = _build([item], [snapshot], state, kind="immediate", slot=None, part=1)
            if len(mail.rendered.payload) > options.max_bytes:
                blocked_item(item, snapshot, "mail_item_too_large")
            else:
                planned.append(mail)
        except MailError as exc:
            if exc.code not in {"render_input_invalid", "render_content_mismatch"}:
                raise
            blocked_item(item, snapshot, exc.code)
    return PreparedPlan(
        source_id,
        at,
        options,
        _token(state),
        slot,
        last_part,
        tuple(planned),
        tuple(blocked),
        len(candidates) > len(selected),
        canonical_json(counts),
    )


def commit_plan_in_transaction(connection: Connection, prepared: PreparedPlan) -> tuple[int, ...]:
    """Validate tokens, then freeze the entire bounded plan in the caller's transaction."""
    state = _channel(connection, prepared.source_id)
    if (
        _token(state) != prepared.channel_token
        or _last_part(connection, state, prepared.digest_slot) != prepared.last_part
    ):
        raise MailError("mail_prepare_stale")
    if len(prepared.messages) > prepared.options.max_messages:
        raise MailError("mail_prepared_payload_invalid")
    snapshots = {}
    for mail in prepared.messages:
        for event_id, token in mail.event_tokens:
            if event_id in snapshots:
                raise MailError("mail_prepared_payload_invalid", event_id=event_id)
            snapshot = _snapshot(connection, event_id)
            if _token(snapshot) != token:
                raise MailError("mail_prepare_stale", event_id=event_id)
            snapshots[event_id] = snapshot
    for error in prepared.blocked:
        snapshot = _snapshot(connection, error.event_id)
        if _token(snapshot) != error.token:
            raise MailError("mail_prepare_stale", event_id=error.event_id)
        if (
            error.event_id in snapshots
            or error.decision_id != snapshot["decision"]["id"]
            or error.code
            not in {"mail_item_too_large", "render_input_invalid", "render_content_mismatch"}
        ):
            raise MailError("mail_prepared_payload_invalid", event_id=error.event_id)
        snapshots[error.event_id] = snapshot
    ids = []
    for mail in prepared.messages:
        rendered = mail.rendered
        try:
            manifest = json.loads(mail.members_json)
        except (ValueError, TypeError) as exc:
            raise MailError("mail_prepared_payload_invalid") from exc
        selected = [snapshots[event_id] for event_id, _ in mail.event_tokens]
        if (
            not manifest
            or manifest != [_member(snapshot) for snapshot in selected]
            or len(manifest) > prepared.options.max_events
            or len(rendered.payload) > prepared.options.max_bytes
            or not rendered.payload
            or hashlib.sha256(rendered.payload).hexdigest() != rendered.payload_sha256
            or len(manifest) != len(mail.event_tokens)
            or rendered.sender != state["sender"]
            or rendered.recipient != state["recipient"]
            or rendered.rendering_version != RENDERING_VERSION
        ):
            raise MailError("mail_prepared_payload_invalid")
        if mail.kind == "immediate":
            intent = selected[0]["intent"]
            if (
                len(selected) != 1
                or intent is None
                or mail.immediate_intent_id != intent["id"]
                or mail.digest_slot is not None
                or mail.part != 1
                or rendered.date_at != intent["created_at"]
            ):
                raise MailError("mail_prepared_payload_invalid")
            key = canonical_sha256(dict(kind=mail.kind, intent_key=intent["delivery_key"]))
        elif mail.kind == "digest":
            if (
                mail.immediate_intent_id is not None
                or mail.digest_slot != prepared.digest_slot
                or mail.part <= prepared.last_part
                or rendered.date_at != mail.digest_slot
                or any(snapshot["intent"] is not None for snapshot in selected)
            ):
                raise MailError("mail_prepared_payload_invalid")
            key = canonical_sha256(
                dict(
                    kind=mail.kind,
                    installation_id=state["installation_id"],
                    recipient_key="primary",
                    slot=mail.digest_slot,
                    part=mail.part,
                )
            )
        else:
            raise MailError("mail_prepared_payload_invalid")
        if (
            mail.delivery_key != key
            or rendered.message_id != f"<signalnest.{key}@{state['sender'].rsplit('@', 1)[-1]}>"
            or any(snapshot["event"]["effective_route"] != mail.kind for snapshot in selected)
        ):
            raise MailError("mail_prepared_payload_invalid")
        mail_id = connection.execute(
            messages.insert().values(
                installation_id=state["installation_id"],
                immediate_intent_id=mail.immediate_intent_id,
                kind=mail.kind,
                delivery_key=mail.delivery_key,
                digest_slot=mail.digest_slot,
                part=mail.part,
                sender=rendered.sender,
                recipient=rendered.recipient,
                subject=rendered.subject,
                message_id=rendered.message_id,
                date_at=rendered.date_at,
                frozen_at=prepared.at,
                rendering_version=rendered.rendering_version,
                payload_bytes=rendered.payload,
                payload_sha256=rendered.payload_sha256,
                members_sha256=canonical_sha256(manifest),
                max_bytes=prepared.options.max_bytes,
                max_events=prepared.options.max_events,
            )
        ).inserted_primary_key[0]
        connection.execute(
            members.insert(),
            [
                dict(
                    event_id=m["event_id"],
                    decision_id=m["decision_id"],
                    mail_id=mail_id,
                    position=position,
                    snapshot=m,
                )
                for position, m in enumerate(manifest)
            ],
        )
        connection.execute(
            errors.delete().where(errors.c.event_id.in_([m["event_id"] for m in manifest]))
        )
        connection.execute(
            mail_delivery.insert().values(
                mail_id=mail_id,
                state="pending",
                attempt_count=0,
                manual_retry_pending=False,
                next_attempt_at=prepared.at,
                updated_at=prepared.at,
            )
        )
        ids.append(mail_id)
    for error in prepared.blocked:
        connection.execute(
            insert(errors)
            .values(
                event_id=error.event_id,
                decision_id=error.decision_id,
                rendering_version=RENDERING_VERSION,
                max_bytes=prepared.options.max_bytes,
                error_code=error.code,
                attempted_at=prepared.at,
            )
            .on_conflict_do_update(
                index_elements=[errors.c.event_id],
                set_=dict(
                    decision_id=error.decision_id,
                    rendering_version=RENDERING_VERSION,
                    max_bytes=prepared.options.max_bytes,
                    error_code=error.code,
                    attempted_at=prepared.at,
                ),
            )
        )
    return tuple(ids)


def plan_mail(engine: Engine, source_id: str, options: PlanOptions, *, at: int) -> dict:
    """Caller holds writer_lock. Stale preparations retry once without open write TX."""
    for attempt in range(2):
        prepared = prepare_plan(engine, source_id, options, at=at)
        try:
            with engine.begin() as connection:
                ids = commit_plan_in_transaction(connection, prepared)
                counts = _counts(connection, _channel(connection, source_id), at)
            return dict(
                planned=len(ids),
                mail_ids=list(ids),
                blocked=[dict(event_id=e.event_id, error_code=e.code) for e in prepared.blocked],
                candidate_limit_reached=prepared.candidate_limit_reached,
                at=at,
                **counts,
            )
        except MailError as exc:
            if exc.code != "mail_prepare_stale" or attempt:
                raise
        except SQLAlchemyError as exc:
            raise MailError("mail_database_write_failed") from exc
    raise AssertionError("bounded retry must return or raise")


def preview_plan(engine: Engine, source_id: str, options: PlanOptions, *, at: int) -> dict:
    prepared = prepare_plan(engine, source_id, options, at=at)
    return dict(
        preview_only=True,
        at=at,
        messages=[
            dict(
                kind=m.kind,
                digest_slot=m.digest_slot,
                part=m.part,
                delivery_key=m.delivery_key,
                subject=m.rendered.subject,
                sender=m.rendered.sender,
                recipient=m.rendered.recipient,
                message_id=m.rendered.message_id,
                date_at=m.rendered.date_at,
                rendering_version=m.rendered.rendering_version,
                byte_count=len(m.rendered.payload),
                payload_sha256=m.rendered.payload_sha256,
                body_text=m.rendered.body_text,
                event_ids=[event_id for event_id, _ in m.event_tokens],
            )
            for m in prepared.messages
        ],
        blocked=[dict(event_id=e.event_id, error_code=e.code) for e in prepared.blocked],
        candidate_limit_reached=prepared.candidate_limit_reached,
        **json.loads(prepared.counts_json),
    )


def load_frozen_mail(engine: Engine, source_id: str, mail_id: int) -> FrozenMessage:
    if type(mail_id) is not int or mail_id < 1:
        raise MailError("mail_id_invalid")
    try:
        with engine.connect() as connection:
            state = _channel(connection, source_id)
            row = _row(
                connection,
                messages,
                messages.c.id == mail_id,
                messages.c.installation_id == state["installation_id"],
            )
            if row is None:
                raise MailError("mail_not_found", mail_id=mail_id)
            member_rows = (
                connection.execute(
                    sa.select(members)
                    .where(members.c.mail_id == mail_id)
                    .order_by(members.c.position)
                )
                .mappings()
                .all()
            )
    except SQLAlchemyError as exc:
        raise MailError("mail_database_read_failed", mail_id=mail_id) from exc
    manifest = [r["snapshot"] for r in member_rows]
    if (
        not manifest
        or len(manifest) > row["max_events"]
        or [r["position"] for r in member_rows] != list(range(len(manifest)))
        or any(
            not isinstance(m, dict)
            or (r["event_id"], r["decision_id"]) != (m.get("event_id"), m.get("decision_id"))
            for r, m in zip(member_rows, manifest, strict=True)
        )
        or canonical_sha256(manifest) != row["members_sha256"]
    ):
        raise MailError("mail_members_corrupt", mail_id=mail_id)
    payload = row["payload_bytes"]
    if (
        len(payload) > row["max_bytes"]
        or hashlib.sha256(payload).hexdigest() != row["payload_sha256"]
    ):
        raise MailError("mail_payload_corrupt", mail_id=mail_id)
    try:
        message = BytesParser(policy=policy.SMTP).parsebytes(payload)
        if (
            message.is_multipart()
            or message.get_content_type() != "text/plain"
            or message.get_content_charset() != "utf-8"
            or len(message.get_all("Date", [])) != 1
            or message.defects
            or any(
                len(message.get_all(name, [])) != 1 or str(message[name]) != row[key]
                for name, key in (
                    ("From", "sender"),
                    ("To", "recipient"),
                    ("Subject", "subject"),
                    ("Message-ID", "message_id"),
                )
            )
            or int(parsedate_to_datetime(message["Date"]).timestamp()) != row["date_at"]
        ):
            raise MailError("mail_payload_corrupt", mail_id=mail_id)
        # Logical preview text uses LF, while frozen RFC 5322 bytes retain CRLF.
        body = message.get_content().replace("\r\n", "\n")
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise MailError("mail_payload_corrupt", mail_id=mail_id) from exc
    rendered = RenderedMail(
        row["sender"],
        row["recipient"],
        row["subject"],
        row["message_id"],
        row["date_at"],
        row["rendering_version"],
        body,
        payload,
        row["payload_sha256"],
    )
    return FrozenMessage(mail_id, row["kind"], rendered, canonical_json(manifest))


def preview_mail(engine: Engine, source_id: str, mail_id: int) -> dict:
    frozen = load_frozen_mail(engine, source_id, mail_id)
    rendered = frozen.rendered
    return dict(
        mail_id=frozen.mail_id,
        kind=frozen.kind,
        sender=rendered.sender,
        recipient=rendered.recipient,
        subject=rendered.subject,
        message_id=rendered.message_id,
        date_at=rendered.date_at,
        rendering_version=rendered.rendering_version,
        byte_count=len(rendered.payload),
        payload_sha256=rendered.payload_sha256,
        body_text=rendered.body_text,
        members=json.loads(frozen.members_json),
    )
