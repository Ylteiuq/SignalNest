"""Explicit local initialization and synchronous SQLite connections, with no import I/O."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.util.exc import CommandError
from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.config import StorageSettings


class StorageError(RuntimeError):
    """Storage initialization failed; never report a successful migration."""


def make_engine(database: Path) -> Engine:
    """Create a lazy engine; opening a connection is an explicit caller action."""
    if not database.is_absolute():
        raise ValueError("database path must be absolute; use load_config first")
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(database)),
        connect_args={"timeout": 5},
        hide_parameters=True,
    )

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection, connection_record):
        # Let SQLAlchemy emit BEGIN, including for DDL. Enable FKs outside a transaction.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN")

    return engine


def migration_config() -> Config:
    """Migration assets are packaged with the application, independent of cwd."""
    config = Config()
    location = str(Path(__file__).parent / "migrations")
    config.set_main_option("script_location", location.replace("%", "%%"))
    return config


def initialize_storage(settings: StorageSettings) -> str:
    """Create local directories and upgrade to head, preserving existing records.

    Filesystem directories may remain after a failure; schema changes are transactional.
    No network requests or content processing take place in this transaction.
    """
    engine = None
    try:
        if not settings.data_dir.is_absolute() or not settings.database.is_absolute():
            raise StorageError("存储路径必须为绝对路径，请先加载配置")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "raw").mkdir(exist_ok=True)
        settings.database.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(settings.database)
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            revision = MigrationContext.configure(connection).get_current_revision()
            if revision is None:
                raise StorageError("迁移未建立版本记录")
        return revision
    except OSError as exc:
        raise StorageError(f"无法创建存储目录：{exc.strerror}") from exc
    except (SQLAlchemyError, CommandError) as exc:
        # Database exceptions can include SQL and values; do not echo them to the CLI.
        raise StorageError(
            "数据库初始化或升级失败；请检查数据库文件、权限、锁占用及迁移版本，"
            "不要删除现有数据来重试"
        ) from exc
    finally:
        if engine is not None:
            engine.dispose()
