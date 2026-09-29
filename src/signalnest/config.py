"""Read and validate ordinary TOML configuration without creating storage."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator


class ConfigurationError(ValueError):
    """A user-actionable configuration error, without echoing input values."""


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StorageSettings(SettingsModel):
    data_dir: Path
    database: Path

    @field_validator("data_dir", "database", mode="before")
    @classmethod
    def nonempty_path(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("must be a nonempty filesystem path")
        return value


class SourceSettings(SettingsModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=80)
    list_url: HttpUrl

    @field_validator("list_url")
    @classmethod
    def public_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None or value.fragment is not None:
            raise ValueError("URL must not contain credentials or a fragment")
        return value


class HttpSettings(SettingsModel):
    connect_timeout_seconds: float = Field(gt=0, le=120, strict=True)
    read_timeout_seconds: float = Field(gt=0, le=300, strict=True)
    request_interval_seconds: float = Field(gt=0, le=3600, strict=True)
    user_agent: str = Field(min_length=1, max_length=200)

    @field_validator("user_agent")
    @classmethod
    def header_value(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError("must contain printable ASCII characters only")
        return value


class Settings(SettingsModel):
    storage: StorageSettings
    source: SourceSettings
    http: HttpSettings


def load_config(path: Path) -> Settings:
    """Resolve both storage paths relative to the config file, never the process cwd."""
    path = path.expanduser().resolve()
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration file: {exc.strerror}") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigurationError("invalid TOML; check syntax and UTF-8 encoding") from exc
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigurationError(details) from exc
    for field in ("data_dir", "database"):
        value = getattr(settings.storage, field).expanduser()
        resolved = (path.parent / value).resolve()
        setattr(settings.storage, field, resolved)
    if settings.storage.database == settings.storage.data_dir:
        raise ConfigurationError("storage.database must differ from storage.data_dir")
    return settings
