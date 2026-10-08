"""Serial frozen-mail delivery. Caller holds the instance writer lock for writes.

SMTP, frozen-payload validation and clock reads occur outside write transactions.
Local acceptance is durable only after finish_attempt commits. No SMTP exactly-once
claim can bridge acceptance and this commit.
"""

import logging
import time
from collections.abc import Callable

import sqlalchemy as sa
from pydantic import ConfigDict
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.config import MailSendingSettings, SmtpSettings
from signalnest.eventlog import Event, log_event
from signalnest.mail.contracts import FrozenMessage, MailError, SendOutcome, SendResult
from signalnest.mail.planning import load_frozen_mail
from signalnest.mail.smtp import send_frozen
from signalnest.notifications.state import channel_on, notification_time
from signalnest.schema import email_outbox, mail_message_members, mail_messages, notification_events
from signalnest.schema import mail_attempts as attempts
from signalnest.schema import mail_delivery as delivery
from signalnest.schema import notification_channel_state as channel

LOGGER = logging.getLogger("signalnest")


class DrainOptions(MailSendingSettings):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


def _time(at):
    try:
        notification_time(at)
    except RuntimeError as exc:
        raise MailError("mail_time_invalid") from exc


def _channel(connection: Connection, source_id: str) -> dict:
    state = channel_on(connection)
    if state is None:
        raise MailError("mail_not_enabled")
    if state["source_id"] != source_id:
        raise MailError("mail_source_mismatch")
    return state


