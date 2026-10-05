"""Read-only diagnosis of persisted facts, with no lock, repair, migration or HTTP.

Foreground/history membership comes from the current run's successfully registered
first two pages and is not persisted. Status cannot recover that split from SQLite.
Runs do not store scan mode, so limited/interrupted coverage is never interpreted as
a full-scan attempt. A running row alone does not prove a live process or held lock.
All ages use the observation time, not publication dates or raw-response timestamps.
"""

import re
import sqlite3
import time
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from signalnest.config import Settings
from signalnest.contracts import Contract
from signalnest.errors import validate_time
from signalnest.schema import documents, ingestion_runs, source_ingestion_state
from signalnest.storage import StorageError, migration_config

COMPLETE_SCAN_STALE_SECONDS = 26 * 3600
FIRST_BACKLOG_STALE_SECONDS = 72 * 3600
RECHECK_OVERDUE_SECONDS = 24 * 3600
RECENT_RUN_LIMIT = 10


class SourceStatus(Contract):
    known: bool
    last_list_attempt_at: int | None = None
    last_list_response_at: int | None = None
    last_list_registered_at: int | None = None
    last_complete_scan_at: int | None = None
    last_complete_scan_run_id: str | None = None
    complete_scan_age_seconds: int | None = None
    bootstrap_completed_at: int | None = None
    not_before_at: int | None = None
    cooldown_active: bool = False
    cooldown_remaining_seconds: int = 0


class DocumentCounts(Contract):
    total: int
    discovered: int
    processed: int
    failed: int
    failed_without_success: int
    failed_with_success: int


class FirstProcessingBacklog(Contract):
    """All never-successful records, including failed records still in retry backoff."""

    total: int
    due: int
    deferred: int
    failed: int
    oldest_document_id: int | None = None
    oldest_discovered_at: int | None = None
    oldest_age_seconds: int | None = None
    oldest_error_code: str | None = None


class RecheckBacklog(Contract):
    """Successful baselines; processed NULL-due legacy records are reported separately."""

    total_with_success: int
    due: int
    deferred: int
    failed_due: int
    due_without_deadline: int
    unscheduled_successful: int
    oldest_due_document_id: int | None = None
    oldest_due_at: int | None = None
    oldest_overdue_seconds: int | None = None
    oldest_error_code: str | None = None


class RunStatus(Contract):
    run_id: str
    origin: str
    parser_version: str
    started_at: int
    finished_at: int | None
    result: str
    coverage: str
    coverage_at: int | None
    coverage_error_code: str | None
    error_code: str | None


class Diagnostic(Contract):
    code: str
    level: Literal["info", "warning"]
    message: str
    age_seconds: int | None = None
    threshold_seconds: int | None = None


class StatusReport(Contract):
    source_id: str
    observed_at: int
    source: SourceStatus
    documents: DocumentCounts
    first_processing: FirstProcessingBacklog
    rechecks: RecheckBacklog
    latest_run: RunStatus | None
    recent_runs: tuple[RunStatus, ...]
    unfinished_runs: int
    diagnostics: tuple[Diagnostic, ...]


def _read_only_engine(database: Path) -> Engine:
    if not database.is_absolute():
        raise StorageError("存储路径必须为绝对路径，请先加载配置")
    # mode=ro cannot create the database and query_only rejects accidental SQL writes.
    # No immutable=1: readers must observe live commits instead of ignoring journals.
    engine = sa.create_engine(
        "sqlite+pysqlite://",
        creator=lambda: sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5),
        poolclass=NullPool,
        hide_parameters=True,
    )

    @sa.event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    @sa.event.listens_for(engine, "begin")
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN")

    return engine


def _safe_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else "unrecognized_identifier"


def _safe_error(value: str | None) -> str | None:
    if value is None:
        return None
    return value if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) else "unrecognized_error_code"


def _count(connection: Connection, *conditions) -> int:
    return connection.execute(
        sa.select(sa.func.count()).select_from(documents).where(*conditions)
    ).scalar_one()


def _age(at: int, since: int | None) -> int | None:
    return None if since is None else max(0, at - since)


