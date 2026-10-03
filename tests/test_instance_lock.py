"""Actual independent POSIX processes contend for the same writer lock."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from signalnest.instance_lock import WriterLockError, writer_lock
from signalnest.storage import StorageError, initialize_storage

SCRIPT = """
import socket, sys
from pathlib import Path
from signalnest.instance_lock import WriterLockError, writer_lock
def forbidden(*args, **kwargs):
    raise AssertionError('lock test is offline')
socket.socket.connect = forbidden
socket.create_connection = forbidden
try:
    with writer_lock(Path(sys.argv[1])):
        print('acquired', flush=True)
        if sys.argv[2] == 'hold':
            sys.stdin.readline()
except WriterLockError as exc:
    print(exc.code, flush=True)
    raise SystemExit(9)
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX advisory locking")
@pytest.mark.parametrize("exit_mode", ["normal", "terminate"])
def test_two_real_processes_contend_and_kernel_releases_on_exit(tmp_path, exit_mode):
    database = tmp_path / "db.sqlite"
    first = subprocess.Popen(
        [sys.executable, "-c", SCRIPT, str(database), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout.readline().strip() == "acquired"
        second = subprocess.run(
            [sys.executable, "-c", SCRIPT, str(database), "once"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert second.returncode == 9 and second.stdout.strip() == "writer_lock_busy"
        if exit_mode == "terminate":
            first.terminate()
            first.wait(timeout=10)
        else:
            first.communicate("finish\n", timeout=10)
            assert first.returncode == 0
        assert Path(str(database) + ".lock").exists()  # Existence is not ownership.
        third = subprocess.run(
            [sys.executable, "-c", SCRIPT, str(database), "once"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert third.returncode == 0 and third.stdout.strip() == "acquired"
    finally:
        if first.poll() is None:
            first.terminate()
        first.communicate(timeout=10)


def test_database_symlink_alias_resolves_to_same_lock(state_env, tmp_path):
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(state_env.settings.database)
    with (
        writer_lock(state_env.settings.database),
        pytest.raises(WriterLockError, match="writer_lock_busy"),
        writer_lock(alias),
    ):
        pytest.fail("alias must not bypass writer protection")


def test_existing_lock_file_and_failed_body_release_lock(state_env):
    with pytest.raises(RuntimeError), writer_lock(state_env.settings.database):
        raise RuntimeError("injected")
    with writer_lock(state_env.settings.database):
        pass


def test_explicit_migration_uses_same_lock_as_other_writers(state_env):
    with (
        writer_lock(state_env.settings.database),
        pytest.raises(StorageError, match="writer_lock_busy"),
    ):
        initialize_storage(state_env.settings)


def test_database_hardlink_alias_and_symlink_lockfile_are_rejected(state_env, tmp_path):
    alias = tmp_path / "hardlink.sqlite"
    alias.hardlink_to(state_env.settings.database)
    with pytest.raises(WriterLockError, match="writer_lock_path_invalid"), writer_lock(alias):
        pytest.fail("hardlinked databases have no unique path lock")
    alias.unlink()
    lockfile = Path(str(state_env.settings.database) + ".lock")
    lockfile.unlink()
    lockfile.symlink_to(tmp_path / "other.lock")
    with (
        pytest.raises(WriterLockError, match="writer_lock_unavailable"),
        writer_lock(state_env.settings.database),
    ):
        pytest.fail("do not follow a lockfile symlink")
