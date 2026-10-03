"""POSIX advisory writer lock located beside the canonical database, not a stale flag."""

import os
import stat
from contextlib import contextmanager
from pathlib import Path


class WriterLockError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@contextmanager
def writer_lock(database: Path, *, create_parent: bool = False):
    """Hold across a serial writing run. No auto-locking of each Connection operation."""
    if os.name != "posix":
        raise WriterLockError("writer_lock_platform_unsupported")
    import fcntl

    if not database.is_absolute():
        raise WriterLockError("writer_lock_path_invalid")
    descriptor = None
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
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WriterLockError("writer_lock_busy") from None
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise WriterLockError("writer_lock_unavailable") from exc
    except WriterLockError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    try:
        yield
    finally:
        os.close(descriptor)  # Kernel releases flock; keep the inode for other processes.
