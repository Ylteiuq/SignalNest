"""Explicit CLI entry point; validation never creates directories or a database."""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from uuid import uuid4

from signalnest.config import ConfigurationError, load_config
from signalnest.eventlog import Event, configure_logging, log_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="signalnest", description="个人校园信息助手（离线处理与有界采集）"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    profile = commands.add_parser("profile-check", help="校验本地示例/个人画像；不访问存储或网络")
    profile.add_argument("--profile", type=Path, required=True)
    preview = commands.add_parser("decision-preview", help="纯本地预览事实、决策理由与路线；不发送")
    preview.add_argument("--profile", type=Path, required=True)
    input_group = preview.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--file", type=Path, help="原始 HTML；同时给出 --url")
    input_group.add_argument("--notice-json", type=Path, help="NoticeContent 规范 JSON 文件")
    preview.add_argument("--url", help="HTML 对应的最终详情 URL，不猜文件名")
    preview.add_argument("--at", required=True, help="显式决策时间 ISO-8601，必须含 UTC offset")
    preview.add_argument(
        "--next-digest-at", required=True, help="显式下次 Digest 时间，含 UTC offset"
    )
    preview.add_argument(
        "--event-kind",
        choices=("new", "update", "activation_recent", "historical"),
        default="new",
        help="仅为预览上下文，不建立真实事件",
    )
    preview.add_argument("--mode", choices=("hybrid", "digest_only"), default="hybrid")
    preview.add_argument(
        "--previous-notice-json", type=Path, help="update 的旧 NoticeContent；本地读取"
    )
    preview.add_argument(
        "--previous-route", choices=("none", "immediate", "digest"), default="none"
    )
    validate = commands.add_parser("config-check", help="校验 TOML 配置，不创建本地存储")
    validate.add_argument("--config", type=Path, required=True, help="TOML 配置文件路径")
    initialize = commands.add_parser("storage-init", help="显式初始化本地存储并升级到最新迁移")
    initialize.add_argument("--config", type=Path, required=True, help="TOML 配置文件路径")
    local = commands.add_parser("import-page", help="归档并解析已有页面；不发起 HTTP 获取")
    local.add_argument("--config", type=Path, required=True)
    local.add_argument(
        "--metadata", type=Path, required=True, help="包含获取时间和来源的 JSON 文件"
    )
    local.add_argument("--file", type=Path, help="完整原始 HTML 文件；304/不可用正文时省略")
    local.add_argument("--processed-at", type=int, help="本次处理 UTC Unix 秒，默认当前处理时间")
    replay = commands.add_parser("reparse", help="验证并重新解析已登记原文，复用原获取证据")
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--response-id", type=int, required=True)
    replay.add_argument("--processed-at", type=int, help="本次处理 UTC Unix 秒，默认当前处理时间")
    status = commands.add_parser("status", help="只读诊断积压、冷却、覆盖与最近运行；不联网")
    status.add_argument("--config", type=Path, required=True)
    policy = commands.add_parser(
        "apply-recheck-policy", help="显式保守重算成功记录到期时间；不联网"
    )
    policy.add_argument("--config", type=Path, required=True)
    scheduled = commands.add_parser("scheduled-run", help="按 TOML 中有界预算执行一次定时任务")
    scheduled.add_argument("--config", type=Path, required=True)
    scheduled.add_argument("--mode", choices=("regular", "full"), required=True)
    crawl = commands.add_parser("crawl-once", help="有界执行一次列表扫描与独立详情处理")
    crawl.add_argument("--config", type=Path, required=True)
    crawl.add_argument("--scan", dest="scan_mode", choices=("full", "limited"), required=True)
    crawl.add_argument("--max-pages", type=int, help="列表页上限；full 默认 64，limited 默认 2")
    crawl.add_argument("--max-details", type=int, default=20, help="详情尝试上限，默认 20")
    crawl.add_argument("--max-requests", type=int, default=120, help="实际 HTTP 请求上限，默认 120")
    crawl.add_argument("--run-seconds", type=float, default=600.0, help="运行时间预算，默认 600 秒")
    crawl.add_argument(
        "--resource-seconds", type=float, default=60.0, help="每个资源时间预算，默认 60 秒"
    )
    crawl.add_argument(
        "--max-body-bytes", type=int, default=2 * 1024 * 1024, help="每份响应正文上限，默认 2 MiB"
    )
    args = parser.parse_args(argv)
    if args.command in {"profile-check", "decision-preview"}:
        return _notification_preview(args)
    logger = configure_logging()
    run_id = uuid4().hex
    try:
        settings = load_config(args.config)
    except ConfigurationError as exc:
        log_event(logger, Event.CONFIG_INVALID, level=logging.ERROR, run_id=run_id)
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if args.command == "storage-init":
        from signalnest.storage import StorageError, initialize_storage

        try:
            revision = initialize_storage(settings.storage)
        except StorageError as exc:
            log_event(
                logger,
                Event.STORAGE_INIT_FAILED,
                level=logging.ERROR,
                source_id=settings.source.id,
                run_id=run_id,
            )
            print(f"存储错误: {exc}", file=sys.stderr)
            return 1
        log_event(logger, Event.STORAGE_INITIALIZED, source_id=settings.source.id, run_id=run_id)
        print(f"存储就绪: database={settings.storage.database}, revision={revision}")
        return 0
    if args.command in {"import-page", "reparse"}:
        return _offline_command(args, settings, logger, run_id)
    if args.command == "crawl-once":
        return _crawl_command(args, settings, logger, run_id)
    if args.command == "scheduled-run":
        budget = getattr(settings.runtime, args.mode)
        args.scan_mode = "limited" if args.mode == "regular" else "full"
        for key, value in budget.model_dump().items():
            setattr(args, key, value)
        return _crawl_command(args, settings, logger, run_id)
    if args.command in {"status", "apply-recheck-policy"}:
        return _maintenance_command(args, settings, logger, run_id)
    log_event(logger, Event.CONFIG_VALIDATED, source_id=settings.source.id, run_id=run_id)
    print(f"配置有效: source_id={settings.source.id}")
    print(f"data_dir={settings.storage.data_dir}")
    print(f"database={settings.storage.database}")
    return 0


