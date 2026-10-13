"""Small query contracts; model output never supplies source URLs or eligibility claims."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from signalnest.contracts import Contract

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
InstanceID = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,39}$")]


class QueryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Question(Contract):
    text: Text
    at: datetime

    @field_validator("at")
    @classmethod
    def explicit_clock(cls, value):
        if value.utcoffset() is None:
            raise ValueError("at must include a UTC offset")
        if not 1970 <= value.year <= 2100:
            raise ValueError("at must be in 1970–2100")
        return value

    @field_validator("text")
    @classmethod
    def visible_question(cls, value):
        if any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("question contains control characters")
        return value


class SearchArgs(Contract):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    instance_id: InstanceID | None = None
    offset: int = Field(default=0, strict=True, ge=0, le=1000)


class ReadArgs(Contract):
    instance_id: InstanceID
    document_id: int = Field(strict=True, ge=1, le=2**63 - 1)
    offset: int = Field(default=0, strict=True, ge=0, le=2_000_000)


class EmptyArgs(Contract):
    pass


class Quote(Contract):
    field: Literal["title", "body_text"]
    text: Annotated[str, StringConstraints(min_length=4, max_length=500)]

    @field_validator("text")
    @classmethod
    def meaningful_quote(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("quote must contain visible source text")
        return value


class SelectedNotice(Contract):
    reference: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    relevance: tuple[Quote, ...] = Field(min_length=1, max_length=3)
    conditions: tuple[Quote, ...] = Field(default=(), max_length=6)
    deadline: tuple[Quote, ...] = Field(default=(), max_length=3)


class AnswerArgs(Contract):
    notices: tuple[SelectedNotice, ...] = Field(max_length=8)


class ToolTrace(Contract):
    tool: str
    ok: bool
    error_code: str | None = None
    instance_id: InstanceID | None = None
    document_id: int | None = None


class QueryReport(Contract):
    status: Literal["answered", "fallback", "local"]
    question: str = Field(repr=False)
    at: datetime
    timezone: str
    publication_window: dict
    notices: tuple[dict, ...]
    search_results: tuple[dict, ...]
    source_status: tuple[dict, ...]
    warnings: tuple[str, ...]
    error_code: str | None = None
    tool_trace: tuple[ToolTrace, ...]
    model_usage: dict
    query_version: str = "campus-query-v2"
