"""Derived search rows commit with the real notice business success."""

import pytest
import sqlalchemy as sa
from bs4 import BeautifulSoup
from test_ingestion import discovered as discovered
from test_ingestion import env as env
from test_ingestion import evidence, raw, target
from test_notification_service import ACTIVATED_AT, NOTICE, live, opportunity, rows
from test_notification_service import activated as activated

from signalnest.ingestion import IngestError, import_page, process_response
from signalnest.schema import (
    documents,
    email_outbox,
    http_resources,
    notice_versions,
    notification_decisions,
    notification_events,
    notification_observations,
    raw_responses,
    search_documents,
)
from signalnest.search import SearchQuery, search_notices
from signalnest.storage import open_initialized_engine


def changed_body():
    soup = BeautifulSoup(raw(), "html.parser")
    paragraph = soup.new_tag("p")
    paragraph.string = "搜索回归新增词"
    soup.select_one("#vsb_content > .v_news_content").append(paragraph)
    return str(soup).encode()


def test_import_repeat_reparse_and_content_restore_update_index(discovered):
    env = discovered
    first = import_page(env.engine, env.store, evidence(), raw(), 102)
    assert (
        search_notices(env.engine, SearchQuery(query="选课")).items[0].version_id
        == first.version_id
    )
    changed = changed_body()
    second = import_page(env.engine, env.store, evidence(fetched_at=103), changed, 104)
    assert second.version_id != first.version_id
    assert search_notices(env.engine, SearchQuery(query="搜索回归新增词")).total == 1
    restored = import_page(env.engine, env.store, evidence(fetched_at=105), raw(), 106)
    assert restored.version_id == first.version_id
    assert search_notices(env.engine, SearchQuery(query="搜索回归新增词")).total == 0
    assert (
        process_response(env.engine, env.store, second.response_id, 107).version_id
        == second.version_id
    )
    # Explicit maintenance reparse is allowed to select historical content; automatic cache
    # recovery uses a different API and keeps its latest-body validation.
    assert search_notices(env.engine, SearchQuery(query="搜索回归新增词")).total == 1
    with env.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(search_documents)
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(notice_versions)).scalar_one()
            == 2
        )


@pytest.mark.parametrize("has_success", [False, True])
def test_index_commit_failure_rolls_back_version_success_and_retains_recoverable_raw(
    discovered, has_success
):
    env = discovered
    initial = import_page(env.engine, env.store, evidence(), raw(), 102) if has_success else None
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_search BEFORE INSERT ON search_documents "
            "BEGIN SELECT RAISE(ABORT, 'PRIVATE_SQL_FAILURE'); END"
        )
    changed = changed_body()
    with pytest.raises(IngestError, match="database_write_failed") as caught:
        import_page(env.engine, env.store, evidence(fetched_at=103), changed, 104, next_due_at=200)
    failed_response_id = caught.value.response_id
    doc = target(env)
    assert doc["status"] == "failed"
    assert doc["current_version_id"] == (initial.version_id if initial else None)
    assert doc["last_success_at"] == (102 if initial else None)
    assert doc["next_due_at"] is None
    with env.engine.connect() as connection:
        assert connection.execute(
            sa.select(sa.func.count()).select_from(notice_versions)
        ).scalar_one() == int(has_success)
        row = (
            connection.execute(
                sa.select(raw_responses).where(raw_responses.c.id == failed_response_id)
            )
            .mappings()
            .one()
        )
        assert row["last_attempt_at"] == 104
        assert row["last_error_code"] == "database_write_failed"
        assert env.store.read(row["body_path"], row["body_sha256"]) == changed
        assert connection.execute(
            sa.select(sa.func.count()).select_from(search_documents)
        ).scalar_one() == int(has_success)
    env.engine.dispose()
    env.engine = open_initialized_engine(env.settings.database)
    if initial:
        assert (
            search_notices(env.engine, SearchQuery(query="选课")).items[0].version_id
            == initial.version_id
        )
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER fail_search")
    result = process_response(env.engine, env.store, failed_response_id, 105)
    assert result.version_id != (initial.version_id if initial else None)
    assert search_notices(env.engine, SearchQuery(query="搜索回归新增词")).total == 1
    with env.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(documents.c.status).where(documents.c.id == doc["id"])
            ).scalar_one()
            == "processed"
        )


def test_live_index_failure_also_rolls_back_notification_decisions_intents_and_resource(activated):
    env = activated
    first = live(env, opportunity())
    tables = (notification_events, notification_decisions, notification_observations, email_outbox)
    before = {table.name: rows(env, table) for table in tables}
    resource = next(row for row in rows(env, http_resources) if row["request_uri"] == NOTICE)
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_search BEFORE INSERT ON search_documents "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    changed = opportunity().replace(
        "国际交流项目报名。".encode(), "国际交流项目报名。名额五人。".encode()
    )
    with pytest.raises(IngestError, match="database_write_failed"):
        live(env, changed, at=ACTIVATED_AT + 5)
    for table in tables:
        assert rows(env, table) == before[table.name]
    after = next(row for row in rows(env, http_resources) if row["request_uri"] == NOTICE)
    for field in (
        "last_processed_response_id",
        "last_processed_parser_version",
        "last_processed_at",
    ):
        assert after[field] == resource[field]
    assert len(rows(env, notice_versions)) == 1
    assert (
        search_notices(env.engine, SearchQuery(query="国际交流")).items[0].version_id
        == first.version_id
    )
