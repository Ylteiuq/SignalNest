"""One serial, bounded crawl using the production evidence and business services.

No resume cursor: every scan starts at home, while detail work is rebuilt from SQLite.
HTTP, archive reads/writes and parsing stay outside business/state transactions.
"""

import logging
import time
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

import httpx
import sqlalchemy as sa
from pydantic import Field
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.config import Settings
from signalnest.contracts import Contract, ListPage, PageInput
from signalnest.errors import IngestError, validate_time
from signalnest.eventlog import Event, log_event
from signalnest.fetching import Clock, FetchCode, FetchLimits, FetchResult, FetchTarget, HttpFetcher
from signalnest.ingestion import process_cached_response, record_failure, record_response
from signalnest.ingestion_state import (
    ScanCompletion,
    finish_run_in_transaction,
    pending_documents,
    read_source_state,
    record_coverage_in_transaction,
    record_list_attempt_in_transaction,
    start_run_in_transaction,
)
from signalnest.instance_lock import writer_lock
from signalnest.parsing import PARSER_VERSION, parse_list
from signalnest.rawstore import RawStore
from signalnest.runtime_policy import (
    GROUPS,
    GroupSummary,
    due_order,
    failure_due,
    group_for,
    select_details,
    success_due,
)
from signalnest.schema import documents
from signalnest.storage import StorageError, open_initialized_engine


class CrawlOptions(Contract):
    scan_mode: Literal["full", "limited"] = "limited"
    max_pages: int = Field(default=2, ge=1, le=10000, strict=True)
    max_details: int = Field(default=20, ge=0, le=100000, strict=True)
    fetch_limits: FetchLimits = Field(default_factory=FetchLimits)
    # Explicit library overrides remain supported; otherwise use the configured runtime policy.
    success_recheck_seconds: int | None = Field(default=None, ge=1, le=31536000, strict=True)
    failure_retry_seconds: int | None = Field(default=None, ge=1, le=31536000, strict=True)


class CrawlSummary(Contract):
    run_id: str
    source_id: str
    origin: Literal["bootstrap", "regular"]
    scan_mode: Literal["full", "limited"]
    result: Literal["succeeded", "partial_failure", "failed", "interrupted"]
    coverage: Literal["complete", "limited", "interrupted"]
    coverage_error_code: str | None
    error_code: str | None
    started_at: int
    finished_at: int
    pages_committed: int
    home_rechecked: bool
    scanned_entries: int
    new_documents: int
    legacy_rechecks_scheduled: int
    details_attempted: int
    details_succeeded: int
    details_failed: int
    physical_requests: int
    remaining_due: int
    remaining_unprocessed: int
    detail_groups: dict[str, GroupSummary] = Field(default_factory=dict)
    foreground_pages: int = 0
    future_dates: int = 0
    remaining_first_processing: int = 0


@dataclass(frozen=True)
class _Page:
    listing: ListPage
    final_uri: str
    uris: frozenset[str]


@dataclass(frozen=True)
class _Outcome:
    page: _Page | None = None
    error_code: str | None = None
    stop_code: str | None = None
    sent: bool = False


_GLOBAL_STOPS = {
    FetchCode.REQUEST_LIMIT,
    FetchCode.RUN_TIME_LIMIT,
    FetchCode.SERVER_COOLDOWN,
    FetchCode.HTTP_RATE_LIMITED,
    FetchCode.RETRY_AFTER_OUT_OF_RANGE,
}
_BUDGET_CODES = {FetchCode.REQUEST_LIMIT, FetchCode.RUN_TIME_LIMIT, FetchCode.RESOURCE_TIME_LIMIT}


