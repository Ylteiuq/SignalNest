"""A small POSIX content-addressed raw directory, with no database or network I/O."""

import hashlib
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class RawStoreError(RuntimeError):
    """Finite error code only; never includes raw content or arbitrary OS messages."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RawBody:
    path: str
    sha256: str


class RawStore:
    def __init__(self, data_dir: Path):
        # Construction, like engine construction, does not touch disk.
        if not data_dir.is_absolute() or ".." in data_dir.parts:
            raise RawStoreError("raw_path_invalid")
        self.data_dir = data_dir

    @contextmanager
    def _directory(self):
        """Walk using directory FDs; reject symlinks in every path component."""
        directory = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(self.data_dir.anchor, flags)
            for part in (*self.data_dir.parts[1:], "raw"):
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            yield directory
        except FileNotFoundError as exc:
            raise RawStoreError("raw_missing") from exc
        except OSError as exc:
            raise RawStoreError("raw_io_or_unsafe_path") from exc
        finally:
            if directory is not None:
                os.close(directory)

    @staticmethod
    def _read(directory: int, name: str, digest: str, *, sync: bool = False) -> bytes:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RawStoreError("raw_not_regular")
            content = stream.read()
            if hashlib.sha256(content).hexdigest() != digest:
                raise RawStoreError("raw_digest_mismatch")
            if sync:
                os.fsync(stream.fileno())
        return content

    def read(self, path: str, sha256: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or path != f"raw/{sha256}.bin":
            raise RawStoreError("raw_path_invalid")
        with self._directory() as directory:
            return self._read(directory, f"{sha256}.bin", sha256)

    def archive(self, content: bytes) -> RawBody:
        if type(content) is not bytes:
            raise TypeError("raw content must be bytes")
        digest = hashlib.sha256(content).hexdigest()
        name = f"{digest}.bin"
        temporary = f".tmp-{uuid4().hex}"
        with self._directory() as directory:
            try:
                self._read(directory, name, digest, sync=True)
            except FileNotFoundError:
                pass
            else:
                # A previous publication may have failed at directory fsync. Retry
                # durability requests even when the complete blob already exists.
                os.fsync(directory)
                return RawBody(f"raw/{name}", digest)
            created = False
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                created = True
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._read(directory, temporary, digest)
                try:
                    # Atomic, exclusive publication. Unlike replace(), this cannot
                    # overwrite a corrupt existing blob in a concurrent publisher.
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    self._read(directory, name, digest, sync=True)
                os.fsync(directory)
            finally:
                # A killed process can leave this name behind; never auto-delete others.
                if created:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
            os.fsync(directory)
        return RawBody(f"raw/{name}", digest)
