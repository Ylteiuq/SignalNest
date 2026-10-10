"""Small durable source/run facts. Connection functions never own a transaction."""

import re
from typing import Literal

import sqlalchemy as sa
from pydantic import Field, field_validator, model_validator
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, Engine

from signalnest.contracts import Contract, Digest, NonemptyText, PaginationEvidence, WebUrl
from signalnest.errors import IngestError, validate_time
from signalnest.parsing import PARSER_VERSION
from signalnest.schema import (
    discovered_references,
    documents,
    ingestion_runs,
    raw_responses,
    source_ingestion_state,
)

Origin = Literal["unknown", "bootstrap", "regular", "historical"]


class RegisteredListPage(Contract):
    """Committed list evidence; row keys are ordered and include repeated observations."""

    body_response_id: int = Field(ge=1, strict=True)
    observed_response_id: int = Field(ge=1, strict=True)
    requested_url: WebUrl
    response_requested_url: WebUrl
    final_url: WebUrl
    next_page_url: WebUrl | None
    pagination: PaginationEvidence
    listing_sha256: Digest
    rows: tuple[tuple[Literal["notice", "reference"], NonemptyText], ...] = Field(min_length=1)


class ScanCompletion(Contract):
    """Coordinator attestation: committed chain, validated URIs and unchanged home recheck.

    This validates page declarations only. The coordinator must already have verified
    the actual URI chain and committed every page; no network/scan is performed here.
    """

    pages: tuple[PaginationEvidence, ...] = Field(min_length=1)
    home_recheck_unchanged: Literal[True]
    # Old attestations remain readable, explicitly without response/member evidence.
    registrations: tuple[RegisteredListPage, ...] | None = None
    home_recheck: RegisteredListPage | None = None

    @field_validator("home_recheck_unchanged", mode="before")
    @classmethod
    def explicit_recheck(cls, value):
        if value is not True:
            raise ValueError("an explicit unchanged home recheck is required")
        return value

    @model_validator(mode="after")
    def complete_chain(self):
        total = self.pages[0].total_pages
        if len(self.pages) != total or any(
            page.current_page != index or page.total_pages != total
            for index, page in enumerate(self.pages, 1)
        ):
            raise ValueError("complete scan requires every page in order with one total")
        if any(page.is_last_page for page in self.pages[:-1]) or not self.pages[-1].is_last_page:
            raise ValueError("complete scan requires an explicit terminal page")
        if any(page.last_page_url != self.pages[0].last_page_url for page in self.pages[:-1]):
            raise ValueError("tail declarations drifted")
        if (self.registrations is None) != (self.home_recheck is None):
            raise ValueError("registered coverage requires both chain and home evidence")
        if self.registrations is not None:
            if len(self.registrations) != len(self.pages) or any(
                registered.pagination != page
                or registered.pagination.is_last_page != (registered.next_page_url is None)
                for registered, page in zip(self.registrations, self.pages, strict=True)
            ):
                raise ValueError("registered pages must agree with pagination declarations")
            if any(
                following.requested_url != prior.next_page_url
                for prior, following in zip(
                    self.registrations[:-1], self.registrations[1:], strict=True
                )
            ):
                raise ValueError("registered requests must follow actual next targets")
            first, home = self.registrations[0], self.home_recheck
            if any(
                getattr(first, field) != getattr(home, field)
                for field in (
                    "requested_url",
                    "response_requested_url",
                    "final_url",
                    "next_page_url",
                    "pagination",
                    "listing_sha256",
                    "rows",
                )
            ):
                raise ValueError("home recheck must preserve all notice and reference rows")
        return self


def ensure_source(connection: Connection, source_id: str) -> None:
    if not source_id.strip():
        raise IngestError("invalid_source", "validation")
    connection.execute(
        insert(source_ingestion_state).values(source_id=source_id).on_conflict_do_nothing()
    )


def advance_source(connection: Connection, source_id: str, field: str, at: int) -> None:
    """Internal finite-field update; older imported timestamps never move a fact backwards."""
    validate_time(at)
    ensure_source(connection, source_id)
    column = source_ingestion_state.c[field]
    connection.execute(
        source_ingestion_state.update()
        .where(source_ingestion_state.c.source_id == source_id)
        .values({field: sa.case((sa.or_(column.is_(None), column < at), at), else_=column)})
    )