class _Run:
    def __init__(self, engine, store, settings, options, fetcher, run_id, clock):
        self.engine, self.store, self.settings = engine, store, settings
        self.options, self.fetcher, self.run_id, self.clock = options, fetcher, run_id, clock
        self.source = settings.source.id
        self.started = False
        self.future_dates = 0

    def now(self) -> int:
        at = int(self.clock.time())
        validate_time(at)
        return at

    def time_exhausted(self) -> bool:
        return self.clock.monotonic() >= self.fetcher.run_deadline

    def failed_due(self, code, at, status=None):
        due = (
            at + self.options.failure_retry_seconds
            if self.options.failure_retry_seconds is not None
            else failure_due(str(code), status, at, self.settings.runtime)
        )
        state = read_source_state(self.engine, self.source)
        return max(due, state["not_before_at"] or 0) if state else due

    def failure(self, code, response_id=None, document_id=None, status=None):
        at = self.now()
        record_failure(
            self.engine,
            IngestError(str(code), "fetch", response_id=response_id, document_id=document_id),
            at,
            self.failed_due(code, at, status) if document_id is not None else None,
        )
        log_event(
            logging.getLogger("signalnest"),
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=self.source,
            run_id=self.run_id,
            document_id=document_id,
            response_id=response_id,
            stage="fetch",
            error_code=str(code),
        )

    def register(self, result: FetchResult, document_id):
        """Every actual response, even intermediate errors/redirects, remains evidence."""
        last_id = None
        for attempt in result.attempts:
            last_id = None
            if attempt.metadata is not None:
                last_id = record_response(
                    self.engine,
                    self.store,
                    attempt.metadata,
                    attempt.content,
                    candidate=attempt.candidate,
                    run_id=self.run_id,
                )
            if attempt.error_code is not None:
                self.failure(
                    attempt.error_code,
                    last_id,
                    document_id,
                    attempt.metadata.status_code if attempt.metadata is not None else None,
                )
        return last_id

    def obtain(self, target: FetchTarget, document_id=None, *, revalidate=False) -> _Outcome:
        result = self.fetcher.fetch(target, revalidate=revalidate)
        uris = {str(httpx.URL(target.uri))}
        sent = False
        # At most one post-processing repair. The Fetcher owns its retry/deadline allowance.
        for repair_round in range(2):
            sent |= bool(result.attempts)
            for attempt in result.attempts:
                if attempt.metadata is not None:
                    uris.update(
                        (str(attempt.metadata.requested_url), str(attempt.metadata.final_url))
                    )
            response_id = self.register(result, document_id)
            if result.error_code is not None:
                if not result.attempts and result.error_code not in _GLOBAL_STOPS:
                    self.failure(result.error_code, document_id=document_id)
                code = str(result.error_code)
                return _Outcome(
                    error_code=code,
                    stop_code=code if result.error_code in _GLOBAL_STOPS else None,
                    sent=sent,
                )
            if response_id is None or result.response is None:
                raise IngestError("fetch_handoff_invalid", "coordinator", document_id=document_id)
            listing = None
            final_uri = None

            def capture(page: PageInput) -> ListPage:
                nonlocal listing, final_uri
                listing, final_uri = parse_list(page), str(page.page_url)
                return listing

            at = self.now()
            future_date = False

            def notice_due(notice, processed_at):
                nonlocal future_date
                due, future_date = success_due(
                    notice.content.published_date, processed_at, self.settings.runtime
                )
                return (
                    processed_at + self.options.success_recheck_seconds
                    if self.options.success_recheck_seconds is not None
                    else due
                )

            def error_due(error, processed_at):
                return (
                    self.failed_due(error.code, processed_at) if document_id is not None else None
                )

            try:
                processed = process_cached_response(
                    self.engine,
                    self.store,
                    response_id,
                    at,
                    list_parser=capture,
                    list_parser_version=PARSER_VERSION,
                    notice_due=notice_due,
                    error_due=error_due,
                    expected_source_id=self.source,
                    run_id=self.run_id,
                    ingestion_run_id=self.run_id,
                )
            except IngestError as exc:
                # This service has already reliably registered these finite content failures.
                if exc.stage == "parse" or exc.code == "identity_mismatch":
                    return _Outcome(error_code=exc.code, sent=sent)
                raise
            if processed.outcome == "processed":
                if target.page_type == "list":
                    if listing is None or final_uri is None:
                        raise IngestError("fetch_handoff_invalid", "coordinator")
                    return _Outcome(_Page(listing, final_uri, frozenset(uris)), sent=sent)
                self.future_dates += int(future_date)
                return _Outcome(sent=sent)
            if (
                processed.outcome != "full_fetch_required"
                or repair_round
                or result.response.metadata.status_code != 304
            ):
                raise IngestError(
                    processed.error_code or "fetch_handoff_invalid",
                    "archive",
                    response_id=response_id,
                    document_id=document_id,
                )
            result = self.fetcher.repair(result)
        raise AssertionError("unreachable repair path")

    def scan(self):
        pages, visited, scanned = [], set(), 0
        home = str(self.settings.source.list_url)
        uri = home
        tail, total = None, None
        while len(pages) < self.options.max_pages:
            if self.time_exhausted():
                return pages, scanned, False, "limited", "run_time_limit", "run_time_limit"
            if str(httpx.URL(uri)) in visited:
                return pages, scanned, False, "interrupted", "pagination_loop", None
            outcome = self.obtain(FetchTarget(uri=uri, page_type="list"))
            if outcome.error_code:
                coverage = "limited" if outcome.error_code in _BUDGET_CODES else "interrupted"
                return pages, scanned, False, coverage, outcome.error_code, outcome.stop_code
            page = outcome.page
            proof = page.listing.pagination
            # Entries are committed even on a subsequently detected cross-page drift.
            scanned += len(page.listing.entries)
            pages.append(page)
            if self.time_exhausted():
                return pages, scanned, False, "limited", "run_time_limit", "run_time_limit"
            if page.uris & visited:
                return pages, scanned, False, "interrupted", "pagination_loop", None
            visited.update(page.uris)
            if proof.current_page != len(pages):
                return pages, scanned, False, "interrupted", "pagination_discontinuity", None
            if total is None:
                total = proof.total_pages
                tail = str(proof.last_page_url) if proof.last_page_url else None
            if proof.total_pages != total or (
                not proof.is_last_page and str(proof.last_page_url) != tail
            ):
                return pages, scanned, False, "interrupted", "pagination_drift", None
            if proof.is_last_page:
                if tail is not None and tail not in page.uris:
                    return pages, scanned, False, "interrupted", "pagination_tail_mismatch", None
                if self.options.scan_mode == "limited":
                    return pages, scanned, False, "limited", "limited_scan", None
                check = self.obtain(FetchTarget(uri=home, page_type="list"), revalidate=True)
                if check.error_code:
                    coverage = "limited" if check.error_code in _BUDGET_CODES else "interrupted"
                    return pages, scanned, False, coverage, check.error_code, check.stop_code
                if self.time_exhausted():
                    return pages, scanned, True, "limited", "run_time_limit", "run_time_limit"
                if (
                    check.page.final_uri != pages[0].final_uri
                    or check.page.listing != pages[0].listing
                ):
                    return pages, scanned, True, "interrupted", "home_changed", None
                return pages, scanned, True, "complete", None, None
            uri = str(page.listing.next_page_url)
        return pages, scanned, False, "limited", "page_limit", None


