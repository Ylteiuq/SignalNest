"""Opt-in standard-library JSON logging with a small, safe field allowlist."""

import json
import logging
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import TextIO


class Event(StrEnum):
    CONFIG_VALIDATED = "config_validated"
    CONFIG_INVALID = "config_invalid"
    STORAGE_INITIALIZED = "storage_initialized"
    STORAGE_INIT_FAILED = "storage_init_failed"
    RAW_ARCHIVED = "raw_archived"
    RESPONSE_RECORDED = "response_recorded"
    PAGE_PROCESSED = "page_processed"
    PROCESSING_FAILED = "processing_failed"
    FETCH_STARTED = "fetch_started"
    FETCH_RETRIED = "fetch_retried"
    FETCH_FINISHED = "fetch_finished"
    CRAWL_STARTED = "crawl_started"
    CRAWL_FINISHED = "crawl_finished"
    DETAIL_GROUP_FINISHED = "detail_group_finished"
    STATUS_READ = "status_read"
    POLICY_APPLIED = "policy_applied"


_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")


def _identifier(value: object) -> str | int | None:
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
        return value
    if type(value) is int and value >= 0:
        return value
    return None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never format arbitrary messages, arguments, exceptions, URLs, or HTML bodies.
        event = record.msg.value if isinstance(record.msg, Event) else "unstructured_log"
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": event,
        }
        for key in ("source_id", "run_id", "document_id", "response_id", "stage", "error_code"):
            value = _identifier(getattr(record, key, None))
            if value is not None:
                payload[key] = value
        for key in (
            "attempted",
            "succeeded",
            "failed",
            "remaining_due",
            "unserved",
            "oldest_overdue_seconds",
        ):
            value = getattr(record, key, None)
            if type(value) is int and value >= 0:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(stream: TextIO | None = None) -> logging.Logger:
    """Configure only SignalNest's logger, explicitly at the CLI boundary, never on import."""
    logger = logging.getLogger("signalnest")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    event: Event,
    *,
    level: int = logging.INFO,
    source_id: str | None = None,
    run_id: str | None = None,
    document_id: int | None = None,
    response_id: int | None = None,
    stage: str | None = None,
    error_code: str | None = None,
    attempted: int | None = None,
    succeeded: int | None = None,
    failed: int | None = None,
    remaining_due: int | None = None,
    unserved: int | None = None,
    oldest_overdue_seconds: int | None = None,
) -> None:
    """Context is identifiers only; never pass configuration values or page contents."""
    if not isinstance(event, Event):
        raise TypeError("event must be an Event")
    logger.log(
        level,
        event,
        extra={
            "source_id": source_id,
            "run_id": run_id,
            "document_id": document_id,
            "response_id": response_id,
            "stage": stage,
            "error_code": error_code,
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": failed,
            "remaining_due": remaining_due,
            "unserved": unserved,
            "oldest_overdue_seconds": oldest_overdue_seconds,
        },
    )
