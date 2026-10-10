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
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.cache import (
    CacheCandidate,
    request_uri,
    reuse_reason,
    validate_binding,
    validation_reason,
)
from signalnest.contracts import (
    REFERENCE_NORMALIZATION_VERSION,
    Contract,
    ListPage,
    NonemptyText,
    PageInput,
    PaginationEvidence,
    ParsedNotice,
    RawResponseReference,
    RequestProfile,
    WebUrl,
)
from signalnest.errors import IngestError, validate_error_code, validate_time
from signalnest.eventlog import Event, log_event
from signalnest.ingestion_state import (
    Origin,
    advance_source,
    discovery_context,
    validate_processing_run,
)
from signalnest.notifications.service import (
    PreparedNotification,
    commit_notification_in_transaction,
    prepare_notification,
)
from signalnest.notifications.state import (
    ProcessingOrigin,
    check_origin,
    register_listing_evidence_in_transaction,
)
from signalnest.parsing import PARSER_VERSION, ParseError, parse_list, parse_notice
from signalnest.rawstore import RawStore, RawStoreError
from signalnest.schema import (
    discovered_references,
    documents,
    http_resources,
    notice_versions,
    raw_responses,
)


class ResponseInput(Contract):
    """Explicit provenance for already obtained bytes; fetched_at has no default."""

    page_type: Literal["list", "notice"]
    source_id: NonemptyText
    source_document_id: NonemptyText | None = None
    requested_url: WebUrl
    final_url: WebUrl
    fetched_at: int = Field(ge=0, le=2**63 - 1, strict=True)
    status_code: int = Field(ge=100, le=599, strict=True)
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    request_profile: RequestProfile | None = None
    body_state: Literal["complete", "unavailable"] | None = None
    vary: str | None = None
    cache_control: str | None = None
    content_encoding: str | None = None

    @model_validator(mode="after")
    def target(self):
        if (self.page_type == "notice") != (self.source_document_id is not None):
            raise ValueError("notice requires source_document_id; list must not have one")
        if self.request_profile is not None:
            try:
                request_uri(str(self.requested_url))
            except IngestError:
                raise ValueError(
                    "request URI cannot contain a fragment or unsupported syntax"
                ) from None
        return self


@dataclass(frozen=True)
class ProcessingResult:
    response_id: int
    outcome: Literal["processed", "evidence_only", "full_fetch_required"] = "processed"
    document_id: int | None = None
    version_id: int | None = None
    discovered_count: int = 0
    reference_count: int = 0
    registered_row_count: int = 0
    next_page_url: str | None = None
    pagination: PaginationEvidence | None = None
    body_response_id: int | None = None
    error_code: str | None = None


def _time(value: int) -> None:
    validate_time(value)


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


