"""A real SQLite backup and cloned raw files are verified completely offline."""

import hashlib
import runpy
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from signalnest.ingestion import ResponseInput, import_page
from signalnest.instance_lock import writer_lock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/verify_backup.py"
VERIFIER = runpy.run_path(str(SCRIPT))
verify_backup = VERIFIER["verify_backup"]
BackupVerificationError = VERIFIER["BackupVerificationError"]


@pytest.fixture
def backup(state_env, tmp_path):
    env = state_env
    fixtures = ROOT / "research/fixtures"
    for kind, filename, uri, identity in (
        ("list", "student-notices-page1.html", "https://uc.whu.edu.cn/tzgg/xstz.htm", None),
        (
            "notice",
            "current-notice-detail.html",
            "https://uc.whu.edu.cn/info/1517/128231.htm",
            "1517:128231",
        ),
    ):
        import_page(
            env.engine,
            env.store,
            ResponseInput(
                source_id="whu-undergrad-student",
                page_type=kind,
                source_document_id=identity,
                requested_url=uri,
                final_url=uri,
                fetched_at=100,
                status_code=200,
            ),
            (fixtures / filename).read_bytes(),
            101,
        )
    clone = tmp_path / "snapshot"
    clone.mkdir()
    database = clone / "signalnest.sqlite3"
    with writer_lock(env.settings.database):
        with (
            closing(
                sqlite3.connect(env.settings.database.as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(database)) as destination,
        ):
            source.backup(destination)
        shutil.copytree(env.settings.data_dir / "raw", clone / "raw", symlinks=True)
    return database, clone


def test_sqlite_snapshot_raw_references_and_current_pointer_are_valid_and_read_only(backup):
    database, data_dir = backup
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in data_dir.rglob("*")
        if path.is_file()
    }
    report = verify_backup(database, data_dir)
    assert report["documents"] == 25
    assert report["notice_versions"] == 1
    assert report["raw_responses"] == 2
    assert report["referenced_bodies"] == report["raw_files"] == 2
    assert report["orphan_files"] == report["temporary_files"] == 0
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in data_dir.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize("damage", ["missing", "corrupt", "escaped", "symlink"])
def test_missing_corrupt_or_unsafe_raw_reference_fails(backup, damage):
    database, data_dir = backup
    with sqlite3.connect(database) as connection:
        path, digest = connection.execute(
            "SELECT body_path, body_sha256 FROM raw_responses ORDER BY id LIMIT 1"
        ).fetchone()
        if damage == "escaped":
            connection.execute(
                "UPDATE raw_responses SET body_path = ? WHERE id = 1", (f"../{digest}.bin",)
            )
    body = data_dir / path
    if damage == "missing":
        body.unlink()
    elif damage == "corrupt":
        body.write_bytes(b"not the archived bytes")
    elif damage == "symlink":
        copy = data_dir / "outside.bin"
        copy.write_bytes(body.read_bytes())
        body.unlink()
        body.symlink_to(copy)
    with pytest.raises(BackupVerificationError):
        verify_backup(database, data_dir)


def test_invalid_success_version_reference_is_not_accepted(backup):
    database, data_dir = backup
    with sqlite3.connect(database) as connection:
        # External corruption with FK enforcement off: verifier must catch it.
        connection.execute(
            "UPDATE documents SET current_version_id = 999 WHERE status = 'processed'"
        )
    with pytest.raises(BackupVerificationError, match="backup_foreign_keys"):
        verify_backup(database, data_dir)


def test_missing_database_is_not_created_by_verification(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(BackupVerificationError, match="backup_database_missing_or_unsafe"):
        verify_backup(path, tmp_path)
    assert not path.exists()


def test_orphans_and_interrupted_temporary_files_are_reported_and_preserved(backup):
    database, data_dir = backup
    orphan = data_dir / "raw" / ("0" * 64 + ".bin")
    temporary = data_dir / "raw/.tmp-interrupted"
    orphan.write_bytes(b"unregistered evidence")
    temporary.write_bytes(b"incomplete unpublished bytes")
    report = verify_backup(database, data_dir)
    assert report["orphan_files"] == report["temporary_files"] == 1
    assert orphan.read_bytes() == b"unregistered evidence"
    assert temporary.read_bytes() == b"incomplete unpublished bytes"


def test_cli_failed_verification_has_nonzero_exit_without_exception_body(tmp_path):
    child = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--database",
            str(tmp_path / "missing"),
            "--data-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 1
    assert "backup_database_missing_or_unsafe" in child.stderr
    assert "Traceback" not in child.stderr
    assert not list(tmp_path.iterdir())
