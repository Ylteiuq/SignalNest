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
    parser = argparse.ArgumentParser(prog="signalnest", description="个人校园信息助手（离线闭环）")
    commands = parser.add_subparsers(dest="command", required=True)
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
    args = parser.parse_args(argv)
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
    log_event(logger, Event.CONFIG_VALIDATED, source_id=settings.source.id, run_id=run_id)
    print(f"配置有效: source_id={settings.source.id}")
    print(f"data_dir={settings.storage.data_dir}")
    print(f"database={settings.storage.database}")
    return 0


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