def _response_on(connection: Connection, response_id: int):
    row = (
        connection.execute(sa.select(raw_responses).where(raw_responses.c.id == response_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise IngestError("response_missing", "evidence", response_id=response_id)
    return row


def _observation(connection: Connection, body, observed_response_id: int | None, at: int):
    observed = (
        body if observed_response_id is None else _response_on(connection, observed_response_id)
    )
    if observed["id"] != body["id"] and (
        observed["status_code"] != 304
        or observed["validated_response_id"] != body["id"]
        or any(
            observed[field] != body[field]
            for field in (
                "resource_id",
                "source_id",
                "requested_url",
                "final_url",
                "page_type",
                "document_id",
            )
        )
    ):
        raise IngestError("cache_binding_invalid", "validation")
    for row in (body, observed):
        if at < row["fetched_at"] or (
            row["last_attempt_at"] is not None and at < row["last_attempt_at"]
        ):
            raise IngestError("stale_processing_time", "validation", response_id=row["id"])
    return observed


def _processing_marks(connection: Connection, body, observed, at: int, parser_version: str):
    if not parser_version.strip():
        raise IngestError("invalid_parser_version", "validation")
    connection.execute(
        raw_responses.update()
        .where(raw_responses.c.id.in_({body["id"], observed["id"]}))
        .values(last_attempt_at=at, last_error_code=None)
    )
    if body["resource_id"] is not None:
        # An explicit historical replay must not promote an old transport baseline.
        connection.execute(
            http_resources.update()
            .where(
                http_resources.c.id == body["resource_id"],
                http_resources.c.latest_response_id == body["id"],
            )
            .values(
                last_processed_response_id=body["id"],
                last_processed_parser_version=parser_version,
                last_processed_at=at,
            )
        )


def discover_page_in_transaction(
    connection: Connection,
    source_id: str,
    page: ListPage,
    discovered_at: int,
    *,
    response_id: int | None = None,
    observed_response_id: int | None = None,
    parser_version: str = PARSER_VERSION,
    origin: Origin = "unknown",
    ingestion_run_id: str | None = None,
    automatic: bool = False,
    processing_origin: ProcessingOrigin = "offline",
) -> int:
    """No commit/rollback; caller must roll back on any error. All I/O/Parser precedes this."""
    _time(discovered_at)
    check_origin(processing_origin, ingestion_run_id)
    if not source_id.strip():
        raise IngestError("invalid_source", "validation")
    origin = discovery_context(connection, source_id, origin, ingestion_run_id, discovered_at)
    validate_processing_run(connection, source_id, ingestion_run_id, discovered_at, parser_version)
    response = observed = None
    if response_id is not None:
        response = _response_on(connection, response_id)
        if (
            response["page_type"] != "list"
            or response["source_id"] != source_id
            or response["status_code"] != 200
            or response["body_path"] is None
        ):
            raise IngestError("response_not_list", "validation", response_id=response_id)
        observed = _observation(connection, response, observed_response_id, discovered_at)
        if automatic and (reason := _automatic_body_reason(connection, response)) is not None:
            raise IngestError(reason, "cache", response_id=response_id)
    elif observed_response_id is not None:
        raise IngestError("body_response_required", "validation")
    if processing_origin == "live" and (response is None or response["body_state"] != "complete"):
        raise IngestError("notification_list_evidence_required", "notification")
    if page.references and response is None:
        raise IngestError("list_reference_evidence_required", "discovery")
    new_document_ids = set()
    for entry in page.entries:
        statement = insert(documents).values(
            source_id=source_id,
            source_document_id=entry.source_document_id,
            detail_url=str(entry.detail_url),
            discovered_title=entry.title,
            discovered_at=discovered_at,
            discovery_origin=origin,
            first_discovery_run_id=ingestion_run_id,
        )
        inserted = connection.execute(statement.on_conflict_do_nothing())
        document_id = connection.execute(
            sa.select(documents.c.id).where(
                documents.c.source_id == source_id,
                documents.c.source_document_id == entry.source_document_id,
            )
        ).scalar_one()
        if inserted.rowcount == 1:
            new_document_ids.add(document_id)
        connection.execute(
            documents.update()
            .where(documents.c.id == document_id)
            .values(
                detail_url=str(entry.detail_url),
                discovered_title=entry.title,
            )
        )
    for reference in page.references:
        key = reference.candidate_key()
        mutable = dict(
            raw_href=reference.raw_href,
            title=reference.title,
            published_date=reference.published_date,
            reference_kind=reference.reference_kind,
            last_seen_at=discovered_at,
            last_run_id=ingestion_run_id,
            last_body_response_id=response["id"],
            last_observed_response_id=observed["id"],
            last_row_index=reference.row_index,
            last_parser_version=parser_version,
        )
        connection.execute(
            insert(discovered_references)
            .values(
                source_id=source_id,
                candidate_key=key,
                normalization_version=REFERENCE_NORMALIZATION_VERSION,
                resolved_url=str(reference.resolved_url),
                first_seen_at=discovered_at,
                discovery_origin=origin,
                first_discovery_run_id=ingestion_run_id,
                first_body_response_id=response["id"],
                first_observed_response_id=observed["id"],
                first_row_index=reference.row_index,
                first_parser_version=parser_version,
                **mutable,
            )
            .on_conflict_do_nothing()
        )
        existing = (
            connection.execute(
                sa.select(discovered_references).where(
                    discovered_references.c.source_id == source_id,
                    discovered_references.c.candidate_key == key,
                )
            )
            .mappings()
            .one()
        )
        if (
            existing["resolved_url"] != str(reference.resolved_url)
            or existing["normalization_version"] != REFERENCE_NORMALIZATION_VERSION
        ):
            raise IngestError("reference_identity_mismatch", "discovery")
        # Older historical replay cannot overwrite a newer observed listing.
        connection.execute(
            discovered_references.update()
            .where(
                discovered_references.c.id == existing["id"],
                discovered_references.c.last_seen_at <= discovered_at,
            )
            .values(**mutable)
        )
    register_listing_evidence_in_transaction(
        connection,
        source_id=source_id,
        page=page,
        body_response_id=response_id,
        observed_response_id=observed["id"] if observed is not None else None,
        processed_at=discovered_at,
        parser_version=parser_version,
        processing_origin=processing_origin,
        ingestion_run_id=ingestion_run_id,
        new_document_ids=new_document_ids,
    )
    if response is not None:
        _processing_marks(connection, response, observed, discovered_at, parser_version)
    advance_source(connection, source_id, "last_list_registered_at", discovered_at)
    return len(page.entries)


def discover_page(
    engine: Engine, source_id: str, page: ListPage, discovered_at: int, **options
) -> int:
    """Convenient atomic page wrapper; preserve rediscovery state and first origin."""
    try:
        with engine.begin() as connection:
            return discover_page_in_transaction(
                connection, source_id, page, discovered_at, **options
            )
    except SQLAlchemyError as exc:
        raise IngestError("database_write_failed", "discovery") from exc


def list_references(engine: Engine, source_id: str, *, limit: int = 20, offset: int = 0) -> dict:
    """Read the separate unadapted backlog; no lock, file access, repair or HTTP."""
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(offset) is not int
        or not 0 <= offset <= 10000
    ):
        raise IngestError("invalid_reference_limit", "validation")
    with engine.connect() as connection:
        condition = discovered_references.c.source_id == source_id
        total = connection.scalar(
            sa.select(sa.func.count()).select_from(discovered_references).where(condition)
        )
        rows = connection.execute(
            sa.select(discovered_references)
            .where(condition)
            .order_by(discovered_references.c.id)
            .limit(limit)
            .offset(offset)
        ).mappings()
        values = [dict(row) | {"published_date": row["published_date"].isoformat()} for row in rows]
    return {
        "source_id": source_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "references": values,
    }


def _resource(connection: Connection, evidence: ResponseInput) -> int | None:
    if evidence.request_profile is None:
        return None
    uri = request_uri(str(evidence.requested_url))
    profile = evidence.request_profile
    connection.execute(
        insert(http_resources)
        .values(
            source_id=evidence.source_id,
            request_uri=uri,
            profile_sha256=profile.sha256(),
            request_profile=profile.model_dump(),
        )
        .on_conflict_do_nothing()
    )
    resource = (
        connection.execute(
            sa.select(http_resources).where(
                http_resources.c.source_id == evidence.source_id,
                http_resources.c.request_uri == uri,
                http_resources.c.profile_sha256 == profile.sha256(),
            )
        )
        .mappings()
        .one()
    )
    if resource["request_profile"] != profile.model_dump():
        raise IngestError("profile_mismatch", "cache")
    return resource["id"]


def record_response(
    engine: Engine,
    raw_store: RawStore,
    evidence: ResponseInput,
    content: bytes | None,
    *,
    candidate: CacheCandidate | None = None,
    run_id: str | None = None,
) -> int:
    """Archive only complete bytes, then independently commit evidence and latest 200."""
    document_id = None
    try:
        document_id = _target(engine, evidence)
        if evidence.status_code == 304 and content is not None:
            raise IngestError("304_has_body", "validation", document_id=document_id)
        if evidence.request_profile is not None and evidence.status_code == 200 and content == b"":
            raise IngestError("empty_complete_body", "validation", document_id=document_id)
        if (evidence.body_state == "complete" and content is None) or (
            evidence.body_state == "unavailable" and content is not None
        ):
            raise IngestError("body_completeness_mismatch", "validation", document_id=document_id)
        if candidate is not None and evidence.status_code != 304:
            raise IngestError("cache_binding_invalid", "validation", document_id=document_id)
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
            **{
                field: getattr(evidence, field)
                for field in RawResponseReference.model_fields
                if field not in {"body_path", "body_sha256"}
            },
            body_path=body.path if body else None,
            body_sha256=body.sha256 if body else None,
        )
        with engine.begin() as connection:
            resource_id = _resource(connection, evidence)
            values = dict(
                **reference.model_dump(mode="json"),
                page_type=evidence.page_type,
                document_id=document_id,
                resource_id=resource_id,
                body_state="complete" if body else "unavailable",
                vary=evidence.vary,
                cache_control=evidence.cache_control,
                content_encoding=evidence.content_encoding,
            )
            baseline = None
            if candidate is not None:
                baseline = validate_binding(connection, candidate, values)
                values["validated_response_id"] = candidate.response_id
            response_id = connection.execute(
                raw_responses.insert().values(**values)
            ).inserted_primary_key[0]
            if resource_id is not None and evidence.status_code == 200 and body is not None:
                resource = (
                    connection.execute(
                        sa.select(http_resources).where(http_resources.c.id == resource_id)
                    )
                    .mappings()
                    .one()
                )
                latest_id = resource["latest_response_id"]
                if (
                    latest_id is None
                    or evidence.fetched_at >= _response_on(connection, latest_id)["fetched_at"]
                ):
                    updates = dict(latest_response_id=response_id)
                    blocked_id = resource["blocked_by_response_id"]
                    if (
                        blocked_id is None
                        or evidence.fetched_at >= _response_on(connection, blocked_id)["fetched_at"]
                    ):
                        updates["blocked_by_response_id"] = None
                    connection.execute(
                        http_resources.update()
                        .where(http_resources.c.id == resource_id)
                        .values(**updates)
                    )
            if baseline is not None and validation_reason(values, baseline) is not None:
                connection.execute(
                    http_resources.update()
                    .where(http_resources.c.id == resource_id)
                    .values(blocked_by_response_id=response_id)
                )
            if evidence.page_type == "list" and (
                (evidence.status_code == 200 and content)
                or (baseline is not None and validation_reason(values, baseline) is None)
            ):
                advance_source(
                    connection, evidence.source_id, "last_list_response_at", evidence.fetched_at
                )
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


def record_failure_in_transaction(
    connection: Connection,
    error: IngestError,
    processed_at: int,
    *,
    failure_due_at: int | None = None,
) -> None:
    """Record a finite fetch/parse/commit failure without clearing successful results."""
    _time(processed_at)
    validate_error_code(error.code)
    if failure_due_at is not None:
        _time(failure_due_at)
        if failure_due_at < processed_at:
            raise IngestError("invalid_next_due_time", "validation")
    document_id = error.document_id
    if error.response_id is not None:
        response = _response_on(connection, error.response_id)
        if document_id is not None and document_id != response["document_id"]:
            raise IngestError("failure_target_mismatch", "validation")
        document_id = response["document_id"]
        _observation(connection, response, None, processed_at)
    if document_id is not None:
        target = (
            connection.execute(sa.select(documents).where(documents.c.id == document_id))
            .mappings()
            .one()
        )
        _attempt_time(target, processed_at)
    if error.response_id is not None:
        connection.execute(
            raw_responses.update()
            .where(raw_responses.c.id == error.response_id)
            .values(last_attempt_at=processed_at, last_error_code=error.code)
        )
    if document_id is not None:
        values = dict(status="failed", last_attempt_at=processed_at, last_error_code=error.code)
        if failure_due_at is not None:
            values["next_due_at"] = failure_due_at
        connection.execute(documents.update().where(documents.c.id == document_id).values(**values))


def record_failure(
    engine: Engine,
    error: IngestError,
    processed_at: int,
    failure_due_at: int | None = None,
) -> None:
    """Convenient separate failure transaction, including metadata-only fetch failures."""
    try:
        with engine.begin() as connection:
            record_failure_in_transaction(
                connection, error, processed_at, failure_due_at=failure_due_at
            )
    except (SQLAlchemyError, IngestError) as exc:
        raise IngestError(
            "failure_state_unavailable",
            "failure_record",
            response_id=error.response_id,
            document_id=error.document_id,
        ) from exc


def _automatic_body_reason(connection: Connection, body) -> str | None:
    if body["status_code"] != 200 or body["body_state"] != "complete" or body["body_path"] is None:
        return "baseline_not_complete_200"
    if body["resource_id"] is None:
        return "cache_profile_unknown"
    resource = (
        connection.execute(
            sa.select(http_resources).where(http_resources.c.id == body["resource_id"])
        )
        .mappings()
        .one()
    )
    if (
        body["source_id"] != resource["source_id"]
        or body["requested_url"] != resource["request_uri"]
    ):
        return "baseline_key_mismatch"
    if resource["latest_response_id"] != body["id"]:
        return "baseline_superseded"
    if resource["blocked_by_response_id"] is not None:
        return "cache_validation_blocked"
    if body["document_id"] is not None:
        latest = connection.execute(
            sa.select(raw_responses.c.id)
            .where(
                raw_responses.c.document_id == body["document_id"],
                raw_responses.c.status_code == 200,
                raw_responses.c.body_path.is_not(None),
            )
            .order_by(raw_responses.c.fetched_at.desc(), raw_responses.c.id.desc())
            .limit(1)
        ).scalar_one()
        if latest != body["id"]:
            return "obsolete_document_body"
    return None


def save_notice_in_transaction(
    connection: Connection,
    response_id: int,
    notice: ParsedNotice,
    processed_at: int,
    *,
    observed_response_id: int | None = None,
    next_due_at: int | None = None,
    automatic: bool = False,
    ingestion_run_id: str | None = None,
    processing_origin: ProcessingOrigin = "offline",
    prepared_notification: PreparedNotification | None = None,
) -> int:
    """Version, success pointer, due and resource marks share the caller's transaction."""
    _time(processed_at)
    check_origin(processing_origin, ingestion_run_id)
    if next_due_at is not None:
        _time(next_due_at)
        if next_due_at < processed_at:
            raise IngestError("invalid_next_due_time", "validation")
    response = _response_on(connection, response_id)
    validate_processing_run(
        connection, response["source_id"], ingestion_run_id, processed_at, notice.parser_version
    )
    document_id = response["document_id"]
    if response["page_type"] != "notice" or document_id is None:
        raise IngestError("response_not_notice", "validation", response_id=response_id)
    if response["status_code"] != 200 or response["body_path"] is None:
        raise IngestError("response_not_valid_html", "validation", response_id=response_id)
    observed = _observation(connection, response, observed_response_id, processed_at)
    if automatic and (reason := _automatic_body_reason(connection, response)) is not None:
        raise IngestError(reason, "cache", response_id=response_id)
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
            "identity_mismatch", "validation", response_id=response_id, document_id=document_id
        )
    digest = notice.content.content_sha256()
    if processing_origin == "live" and (
        prepared_notification is None
        or prepared_notification.document_id != document_id
        or prepared_notification.source_document_id != notice.source_document_id
        or prepared_notification.body_response_id != response_id
        or prepared_notification.observed_response_id != observed["id"]
        or prepared_notification.ingestion_run_id != ingestion_run_id
        or prepared_notification.evaluated_at != processed_at
        or prepared_notification.content_sha256 != digest
        or prepared_notification.parser_version != notice.parser_version
    ):
        raise IngestError("notification_prepared_required", "notification")
    connection.execute(
        insert(notice_versions)
        .values(
            document_id=document_id,
            raw_response_id=response_id,
            content_sha256=digest,
            parser_version=notice.parser_version,
            parsed_at=processed_at,
            title=notice.content.title,
            published_date=notice.content.published_date,
            normalized_content=notice.content.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(
            index_elements=[
                notice_versions.c.document_id,
                notice_versions.c.content_sha256,
                notice_versions.c.parser_version,
            ]
        )
    )
    version_id = connection.execute(
        sa.select(notice_versions.c.id).where(
            notice_versions.c.document_id == document_id,
            notice_versions.c.content_sha256 == digest,
            notice_versions.c.parser_version == notice.parser_version,
        )
    ).scalar_one()
    values = dict(
        status="processed",
        current_version_id=version_id,
        last_attempt_at=processed_at,
        last_success_at=processed_at,
        last_error_code=None,
    )
    if next_due_at is not None:
        values["next_due_at"] = next_due_at
    connection.execute(documents.update().where(documents.c.id == document_id).values(**values))
    _processing_marks(connection, response, observed, processed_at, notice.parser_version)
    if processing_origin == "live":
        commit_notification_in_transaction(connection, prepared_notification, version_id=version_id)
    # The derived index must follow the current pointer in this same short transaction.
    # Local imports and maintenance reparse share this path; no HTML or raw-file I/O here.
    from signalnest.search import SearchError, sync_document_in_transaction

    try:
        sync_document_in_transaction(connection, document_id)
    except SearchError as exc:
        raise IngestError(
            "search_index_write_failed",
            "success_commit",
            response_id=response_id,
            document_id=document_id,
        ) from exc
    return version_id


def save_notice(
    engine: Engine, response_id: int, notice: ParsedNotice, processed_at: int, **options
) -> int:
    """Convenient atomic wrapper; composed callers use save_notice_in_transaction."""
    try:
        with engine.begin() as connection:
            return save_notice_in_transaction(
                connection, response_id, notice, processed_at, **options
            )
    except SQLAlchemyError as exc:
        raise IngestError(
            "database_write_failed", "success_commit", response_id=response_id
        ) from exc


def process_response(
    engine: Engine,
    raw_store: RawStore,
    response_id: int,
    processed_at: int,
    *,
    notice_parser: Callable[[PageInput], ParsedNotice] = parse_notice,
    list_parser: Callable[[PageInput], ListPage] = parse_list,
    list_parser_version: str = PARSER_VERSION,
    expected_source_id: str | None = None,
    run_id: str | None = None,
    next_due_at: int | None = None,
    failure_due_at: int | None = None,
    notice_due: Callable[[ParsedNotice, int], int] | None = None,
    error_due: Callable[[IngestError, int], int | None] | None = None,
    origin: Origin = "unknown",
    ingestion_run_id: str | None = None,
    automatic: bool = False,
    processing_origin: ProcessingOrigin = "offline",
) -> ProcessingResult:
    """Explicit replay permits history. Automatic callers require the latest matching body."""
    _time(processed_at)
    check_origin(processing_origin, ingestion_run_id)
    for due in (next_due_at, failure_due_at):
        if due is not None:
            _time(due)
            if due < processed_at:
                raise IngestError("invalid_next_due_time", "validation")
    observed = _response(engine, response_id)
    document_id = observed["document_id"]
    if expected_source_id is not None and observed["source_id"] != expected_source_id:
        raise IngestError("source_mismatch", "validation", response_id=response_id)
    if observed["page_type"] is None:
        raise IngestError("legacy_response_type_unknown", "validation", response_id=response_id)
    if processed_at < observed["fetched_at"]:
        raise IngestError("processing_before_fetch", "validation", response_id=response_id)
    if observed["last_attempt_at"] is not None and processed_at < observed["last_attempt_at"]:
        raise IngestError("stale_processing_time", "validation", response_id=response_id)
    if document_id is not None:
        with engine.connect() as connection:
            target = (
                connection.execute(sa.select(documents).where(documents.c.id == document_id))
                .mappings()
                .one()
            )
            _attempt_time(target, processed_at)
    body = observed
    error = None
    try:
        reason = None
        if observed["status_code"] not in {200, 304}:
            raise IngestError("http_status_not_200", "validation")
        if observed["status_code"] == 304 and observed["validated_response_id"] is not None:
            body = _response(engine, observed["validated_response_id"])
            with engine.connect() as connection:
                _observation(connection, body, response_id, processed_at)
                resource = (
                    connection.execute(
                        sa.select(http_resources).where(http_resources.c.id == body["resource_id"])
                    )
                    .mappings()
                    .one()
                )
            reason = reuse_reason(body, resource) or validation_reason(observed, body)
        elif observed["status_code"] == 304 and automatic:
            reason = "cache_binding_missing"
        if automatic and reason is None:
            with engine.connect() as connection:
                reason = _automatic_body_reason(connection, body)
        if reason is not None:
            raise IngestError(reason, "cache")
        if body["body_path"] is None:
            raise IngestError("response_has_no_body", "archive")
        if body["status_code"] != 200:
            raise IngestError("http_status_not_200", "validation")
        content = raw_store.read(body["body_path"], body["body_sha256"])
        if not content:
            raise IngestError("parse_empty_page", "parse")
        page = PageInput(content=content, page_url=body["final_url"])
        if body["page_type"] == "list":
            listing = list_parser(page)
            count = discover_page(
                engine,
                body["source_id"],
                listing,
                processed_at,
                response_id=body["id"],
                observed_response_id=response_id,
                parser_version=list_parser_version,
                origin=origin,
                ingestion_run_id=ingestion_run_id,
                automatic=automatic,
                processing_origin=processing_origin,
            )
            result = ProcessingResult(
                response_id,
                discovered_count=count,
                reference_count=len(listing.references),
                registered_row_count=listing.row_count,
                next_page_url=str(listing.next_page_url) if listing.next_page_url else None,
                pagination=listing.pagination,
                body_response_id=body["id"],
            )
        else:
            notice = notice_parser(page)
            # Policy sees validated notice outside the transaction; due commits with success.
            due = notice_due(notice, processed_at) if notice_due is not None else next_due_at
            # A stale preparation is retried once, entirely outside the rolled-back
            # business transaction. SQL/ownership failures are never swallowed.
            for attempt in range(2):
                prepared = (
                    prepare_notification(
                        engine,
                        raw_store,
                        notice=notice,
                        body_response_id=body["id"],
                        observed_response_id=response_id,
                        processing_origin=processing_origin,
                        ingestion_run_id=ingestion_run_id,
                        evaluated_at=processed_at,
                        notice_parser=notice_parser,
                    )
                    if processing_origin == "live"
                    else None
                )
                try:
                    version_id = save_notice(
                        engine,
                        body["id"],
                        notice,
                        processed_at,
                        observed_response_id=response_id,
                        next_due_at=due,
                        automatic=automatic,
                        ingestion_run_id=ingestion_run_id,
                        processing_origin=processing_origin,
                        prepared_notification=prepared,
                    )
                    break
                except IngestError as exc:
                    if exc.code != "notification_prepare_stale" or attempt:
                        raise
            if prepared is not None and (
                prepared.kind is not None or prepared.comparison_error_code
            ):
                log_event(
                    logging.getLogger("signalnest"),
                    Event.NOTIFICATION_EVENT_REGISTERED
                    if prepared.kind is not None
                    else Event.NOTIFICATION_COMPARISON_UNKNOWN,
                    source_id=observed["source_id"],
                    document_id=document_id,
                    response_id=response_id,
                    run_id=run_id,
                    stage="notification",
                    error_code=prepared.comparison_error_code,
                )
            result = ProcessingResult(
                response_id,
                document_id=document_id,
                version_id=version_id,
                body_response_id=body["id"],
            )
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
    except SQLAlchemyError as exc:
        error = IngestError("database_read_failed", "evidence")
        error.__cause__ = exc
    if error is not None:
        error.response_id, error.document_id = response_id, document_id
        log_event(
            logging.getLogger("signalnest"),
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=observed["source_id"],
            response_id=response_id,
            document_id=document_id,
            stage=error.stage,
            error_code=error.code,
            run_id=run_id,
        )
        due = error_due(error, processed_at) if error_due is not None else failure_due_at
        record_failure(engine, error, processed_at, due)
        if automatic and (
            error.stage in {"archive", "cache"} or error.code == "response_has_no_body"
        ):
            return ProcessingResult(
                response_id,
                outcome="full_fetch_required",
                document_id=document_id,
                body_response_id=body["id"] if body["status_code"] == 200 else None,
                error_code=error.code,
            )
        raise error
    log_event(
        logging.getLogger("signalnest"),
        Event.PAGE_PROCESSED,
        source_id=observed["source_id"],
        response_id=response_id,
        document_id=document_id,
        stage="success_commit",
        run_id=run_id,
        reference_count=result.reference_count if result.registered_row_count else None,
    )
    return result


def process_cached_response(
    engine: Engine, raw_store: RawStore, response_id: int, processed_at: int, **options
) -> ProcessingResult:
    """Automatic latest-body recovery, including a bound 304; never chooses old success."""
    return process_response(engine, raw_store, response_id, processed_at, automatic=True, **options)


def import_page(
    engine: Engine,
    raw_store: RawStore,
    evidence: ResponseInput,
    content: bytes | None,
    processed_at: int,
    *,
    run_id: str | None = None,
    candidate: CacheCandidate | None = None,
    **processing_options,
) -> ProcessingResult:
    """Import an explicit observation, preserving its supplied fetch timestamp."""
    _time(processed_at)
    if processed_at < evidence.fetched_at:
        raise IngestError("processing_before_fetch", "validation")
    try:
        response_id = record_response(
            engine, raw_store, evidence, content, candidate=candidate, run_id=run_id
        )
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
            record_failure(engine, exc, processed_at)
        raise
    if evidence.status_code == 304 and candidate is None:
        return ProcessingResult(response_id, outcome="evidence_only")
    return process_response(
        engine, raw_store, response_id, processed_at, run_id=run_id, **processing_options
    )