def record_list_attempt_in_transaction(connection: Connection, source_id: str, at: int) -> None:
    advance_source(connection, source_id, "last_list_attempt_at", at)


def set_cooldown_in_transaction(connection: Connection, source_id: str, not_before_at: int) -> None:
    # Never shorten an already recorded server cooldown; HTTP policy supplies the full deadline.
    advance_source(connection, source_id, "not_before_at", not_before_at)


def _error_code(code: str | None) -> None:
    if code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        raise IngestError("invalid_error_code", "validation")


def _run(connection: Connection, run_id: str, at: int):
    validate_time(at)
    run = (
        connection.execute(sa.select(ingestion_runs).where(ingestion_runs.c.id == run_id))
        .mappings()
        .one_or_none()
    )
    if run is None or run["result"] != "running":
        raise IngestError("run_not_running", "state")
    if at < run["started_at"] or (run["coverage_at"] is not None and at < run["coverage_at"]):
        raise IngestError("stale_run_time", "state")
    return run


def start_run_in_transaction(
    connection: Connection,
    source_id: str,
    run_id: str,
    at: int,
    *,
    origin: Literal["bootstrap", "regular", "historical"],
    parser_version: str = PARSER_VERSION,
) -> None:
    validate_time(at)
    if (
        origin not in {"bootstrap", "regular", "historical"}
        or not run_id.strip()
        or not parser_version.strip()
    ):
        raise IngestError("invalid_run", "validation")
    ensure_source(connection, source_id)
    old = (
        connection.execute(
            sa.select(ingestion_runs).where(
                ingestion_runs.c.source_id == source_id, ingestion_runs.c.result == "running"
            )
        )
        .mappings()
        .all()
    )
    for run in old:
        _run(connection, run["id"], at)
        values = dict(result="interrupted", finished_at=at, error_code="previous_run_unfinished")
        if run["coverage"] == "pending":
            values.update(
                coverage="interrupted",
                coverage_at=at,
                coverage_error_code="previous_run_unfinished",
            )
        connection.execute(
            ingestion_runs.update().where(ingestion_runs.c.id == run["id"]).values(**values)
        )
    connection.execute(
        ingestion_runs.insert().values(
            id=run_id,
            source_id=source_id,
            origin=origin,
            parser_version=parser_version,
            started_at=at,
        )
    )


def discovery_context(
    connection: Connection, source_id: str, origin: Origin, run_id: str | None, at: int
) -> Origin:
    if origin not in {"unknown", "bootstrap", "regular", "historical"}:
        raise IngestError("invalid_discovery_origin", "validation")
    if run_id is not None:
        run = _run(connection, run_id, at)
        if run["source_id"] != source_id or origin not in {"unknown", run["origin"]}:
            raise IngestError("discovery_run_mismatch", "validation")
        return run["origin"]
    return origin


def validate_processing_run(
    connection: Connection, source_id: str, run_id: str | None, at: int, parser_version: str
) -> None:
    if run_id is not None:
        run = _run(connection, run_id, at)
        if run["source_id"] != source_id or run["parser_version"] != parser_version:
            raise IngestError("run_parser_or_source_mismatch", "validation")


def record_coverage_in_transaction(
    connection: Connection,
    run_id: str,
    coverage: Literal["complete", "limited", "interrupted"],
    at: int,
    *,
    completion: ScanCompletion | None = None,
    error_code: str | None = None,
) -> None:
    run = _run(connection, run_id, at)
    _error_code(error_code)
    if run["coverage"] != "pending" or coverage not in {"complete", "limited", "interrupted"}:
        raise IngestError("coverage_already_final_or_invalid", "state")
    if (coverage == "complete") != (completion is not None) or (
        coverage == "complete" and error_code is not None
    ):
        raise IngestError("complete_scan_evidence_required", "state")
    if completion is not None and not isinstance(completion, ScanCompletion):
        raise IngestError("complete_scan_evidence_required", "state")
    if completion is not None and completion.registrations is not None:
        for page in (*completion.registrations, completion.home_recheck):
            _check_registered_page(connection, run, page)
    connection.execute(
        ingestion_runs.update()
        .where(ingestion_runs.c.id == run_id)
        .values(
            coverage=coverage,
            coverage_at=at,
            coverage_error_code=error_code,
            coverage_evidence=completion.model_dump(mode="json") if completion else None,
        )
    )
    if coverage == "complete":
        source = (
            connection.execute(
                sa.select(source_ingestion_state).where(
                    source_ingestion_state.c.source_id == run["source_id"]
                )
            )
            .mappings()
            .one()
        )
        if source["last_complete_scan_at"] is not None and at < source["last_complete_scan_at"]:
            raise IngestError("stale_scan_time", "state")
        values = dict(last_complete_scan_at=at, last_complete_scan_run_id=run_id)
        if run["origin"] == "bootstrap" and source["bootstrap_completed_at"] is None:
            values["bootstrap_completed_at"] = at
        connection.execute(
            source_ingestion_state.update()
            .where(source_ingestion_state.c.source_id == run["source_id"])
            .values(**values)
        )


