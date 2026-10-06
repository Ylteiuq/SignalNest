"""Explicit local TOML Profile loading; no application configuration or storage access."""

import tomllib
from pathlib import Path

from pydantic import ValidationError

from signalnest.notifications.contracts import Profile


class ProfileError(ValueError):
    """Finite, user-actionable errors that do not echo the entire profile input."""


def load_profile(path: Path) -> Profile:
    try:
        with path.expanduser().open("rb") as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ProfileError("profile exceeds 64 KiB")
        data = tomllib.loads(raw.decode("utf-8"))
    except OSError as exc:
        raise ProfileError("cannot read profile file") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ProfileError("invalid TOML; use UTF-8 and valid syntax") from exc
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or 'profile'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ProfileError(details) from exc