def _notification_preview(args):
    from datetime import datetime

    from pydantic import ValidationError

    from signalnest.contracts import NoticeContent, PageInput
    from signalnest.notifications.contracts import EventContext, aware_time
    from signalnest.notifications.decision import decide
    from signalnest.notifications.facts import extract_facts
    from signalnest.notifications.profile import ProfileError, load_profile
    from signalnest.parsing import ParseError, parse_notice

    # Reads are explicit local files. No config, database, lock, HTTP client or clock.
    limit = 4 * 1024 * 1024

    def read(path):
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError("local preview input exceeds 4 MiB")
        return value

    def content(path):
        return NoticeContent.model_validate_json(read(path))

    def explicit_time(value, field):
        try:
            return aware_time(datetime.fromisoformat(value))
        except ValueError as exc:
            raise ValueError(f"{field} requires ISO-8601 with an explicit UTC offset") from exc

    try:
        profile = load_profile(args.profile)
        if args.command == "profile-check":
            print(
                json.dumps(
                    {"profile_valid": True, "profile_sha256": profile.sha256()}, ensure_ascii=False
                )
            )
            return 0
        now = explicit_time(args.at, "--at")
        next_digest = explicit_time(args.next_digest_at, "--next-digest-at")
        if args.file:
            if not args.url:
                raise ValueError("HTML input requires --url with its final page URL")
            notice = parse_notice(PageInput(content=read(args.file), page_url=args.url)).content
        else:
            if args.url:
                raise ValueError("--url is used only with --file")
            notice = content(args.notice_json)
        previous = content(args.previous_notice_json) if args.previous_notice_json else None
        if (previous is not None or args.previous_route != "none") and args.event_kind != "update":
            raise ValueError("previous content/route is only valid for update previews")
        context = EventContext(
            kind=args.event_kind,
            notification_mode=args.mode,
            next_digest_at=next_digest,
            previous_facts=extract_facts(previous) if previous is not None else None,
            previous_effective_route=args.previous_route,
            comparison_known=args.event_kind != "update" or previous is not None,
        )
        facts = extract_facts(notice)
        decision = decide(profile, facts, context, now=now)
    except ProfileError as exc:
        print(f"画像错误: {exc}", file=sys.stderr)
        return 2
    except ParseError as exc:
        print(f"解析失败: {exc.code}；未生成决策，请检查页面及 Parser 支持范围", file=sys.stderr)
        return 1
    except OSError:
        print("预览输入不可读；请检查本地文件路径和权限", file=sys.stderr)
        return 2
    except ValidationError:
        print("预览输入无效；检查 NoticeContent、HTTP(S) URL 及带时区的时间", file=sys.stderr)
        return 2
    except ValueError as exc:
        # Local validation failures have no successful empty-result fallback.
        print(f"预览参数错误: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "preview_only": True,
                "facts": facts.model_dump(mode="json", exclude={"body_text"}),
                "decision": decision.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _maintenance_command(args, settings, logger, run_id):
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.errors import IngestError
    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.runtime_policy import apply_recheck_policy
    from signalnest.status import inspect_status
    from signalnest.storage import StorageError, open_initialized_engine

    try:
        if args.command == "status":
            result = inspect_status(settings).model_dump(mode="json")
            event = Event.STATUS_READ
        else:
            if not settings.storage.database.is_file():
                raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
            with writer_lock(settings.storage.database):
                engine = open_initialized_engine(settings.storage.database)
                try:
                    at = int(time.time())
                    changed = apply_recheck_policy(engine, settings.source.id, at, settings.runtime)
                finally:
                    engine.dispose()
            result = {"source_id": settings.source.id, "processed_at": at, "changed": changed}
            event = Event.POLICY_APPLIED
    except (StorageError, IngestError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code if isinstance(exc, (IngestError, WriterLockError)) else "database_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            stage=args.command,
            error_code=code,
        )
        print(f"状态或策略操作失败: {code}；请检查存储路径、初始化和锁占用", file=sys.stderr)
        return 1
    log_event(logger, event, source_id=settings.source.id, run_id=run_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _crawl_command(args, settings, logger, run_id) -> int:
    # Importing help/config-check neither builds an HTTP client nor acquires a lock.
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.crawling import CrawlOptions, crawl_once
    from signalnest.fetching import FetchLimits
    from signalnest.ingestion import IngestError
    from signalnest.instance_lock import WriterLockError
    from signalnest.rawstore import RawStoreError
    from signalnest.storage import StorageError

    try:
        options = CrawlOptions(
            scan_mode=args.scan_mode,
            max_pages=args.max_pages
            if args.max_pages is not None
            else (64 if args.scan_mode == "full" else 2),
            max_details=args.max_details,
            fetch_limits=FetchLimits(
                max_requests=args.max_requests,
                run_seconds=args.run_seconds,
                resource_seconds=args.resource_seconds,
                max_body_bytes=args.max_body_bytes,
            ),
        )
    except ValidationError:
        print("采集参数错误: 检查页数、详情数、请求数、时间预算与正文上限", file=sys.stderr)
        return 2
    try:
        summary = crawl_once(settings, options, run_id=run_id)
    except KeyboardInterrupt:
        print("采集已中断；请检查运行记录，下一次采集会从数据库事实恢复待办", file=sys.stderr)
        return 130
    except (StorageError, IngestError, RawStoreError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code
            if isinstance(exc, (IngestError, RawStoreError, WriterLockError))
            else "database_unavailable"
        )
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            error_code=code,
            stage=exc.stage if isinstance(exc, IngestError) else "storage",
            response_id=exc.response_id if isinstance(exc, IngestError) else None,
            document_id=exc.document_id if isinstance(exc, IngestError) else None,
        )
        detail = str(exc) if not isinstance(exc, SQLAlchemyError) else code
        print(f"采集失败: {detail}；未报告成功，请检查运行证据与本地存储", file=sys.stderr)
        return 1
    except Exception:
        # Fatal CLI boundary: no arbitrary exception text or claim of saved recovery.
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            error_code="unexpected_error",
            stage="internal",
        )
        print("内部采集错误: 未报告成功，也未确认恢复状态已保存；请修复后重试", file=sys.stderr)
        return 1
    print(summary.model_dump_json())
    return 0 if summary.result == "succeeded" else 1