def _check_registered_page(connection: Connection, run, page: RegisteredListPage) -> None:
    """Check response binding and every row's registration; no file/Parser work here."""
    evidence = {
        row["id"]: row
        for row in connection.execute(
            sa.select(raw_responses).where(
                raw_responses.c.id.in_({page.body_response_id, page.observed_response_id}),
            )
        ).mappings()
    }
    body = evidence.get(page.body_response_id)
    observed = evidence.get(page.observed_response_id)
    if (
        body is None
        or observed is None
        or any(
            row["source_id"] != run["source_id"]
            or row["page_type"] != "list"
            or row["last_error_code"] is not None
            or row["last_attempt_at"] is None
            or row["last_attempt_at"] < run["started_at"]
            for row in (body, observed)
        )
        or body["status_code"] != 200
        or body["body_path"] is None
        or body["body_state"] != "complete"
    ):
        raise IngestError("scan_registration_invalid", "coverage")
    if body["requested_url"] != str(page.response_requested_url) or body["final_url"] != str(
        page.final_url
    ):
        raise IngestError("scan_registration_invalid", "coverage")
    if observed["id"] != body["id"] and (
        observed["status_code"] != 304
        or observed["validated_response_id"] != body["id"]
        or any(
            observed[field] != body[field]
            for field in (
                "source_id",
                "page_type",
                "resource_id",
                "requested_url",
                "final_url",
            )
        )
    ):
        raise IngestError("scan_registration_invalid", "coverage")
    for kind, table, column in (
        ("notice", documents, documents.c.source_document_id),
        ("reference", discovered_references, discovered_references.c.candidate_key),
    ):
        keys = {key for row_kind, key in page.rows if row_kind == kind}
        if (
            keys
            and set(
                connection.scalars(
                    sa.select(column).where(
                        table.c.source_id == run["source_id"],
                        column.in_(keys),
                    )
                )
            )
            != keys
        ):
            raise IngestError("scan_registration_invalid", "coverage")


def finish_run_in_transaction(
    connection: Connection,
    run_id: str,
    result: Literal["succeeded", "partial_failure", "failed", "interrupted"],
    at: int,
    *,
    error_code: str | None = None,
) -> None:
    run = _run(connection, run_id, at)
    _error_code(error_code)
    if result not in {"succeeded", "partial_failure", "failed", "interrupted"}:
        raise IngestError("invalid_run_result", "state")
    if run["coverage"] == "pending":
        record_coverage_in_transaction(
            connection, run_id, "interrupted", at, error_code="coverage_unfinished"
        )
    connection.execute(
        ingestion_runs.update()
        .where(ingestion_runs.c.id == run_id)
        .values(
            result=result,
            finished_at=at,
            error_code=error_code,
        )
    )


def read_source_state(engine: Engine, source_id: str):
    with engine.connect() as connection:
        return (
            connection.execute(
                sa.select(source_ingestion_state).where(
                    source_ingestion_state.c.source_id == source_id
                )
            )
            .mappings()
            .one_or_none()
        )


def pending_documents(engine: Engine, source_id: str, at: int):
    """Database facts only; caller chooses budgets, ordering priorities and HTTP policy."""
    validate_time(at)
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                sa.select(documents)
                .where(
                    documents.c.source_id == source_id,
                    sa.or_(documents.c.next_due_at.is_(None), documents.c.next_due_at <= at),
                    sa.or_(documents.c.status != "processed", documents.c.next_due_at.is_not(None)),
                )
                .order_by(documents.c.id)
            ).mappings()
        )