def _document_count(engine: Engine, source: str) -> int:
    with engine.connect() as connection:
        return connection.scalar(
            sa.select(sa.func.count()).select_from(documents).where(documents.c.source_id == source)
        )


def _execute(run: _Run) -> CrawlSummary:
    source, engine = run.source, run.engine
    started = run.now()
    state = read_source_state(engine, source)
    origin = "regular" if state and state["bootstrap_completed_at"] is not None else "bootstrap"
    initial_count = _document_count(engine, source)
    with engine.begin() as connection:
        start_run_in_transaction(connection, source, run.run_id, started, origin=origin)
        # Explicit first-online enrollment, irrespective of publication date or discovery origin.
        scheduled = connection.execute(
            documents.update()
            .where(
                documents.c.source_id == source,
                documents.c.status == "processed",
                documents.c.next_due_at.is_(None),
            )
            .values(next_due_at=started)
        ).rowcount
    run.started = True
    log_event(
        logging.getLogger("signalnest"),
        Event.CRAWL_STARTED,
        source_id=source,
        run_id=run.run_id,
        stage="scan",
    )
    pages, scanned, rechecked, coverage, coverage_error, stop = run.scan()
    completion = (
        ScanCompletion(
            pages=tuple(page.listing.pagination for page in pages), home_recheck_unchanged=True
        )
        if coverage == "complete"
        else None
    )
    with engine.begin() as connection:
        record_coverage_in_transaction(
            connection,
            run.run_id,
            coverage,
            run.now(),
            completion=completion,
            error_code=coverage_error,
        )
    attempted, succeeded, failed = 0, 0, 0
    errors = []
    if coverage_error not in {None, "page_limit", "limited_scan"} and not stop:
        errors.append(coverage_error)
    # The independent database batch is queried even after a list failure/304/cooldown.
    foreground_pages = [page for page in pages if page.listing.pagination.current_page <= 2]
    foreground_ids = {
        entry.source_document_id for page in foreground_pages for entry in page.listing.entries
    }
    groups = {group: GroupSummary() for group in GROUPS}
    batch = select_details(
        pending_documents(engine, source, run.now()),
        foreground_ids,
        run.options.max_details,
        run.settings.runtime,
    )
    for group, _ in batch:
        groups[group].allocated += 1
    for group, document in batch:
        if stop:
            break
        outcome = run.obtain(
            FetchTarget(
                uri=document["detail_url"],
                page_type="notice",
                source_document_id=document["source_document_id"],
            ),
            document["id"],
        )
        stop = outcome.stop_code
        if outcome.sent or not stop:
            attempted += 1
            groups[group].attempted += 1
        if outcome.error_code:
            if outcome.sent or not stop:
                failed += 1
                groups[group].failed += 1
                errors.append(outcome.error_code)
        else:
            succeeded += 1
            groups[group].succeeded += 1
        if run.time_exhausted():
            stop = "run_time_limit"
    finished = run.now()
    if stop:
        result, error = "interrupted", stop
    elif run.options.scan_mode == "full" and coverage_error == "page_limit":
        result, error = "interrupted", "page_limit"
    elif errors:
        result = "partial_failure" if pages or succeeded else "failed"
        error = errors[0]
    else:
        result, error = "succeeded", None
    remaining = pending_documents(engine, source, finished)
    due = len(remaining)
    for document in remaining:
        group = groups[group_for(document, foreground_ids)]
        group.remaining_due += 1
        group.oldest_overdue_seconds = max(
            group.oldest_overdue_seconds, finished - due_order(document)[0]
        )
    for group in groups.values():
        group.unserved = group.allocated - group.attempted
    with engine.connect() as connection:
        unprocessed = connection.scalar(
            sa.select(sa.func.count())
            .select_from(documents)
            .where(documents.c.source_id == source, documents.c.status != "processed")
        )
        first_processing = connection.scalar(
            sa.select(sa.func.count())
            .select_from(documents)
            .where(documents.c.source_id == source, documents.c.last_success_at.is_(None))
        )
    new = _document_count(engine, source) - initial_count
    with engine.begin() as connection:
        finish_run_in_transaction(connection, run.run_id, result, finished, error_code=error)
    summary = CrawlSummary(
        run_id=run.run_id,
        source_id=source,
        origin=origin,
        scan_mode=run.options.scan_mode,
        result=result,
        coverage=coverage,
        coverage_error_code=coverage_error,
        error_code=error,
        started_at=started,
        finished_at=finished,
        pages_committed=len(pages),
        home_rechecked=rechecked,
        scanned_entries=scanned,
        new_documents=new,
        legacy_rechecks_scheduled=scheduled,
        details_attempted=attempted,
        details_succeeded=succeeded,
        details_failed=failed,
        physical_requests=run.fetcher.requests_sent,
        remaining_due=due,
        remaining_unprocessed=unprocessed,
        detail_groups=groups,
        foreground_pages=len({page.listing.pagination.current_page for page in foreground_pages}),
        future_dates=run.future_dates,
        remaining_first_processing=first_processing,
    )
    log_event(
        logging.getLogger("signalnest"),
        Event.CRAWL_FINISHED,
        source_id=source,
        run_id=run.run_id,
        stage=result,
        error_code=error,
    )
    for name, group in groups.items():
        log_event(
            logging.getLogger("signalnest"),
            Event.DETAIL_GROUP_FINISHED,
            source_id=source,
            run_id=run.run_id,
            stage=name,
            attempted=group.attempted,
            succeeded=group.succeeded,
            failed=group.failed,
            remaining_due=group.remaining_due,
            unserved=group.unserved,
            oldest_overdue_seconds=group.oldest_overdue_seconds,
        )
    return summary