def _mail(connection, state, mail_id):
    if type(mail_id) is not int or not 1 <= mail_id <= 2**63 - 1:
        raise MailError("mail_id_invalid")
    row = (
        connection.execute(
            sa.select(delivery)
            .join(mail_messages, mail_messages.c.id == delivery.c.mail_id)
            .where(
                delivery.c.mail_id == mail_id,
                mail_messages.c.installation_id == state["installation_id"],
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise MailError("mail_not_found", mail_id=mail_id)
    return dict(row)


def _last_attempt(connection, row):
    result = (
        connection.execute(
            sa.select(attempts).where(
                attempts.c.mail_id == row["mail_id"], attempts.c.attempt_no == row["attempt_count"]
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(result) if result is not None else None


def ensure_delivery_in_transaction(connection: Connection, source_id: str) -> None:
    """Also supports messages frozen by an older local client after migration."""
    state = _channel(connection, source_id)
    connection.execute(
        insert(delivery).from_select(
            [
                "mail_id",
                "state",
                "attempt_count",
                "manual_retry_pending",
                "next_attempt_at",
                "updated_at",
            ],
            sa.select(
                mail_messages.c.id,
                sa.literal("pending"),
                sa.literal(0),
                sa.literal(False),
                mail_messages.c.frozen_at,
                mail_messages.c.frozen_at,
            )
            .where(mail_messages.c.installation_id == state["installation_id"])
            .where(
                ~sa.exists(
                    sa.select(delivery.c.mail_id).where(delivery.c.mail_id == mail_messages.c.id)
                )
            ),
        )
    )


def _delay(options: DrainOptions, attempt_no: int, uncertain: bool) -> int:
    values = options.retry_delays_seconds
    delay = values[min(attempt_no - 1, len(values) - 1)] if values else 300
    return max(delay, options.uncertain_delay_seconds) if uncertain else delay


def _record_finish(
    connection,
    row,
    attempt,
    *,
    at,
    outcome,
    stage,
    error_code,
    smtp_code,
    scope,
    cleanup_failed,
    recovered,
    options,
):
    if at < attempt["started_at"]:
        raise MailError("mail_clock_regressed", mail_id=row["mail_id"])
    if outcome == "accepted":
        state, reason, due, accepted = "accepted", None, None, at
    elif outcome == "permanent":
        state, reason, due, accepted = "blocked", "permanent", None, None
    elif attempt["manual"] or row["attempt_count"] >= options.max_attempts:
        state = "blocked"
        reason = "manual_attempt_failed" if attempt["manual"] else "retry_exhausted"
        due, accepted = None, None
    else:
        state = "uncertain" if outcome == "uncertain" else "retry"
        reason, accepted = None, None
        due = at + _delay(options, row["attempt_count"], outcome == "uncertain")
        _time(due)
    connection.execute(
        attempts.update()
        .where(attempts.c.mail_id == row["mail_id"], attempts.c.attempt_no == row["attempt_count"])
        .values(
            finished_at=at,
            outcome=outcome,
            stage=stage,
            error_code=error_code,
            smtp_code=smtp_code,
            scope=scope,
            cleanup_failed=cleanup_failed,
            recovered=recovered,
            uncertain_until=at + options.uncertain_delay_seconds
            if outcome == "uncertain"
            else None,
        )
    )
    connection.execute(
        delivery.update()
        .where(delivery.c.mail_id == row["mail_id"])
        .values(
            state=state,
            next_attempt_at=due,
            accepted_at=accepted,
            updated_at=at,
            blocked_reason=reason,
            manual_retry_pending=False,
        )
    )
    if outcome == "permanent" and scope == "channel":
        connection.execute(
            channel.update()
            .where(channel.c.id == "primary")
            .values(
                paused=True,
                pause_reason=error_code,
                paused_at=at,
            )
        )
    return state


def recover_sending(engine: Engine, source_id: str, *, at: int, options: DrainOptions) -> int:
    """Recover unfinished attempts once, before considering due work. Caller holds lock."""
    _time(at)
    try:
        with engine.begin() as connection:
            state = _channel(connection, source_id)
            ensure_delivery_in_transaction(connection, source_id)
            stale = (
                connection.execute(
                    sa.select(delivery)
                    .join(mail_messages, mail_messages.c.id == delivery.c.mail_id)
                    .where(
                        mail_messages.c.installation_id == state["installation_id"],
                        delivery.c.state == "sending",
                    )
                    .order_by(delivery.c.mail_id)
                )
                .mappings()
                .all()
            )
            for item in stale:
                row = dict(item)
                attempt = _last_attempt(connection, row)
                if attempt is None or attempt["finished_at"] is not None:
                    raise MailError("mail_attempt_state_corrupt", mail_id=row["mail_id"])
                _record_finish(
                    connection,
                    row,
                    attempt,
                    at=at,
                    outcome="uncertain",
                    stage="unknown",
                    error_code="process_interrupted",
                    smtp_code=None,
                    scope="message",
                    cleanup_failed=False,
                    recovered=True,
                    options=options,
                )
            # Lowering an automatic budget must not strand one due item or stop
            # unrelated mail: block exhausted work without another network call.
            if (
                connection.execute(
                    sa.select(delivery.c.mail_id)
                    .where(
                        delivery.c.mail_id.in_(
                            sa.select(mail_messages.c.id).where(
                                mail_messages.c.installation_id == state["installation_id"]
                            )
                        ),
                        delivery.c.state.in_(["pending", "retry", "uncertain"]),
                        delivery.c.attempt_count >= options.max_attempts,
                        delivery.c.manual_retry_pending.is_(False),
                        delivery.c.updated_at > at,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                is not None
            ):
                raise MailError("mail_clock_regressed")
            connection.execute(
                delivery.update()
                .where(
                    delivery.c.mail_id.in_(
                        sa.select(mail_messages.c.id).where(
                            mail_messages.c.installation_id == state["installation_id"]
                        )
                    ),
                    delivery.c.state.in_(["pending", "retry", "uncertain"]),
                    delivery.c.attempt_count >= options.max_attempts,
                    delivery.c.manual_retry_pending.is_(False),
                )
                .values(
                    state="blocked",
                    blocked_reason="retry_exhausted",
                    next_attempt_at=None,
                    updated_at=at,
                )
            )
    except SQLAlchemyError as exc:
        raise MailError("mail_recovery_write_failed") from exc
    for row in stale:
        log_event(
            LOGGER,
            Event.MAIL_SEND_RECOVERED,
            source_id=source_id,
            mail_id=row["mail_id"],
            attempt_no=row["attempt_count"],
            stage="recovery",
            error_code="process_interrupted",
        )
    return len(stale)


def register_attempt(
    engine: Engine, source_id: str, mail_id: int, *, at: int, options: DrainOptions
) -> dict:
    """Commit sending + attempt before any network. Caller has validated frozen bytes."""
    _time(at)
    try:
        with engine.begin() as connection:
            state = _channel(connection, source_id)
            if state["paused"]:
                raise MailError("mail_channel_paused", mail_id=mail_id)
            row = _mail(connection, state, mail_id)
            if row["state"] not in {"pending", "retry", "uncertain"} or row["next_attempt_at"] > at:
                raise MailError("mail_not_due", mail_id=mail_id)
            if row["updated_at"] > at:
                raise MailError("mail_clock_regressed", mail_id=mail_id)
            if row["attempt_count"] >= options.max_attempts and not row["manual_retry_pending"]:
                raise MailError("mail_retry_exhausted", mail_id=mail_id)
            values = dict(
                mail_id=mail_id,
                attempt_no=row["attempt_count"] + 1,
                started_at=at,
                manual=row["manual_retry_pending"],
            )
            connection.execute(attempts.insert().values(**values))
            connection.execute(
                delivery.update()
                .where(delivery.c.mail_id == mail_id)
                .values(
                    state="sending",
                    attempt_count=values["attempt_no"],
                    manual_retry_pending=False,
                    next_attempt_at=None,
                    updated_at=at,
                )
            )
    except SQLAlchemyError as exc:
        raise MailError("mail_attempt_write_failed", mail_id=mail_id) from exc
    log_event(
        LOGGER,
        Event.MAIL_SEND_STARTED,
        source_id=source_id,
        mail_id=mail_id,
        attempt_no=values["attempt_no"],
        stage="sending",
    )
    return values


def finish_attempt(
    engine: Engine,
    source_id: str,
    mail_id: int,
    attempt_no: int,
    result: SendResult,
    *,
    at: int,
    options: DrainOptions,
) -> str:
    """Persist actual observation; write failure leaves sending for conservative recovery."""
    _time(at)
    if not isinstance(result, SendResult):
        raise TypeError("sender must return SendResult")
    try:
        with engine.begin() as connection:
            row = _mail(connection, _channel(connection, source_id), mail_id)
            attempt = _last_attempt(connection, row)
            if (
                row["state"] != "sending"
                or row["attempt_count"] != attempt_no
                or attempt is None
                or attempt["finished_at"] is not None
            ):
                raise MailError("mail_attempt_state_corrupt", mail_id=mail_id)
            state = _record_finish(
                connection,
                row,
                attempt,
                at=at,
                outcome=result.outcome.value,
                stage=result.stage.value,
                error_code=result.error_code.value if result.error_code else None,
                smtp_code=result.smtp_code,
                scope=result.scope,
                cleanup_failed=result.cleanup_failed,
                recovered=False,
                options=options,
            )
    except SQLAlchemyError as exc:
        raise MailError("mail_result_write_failed", mail_id=mail_id) from exc
    log_event(
        LOGGER,
        Event.MAIL_SEND_FINISHED,
        source_id=source_id,
        mail_id=mail_id,
        attempt_no=attempt_no,
        stage=result.stage.value,
        error_code=result.error_code.value if result.error_code else None,
    )
    return state


def _block_corrupt(engine, source_id, mail_id, at):
    _time(at)
    try:
        with engine.begin() as connection:
            row = _mail(connection, _channel(connection, source_id), mail_id)
            if row["updated_at"] > at:
                raise MailError("mail_clock_regressed", mail_id=mail_id)
            if row["state"] not in {"pending", "retry", "uncertain"}:
                raise MailError("mail_attempt_state_corrupt", mail_id=mail_id)
            connection.execute(
                delivery.update()
                .where(delivery.c.mail_id == mail_id)
                .values(
                    state="blocked",
                    blocked_reason="frozen_corrupt",
                    next_attempt_at=None,
                    manual_retry_pending=False,
                    updated_at=at,
                )
            )
    except SQLAlchemyError as exc:
        raise MailError("mail_result_write_failed", mail_id=mail_id) from exc


def _due_ids(engine, source_id, at, limit):
    try:
        with engine.connect() as connection:
            state = _channel(connection, source_id)
            if state["paused"]:
                return []
            return (
                connection.execute(
                    sa.select(delivery.c.mail_id)
                    .join(mail_messages, mail_messages.c.id == delivery.c.mail_id)
                    .where(
                        mail_messages.c.installation_id == state["installation_id"],
                        delivery.c.state.in_(["pending", "retry", "uncertain"]),
                        delivery.c.next_attempt_at <= at,
                    )
                    .order_by(delivery.c.next_attempt_at, delivery.c.mail_id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
    except SQLAlchemyError as exc:
        raise MailError("mail_database_read_failed") from exc


def drain_mail(
    engine: Engine,
    source_id: str,
    settings: SmtpSettings,
    options: DrainOptions,
    *,
    now: Callable[[], int] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sender: Callable[[FrozenMessage, SmtpSettings], SendResult] | None = None,
) -> dict:
    """One bounded serial pass. Caller holds writer lock across the entire operation."""
    now = now or (lambda: int(time.time()))
    sender = sender or send_frozen
    started_at, started_clock = now(), monotonic()
    _time(started_at)
    recovered = recover_sending(engine, source_id, at=started_at, options=options)
    candidates = _due_ids(engine, source_id, started_at, options.max_messages)
    counts = dict(
        attempted=0,
        accepted=0,
        retryable=0,
        uncertain=0,
        permanent=0,
        corrupt=0,
        recovered=recovered,
        budget_exhausted=False,
    )
    for mail_id in candidates:
        if monotonic() - started_clock >= options.run_seconds:
            counts["budget_exhausted"] = True
            break
        try:
            frozen = load_frozen_mail(engine, source_id, mail_id)
        except MailError as exc:
            if exc.code not in {"mail_payload_corrupt", "mail_members_corrupt"}:
                raise
            _block_corrupt(engine, source_id, mail_id, now())
            counts["corrupt"] += 1
            log_event(
                LOGGER,
                Event.PROCESSING_FAILED,
                source_id=source_id,
                mail_id=mail_id,
                stage="frozen_validation",
                error_code=exc.code,
            )
            continue
        # Reading/validating can consume time; do not register a new attempt after budget.
        if monotonic() - started_clock >= options.run_seconds:
            counts["budget_exhausted"] = True
            break
        attempt = register_attempt(engine, source_id, mail_id, at=now(), options=options)
        result = sender(frozen, settings)
        finish_attempt(
            engine, source_id, mail_id, attempt["attempt_no"], result, at=now(), options=options
        )
        counts["attempted"] += 1
        counts[result.outcome.value] += 1
        # A temporary channel failure stops this pass too; retry due persists on this mail.
        if result.scope == "channel" and result.outcome != SendOutcome.ACCEPTED:
            break
    status = mail_status(engine, source_id, at=now())
    return dict(
        **counts,
        status=status,
        paused=status["paused"],
        needs_attention=bool(
            status["paused"]
            or status["counts"]["blocked"]
            or status["uncertain_count"]
            or counts["retryable"]
            or counts["permanent"]
            or counts["corrupt"]
        ),
    )


def retry_mail(engine: Engine, source_id: str, mail_id: int, *, at: int) -> dict:
    """Grant one extra attempt to the same frozen message; never reset lifetime count."""
    _time(at)
    try:
        with engine.begin() as connection:
            row = _mail(connection, _channel(connection, source_id), mail_id)
            if row["state"] == "accepted":
                raise MailError("mail_already_accepted", mail_id=mail_id)
            if row["state"] == "sending":
                raise MailError("mail_recovery_required", mail_id=mail_id)
            if row["updated_at"] > at:
                raise MailError("mail_clock_regressed", mail_id=mail_id)
            if row["manual_retry_pending"]:
                return dict(
                    mail_id=mail_id,
                    manual_retry_pending=True,
                    next_attempt_at=row["next_attempt_at"],
                    already_granted=True,
                )
            if row["state"] != "blocked":
                raise MailError("mail_manual_retry_requires_blocked", mail_id=mail_id)
            last = _last_attempt(connection, row)
            due = at
            if last and last["outcome"] == "uncertain":
                # Respect the originally configured safety interval even across config changes.
                due = max(due, last["uncertain_until"])
            connection.execute(
                delivery.update()
                .where(delivery.c.mail_id == mail_id)
                .values(
                    state="pending",
                    blocked_reason=None,
                    next_attempt_at=due,
                    manual_retry_pending=True,
                    updated_at=at,
                )
            )
    except SQLAlchemyError as exc:
        raise MailError("mail_retry_write_failed", mail_id=mail_id) from exc
    return dict(
        mail_id=mail_id, manual_retry_pending=True, next_attempt_at=due, already_granted=False
    )


def set_sending_paused(engine: Engine, source_id: str, paused: bool, *, at: int) -> dict:
    _time(at)
    if type(paused) is not bool:
        raise MailError("mail_pause_invalid")
    try:
        with engine.begin() as connection:
            state = _channel(connection, source_id)
            if state["paused_at"] is not None and state["paused_at"] > at:
                raise MailError("mail_clock_regressed")
            connection.execute(
                channel.update()
                .where(channel.c.id == "primary")
                .values(
                    paused=paused,
                    pause_reason="manual" if paused else None,
                    paused_at=at if paused else None,
                )
            )
    except SQLAlchemyError as exc:
        raise MailError("mail_pause_write_failed") from exc
    return dict(paused=paused, pause_reason="manual" if paused else None, at=at)


def mail_status(engine: Engine, source_id: str, *, at: int) -> dict:
    """Read only: even legacy missing delivery rows are reported as pending, not written."""
    _time(at)
    try:
        with engine.connect() as connection:
            state = _channel(connection, source_id)
            # No payload/body/address/credential read is needed for diagnostics.
            status_query = (
                sa.select(
                    mail_messages.c.id,
                    mail_messages.c.frozen_at,
                    delivery,
                    attempts.c.outcome,
                    attempts.c.stage,
                    attempts.c.error_code,
                    attempts.c.smtp_code,
                    attempts.c.finished_at,
                    attempts.c.recovered,
                    attempts.c.cleanup_failed,
                )
                .select_from(
                    mail_messages.outerjoin(
                        delivery, delivery.c.mail_id == mail_messages.c.id
                    ).outerjoin(
                        attempts,
                        sa.and_(
                            attempts.c.mail_id == delivery.c.mail_id,
                            attempts.c.attempt_no == delivery.c.attempt_count,
                        ),
                    )
                )
                .where(mail_messages.c.installation_id == state["installation_id"])
            )
            snapshot = status_query.subquery()
            status_column = sa.func.coalesce(snapshot.c.state, "pending")
            due_column = sa.case(
                (snapshot.c.mail_id.is_(None), snapshot.c.frozen_at),
                else_=snapshot.c.next_attempt_at,
            )
            counts = dict.fromkeys(
                ("pending", "sending", "retry", "uncertain", "accepted", "blocked"), 0
            )
            for state_name, count in connection.execute(
                sa.select(status_column, sa.func.count()).group_by(status_column)
            ):
                counts[state_name] = count
            due_count, unknown, oldest = connection.execute(
                sa.select(
                    sa.func.coalesce(sa.func.sum(sa.case((due_column <= at, 1), else_=0)), 0),
                    sa.func.coalesce(
                        sa.func.sum(
                            sa.case(
                                (
                                    sa.or_(
                                        status_column == "sending",
                                        snapshot.c.outcome == "uncertain",
                                    ),
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    sa.func.min(
                        sa.case((status_column != "accepted", snapshot.c.frozen_at), else_=None)
                    ),
                )
            ).one()
            uncertain_attempt_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(attempts)
                .join(mail_messages, mail_messages.c.id == attempts.c.mail_id)
                .where(
                    mail_messages.c.installation_id == state["installation_id"],
                    attempts.c.outcome == "uncertain",
                )
            ).scalar_one()
            rows = (
                connection.execute(
                    status_query.order_by(
                        sa.case((delivery.c.state == "accepted", 1), else_=0), mail_messages.c.id
                    ).limit(100)
                )
                .mappings()
                .all()
            )
            intent_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(email_outbox)
                .join(notification_events, email_outbox.c.event_id == notification_events.c.id)
                .where(
                    notification_events.c.installation_id == state["installation_id"],
                    ~sa.exists(
                        sa.select(mail_messages.c.id).where(
                            mail_messages.c.immediate_intent_id == email_outbox.c.id
                        )
                    ),
                )
            ).scalar_one()
            digest_count = connection.execute(
                sa.select(sa.func.count())
                .select_from(notification_events)
                .where(
                    notification_events.c.installation_id == state["installation_id"],
                    notification_events.c.effective_route == "digest",
                    ~sa.exists(
                        sa.select(mail_message_members.c.event_id).where(
                            mail_message_members.c.event_id == notification_events.c.id
                        )
                    ),
                )
            ).scalar_one()
    except SQLAlchemyError as exc:
        raise MailError("mail_database_read_failed") from exc
    diagnostics = []
    for row in rows:
        status = row["state"] or "pending"
        due = row["next_attempt_at"] if row["state"] is not None else row["frozen_at"]
        diagnostics.append(
            dict(
                mail_id=row["id"],
                state=status,
                attempt_count=row["attempt_count"] or 0,
                next_attempt_at=due,
                accepted_at=row["accepted_at"],
                blocked_reason=row["blocked_reason"],
                manual_retry_pending=bool(row["manual_retry_pending"]),
                outcome=row["outcome"],
                stage=row["stage"],
                error_code=row["error_code"],
                smtp_code=row["smtp_code"],
                finished_at=row["finished_at"],
                recovered=bool(row["recovered"]),
                cleanup_failed=bool(row["cleanup_failed"]),
            )
        )
    return dict(
        at=at,
        paused=state["paused"],
        pause_reason=state["pause_reason"],
        paused_at=state["paused_at"],
        counts=counts,
        due_count=due_count,
        uncertain_count=unknown,
        uncertain_attempt_count=uncertain_attempt_count,
        messages_truncated=sum(counts.values()) > len(rows),
        oldest_pending_at=oldest,
        unplanned_immediate=intent_count,
        unallocated_digest=digest_count,
        messages=diagnostics,
    )
