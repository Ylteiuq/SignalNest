"""Query an installed snapshot, optionally using one explicit private credential file.

No file or .env is discovered automatically. Local/help/config checks never read
credentials. The credential is neither hashed nor included in reports or argv.
"""

import argparse
import os
import stat
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from signalnest.cli import main as signalnest_main
from signalnest.config import ConfigurationError
from signalnest.querying.config import load_query_config, load_query_inputs
from signalnest.querying.contracts import Question

MAX_KEY_BYTES = 4096


class CredentialFileError(ValueError):
    """Finite diagnostic without paths, credential bytes or arbitrary OS errors."""


def _read_private_key(path):
    if os.name != "posix":
        raise CredentialFileError("credential_file_requires_posix")
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise CredentialFileError("credential_path_unsafe")
    if not path.is_absolute():
        path = Path.cwd() / path
    directory = descriptor = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(path.anchor, flags)
        # Retain directory FDs throughout the walk; no resolve()/symlink following.
        for index, component in enumerate(path.parts[1:-1]):
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            shared_sticky_ancestor = (
                index < len(path.parts[1:-1]) - 1
                and info.st_uid == 0
                and bool(info.st_mode & stat.S_ISVTX)
            )
            if info.st_uid not in {0, os.geteuid()} or (
                info.st_mode & 0o022 and not shared_sticky_ancestor
            ):
                raise CredentialFileError("credential_parent_unsafe")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
        ):
            raise CredentialFileError("credential_file_unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(MAX_KEY_BYTES + 1)
        if not raw or len(raw) > MAX_KEY_BYTES:
            raise CredentialFileError("credential_file_invalid")
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        if not raw or any(byte < 33 or byte > 126 for byte in raw):
            raise CredentialFileError("credential_file_invalid")
        return raw.decode("ascii")
    except OSError as exc:
        raise CredentialFileError("credential_file_unavailable_or_unsafe") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


@contextmanager
def _temporary_key(name, value):
    previous = os.environ.get(name)
    try:
        os.environ[name] = value
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--question")
    parser.add_argument("--at", help="query clock including UTC offset; default actual UTC time")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--check", action="store_true", help="validate config without reading keys")
    parser.add_argument("--api-key-file", type=Path, help="explicit owner-only POSIX key file")
    args = parser.parse_args(argv)
    if args.check:
        return signalnest_main(["ask-config-check", "--config", str(args.config)])
    if args.question is None:
        parser.error("--question is required unless --check is used")
    forwarded = ["ask", "--config", str(args.config), "--question", args.question]
    for flag, value in (("--json", args.json), ("--local", args.local)):
        if value:
            forwarded.append(flag)
    if args.at:
        forwarded.extend(["--at", args.at])
    if args.local or args.api_key_file is None:
        return signalnest_main(forwarded)
    try:
        settings = load_query_config(args.config)
        load_query_inputs(settings)
        Question(
            text=args.question,
            at=datetime.fromisoformat(args.at) if args.at else datetime.now(UTC),
        )
        if settings.model is None:
            raise CredentialFileError("query_model_not_configured")
        key = _read_private_key(args.api_key_file)
    except (ConfigurationError, ValidationError, ValueError) as exc:
        code = str(exc) if isinstance(exc, CredentialFileError) else "query_configuration_invalid"
        print(f"saved query failed: {code}", file=sys.stderr)
        return 2
    with _temporary_key(settings.model.api_key_env, key):
        return signalnest_main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
