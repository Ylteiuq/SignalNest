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


class ListPage(Contract):
    # For this source, an empty page must be investigated rather than treated as success.
    entries: tuple[ListEntry, ...] = Field(min_length=1)
    next_page_url: WebUrl | None = None


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
