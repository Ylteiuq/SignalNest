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

WAIT_SCRIPT = """
import socket, sys, time
from pathlib import Path
from signalnest.instance_lock import WriterLockError, writer_lock
def forbidden(*args, **kwargs):
    raise AssertionError('lock test is offline')
socket.socket.connect = forbidden
socket.create_connection = forbidden
started = time.monotonic()
try:
    with writer_lock(Path(sys.argv[1]), wait_seconds=float(sys.argv[2]),
                     on_contention=lambda: print('waiting', flush=True)) as elapsed:
        print(f'acquired {elapsed}', flush=True)
except WriterLockError as exc:
    print(f'{exc.code} {time.monotonic() - started}', flush=True)
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX advisory locking")
def test_real_waiter_acquires_after_holder_exits(tmp_path):
    database = tmp_path / "db.sqlite"
    first = subprocess.Popen(
        [sys.executable, "-c", SCRIPT, str(database), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    waiter = None
    try:
        assert first.stdout.readline().strip() == "acquired"
        waiter = subprocess.Popen(
            [sys.executable, "-c", WAIT_SCRIPT, str(database), "3"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert waiter.stdout.readline().strip() == "waiting"
        assert waiter.poll() is None
        first.communicate("release\n", timeout=10)
        output, error = waiter.communicate(timeout=10)
        assert waiter.returncode == 0, error
        assert output.startswith("acquired ") and float(output.split()[1]) < 3
    finally:
        for process in (first, waiter):
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="POSIX advisory locking")
def test_real_waiter_times_out_while_holder_retains_lock(tmp_path):
    database = tmp_path / "db.sqlite"
    with writer_lock(database):
        waiter = subprocess.run(
            [sys.executable, "-c", WAIT_SCRIPT, str(database), "0.25"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = waiter.stdout.splitlines()
        assert waiter.returncode == 9 and lines[0] == "waiting"
        code, elapsed = lines[1].split()
        assert code == "writer_lock_timeout" and 0.25 <= float(elapsed) < 3
        with pytest.raises(WriterLockError, match="writer_lock_busy"), writer_lock(database):
            pytest.fail("timing out the waiter must not release the holder")
    with writer_lock(database):
        pass


@pytest.mark.parametrize("wait", [-1, 601, 10**400, float("nan"), float("inf"), True, "1"])
def test_wait_validation_creates_no_lock_file(tmp_path, wait):
    with (
        pytest.raises(WriterLockError, match="writer_lock_wait_invalid"),
        writer_lock(tmp_path / "db.sqlite", wait_seconds=wait),
    ):
        pytest.fail("invalid waits must fail before file creation")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_interrupted_sleep_closes_wait_descriptor_and_preserves_error(tmp_path, monkeypatch, error):
    import signalnest.instance_lock as locks

    class InterruptedClock:
        def monotonic(self):
            return 0.0

        def sleep(self, seconds):
            raise error("injected")

    database = tmp_path / "db.sqlite"
    actual_close, closed = locks.os.close, []

    def close(descriptor):
        closed.append(descriptor)
        actual_close(descriptor)

    with writer_lock(database):
        monkeypatch.setattr(locks.os, "close", close)
        with (
            pytest.raises(error, match="injected"),
            writer_lock(database, wait_seconds=1.0, clock=InterruptedClock()),
        ):
            pytest.fail("wait should be interrupted")
        assert len(closed) == 1
    assert len(closed) == 2


def test_non_contention_flock_error_is_not_retried(tmp_path, monkeypatch):
    import errno
    import fcntl

    calls = []

    def fail(*args):
        calls.append(1)
        raise OSError(errno.EIO, "injected path/IO detail")

    monkeypatch.setattr(fcntl, "flock", fail)
    with (
        pytest.raises(WriterLockError, match="writer_lock_unavailable"),
        writer_lock(tmp_path / "db.sqlite", wait_seconds=1.0),
    ):
        pytest.fail("only contention may wait")
    assert calls == [1]


def test_lock_error_in_protected_body_is_never_retried(tmp_path):
    calls = []
    with (
        pytest.raises(WriterLockError, match="writer_lock_busy"),
        writer_lock(tmp_path / "db.sqlite", wait_seconds=1.0),
    ):
        calls.append(1)
        raise WriterLockError("writer_lock_busy")
    assert calls == [1]
    with writer_lock(tmp_path / "db.sqlite"):
        pass
