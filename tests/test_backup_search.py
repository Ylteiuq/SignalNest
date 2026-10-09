"""A restorable lexical index needs its schema, not fresh derived contents."""

import hashlib
import sqlite3

import pytest
from test_backup_verification import BackupVerificationError, verify_backup
from test_backup_verification import backup as backup

from signalnest.instance_lock import writer_lock
from signalnest.search import SearchError, SearchQuery, rebuild_index, search_notices
from signalnest.storage import open_initialized_engine


def test_stale_derived_backup_passes_readonly_verification_then_explicitly_rebuilds(backup):
    database, data_dir = backup
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM search_documents")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    assert verify_backup(database, data_dir)["notice_versions"] == 1
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    engine = open_initialized_engine(database)
    try:
        with pytest.raises(SearchError, match="search_index_stale"):
            search_notices(engine, SearchQuery(query="选课"))
        with writer_lock(database):
            assert rebuild_index(engine).indexed_count == 1
        result = search_notices(engine, SearchQuery(query="本科生选课"))
        assert result.total == 1
        assert result.items[0].original_url.endswith("/1517/128231.htm")
        assert verify_backup(database, data_dir)["notice_versions"] == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "object_name, object_type",
    [
        ("search_fts", "TABLE"),
        ("search_documents", "TABLE"),
        ("search_documents_insert", "TRIGGER"),
        ("search_documents_delete", "TRIGGER"),
        ("search_documents_update", "TRIGGER"),
    ],
)
def test_missing_index_schema_objects_fail_before_restore(backup, object_name, object_type):
    database, data_dir = backup
    with sqlite3.connect(database) as connection:
        connection.execute(f"DROP {object_type} {object_name}")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(BackupVerificationError, match="backup_search_schema_missing"):
        verify_backup(database, data_dir)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_regular_table_cannot_impersonate_the_fts_virtual_table(backup):
    database, data_dir = backup
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE search_fts")
        connection.execute("CREATE TABLE search_fts(title_text TEXT, body_text TEXT)")
    with pytest.raises(BackupVerificationError, match="backup_search_schema_missing"):
        verify_backup(database, data_dir)


def test_wrong_object_type_cannot_impersonate_a_maintenance_trigger(backup):
    database, data_dir = backup
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER search_documents_insert")
        connection.execute("CREATE TABLE search_documents_insert(id INTEGER)")
    with pytest.raises(BackupVerificationError, match="backup_search_schema_missing"):
        verify_backup(database, data_dir)
