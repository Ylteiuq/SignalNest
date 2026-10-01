"""Offline business operations reusable by a future synchronous HTTP coordinator.

File I/O and parsing always finish outside database transactions. Each new import
records new evidence; replay uses the same evidence. Only versions are idempotent.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import sqlalchemy as sa
from pydantic import Field, ValidationError, model_validator
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.contracts import (
    Contract,
    ListPage,
    NonemptyText,
    PageInput,
    ParsedNotice,
    RawResponseReference,
    WebUrl,
)
from signalnest.eventlog import Event, log_event
from signalnest.parsing import ParseError, parse_list, parse_notice
from signalnest.rawstore import RawStore, RawStoreError
from signalnest.schema import documents, notice_versions, raw_responses


class ResponseInput(Contract):
    """Explicit provenance for already obtained bytes; fetched_at has no default."""

    page_type: Literal["list", "notice"]
    source_id: NonemptyText
    source_document_id: NonemptyText | None = None
    requested_url: WebUrl
    final_url: WebUrl
    fetched_at: int = Field(ge=0, strict=True)
    status_code: int = Field(ge=100, le=599, strict=True)
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None

    @model_validator(mode="after")
    def target(self):
        if (self.page_type == "notice") != (self.source_document_id is not None):
            raise ValueError("notice requires source_document_id; list must not have one")
        return self


class IngestError(RuntimeError):
    def __init__(
        self,
        code: str,
        stage: str,
        *,
        response_id: int | None = None,
        document_id: int | None = None,
    ):
        self.code = code
        self.stage = stage
        self.response_id = response_id
        self.document_id = document_id
        super().__init__(f"{stage}: {code} (response_id={response_id}, document_id={document_id})")

    def __str__(self) -> str:
        return (
            f"{self.stage}: {self.code} "
            f"(response_id={self.response_id}, document_id={self.document_id})"
        )


@dataclass(frozen=True)
class ProcessingResult:
    response_id: int
    outcome: Literal["processed", "evidence_only"] = "processed"
    document_id: int | None = None
    version_id: int | None = None
    discovered_count: int = 0
    next_page_url: str | None = None


def _time(value: int) -> None:
    if type(value) is not int or value < 0:
        raise IngestError("invalid_processing_time", "validation")


def _attempt_time(row, processed_at: int) -> None:
    _time(processed_at)
    if row["last_attempt_at"] is not None and processed_at < row["last_attempt_at"]:
        raise IngestError("stale_processing_time", "validation", document_id=row["id"])


def _target(engine: Engine, evidence: ResponseInput) -> int | None:
    if evidence.page_type == "list":
        return None
    with engine.connect() as connection:
        document_id = connection.execute(
            sa.select(documents.c.id).where(
                documents.c.source_id == evidence.source_id,
                documents.c.source_document_id == evidence.source_document_id,
            )
        ).scalar_one_or_none()
    if document_id is None:
        raise IngestError("target_not_discovered", "validation")
    return document_id


def discover_page(
    engine: Engine,
    source_id: str,
    page: ListPage,
    discovered_at: int,
    *,
    response_id: int | None = None,
) -> int:
    """Atomic per page; rediscovery refreshes URL/title but preserves all state/times."""
    _time(discovered_at)
    if not source_id.strip():
        raise IngestError("invalid_source", "validation")
    try:
        with engine.begin() as connection:
            if response_id is not None:
                response = (
                    connection.execute(
                        sa.select(raw_responses).where(raw_responses.c.id == response_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    response is None
                    or response["page_type"] != "list"
                    or response["source_id"] != source_id
                    or response["status_code"] != 200
                    or response["body_path"] is None
                ):
                    raise IngestError("response_not_list", "validation", response_id=response_id)
                if discovered_at < response["fetched_at"] or (
                    response["last_attempt_at"] is not None
                    and discovered_at < response["last_attempt_at"]
                ):
                    raise IngestError(
                        "stale_processing_time", "validation", response_id=response_id
                    )
            for entry in page.entries:
                statement = insert(documents).values(
                    source_id=source_id,
                    source_document_id=entry.source_document_id,
                    detail_url=str(entry.detail_url),
                    discovered_title=entry.title,
                    discovered_at=discovered_at,
                )
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=[documents.c.source_id, documents.c.source_document_id],
                        set_={
                            "detail_url": statement.excluded.detail_url,
                            "discovered_title": statement.excluded.discovered_title,
                        },
                    )
                )
            if response_id is not None:
                connection.execute(
                    raw_responses.update()
                    .where(raw_responses.c.id == response_id)
                    .values(last_attempt_at=discovered_at, last_error_code=None)
                )
    except SQLAlchemyError as exc:
        raise IngestError("database_write_failed", "discovery") from exc
    return len(page.entries)


def record_response(
    engine: Engine,
    raw_store: RawStore,
    evidence: ResponseInput,
    content: bytes | None,
    *,
    run_id: str | None = None,
) -> int:
    """Publish/verify body, then commit a fresh response row. No parse success implied."""
    document_id = None
    try:
        document_id = _target(engine, evidence)
        if evidence.status_code == 304 and content is not None:
            raise IngestError("304_has_body", "validation", document_id=document_id)
        if evidence.status_code != 304 and content is None:
            raise IngestError("body_required", "validation", document_id=document_id)
        body = raw_store.archive(content) if content is not None else None
        if body is not None:
            log_event(
                logging.getLogger("signalnest"),
                Event.RAW_ARCHIVED,
                source_id=evidence.source_id,
                document_id=document_id,
                run_id=run_id,
                stage="archive",
            )
        reference = RawResponseReference(
            **evidence.model_dump(exclude={"page_type", "source_document_id"}),
            body_path=body.path if body else None,
            body_sha256=body.sha256 if body else None,
        )
        with engine.begin() as connection:
            response_id = connection.execute(
                raw_responses.insert().values(
                    **reference.model_dump(mode="json"),
                    page_type=evidence.page_type,
                    document_id=document_id,
                )
            ).inserted_primary_key[0]
    except RawStoreError as exc:
        raise IngestError(exc.code, "archive", document_id=document_id) from exc
    except SQLAlchemyError as exc:
        raise IngestError("database_write_failed", "evidence", document_id=document_id) from exc
    log_event(
        logging.getLogger("signalnest"),
        Event.RESPONSE_RECORDED,
        source_id=evidence.source_id,
        document_id=document_id,
        response_id=response_id,
        run_id=run_id,
        stage="evidence",
    )
    return response_id


def _response(engine: Engine, response_id: int):
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(raw_responses).where(raw_responses.c.id == response_id)
                )
                .mappings()
                .one_or_none()
            )
    except SQLAlchemyError as exc:
        raise IngestError("database_read_failed", "evidence", response_id=response_id) from exc
    if row is None:
        raise IngestError("response_missing", "evidence", response_id=response_id)
    return row


def _failure(engine: Engine, error: IngestError, processed_at: int) -> None:
    """A separate short failure transaction; never touches the previous success."""
    try:
        with engine.begin() as connection:
            if error.response_id is not None:
                connection.execute(
                    raw_responses.update()
                    .where(raw_responses.c.id == error.response_id)
                    .values(last_attempt_at=processed_at, last_error_code=error.code)
                )
            if error.document_id is not None:
                row = (
                    connection.execute(
                        sa.select(documents).where(documents.c.id == error.document_id)
                    )
                    .mappings()
                    .one()
                )
                _attempt_time(row, processed_at)
                connection.execute(
                    documents.update()
                    .where(documents.c.id == error.document_id)
                    .values(
                        status="failed",
                        last_attempt_at=processed_at,
                        last_error_code=error.code,
                    )
                )
    except SQLAlchemyError as exc:
        raise IngestError(
            "failure_state_unavailable",
            "failure_record",
            response_id=error.response_id,
            document_id=error.document_id,
        ) from exc


def save_notice(engine: Engine, response_id: int, notice: ParsedNotice, processed_at: int) -> int:
    """Commit the idempotent version and success pointer together, or neither."""
    _time(processed_at)
    response = _response(engine, response_id)
    document_id = response["document_id"]
    if response["page_type"] != "notice" or document_id is None:
        raise IngestError("response_not_notice", "validation", response_id=response_id)
    if response["status_code"] != 200 or response["body_path"] is None:
        raise IngestError("response_not_valid_html", "validation", response_id=response_id)
    if processed_at < response["fetched_at"]:
        raise IngestError("processing_before_fetch", "validation", response_id=response_id)
    digest = notice.content.content_sha256()
    normalized_content = notice.content.model_dump(mode="json")
    try:
        with engine.begin() as connection:
            target = (
                connection.execute(sa.select(documents).where(documents.c.id == document_id))
                .mappings()
                .one()
            )
            _attempt_time(target, processed_at)
            if (
                notice.source_document_id != target["source_document_id"]
                or response["source_id"] != target["source_id"]
                or str(notice.page_url) != response["final_url"]
            ):
                raise IngestError(
                    "identity_mismatch",
                    "validation",
                    response_id=response_id,
                    document_id=document_id,
                )
            statement = (
                insert(notice_versions)
                .values(
                    document_id=document_id,
                    raw_response_id=response_id,
                    content_sha256=digest,
                    parser_version=notice.parser_version,
                    parsed_at=processed_at,
                    title=notice.content.title,
                    published_date=notice.content.published_date,
                    normalized_content=normalized_content,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        notice_versions.c.document_id,
                        notice_versions.c.content_sha256,
                        notice_versions.c.parser_version,
                    ]
                )
            )
            connection.execute(statement)
            version_id = connection.execute(
                sa.select(notice_versions.c.id).where(
                    notice_versions.c.document_id == document_id,
                    notice_versions.c.content_sha256 == digest,
                    notice_versions.c.parser_version == notice.parser_version,
                )
            ).scalar_one()
            connection.execute(
                documents.update()
                .where(documents.c.id == document_id)
                .values(
                    status="processed",
                    current_version_id=version_id,
                    last_attempt_at=processed_at,
                    last_success_at=processed_at,
                    last_error_code=None,
                )
            )
            connection.execute(
                raw_responses.update()
                .where(raw_responses.c.id == response_id)
                .values(last_attempt_at=processed_at, last_error_code=None)
            )
    except SQLAlchemyError as exc:
        raise IngestError(
            "database_write_failed",
            "success_commit",
            response_id=response_id,
            document_id=document_id,
        ) from exc
    return version_id


def process_response(
    engine: Engine,
    raw_store: RawStore,
    response_id: int,
    processed_at: int,
    *,
    notice_parser: Callable[[PageInput], ParsedNotice] = parse_notice,
    list_parser: Callable[[PageInput], ListPage] = parse_list,
    expected_source_id: str | None = None,
    run_id: str | None = None,
) -> ProcessingResult:
    """Replay existing evidence with current rules; no checksum shortcuts or new fetch."""
    _time(processed_at)
    response = _response(engine, response_id)
    document_id = response["document_id"]
    if expected_source_id is not None and response["source_id"] != expected_source_id:
        raise IngestError("source_mismatch", "validation", response_id=response_id)
    if response["page_type"] is None:
        raise IngestError("legacy_response_type_unknown", "validation", response_id=response_id)
    if processed_at < response["fetched_at"]:
        raise IngestError("processing_before_fetch", "validation", response_id=response_id)
    if response["last_attempt_at"] is not None and processed_at < response["last_attempt_at"]:
        raise IngestError("stale_processing_time", "validation", response_id=response_id)
    if document_id is not None:
        with engine.connect() as connection:
            target = (
                connection.execute(sa.select(documents).where(documents.c.id == document_id))
                .mappings()
                .one()
            )
            _attempt_time(target, processed_at)
    error = None
    try:
        if response["body_path"] is None:
            raise IngestError("response_has_no_body", "archive")
        if response["status_code"] != 200:
            raise IngestError("http_status_not_200", "validation")
        content = raw_store.read(response["body_path"], response["body_sha256"])
        if not content:
            raise IngestError("parse_empty_page", "parse")
        page = PageInput(content=content, page_url=response["final_url"])
        if response["page_type"] == "list":
            listing = list_parser(page)
            count = discover_page(
                engine, response["source_id"], listing, processed_at, response_id=response_id
            )
            result = ProcessingResult(
                response_id,
                discovered_count=count,
                next_page_url=str(listing.next_page_url) if listing.next_page_url else None,
            )
        else:
            notice = notice_parser(page)
            version_id = save_notice(engine, response_id, notice, processed_at)
            result = ProcessingResult(response_id, document_id=document_id, version_id=version_id)
    except ParseError as exc:
        error = IngestError(f"parse_{exc.code}", "parse")
        error.__cause__ = exc
    except RawStoreError as exc:
        error = IngestError(exc.code, "archive")
        error.__cause__ = exc
    except ValidationError as exc:
        error = IngestError("invalid_response_metadata", "validation")
        error.__cause__ = exc
    except IngestError as exc:
        error = exc
    if error is not None:
        error.response_id, error.document_id = response_id, document_id
        log_event(
            logging.getLogger("signalnest"),
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=response["source_id"],
            response_id=response_id,
            document_id=document_id,
            stage=error.stage,
            error_code=error.code,
            run_id=run_id,
        )
        _failure(engine, error, processed_at)
        raise error
    log_event(
        logging.getLogger("signalnest"),
        Event.PAGE_PROCESSED,
        source_id=response["source_id"],
        response_id=response_id,
        document_id=document_id,
        stage="success_commit",
        run_id=run_id,
    )
    return result


def import_page(
    engine: Engine,
    raw_store: RawStore,
    evidence: ResponseInput,
    content: bytes | None,
    processed_at: int,
    *,
    run_id: str | None = None,
) -> ProcessingResult:
    """Import an explicit observation, preserving its supplied fetch timestamp."""
    _time(processed_at)
    if processed_at < evidence.fetched_at:
        raise IngestError("processing_before_fetch", "validation")
    try:
        response_id = record_response(engine, raw_store, evidence, content, run_id=run_id)
    except IngestError as exc:
        if exc.stage in {"archive", "evidence"} and exc.document_id is not None:
            log_event(
                logging.getLogger("signalnest"),
                Event.PROCESSING_FAILED,
                level=logging.ERROR,
                source_id=evidence.source_id,
                document_id=exc.document_id,
                stage=exc.stage,
                error_code=exc.code,
                run_id=run_id,
            )
            _failure(engine, exc, processed_at)
        raise
    if evidence.status_code == 304:
        return ProcessingResult(response_id, outcome="evidence_only")
    return process_response(engine, raw_store, response_id, processed_at, run_id=run_id)
