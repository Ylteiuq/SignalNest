"""Explicit CLI entry point; validation never creates directories or a database."""

import argparse
import sys
from pathlib import Path

from campus_information_agent.config import ConfigurationError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="campus-information-agent", description="个人校园信息助手（项目骨架）"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("config-check", help="校验 TOML 配置，不创建本地存储")
    validate.add_argument("--config", type=Path, required=True, help="TOML 配置文件路径")
    initialize = commands.add_parser("storage-init", help="显式初始化本地存储并升级到最新迁移")
    initialize.add_argument("--config", type=Path, required=True, help="TOML 配置文件路径")
    args = parser.parse_args(argv)
    try:
        settings = load_config(args.config)
    except ConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if args.command == "storage-init":
        from campus_information_agent.storage import StorageError, initialize_storage

        try:
            revision = initialize_storage(settings.storage)
        except StorageError as exc:
            print(f"存储错误: {exc}", file=sys.stderr)
            return 1
        print(f"存储就绪: database={settings.storage.database}, revision={revision}")
        return 0
    print(f"配置有效: source_id={settings.source.id}")
    print(f"data_dir={settings.storage.data_dir}")
    print(f"database={settings.storage.database}")
    return 0
