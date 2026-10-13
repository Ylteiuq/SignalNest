"""Separate TOML query configuration; validation never opens a database or reads keys."""

import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator, model_validator

from signalnest.config import ConfigurationError, Settings, load_config
from signalnest.contracts import Contract
from signalnest.model_client import ModelConfig
from signalnest.notifications.contracts import Profile
from signalnest.notifications.profile import ProfileError, load_profile
from signalnest.querying.contracts import InstanceID


class InstanceConfig(Contract):
    id: InstanceID
    config: Path


class QueryLimits(Contract):
    max_model_calls: int = Field(default=6, strict=True, ge=1, le=20)
    max_requests: int = Field(default=8, strict=True, ge=1, le=30)
    max_tool_calls: int = Field(default=16, strict=True, ge=4, le=64)
    run_seconds: float = Field(default=90.0, strict=True, gt=0, le=600, allow_inf_nan=False)
    max_tool_chars: int = Field(default=60_000, strict=True, ge=4000, le=200_000)
    notice_chunk_chars: int = Field(default=6000, strict=True, ge=500, le=12_000)
    max_read_notices: int = Field(default=12, strict=True, ge=1, le=30)
    results_per_source: int = Field(default=5, strict=True, ge=1, le=10)


class QuerySettings(Contract):
    instances: tuple[InstanceConfig, ...] = Field(min_length=1, max_length=8)
    profile: Path | None = None
    timezone: str = "Asia/Shanghai"
    limits: QueryLimits = QueryLimits()
    model: ModelConfig | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an available IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def unique_instances(self):
        if len({item.id for item in self.instances}) != len(self.instances):
            raise ValueError("instance IDs must be unique")
        return self


def load_query_config(path: Path) -> QuerySettings:
    path = path.expanduser().resolve()
    try:
        with path.open("rb") as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ConfigurationError("query configuration exceeds 64 KiB")
        settings = QuerySettings.model_validate(tomllib.loads(raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError("cannot read valid UTF-8 query TOML") from exc
    except ValidationError as exc:
        # Never echo a supplied value: unknown fields could contain a pasted secret.
        known = (
            set(QuerySettings.model_fields)
            | set(QueryLimits.model_fields)
            | set(InstanceConfig.model_fields)
            | set(ModelConfig.model_fields)
        )
        fields = ", ".join(
            ".".join(str(p) if type(p) is int or p in known else "unknown_field" for p in e["loc"])
            for e in exc.errors()
        )
        raise ConfigurationError(f"invalid query configuration fields: {fields}") from exc
    return settings.model_copy(
        update={
            "instances": tuple(
                item.model_copy(
                    update={"config": (path.parent / item.config.expanduser()).resolve()}
                )
                for item in settings.instances
            ),
            "profile": (path.parent / settings.profile.expanduser()).resolve()
            if settings.profile is not None
            else None,
        }
    )


def load_query_inputs(settings: QuerySettings) -> tuple[dict[str, Settings], Profile | None]:
    """Read only explicitly configured TOML files; no storage initialization or credentials."""
    instances = {}
    for item in settings.instances:
        try:
            instances[item.id] = load_config(item.config)
        except ConfigurationError as exc:
            raise ConfigurationError(
                f"instance {item.id} configuration invalid; run config-check for that instance"
            ) from exc
    if len({item.storage.database for item in instances.values()}) != len(instances):
        raise ConfigurationError("query instances must point to distinct databases")
    try:
        profile = load_profile(settings.profile) if settings.profile is not None else None
    except ProfileError as exc:
        raise ConfigurationError("query profile is invalid or unreadable") from exc
    return instances, profile