def _source_status(connection: Connection, source_id: str, at: int) -> SourceStatus:
    row = (
        connection.execute(
            sa.select(source_ingestion_state).where(source_ingestion_state.c.source_id == source_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return SourceStatus(known=False)
    deadline = row["not_before_at"]
    remaining = max(0, deadline - at) if deadline is not None else 0
    return SourceStatus(
        known=True,
        last_list_attempt_at=row["last_list_attempt_at"],
        last_list_response_at=row["last_list_response_at"],
        last_list_registered_at=row["last_list_registered_at"],
        last_complete_scan_at=row["last_complete_scan_at"],
        last_complete_scan_run_id=_safe_identifier(row["last_complete_scan_run_id"]),
        complete_scan_age_seconds=_age(at, row["last_complete_scan_at"]),
        bootstrap_completed_at=row["bootstrap_completed_at"],
        not_before_at=deadline,
        cooldown_active=remaining > 0,
        cooldown_remaining_seconds=remaining,
    )


def _document_status(connection: Connection, source_id: str, at: int):
    source = documents.c.source_id == source_id
    first = documents.c.last_success_at.is_(None)
    success = documents.c.last_success_at.is_not(None)
    due = sa.or_(documents.c.next_due_at.is_(None), documents.c.next_due_at <= at)
    failed = documents.c.status == "failed"
    counts = DocumentCounts(
        total=_count(connection, source),
        discovered=_count(connection, source, documents.c.status == "discovered"),
        processed=_count(connection, source, documents.c.status == "processed"),
        failed=_count(connection, source, failed),
        failed_without_success=_count(connection, source, failed, first),
        failed_with_success=_count(connection, source, failed, success),
    )
    oldest = connection.execute(
        sa.select(documents.c.id, documents.c.discovered_at, documents.c.last_error_code)
        .where(source, first)
        .order_by(documents.c.discovered_at, documents.c.id)
        .limit(1)
    ).first()
    first_total = _count(connection, source, first)
    first_due = _count(connection, source, first, due)
    backlog = FirstProcessingBacklog(
        total=first_total,
        due=first_due,
        deferred=first_total - first_due,
        failed=counts.failed_without_success,
        oldest_document_id=oldest.id if oldest else None,
        oldest_discovered_at=oldest.discovered_at if oldest else None,
        oldest_age_seconds=_age(at, oldest.discovered_at) if oldest else None,
        oldest_error_code=_safe_error(oldest.last_error_code) if oldest else None,
    )
    # Match existing due selection: a failed baseline without a deadline is retryable;
    # a processed legacy NULL deadline is not scheduled until an explicit crawl enrolls it.
    recheck_due = sa.and_(due, sa.or_(documents.c.next_due_at.is_not(None), failed))
    oldest_due = connection.execute(
        sa.select(documents.c.id, documents.c.next_due_at, documents.c.last_error_code)
        .where(source, success, documents.c.next_due_at <= at)
        .order_by(documents.c.next_due_at, documents.c.id)
        .limit(1)
    ).first()
    successful_count = _count(connection, source, success)
    due_count = _count(connection, source, success, recheck_due)
    unscheduled = _count(connection, source, success, documents.c.next_due_at.is_(None), ~failed)
    rechecks = RecheckBacklog(
        total_with_success=successful_count,
        due=due_count,
        deferred=successful_count - due_count - unscheduled,
        failed_due=_count(connection, source, success, failed, recheck_due),
        due_without_deadline=_count(
            connection, source, success, failed, documents.c.next_due_at.is_(None)
        ),
        unscheduled_successful=unscheduled,
        oldest_due_document_id=oldest_due.id if oldest_due else None,
        oldest_due_at=oldest_due.next_due_at if oldest_due else None,
        oldest_overdue_seconds=_age(at, oldest_due.next_due_at) if oldest_due else None,
        oldest_error_code=_safe_error(oldest_due.last_error_code) if oldest_due else None,
    )
    return counts, backlog, rechecks


def _run_status(row) -> RunStatus:
    return RunStatus(
        run_id=_safe_identifier(row["id"]),
        origin=_safe_identifier(row["origin"]),
        parser_version=_safe_identifier(row["parser_version"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=_safe_identifier(row["result"]),
        coverage=_safe_identifier(row["coverage"]),
        coverage_at=row["coverage_at"],
        coverage_error_code=_safe_error(row["coverage_error_code"]),
        error_code=_safe_error(row["error_code"]),
    )


def _diagnostics(source, backlog, rechecks, recent_runs, unfinished):
    notices = []
    if source.last_complete_scan_at is None:
        notices.append(
            Diagnostic(
                code="complete_scan_missing",
                level="warning",
                message="尚无完整扫描成功记录；首页成功或受限扫描不代表完整覆盖。",
                threshold_seconds=COMPLETE_SCAN_STALE_SECONDS,
            )
        )
    elif source.complete_scan_age_seconds > COMPLETE_SCAN_STALE_SECONDS:
        notices.append(
            Diagnostic(
                code="complete_scan_stale",
                level="warning",
                message="最近一次完整扫描成功已超过 26 小时。",
                age_seconds=source.complete_scan_age_seconds,
                threshold_seconds=COMPLETE_SCAN_STALE_SECONDS,
            )
        )
    if (
        backlog.oldest_age_seconds is not None
        and backlog.oldest_age_seconds > FIRST_BACKLOG_STALE_SECONDS
    ):
        notices.append(
            Diagnostic(
                code="first_processing_backlog_stale",
                level="warning",
                message="最早首次处理待办已等待超过 72 小时；含仍在失败退避的条目。",
                age_seconds=backlog.oldest_age_seconds,
                threshold_seconds=FIRST_BACKLOG_STALE_SECONDS,
            )
        )
    if (
        rechecks.oldest_overdue_seconds is not None
        and rechecks.oldest_overdue_seconds > RECHECK_OVERDUE_SECONDS
    ):
        notices.append(
            Diagnostic(
                code="rechecks_overdue",
                level="warning",
                message="最早成功基线复查已逾期超过 24 小时。",
                age_seconds=rechecks.oldest_overdue_seconds,
                threshold_seconds=RECHECK_OVERDUE_SECONDS,
            )
        )
    if rechecks.unscheduled_successful:
        notices.append(
            Diagnostic(
                code="successful_rechecks_unscheduled",
                level="warning",
                message="存在成功但尚未安排复查的旧记录；显式采集将纳入到期调度。",
            )
        )
    if source.cooldown_active:
        notices.append(
            Diagnostic(
                code="source_cooldown_active",
                level="info",
                message="来源冷却仍有效；不要通过密集重跑绕过服务端等待时间。",
            )
        )
    if unfinished:
        notices.append(
            Diagnostic(
                code="unfinished_runs",
                level="info",
                message="存在未收尾运行记录；仅凭数据库不能判断进程或写入锁是否仍活动。",
            )
        )
    finished = [run for run in recent_runs if run.finished_at is not None][:3]
    if len(finished) == 3 and all(
        run.error_code in {"request_limit", "run_time_limit"} for run in finished
    ):
        notices.append(
            Diagnostic(
                code="recent_budget_truncation",
                level="warning",
                message="最近三次已结束运行均遇请求或运行时间预算；未保存扫描模式，无法判断是否均为普通轮询。",
            )
        )
    return tuple(notices)


def inspect_status(settings: Settings, *, at: int | None = None) -> StatusReport:
    """Read one consistent snapshot at migration head; never initialize missing storage.

    A successful read is an observation, not a health guarantee or a scheduling action.
    Raw files and locks are not opened; report timestamps are integer UTC Unix seconds.
    Errors intentionally omit database paths, SQL parameters, HTML and exception text.
    """
    observed_at = int(time.time()) if at is None else at
    validate_time(observed_at)
    engine = _read_only_engine(settings.storage.database)
    try:
        with engine.begin() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
            head = ScriptDirectory.from_config(migration_config()).get_current_head()
            if revision != head:
                raise StorageError("数据库未初始化或未升级，请先执行 storage-init")
            source = _source_status(connection, settings.source.id, observed_at)
            counts, backlog, rechecks = _document_status(
                connection, settings.source.id, observed_at
            )
            recent = tuple(
                _run_status(row)
                for row in connection.execute(
                    sa.select(ingestion_runs)
                    .where(ingestion_runs.c.source_id == settings.source.id)
                    .order_by(ingestion_runs.c.started_at.desc(), ingestion_runs.c.id.desc())
                    .limit(RECENT_RUN_LIMIT)
                ).mappings()
            )
            unfinished = connection.execute(
                sa.select(sa.func.count())
                .select_from(ingestion_runs)
                .where(
                    ingestion_runs.c.source_id == settings.source.id,
                    ingestion_runs.c.result == "running",
                )
            ).scalar_one()
            return StatusReport(
                source_id=settings.source.id,
                observed_at=observed_at,
                source=source,
                documents=counts,
                first_processing=backlog,
                rechecks=rechecks,
                latest_run=recent[0] if recent else None,
                recent_runs=recent,
                unfinished_runs=unfinished,
                diagnostics=_diagnostics(source, backlog, rechecks, recent, unfinished),
            )
    except (SQLAlchemyError, CommandError) as exc:
        raise StorageError("数据库无法只读查询或未初始化，请检查存储并先执行 storage-init") from exc
    finally:
        engine.dispose()
