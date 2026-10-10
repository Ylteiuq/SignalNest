"""Small immutable contracts shared by future parsing and orchestration code.

No contract fetches a URL or opens storage. Content fields are excluded from repr.
"""

import hashlib
import json
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)


def without_credentials(url: HttpUrl) -> HttpUrl:
    if url.username is not None or url.password is not None:
        raise ValueError("URL must not contain credentials")
    return url


WebUrl = Annotated[HttpUrl, AfterValidator(without_credentials)]
NonemptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def header_value(value: str) -> str:
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ValueError("request header must contain printable ASCII characters only")
    return value


class RequestProfile(Contract):
    """Every supported representation header; the HTTP caller must send these exact values."""

    user_agent: Annotated[NonemptyText, AfterValidator(header_value)]
    accept: Annotated[NonemptyText, AfterValidator(header_value)]
    accept_encoding: Literal["identity"] = "identity"

    def sha256(self) -> str:
        encoded = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PageInput(Contract):
    """Response bytes plus final page URL, also usable for replaying fixtures."""

    content: bytes = Field(min_length=1, repr=False, strict=True)
    page_url: WebUrl


class ListEntry(Contract):
    """source_id belongs to the configured source; the parser returns its local identity."""

    source_document_id: NonemptyText
    detail_url: WebUrl
    title: NonemptyText
    published_date: date


REFERENCE_NORMALIZATION_VERSION = "http-url-v1"


class PendingReference(Contract):
    """A valid list target with no adapter; it is not a discovered article identity.

    HttpUrl normalizes authority/default ports, retaining path/query order and
    fragment. The original href is kept separately. No fetch permission is implied.
    """

    raw_href: str = Field(min_length=1, max_length=8192)
    resolved_url: WebUrl
    title: NonemptyText
    published_date: date
    row_index: int = Field(ge=0, strict=True)
    reference_kind: Literal["external", "unsupported_column", "unsupported_route"]

    def candidate_key(self) -> str:
        return "link:v1:" + hashlib.sha256(str(self.resolved_url).encode("utf-8")).hexdigest()


class PaginationEvidence(Contract):
    """Validated visible declarations, not a guarantee of complete cross-page coverage."""

    current_page: int = Field(ge=1, strict=True)
    total_pages: int = Field(ge=1, strict=True)
    is_last_page: bool = Field(strict=True)
    terminal_evidence: Literal["disabled_next_and_last"] | None = None
    # Disabled last has no link; retain an active last target for the coordinator.
    last_page_url: WebUrl | None = None

    @model_validator(mode="after")
    def consistent(self) -> "PaginationEvidence":
        if self.current_page > self.total_pages:
            raise ValueError("current page must not exceed total pages")
        if self.is_last_page != (self.current_page == self.total_pages):
            raise ValueError("last-page flag must agree with visible page numbers")
        if self.is_last_page:
            if self.terminal_evidence is None or self.last_page_url is not None:
                raise ValueError("last page requires disabled next/last evidence and no last link")
        elif self.terminal_evidence is not None or self.last_page_url is None:
            raise ValueError(
                "non-terminal page requires an active last link, without terminal evidence"
            )
        return self


class ListPage(Contract):
    # For this source, an empty page must be investigated rather than treated as success.
    entries: tuple[ListEntry, ...] = ()
    references: tuple[PendingReference, ...] = ()
    next_page_url: WebUrl | None = None
    pagination: PaginationEvidence

    @model_validator(mode="after")
    def next_matches_evidence(self) -> "ListPage":
        if not self.row_count:
            raise ValueError("list requires at least one notice or pending reference")
        positions = [reference.row_index for reference in self.references]
        if len(set(positions)) != len(positions) or any(p >= self.row_count for p in positions):
            raise ValueError("reference positions must uniquely locate actual list rows")
        if self.pagination.is_last_page != (self.next_page_url is None):
            raise ValueError("next-page URL must agree with terminal evidence")
        return self

    @property
    def row_count(self) -> int:
        return len(self.entries) + len(self.references)

    def ordered_rows(self) -> tuple[ListEntry | PendingReference, ...]:
        references = {reference.row_index: reference for reference in self.references}
        entries = iter(self.entries)
        return tuple(
            references[index] if index in references else next(entries)
            for index in range(self.row_count)
        )


class AttachmentReference(Contract):
    name: NonemptyText
    url: WebUrl
    source_attachment_id: NonemptyText | None = None
    access: Literal["not_checked", "manual_required"] = "not_checked"


class LinkReference(Contract):
    url: WebUrl
    text: str = ""


class ImageReference(Contract):
    url: WebUrl
    alt_text: str = ""


class NoticeContent(Contract):
    """Normalized content only; HTML preserves structure but is not sanitized for display."""

    title: NonemptyText
    published_date: date
    body_html: NonemptyText = Field(repr=False)
    # Image-only notices may have no textual body; parser must validate meaningful HTML.
    body_text: str = Field(repr=False)
    links: tuple[LinkReference, ...] = ()
    images: tuple[ImageReference, ...] = ()
    attachments: tuple[AttachmentReference, ...] = ()

    def canonical_json(self) -> str:
        """Deterministic serialization, preserving document order in reference arrays."""
        # Access status is operational metadata, not a change to the published notice.
        payload = self.model_dump(mode="json", exclude={"attachments": {"__all__": {"access"}}})
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def content_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ParsedNotice(Contract):
    source_document_id: NonemptyText
    page_url: WebUrl
    parser_version: NonemptyText
    content: NoticeContent = Field(repr=False)


class RawResponseReference(Contract):
    """Metadata only; RawStore verifies disk references at the I/O boundary."""

    source_id: NonemptyText
    requested_url: WebUrl
    final_url: WebUrl
    fetched_at: int = Field(ge=0, strict=True)
    status_code: int = Field(ge=100, le=599, strict=True)
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    body_path: str | None = None
    body_sha256: Digest | None = None

    @model_validator(mode="after")
    def body_reference(self) -> "RawResponseReference":
        if (self.body_path is None) != (self.body_sha256 is None):
            raise ValueError("body path and digest must be present together")
        if self.status_code == 304 and self.body_path is not None:
            raise ValueError("304 has no response body")
        if self.body_path is not None and self.body_path != f"raw/{self.body_sha256}.bin":
            raise ValueError("body path must be raw/<sha256>.bin relative to data_dir")
        return self
