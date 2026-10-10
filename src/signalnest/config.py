"""Read and validate ordinary TOML configuration without creating storage."""

import ipaddress
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

from signalnest.sources import CS, UC, SourceName, source_binding


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
    parser: SourceName = "whu-student-notices"
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=80)
    list_url: HttpUrl

    @field_validator("list_url")
    @classmethod
    def public_url(cls, value: HttpUrl) -> HttpUrl:
        if value.username is not None or value.password is not None or value.fragment is not None:
            raise ValueError("URL must not contain credentials or a fragment")
        return value

    @model_validator(mode="after")
    def bound_home(self):
        binding = source_binding(self.parser)
        try:
            uri = binding.target_uri(str(self.list_url), "list")
        except ValueError:
            raise ValueError("list_url must be the selected Parser's HTTPS list home") from None
        if uri != f"https://{binding.host}{binding.list_path}.htm":
            raise ValueError("list_url must be the selected Parser's HTTPS list home")
        if self.id in {UC.source_id, CS.source_id} and self.id != binding.source_id:
            raise ValueError("id and parser refer to different sources")
        return self


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


class SmtpSettings(SettingsModel):
    """Optional transport settings; environment values are read only when sending."""

    host: str = Field(min_length=1, max_length=253, strict=True)
    port: int = Field(ge=1, le=65535, strict=True)
    security: Literal["starttls", "tls"] = "starttls"
    timeout_seconds: float = Field(default=30.0, gt=0, le=120, strict=True)
    username_env: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128, strict=True
    )
    password_env: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128, strict=True
    )

    @field_validator("host")
    @classmethod
    def server_host(cls, value: str) -> str:
        if not value.isascii() or any(char.isspace() for char in value) or "%" in value:
            raise ValueError("must be a DNS hostname or an IP address without URL or credentials")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            labels = value.removesuffix(".").split(".")
            if not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in labels
            ):
                raise ValueError(
                    "must be a DNS hostname or an IP address without URL or credentials"
                ) from None
        return value

    @model_validator(mode="after")
    def paired_credentials(self):
        if (self.username_env is None) != (self.password_env is None):
            raise ValueError("username_env and password_env must be configured together")
        return self


class MailSendingSettings(SettingsModel):
    """Finite sending budgets; these settings alone never enable SMTP traffic."""

    max_attempts: int = Field(default=6, ge=1, le=6, strict=True)
    max_messages: int = Field(default=5, ge=1, le=20, strict=True)
    run_seconds: float = Field(default=300.0, gt=0, le=3600, strict=True)
    retry_delays_seconds: list[int] = Field(
        default_factory=lambda: [300, 900, 3600, 21600, 86400], max_length=5
    )
    uncertain_delay_seconds: int = Field(default=1800, ge=1800, le=86400, strict=True)

    @field_validator("retry_delays_seconds", mode="before")
    @classmethod
    def retry_delay_values(cls, value: object) -> object:
        if not isinstance(value, list) or any(
            type(delay) is not int or not 1 <= delay <= 31536000 for delay in value
        ):
            raise ValueError("must be a list of positive integer seconds, each at most 31536000")
        return value

    @model_validator(mode="after")
    def finite_backoff(self):
        if len(self.retry_delays_seconds) < self.max_attempts - 1:
            raise ValueError(
                "retry_delays_seconds must contain at least max_attempts minus one delays"
            )
        if any(
            previous > following
            for previous, following in zip(
                self.retry_delays_seconds, self.retry_delays_seconds[1:], strict=False
            )
        ):
            raise ValueError("retry_delays_seconds must be nondecreasing")
        return self


class MailRuntimeSettings(SettingsModel):
    """Opt-in timer consumption, separate from activation and transport configuration."""

    enabled: bool = Field(default=False, strict=True)
    max_plan_messages: int = Field(default=5, ge=1, le=20, strict=True)
    max_events: int = Field(default=50, ge=1, le=100, strict=True)
    max_bytes: int = Field(default=128 * 1024, ge=1024, le=1024 * 1024, strict=True)


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
    smtp: SmtpSettings | None = None
    mail_sending: MailSendingSettings = Field(default_factory=MailSendingSettings)
    mail_runtime: MailRuntimeSettings = Field(default_factory=MailRuntimeSettings)

    @model_validator(mode="after")
    def background_requires_transport(self):
        if self.mail_runtime.enabled and self.smtp is None:
            raise ValueError("mail_runtime.enabled requires an explicit smtp section")
        return self


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
