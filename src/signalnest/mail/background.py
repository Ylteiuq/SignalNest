"""Explicit bounded background passes. External timers, not Python threads, repeat them."""

import logging
import time
from collections.abc import Callable
from uuid import uuid4

import httpx
from sqlalchemy.exc import SQLAlchemyError

from signalnest.config import Settings, SmtpSettings
from signalnest.crawling import CrawlOptions, crawl_once
from signalnest.eventlog import Event, log_event
from signalnest.fetching import Clock, FetchLimits
from signalnest.instance_lock import writer_lock
from signalnest.mail.contracts import FrozenMessage, MailError, PlanOptions, SendResult
from signalnest.mail.planning import plan_mail
from signalnest.mail.sending import DrainOptions, drain_mail
from signalnest.storage import open_initialized_engine

LOGGER = logging.getLogger("signalnest")


def run_mail_pass(
    settings: Settings,
    *,
    run_id: str | None = None,
    now: Callable[[], int] | None = None,
    sender: Callable[[FrozenMessage, SmtpSettings], SendResult] | None = None,
) -> dict:
    """Plan then send under one instance lock. System errors never fall through to SMTP.

    Pausing sends does not pause local planning. A per-item rendering block is a
    durable ordinary outcome, so already frozen mail can still advance. Credentials
    are read only by the SMTP adapter when an actual due attempt begins.
    """
    settings = settings.model_copy(deep=True)
    if not settings.mail_runtime.enabled:
        return dict(enabled=False, skipped="mail_disabled", needs_attention=False)
    if settings.smtp is None:
        raise MailError("mail_smtp_not_configured")
    now = now or (lambda: int(time.time()))
    run_id = run_id or uuid4().hex
    with writer_lock(settings.storage.database):
        engine = open_initialized_engine(settings.storage.database)
        try:
            log_event(
                LOGGER,
                Event.MAIL_BACKGROUND_STARTED,
                source_id=settings.source.id,
                run_id=run_id,
                stage="mail_plan",
            )
            try:
                planned = plan_mail(
                    engine,
                    settings.source.id,
                    PlanOptions(
                        max_messages=settings.mail_runtime.max_plan_messages,
                        max_events=settings.mail_runtime.max_events,
                        max_bytes=settings.mail_runtime.max_bytes,
                    ),
                    at=now(),
                )
            except MailError as exc:
                log_event(
                    LOGGER,
                    Event.PROCESSING_FAILED,
                    level=logging.ERROR,
                    source_id=settings.source.id,
                    run_id=run_id,
                    stage="mail_plan",
                    error_code=exc.code,
                )
                raise
            log_event(
                LOGGER,
                Event.MAIL_PLANNED,
                source_id=settings.source.id,
                run_id=run_id,
                stage="mail_plan",
                failed=planned["blocked_count"],
                error_code="mail_render_blocked" if planned["blocked_count"] else None,
            )
            try:
                sent = drain_mail(
                    engine,
                    settings.source.id,
                    settings.smtp,
                    DrainOptions.model_validate(settings.mail_sending.model_dump()),
                    now=now,
                    sender=sender,
                )
            except MailError as exc:
                log_event(
                    LOGGER,
                    Event.PROCESSING_FAILED,
                    level=logging.ERROR,
                    source_id=settings.source.id,
                    run_id=run_id,
                    stage="mail_send",
                    error_code=exc.code,
                )
                raise
        except SQLAlchemyError as exc:
            log_event(
                LOGGER,
                Event.PROCESSING_FAILED,
                level=logging.ERROR,
                source_id=settings.source.id,
                run_id=run_id,
                stage="mail_storage",
                error_code="mail_database_unavailable",
            )
            raise MailError("mail_database_unavailable") from exc
        finally:
            engine.dispose()
    attention = bool(planned["blocked_count"] or sent["needs_attention"])
    log_event(
        LOGGER,
        Event.MAIL_BACKGROUND_FINISHED,
        source_id=settings.source.id,
        run_id=run_id,
        stage="mail_finish",
        attempted=sent["attempted"],
        succeeded=sent["accepted"],
        failed=sent["attempted"] - sent["accepted"],
        error_code="mail_attention_required" if attention else None,
    )
    return dict(
        enabled=True, run_id=run_id, planning=planned, sending=sent, needs_attention=attention
    )


def run_scheduled_cycle(
    settings: Settings,
    mode: str,
    *,
    run_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Clock = time,
    sender: Callable[[FrozenMessage, SmtpSettings], SendResult] | None = None,
) -> dict:
    """Ordinary persisted crawl outcomes continue mail; thrown system errors stop here.

    Collection owns and releases its lock before the mail pass acquires the same
    lock. A competing writer in this gap rejects this pass; the independent mail
    timer can retry later. There is no inner loop, second network retry, or daemon.
    """
    if mode not in {"regular", "full"}:
        raise ValueError("unsupported scheduled mode")
    settings = settings.model_copy(deep=True)
    run_id = run_id or uuid4().hex
    budget = getattr(settings.runtime, mode)
    options = CrawlOptions(
        scan_mode="limited" if mode == "regular" else "full",
        max_pages=budget.max_pages,
        max_details=budget.max_details,
        fetch_limits=FetchLimits(
            max_requests=budget.max_requests,
            run_seconds=budget.run_seconds,
            resource_seconds=budget.resource_seconds,
            max_body_bytes=budget.max_body_bytes,
        ),
    )
    # crawl_once returns ordinary HTTP/Parser/coverage failures only after saving
    # their facts. DB, evidence and finalization faults propagate and skip mail.
    collected = crawl_once(settings, options, transport=transport, clock=clock, run_id=run_id)
    mailed = run_mail_pass(settings, run_id=run_id, now=lambda: int(clock.time()), sender=sender)
    return dict(
        run_id=run_id,
        collection=collected.model_dump(mode="json"),
        mail=mailed,
        needs_attention=collected.result != "succeeded" or mailed["needs_attention"],
    )