def _offline_command(args, settings, logger, run_id) -> int:
    # Lazy imports keep help/config-check independent of storage operations.
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError

    from signalnest.ingestion import IngestError, ResponseInput, import_page, process_response
    from signalnest.instance_lock import WriterLockError, writer_lock
    from signalnest.rawstore import RawStore, RawStoreError
    from signalnest.storage import StorageError, open_initialized_engine

    evidence = None
    content = None
    if args.command == "import-page":
        try:
            evidence = ResponseInput.model_validate_json(args.metadata.read_bytes())
        except OSError:
            print("导入元数据错误: 无法读取 --metadata 文件", file=sys.stderr)
            return 2
        except ValidationError as exc:
            # Locations only; Pydantic messages/input values may contain untrusted data.
            fields = ", ".join(
                ".".join(str(part) for part in error["loc"]) or "metadata"
                for error in exc.errors(include_input=False)
                if error["loc"] and error["loc"][0] in ResponseInput.model_fields
            )
            print(f"导入元数据错误: 检查 JSON 格式及必要字段 ({fields})", file=sys.stderr)
            return 2
        if evidence.source_id != settings.source.id:
            print("导入元数据错误: source_id 必须与配置一致", file=sys.stderr)
            return 2
        if args.file is not None and (
            evidence.status_code == 304 or evidence.body_state == "unavailable"
        ):
            print("导入参数错误: 304 或不可用正文必须省略 --file", file=sys.stderr)
            return 2
        if args.file is not None:
            try:
                content = args.file.read_bytes()
            except OSError:
                print("导入文件错误: 无法读取 --file 文件", file=sys.stderr)
                return 1
    engine = None
    try:
        if not settings.storage.database.is_file():
            raise StorageError("数据库不可用或未初始化，请先执行 storage-init")
        with writer_lock(settings.storage.database):
            engine = open_initialized_engine(settings.storage.database)
            raw_store = RawStore(settings.storage.data_dir)
            processed_at = args.processed_at if args.processed_at is not None else int(time.time())
            if evidence is not None:
                result = import_page(
                    engine, raw_store, evidence, content, processed_at, run_id=run_id
                )
            else:
                result = process_response(
                    engine,
                    raw_store,
                    args.response_id,
                    processed_at,
                    expected_source_id=settings.source.id,
                    run_id=run_id,
                )
    except (StorageError, IngestError, RawStoreError, WriterLockError, SQLAlchemyError) as exc:
        code = (
            exc.code
            if isinstance(exc, (IngestError, RawStoreError, WriterLockError))
            else "database_unavailable"
        )
        stage = exc.stage if isinstance(exc, IngestError) else "storage"
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            error_code=code,
            stage=stage,
            response_id=exc.response_id if isinstance(exc, IngestError) else None,
            document_id=exc.document_id if isinstance(exc, IngestError) else None,
        )
        # Never print SQLAlchemy's statements/parameters or arbitrary exception messages.
        detail = (
            str(exc)
            if isinstance(exc, (StorageError, IngestError, RawStoreError, WriterLockError))
            else code
        )
        print(f"离线处理失败: {detail}；未报告成功，请检查原文、元数据或本地存储", file=sys.stderr)
        return 1
    except Exception:
        # Last CLI boundary only: business functions still propagate programming defects.
        # Do not expose arbitrary exception values, or claim failure recovery was saved.
        log_event(
            logger,
            Event.PROCESSING_FAILED,
            level=logging.ERROR,
            source_id=settings.source.id,
            run_id=run_id,
            error_code="unexpected_error",
            stage="internal",
        )
        print(
            "内部处理错误: 未报告成功，也未确认失败状态已保存；请检查响应证据并修复程序后重试",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    from dataclasses import asdict

    payload = asdict(result)
    if result.pagination is not None:
        payload["pagination"] = result.pagination.model_dump(mode="json")
    print(json.dumps(payload, ensure_ascii=False))
    return 0
