"""Explicit CLI entry point; validation never creates directories or a database."""

import argparse
import logging
import sys
from pathlib import Path
from uuid import uuid4

from signalnest.config import ConfigurationError, load_config
from signalnest.eventlog import Event, configure_logging, log_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signalnest", description="个人校园信息助手（项目骨架）")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("config-check", help="校验 TOML 配置，不创建本地存储")
    validate.add_argument("--config", type=Path, required=True, help="TOML 配置文件路径")
    initialize = commands.add_parser("storage-init", help="显式初始化本地存储并升级到最新迁移")
    initialize.add_argument("--config", type=Path, required=True, help="TOML 配置文件路径")
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
    log_event(logger, Event.CONFIG_VALIDATED, source_id=settings.source.id, run_id=run_id)
    print(f"配置有效: source_id={settings.source.id}")
    print(f"data_dir={settings.storage.data_dir}")
    print(f"database={settings.storage.database}")
    return 0
