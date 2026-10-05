"""Read and validate ordinary TOML configuration without creating storage."""

import tomllib
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)


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


class RunBudget(SettingsModel):
    max_pages: int = Field(default=2, ge=1, le=10000, strict=True)
    max_details: int = Field(default=20, ge=0, le=100000, strict=True)
    max_requests: int = Field(default=64, ge=1, le=100000, strict=True)
    run_seconds: float = Field(default=600.0, gt=0, le=86400, strict=True)
    resource_seconds: float = Field(default=60.0, gt=0, le=3600, strict=True)
    max_body_bytes: int = Field(default=2097152, ge=1, le=67108864, strict=True)


class RuntimeSettings(SettingsModel):
    """Small serial processing policy; external timers own execution frequency."""

    foreground_slots: int = Field(default=12, ge=1, le=100000, strict=True)
    history_slots: int = Field(default=4, ge=1, le=100000, strict=True)
    recheck_slots: int = Field(default=4, ge=1, le=100000, strict=True)
    recent_days: int = Field(default=7, ge=0, le=3650, strict=True)
    middle_days: int = Field(default=30, ge=1, le=3650, strict=True)
    recent_recheck_seconds: int = Field(default=86400, ge=1, le=31536000, strict=True)
    middle_recheck_seconds: int = Field(default=604800, ge=1, le=31536000, strict=True)
    old_recheck_seconds: int = Field(default=2592000, ge=1, le=31536000, strict=True)
    transient_failure_seconds: int = Field(default=1800, ge=1, le=31536000, strict=True)
    access_failure_seconds: int = Field(default=86400, ge=1, le=31536000, strict=True)
    parse_failure_seconds: int = Field(default=21600, ge=1, le=31536000, strict=True)
    missing_failure_seconds: int = Field(default=604800, ge=1, le=31536000, strict=True)
    regular: RunBudget = Field(default_factory=RunBudget)
    full: RunBudget = Field(
        default_factory=lambda: RunBudget(max_pages=64, max_details=0, max_requests=96)
    )

    @model_validator(mode="after")
    def ordered_intervals(self):
        if self.middle_days <= self.recent_days:
            raise ValueError("middle_days must exceed recent_days")
        if not (
            self.recent_recheck_seconds <= self.middle_recheck_seconds <= self.old_recheck_seconds
        ):
            raise ValueError("recheck intervals must be nondecreasing")
        if self.foreground_slots + self.history_slots + self.recheck_slots > 100000:
            raise ValueError("detail slot total must not exceed 100000")
        return self


class Settings(SettingsModel):
    storage: StorageSettings
    source: SourceSettings
    http: HttpSettings
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)


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
