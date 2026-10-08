"""Upgrade genuine populated N1 storage; faults are injected DDL exceptions."""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.runtime.migration import MigrationContext
from test_notification_service import (
    ACTIVATED_AT,
    OPTIONS,
    SOURCE,
    USER_PROFILE,
    complete_scan_fact,
    discover_historical,
    live,
    opportunity,
    start_live,
)

from signalnest.config import StorageSettings
from signalnest.mail.contracts import PlanOptions
from signalnest.mail.planning import plan_mail
from signalnest.notifications.state import activate_notifications
from signalnest.rawstore import RawStore
from signalnest.schema import (
    email_outbox,
    mail_message_members,
    mail_messages,
    mail_plan_errors,
    metadata,
    notification_events,
)
from signalnest.storage import initialize_storage, make_engine, migration_config

NEW_TABLES = {"mail_messages", "mail_message_members", "mail_plan_errors"}


N4_TABLES = {"mail_delivery", "mail_attempts", "notification_operations"}


def snapshot(engine):
    with engine.connect() as connection:
        return {
            name: [
                {k: v for k, v in dict(row).items() if k not in {"pause_reason", "paused_at"}}
                for row in connection.execute(
                    sa.select(sa.Table(name, sa.MetaData(), autoload_with=connection))
                ).mappings()
            ]
            for name in sa.inspect(connection).get_table_names()
            if name != "alembic_version" and name not in NEW_TABLES | N4_TABLES
        }


@pytest.mark.parametrize("failure_table", [None, *sorted(NEW_TABLES)])
def test_0004_upgrade_preserves_pending_events_and_intents(tmp_path, failure_table, monkeypatch):
    settings = StorageSettings(
        data_dir=str(tmp_path / "data"), database=str(tmp_path / "db.sqlite")
    )
    (settings.data_dir / "raw").mkdir(parents=True)
    engine = make_engine(settings.database)
    env = SimpleNamespace(settings=settings, engine=engine, store=RawStore(settings.data_dir))
    try:
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0004_notification_state")
        from signalnest.notifications import state as notification_state

        # This test seeds the historical N1 schema with its actual column set.
        # Production entry points always require head; no legacy fallback is added.
        with engine.connect() as connection:
            old_channel = sa.Table(
                "notification_channel_state", sa.MetaData(), autoload_with=connection
            )
        monkeypatch.setattr(notification_state, "channel", old_channel)
        discover_historical(env)
        complete_scan_fact(env)
        activate_notifications(engine, SOURCE, USER_PROFILE, OPTIONS, at=ACTIVATED_AT)
        start_live(env)
        live(env)
        live(env, opportunity(), at=ACTIVATED_AT + 5)
        original = snapshot(engine)
        assert len(original[notification_events.name]) == 2
        assert len(original[email_outbox.name]) == 1
        raw_files = {path.name: path.read_bytes() for path in (settings.data_dir / "raw").iterdir()}

        def inject(connection, cursor, statement, parameters, context, executemany):
            if failure_table and statement.lstrip().startswith(f"CREATE TABLE {failure_table} "):
                raise RuntimeError("injected N2 migration failure")

        if failure_table:
            sa.event.listen(sa.engine.Engine, "after_cursor_execute", inject)
            try:
                with pytest.raises(RuntimeError, match="injected N2 migration failure"):
                    initialize_storage(settings)
            finally:
                sa.event.remove(sa.engine.Engine, "after_cursor_execute", inject)
            assert snapshot(engine) == original
            with engine.connect() as connection:
                assert (
                    MigrationContext.configure(connection).get_current_revision()
                    == "0004_notification_state"
                )
                assert not NEW_TABLES.intersection(sa.inspect(connection).get_table_names())

        assert initialize_storage(settings) == "0006_mail_sending"
        assert initialize_storage(settings) == "0006_mail_sending"
        assert snapshot(engine) == original
        with engine.connect() as connection:
            for table in (mail_messages, mail_message_members, mail_plan_errors):
                assert connection.execute(sa.select(table)).all() == []
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert {
            path.name: path.read_bytes() for path in (settings.data_dir / "raw").iterdir()
        } == raw_files
        # Migration itself never allocates historical mail. An explicit plan can
        # consume the already eligible N1 intent after upgrade.
        monkeypatch.setattr(
            notification_state, "channel", metadata.tables["notification_channel_state"]
        )
        result = plan_mail(engine, SOURCE, PlanOptions(), at=ACTIVATED_AT + 20)
        assert result["planned"] == 1
        assert result["deferred_digest"] == 1
    finally:
        engine.dispose()
