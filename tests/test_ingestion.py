"""Offline integration with immutable fixtures, real SQLite and fault injection."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from signalnest.config import StorageSettings
from signalnest.contracts import PageInput
from signalnest.ingestion import (
    IngestError,
    ResponseInput,
    discover_page,
    import_page,
    process_response,
    record_response,
)
from signalnest.parsing import PARSER_VERSION, parse_list, parse_notice
from signalnest.rawstore import RawStore
from signalnest.schema import documents, notice_versions, raw_responses
from signalnest.storage import initialize_storage, open_initialized_engine

FIXTURES = Path(__file__).resolve().parents[1] / "research/fixtures"
SOURCE = "whu-undergrad-student"
LIST_URL = "https://uc.whu.edu.cn/tzgg/xstz.htm"
NOTICE_URL = "https://uc.whu.edu.cn/info/1517/128231.htm"
LEGACY_URL = "https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=127581"


def raw(name="current-notice-detail.html"):
    return (FIXTURES / name).read_bytes()


def evidence(kind="notice", url=NOTICE_URL, identity="1517:128231", **overrides):
    values = dict(
        page_type=kind,
        source_id=SOURCE,
        source_document_id=identity if kind == "notice" else None,
        requested_url=url,
        final_url=url,
        fetched_at=100,
        status_code=200,
    )
    return ResponseInput(**(values | overrides))


def rows(env, table):
    with env.engine.connect() as connection:
        return connection.execute(sa.select(table).order_by(table.c.id)).mappings().all()


def target(env, identity="1517:128231"):
    return next(row for row in rows(env, documents) if row["source_document_id"] == identity)


def discover(env):
    for name, url in [
        ("student-notices-page1.html", LIST_URL),
        ("student-notices-page2.html", "https://uc.whu.edu.cn/tzgg/xstz/23.htm"),
    ]:
        import_page(env.engine, env.store, evidence("list", url), raw(name), 101)


@pytest.fixture
def env(tmp_path):
    settings = StorageSettings(
        data_dir=str(tmp_path / "data"), database=str(tmp_path / "db.sqlite3")
    )
    initialize_storage(settings)
    engine = open_initialized_engine(settings.database)
    env = SimpleNamespace(settings=settings, engine=engine, store=RawStore(settings.data_dir))
    yield env
    env.engine.dispose()


@pytest.fixture
def discovered(env):
    discover(env)
    return env


def test_fifty_discovered_two_processed_and_repeat_import(discovered):
    env = discovered
    first = import_page(env.engine, env.store, evidence(), raw(), 102)
    second = import_page(
        env.engine,
        env.store,
        evidence(url=LEGACY_URL, identity="1517:127581"),
        raw("legacy-notice-detail.html"),
        102,
    )
    assert first.version_id != second.version_id
    states = [row["status"] for row in rows(env, documents)]
    assert states.count("processed") == 2
    assert states.count("discovered") == 48
    assert len(rows(env, documents)) == 50
    assert len(rows(env, notice_versions)) == 2
    discover(env)
    repeat = import_page(env.engine, env.store, evidence(), raw(), 103)
    assert repeat.version_id == first.version_id
    assert repeat.response_id != first.response_id
    assert len(rows(env, notice_versions)) == 2
    assert len(rows(env, raw_responses)) == 7
    assert len(list((env.settings.data_dir / "raw").glob("*.bin"))) == 4
    assert target(env)["discovered_at"] == 101
    assert rows(env, notice_versions)[0]["raw_response_id"] == first.response_id


def test_rediscovery_updates_labels_only_and_preserves_failure(discovered):
    env = discovered
    with pytest.raises(IngestError):
        import_page(env.engine, env.store, evidence(), b"<html>bad page</html>", 102)
    before = target(env)
    page = parse_list(PageInput(content=raw("student-notices-page1.html"), page_url=LIST_URL))
    entry = next(item for item in page.entries if item.source_document_id == "1517:128231")
    changed = entry.model_copy(update={"title": "更新列表标题", "detail_url": entry.detail_url})
    discover_page(env.engine, SOURCE, page.model_copy(update={"entries": (changed,)}), 103)
    after = target(env)
    assert after["discovered_title"] == "更新列表标题"
    for field in ["id", "discovered_at", "status", "last_attempt_at", "last_error_code"]:
        assert after[field] == before[field]


def test_page_discovery_transaction_rolls_back_all_entries(env):
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_second BEFORE INSERT ON documents "
            "WHEN NEW.source_document_id = '1517:128481' "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    page = parse_list(PageInput(content=raw("student-notices-page1.html"), page_url=LIST_URL))
    assert page.entries[1].source_document_id == "1517:128481"
    with pytest.raises(IngestError, match="database_write_failed"):
        import_page(
            env.engine,
            env.store,
            evidence("list", LIST_URL),
            raw("student-notices-page1.html"),
            101,
        )
    assert rows(env, documents) == []
    response = rows(env, raw_responses)[0]
    assert response["last_error_code"] == "database_write_failed"
    assert response["last_attempt_at"] == 101
    assert env.store.read(response["body_path"], response["body_sha256"])


def test_failure_keeps_evidence_retries_and_does_not_shortcut(discovered):
    env = discovered
    with pytest.raises(IngestError) as caught:
        import_page(env.engine, env.store, evidence(), b"<html>bad page</html>", 102)
    response_id = caught.value.response_id
    assert target(env)["current_version_id"] is None
    assert target(env)["last_error_code"] == "parse_missing_structure"
    response = rows(env, raw_responses)[-1]
    assert response["document_id"] == target(env)["id"]
    assert response["last_error_code"] == "parse_missing_structure"
    assert (
        env.store.read(response["body_path"], response["body_sha256"]) == b"<html>bad page</html>"
    )
    for timestamp in [103, 104]:
        with pytest.raises(IngestError, match="parse_missing_structure"):
            process_response(env.engine, env.store, response_id, timestamp)
    assert len(rows(env, raw_responses)) == 3
    calls = []

    def newer_parser(page):
        calls.append(page.content)
        # A rule-upgrade stand-in, not a modification to the production Parser.
        result = parse_notice(PageInput(content=raw(), page_url=page.page_url))
        return result.model_copy(update={"parser_version": "test-new-rules"})

    result = process_response(env.engine, env.store, response_id, 105, notice_parser=newer_parser)
    assert calls == [b"<html>bad page</html>"]
    assert result.version_id
    assert target(env)["status"] == "processed"
    assert rows(env, raw_responses)[-1]["last_error_code"] is None


def test_recheck_failure_keeps_success_and_reopen_can_resume(discovered):
    env = discovered
    success = import_page(env.engine, env.store, evidence(), raw(), 102)
    with pytest.raises(IngestError) as caught:
        import_page(env.engine, env.store, evidence(), b"invalid page", 103)
    response_id = caught.value.response_id
    env.engine.dispose()
    env.engine = open_initialized_engine(env.settings.database)
    failed = target(env)
    assert failed["status"] == "failed"
    assert failed["current_version_id"] == success.version_id
    assert failed["last_success_at"] == 102
    assert failed["last_attempt_at"] == 103
    with env.engine.connect() as connection:
        pending = connection.execute(
            sa.select(documents.c.id).where(documents.c.status.in_(["discovered", "failed"]))
        ).all()
    assert len(pending) == 50
    process_response(env.engine, env.store, success.response_id, 104)
    assert target(env)["status"] == "processed"
    assert rows(env, raw_responses)[-1]["id"] == response_id


def test_content_a_b_a_restores_old_version(discovered):
    env = discovered
    a = import_page(env.engine, env.store, evidence(), raw(), 102)
    html_b = raw().replace(b"2026-2027", b"2026-2028")
    assert html_b != raw()
    b = import_page(env.engine, env.store, evidence(), html_b, 103)
    assert b.version_id != a.version_id
    restored = import_page(env.engine, env.store, evidence(), raw(), 104)
    assert restored.version_id == a.version_id
    assert target(env)["current_version_id"] == a.version_id
    assert len(rows(env, notice_versions)) == 2


def test_reparse_preserves_fetch_and_allows_new_parser_version(discovered):
    env = discovered
    first = import_page(env.engine, env.store, evidence(), raw(), 102)

    def new_parser(page):
        return parse_notice(page).model_copy(update={"parser_version": "test-v2"})

    second = process_response(
        env.engine, env.store, first.response_id, 110, notice_parser=new_parser
    )
    assert first.version_id != second.version_id
    versions = rows(env, notice_versions)
    assert versions[0]["content_sha256"] == versions[1]["content_sha256"]
    assert versions[0]["parser_version"] == PARSER_VERSION
    assert versions[1]["parser_version"] == "test-v2"
    response = rows(env, raw_responses)[-1]
    assert response["fetched_at"] == 100
    assert response["last_attempt_at"] == 110
    assert len(rows(env, raw_responses)) == 3
    assert target(env)["last_success_at"] == 110


def test_identity_mismatch_keeps_target_failed(discovered):
    env = discovered
    metadata = evidence(url=LEGACY_URL)
    with pytest.raises(IngestError, match="identity_mismatch"):
        import_page(env.engine, env.store, metadata, raw("legacy-notice-detail.html"), 102)
    assert target(env)["status"] == "failed"
    assert target(env, "1517:127581")["status"] == "discovered"
    assert rows(env, notice_versions) == []


def test_publish_failure_creates_no_database_reference(discovered, monkeypatch):
    import signalnest.rawstore as rawstore

    env = discovered

    def fail(*args, **kwargs):
        raise OSError("injected publication failure")

    monkeypatch.setattr(rawstore.os, "link", fail)
    with pytest.raises(IngestError, match="raw_io_or_unsafe_path"):
        import_page(env.engine, env.store, evidence(), raw(), 102)
    assert len(rows(env, raw_responses)) == 2
    assert rows(env, notice_versions) == []
    assert target(env)["status"] == "failed"
    assert len(list((env.settings.data_dir / "raw").glob("*.bin"))) == 2


def test_database_failure_after_publish_leaves_identifiable_orphan(discovered):
    env = discovered
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_response BEFORE INSERT ON raw_responses "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError, match="database_write_failed"):
        import_page(env.engine, env.store, evidence(), raw(), 102)
    assert len(rows(env, raw_responses)) == 2
    assert target(env)["current_version_id"] is None
    assert target(env)["status"] == "failed"
    referenced = {row["body_path"] for row in rows(env, raw_responses)}
    files = {f"raw/{path.name}" for path in (env.settings.data_dir / "raw").glob("*.bin")}
    assert len(files - referenced) == 1


def test_version_and_success_rollback_together_then_retry(discovered):
    env = discovered
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_success BEFORE UPDATE ON documents "
            "WHEN NEW.status = 'processed' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError) as caught:
        import_page(env.engine, env.store, evidence(), raw(), 102)
    assert caught.value.code == "database_write_failed"
    assert rows(env, notice_versions) == []
    assert target(env)["current_version_id"] is None
    assert target(env)["last_success_at"] is None
    assert target(env)["status"] == "failed"
    with env.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER reject_success")
    result = process_response(env.engine, env.store, caught.value.response_id, 103)
    assert result.version_id
    assert target(env)["status"] == "processed"


def test_failure_record_failure_is_explicit(discovered):
    env = discovered
    with env.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_failure BEFORE UPDATE ON documents "
            "WHEN NEW.status = 'failed' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(IngestError) as caught:
        import_page(env.engine, env.store, evidence(), b"bad", 102)
    assert caught.value.code == "failure_state_unavailable"
    assert caught.value.response_id == rows(env, raw_responses)[-1]["id"]
    assert target(env)["status"] == "discovered"
    assert rows(env, notice_versions) == []


@pytest.mark.parametrize("fault", ["missing", "corrupt", "path", "symlink"])
def test_reparse_verifies_files_and_preserves_success(discovered, fault):
    env = discovered
    success = import_page(env.engine, env.store, evidence(), raw(), 102)
    response = rows(env, raw_responses)[-1]
    path = env.settings.data_dir / response["body_path"]
    if fault == "missing":
        path.unlink()
    elif fault == "corrupt":
        path.write_bytes(b"damaged")
    elif fault == "path":
        with env.engine.begin() as connection:
            connection.execute(
                raw_responses.update()
                .where(raw_responses.c.id == success.response_id)
                .values(body_path="../escape")
            )
    else:
        saved = env.settings.data_dir / "saved"
        path.rename(saved)
        path.symlink_to(saved)
    with pytest.raises(IngestError):
        process_response(env.engine, env.store, success.response_id, 103)
    assert target(env)["status"] == "failed"
    assert target(env)["current_version_id"] == success.version_id
    assert target(env)["last_success_at"] == 102


def test_304_is_evidence_only_and_never_creates_empty_html(discovered):
    env = discovered
    result = import_page(env.engine, env.store, evidence(status_code=304), None, 102)
    assert result.outcome == "evidence_only"
    assert target(env)["status"] == "discovered"
    assert rows(env, raw_responses)[-1]["body_path"] is None
    with pytest.raises(IngestError, match="response_has_no_body"):
        process_response(env.engine, env.store, result.response_id, 103)
    with pytest.raises(IngestError, match="304_has_body"):
        record_response(env.engine, env.store, evidence(status_code=304), b"")
    assert len(list((env.settings.data_dir / "raw").glob("*.bin"))) == 2


@pytest.mark.parametrize(
    "body,status,code", [(b"", 200, "parse_empty_page"), (b"denied", 403, "http_status_not_200")]
)
def test_invalid_response_is_evidence_not_success(discovered, body, status, code):
    env = discovered
    with pytest.raises(IngestError, match=code):
        import_page(env.engine, env.store, evidence(status_code=status), body, 102)
    assert target(env)["last_error_code"] == code
    assert target(env)["current_version_id"] is None
    response = rows(env, raw_responses)[-1]
    assert env.store.read(response["body_path"], response["body_sha256"]) == body


def test_bad_metadata_and_unknown_target(env):
    with pytest.raises(ValidationError):
        evidence(fetched_at=None)
    with pytest.raises(ValidationError):
        evidence("notice", source_document_id=None)
    with pytest.raises(IngestError, match="target_not_discovered"):
        import_page(env.engine, env.store, evidence(), raw(), 102)
    with pytest.raises(IngestError, match="processing_before_fetch"):
        import_page(env.engine, env.store, evidence("list", LIST_URL), raw(), 99)
    assert rows(env, raw_responses) == []


def test_older_processing_time_cannot_rewind_state(discovered):
    env = discovered
    first = import_page(env.engine, env.store, evidence(), raw(), 102)
    with pytest.raises(IngestError, match="stale_processing_time"):
        process_response(env.engine, env.store, first.response_id, 101)
    assert target(env)["last_success_at"] == 102


def test_programming_errors_are_not_disguised_as_success(discovered):
    env = discovered
    response_id = record_response(env.engine, env.store, evidence(), raw())

    def broken_parser(page):
        raise RuntimeError("programming defect")

    with pytest.raises(RuntimeError, match="programming defect"):
        process_response(env.engine, env.store, response_id, 102, notice_parser=broken_parser)
    assert target(env)["current_version_id"] is None
    assert rows(env, raw_responses)[-1]["last_attempt_at"] is None


def test_legacy_response_requires_explicit_reimport_metadata(env):
    with env.engine.begin() as connection:
        response_id = connection.execute(
            raw_responses.insert().values(
                source_id=SOURCE,
                requested_url=NOTICE_URL,
                final_url=NOTICE_URL,
                fetched_at=100,
                status_code=304,
            )
        ).inserted_primary_key[0]
    with pytest.raises(IngestError, match="legacy_response_type_unknown"):
        process_response(env.engine, env.store, response_id, 101)


def test_file_io_and_parser_are_outside_transactions(discovered, monkeypatch):
    env = discovered
    active = set()

    @sa.event.listens_for(env.engine, "begin")
    def begin(connection):
        active.add(connection)

    @sa.event.listens_for(env.engine, "commit")
    @sa.event.listens_for(env.engine, "rollback")
    def end(connection):
        active.discard(connection)

    archive = env.store.archive
    read = env.store.read

    def checked_archive(content):
        assert not active
        return archive(content)

    def checked_read(path, digest):
        assert not active
        return read(path, digest)

    def checked_parser(page):
        assert not active
        return parse_notice(page)

    monkeypatch.setattr(env.store, "archive", checked_archive)
    monkeypatch.setattr(env.store, "read", checked_read)
    response_id = record_response(env.engine, env.store, evidence(), raw())
    process_response(env.engine, env.store, response_id, 102, notice_parser=checked_parser)
    assert target(env)["status"] == "processed"


def test_interrupt_before_success_commit_rolls_back_and_reopens(discovered):
    # In-process KeyboardInterrupt injection, NOT a terminated-process experiment.
    env = discovered

    def interrupt(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE documents"):
            raise KeyboardInterrupt()

    sa.event.listen(env.engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            import_page(env.engine, env.store, evidence(), raw(), 102)
    finally:
        sa.event.remove(env.engine, "before_cursor_execute", interrupt)
    env.engine.dispose()
    env.engine = open_initialized_engine(env.settings.database)
    assert rows(env, notice_versions) == []
    assert target(env)["current_version_id"] is None
    assert target(env)["status"] == "discovered"
    response = rows(env, raw_responses)[-1]
    assert response["last_attempt_at"] is None
    result = process_response(env.engine, env.store, response["id"], 103)
    assert result.version_id
    assert target(env)["status"] == "processed"


def test_commit_failure_is_never_reported_as_success(discovered):
    env = discovered
    response_id = record_response(env.engine, env.store, evidence(), raw())

    def reject_commit(connection):
        raise sa.exc.OperationalError("injected", {}, Exception("commit failure"))

    sa.event.listen(env.engine, "commit", reject_commit, once=True)
    with pytest.raises(IngestError, match="database_write_failed"):
        process_response(env.engine, env.store, response_id, 102)
    assert rows(env, notice_versions) == []
    assert target(env)["status"] == "failed"
    assert target(env)["current_version_id"] is None


def test_body_store_reuse_checks_existing_corruption_before_response(discovered):
    env = discovered
    result = import_page(env.engine, env.store, evidence(), raw(), 102)
    response = rows(env, raw_responses)[-1]
    (env.settings.data_dir / response["body_path"]).write_bytes(b"damaged")
    with pytest.raises(IngestError, match="raw_digest_mismatch"):
        import_page(env.engine, env.store, evidence(), raw(), 103)
    assert len(rows(env, raw_responses)) == 3
    assert target(env)["current_version_id"] == result.version_id
    assert target(env)["last_success_at"] == 102


def test_directory_sync_failure_after_publish_has_no_reference_and_can_retry(
    discovered, monkeypatch
):
    import signalnest.rawstore as rawstore

    env = discovered
    sync = rawstore.os.fsync
    calls = []

    def fail_directory_once(descriptor):
        calls.append(descriptor)
        if len(calls) == 2:
            raise OSError("injected directory sync failure after publication")
        sync(descriptor)

    monkeypatch.setattr(rawstore.os, "fsync", fail_directory_once)
    with pytest.raises(IngestError, match="raw_io_or_unsafe_path"):
        import_page(env.engine, env.store, evidence(), raw(), 102)
    assert len(rows(env, raw_responses)) == 2
    assert target(env)["current_version_id"] is None
    assert len(list((env.settings.data_dir / "raw").glob("*.bin"))) == 3
    result = import_page(env.engine, env.store, evidence(), raw(), 103)
    assert result.version_id
    assert len(calls) == 4  # New file/dir first attempt, existing file/dir on retry.
    assert target(env)["status"] == "processed"