def crawl_once(
    settings: Settings,
    options: CrawlOptions,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Clock = time,
    run_id: str | None = None,
) -> CrawlSummary:
    """Own the POSIX instance lock and Client/Engine lifecycle; never implicitly migrate.

    Systemic errors abort, preserving committed evidence/business results. A run whose
    finalization cannot be persisted remains running, recovered by the next locked run.
    MockTransport/Clock injection is for offline verification, not a second fetch layer.
    """
    run_id = run_id or uuid4().hex
    settings = settings.model_copy(deep=True)
    if not settings.storage.database.is_file():
        raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
    with writer_lock(settings.storage.database):
        engine = open_initialized_engine(settings.storage.database)
        try:
            store = RawStore(settings.storage.data_dir)

            def before_request(target: FetchTarget, at: int) -> None:
                if target.page_type == "list":
                    try:
                        with engine.begin() as connection:
                            record_list_attempt_in_transaction(connection, settings.source.id, at)
                    except SQLAlchemyError as exc:
                        raise IngestError("list_attempt_state_unavailable", "list_attempt") from exc

            with HttpFetcher(
                engine,
                store,
                settings.http,
                source_id=settings.source.id,
                limits=options.fetch_limits,
                transport=transport,
                clock=clock,
                run_id=run_id,
                before_request=before_request,
            ) as fetcher:
                run = _Run(engine, store, settings, options, fetcher, run_id, clock)
                try:
                    return _execute(run)
                except (IngestError, SQLAlchemyError, KeyboardInterrupt) as exc:
                    if not run.started:
                        raise
                    code = (
                        exc.code
                        if isinstance(exc, IngestError)
                        else "user_interrupted"
                        if isinstance(exc, KeyboardInterrupt)
                        else "database_unavailable"
                    )
                    # Best effort only; failed finalization itself is an explicit system error.
                    try:
                        with engine.begin() as connection:
                            finish_run_in_transaction(
                                connection,
                                run_id,
                                "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                                run.now(),
                                error_code=code,
                            )
                    except (IngestError, SQLAlchemyError) as finalization:
                        raise IngestError(
                            "run_finalization_unavailable", "run_finish"
                        ) from finalization
                    raise
        finally:
            engine.dispose()
