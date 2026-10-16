"""POSIX advisory writer lock located beside the canonical database, not a stale flag."""

import errno
import os
import stat
import time
from collections.abc import Callable
from contextlib import contextmanager
from math import isfinite
from pathlib import Path
from typing import Protocol


class WriterLockError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class LockClock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@contextmanager
def writer_lock(
    database: Path,
    *,
    create_parent: bool = False,
    wait_seconds: float = 0.0,
    clock: LockClock = time,
    on_contention: Callable[[], None] | None = None,
):
    """Hold one POSIX lock; optionally wait finitely for flock contention only.

    Yield elapsed acquisition seconds. The default remains immediate rejection.
    The caller owns the overall run budget; no work inside the lock is retried.
    """
    if os.name != "posix":
        raise WriterLockError("writer_lock_platform_unsupported")
    import fcntl

    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, int | float)
        or not 0 <= wait_seconds <= 600
        or not isfinite(wait_seconds)
    ):
        raise WriterLockError("writer_lock_wait_invalid")
    if not database.is_absolute():
        raise WriterLockError("writer_lock_path_invalid")
    started = clock.monotonic()
    deadline = started + wait_seconds
    descriptor = None
    try:
        try:
            canonical = database.resolve()
            if canonical.exists() and canonical.stat().st_nlink != 1:
                raise WriterLockError("writer_lock_path_invalid")
            if create_parent:
                canonical.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                str(canonical) + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise WriterLockError("writer_lock_path_invalid")
        except OSError as exc:
            raise WriterLockError("writer_lock_unavailable") from exc
        waiting = False
        while True:
            # Once contention starts, never acquire after the finite deadline.
            if waiting and clock.monotonic() >= deadline:
                raise WriterLockError("writer_lock_timeout")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise WriterLockError("writer_lock_unavailable") from exc
                if wait_seconds == 0:
                    raise WriterLockError("writer_lock_busy") from None
                if not waiting:
                    waiting = True
                    if on_contention is not None:
                        on_contention()
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                raise WriterLockError("writer_lock_timeout")
            clock.sleep(min(0.25, remaining))
        yield max(0.0, clock.monotonic() - started)
    finally:
        # Includes cancellation during acquisition/sleep and failures in the body.
        if descriptor is not None:
            os.close(descriptor)
        # Kernel releases flock; keep the inode for other processes.
