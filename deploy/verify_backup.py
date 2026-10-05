"""Read-only verification of a stopped/restored SignalNest database and raw directory.

Run with the installed project Python. This script never creates storage, upgrades a
schema, downloads missing files, modifies evidence, or deletes orphan files.
"""

import argparse
import json
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path

from alembic.script import ScriptDirectory

from signalnest.rawstore import RawStore, RawStoreError
from signalnest.storage import migration_config


class BackupVerificationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def verify_backup(database: Path, data_dir: Path) -> dict[str, int | str]:
    """Check relational integrity and every raw reference without opening a writer."""
    database = database.absolute()
    data_dir = data_dir.absolute()
    if database.is_symlink() or not database.is_file():
        raise BackupVerificationError("backup_database_missing_or_unsafe")
    expected_revision = ScriptDirectory.from_config(migration_config()).get_current_head()
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupVerificationError("backup_database_integrity")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupVerificationError("backup_foreign_keys")
            revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            if revisions != [(expected_revision,)]:
                raise BackupVerificationError("backup_revision_mismatch")
            invalid_success = connection.execute(
                "SELECT d.id FROM documents d LEFT JOIN notice_versions v "
                "ON v.id = d.current_version_id AND v.document_id = d.id "
                "WHERE (d.current_version_id IS NULL) != (d.last_success_at IS NULL) "
                "OR (d.current_version_id IS NOT NULL AND v.id IS NULL) LIMIT 1"
            ).fetchone()
            if invalid_success is not None:
                raise BackupVerificationError("backup_success_pointer")
            bodies = connection.execute(
                "SELECT DISTINCT body_path, body_sha256 FROM raw_responses "
                "WHERE body_path IS NOT NULL"
            ).fetchall()
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("documents", "notice_versions", "raw_responses", "ingestion_runs")
            }
        raw_store = RawStore(data_dir)
        raw_dir = data_dir / "raw"
        if not raw_dir.is_dir() or any(path.is_symlink() for path in (raw_dir, *raw_dir.parents)):
            raise BackupVerificationError("backup_raw_directory_missing_or_unsafe")
        for path, digest in bodies:
            raw_store.read(path, digest)
        referenced = {Path(path).name for path, _ in bodies}
        raw_files = 0
        temporary_files = 0
        orphan_files = 0
        for entry in raw_dir.iterdir():
            if not stat.S_ISREG(entry.lstat().st_mode):
                raise BackupVerificationError("backup_raw_unsafe_entry")
            if entry.name.startswith(".tmp-"):
                temporary_files += 1
            else:
                raw_files += 1
                orphan_files += entry.name not in referenced
    except RawStoreError as exc:
        raise BackupVerificationError(exc.code) from exc
    except (sqlite3.Error, OSError) as exc:
        raise BackupVerificationError("backup_unreadable") from exc
    return {
        "revision": expected_revision,
        **counts,
        "referenced_bodies": len(bodies),
        "raw_files": raw_files,
        "orphan_files": orphan_files,
        "temporary_files": temporary_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_backup(args.database, args.data_dir)
    except BackupVerificationError as exc:
        print(f"备份校验失败: {exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
