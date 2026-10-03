"""Conservative exact-URI conditional candidates; never sends HTTP or guesses a baseline."""

import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import sqlalchemy as sa
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.contracts import RequestProfile, WebUrl
from signalnest.errors import IngestError
from signalnest.rawstore import RawStore, RawStoreError
from signalnest.schema import http_resources, raw_responses

_URI = TypeAdapter(WebUrl)
_ETAG = re.compile(r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"\Z')
_VARY_HEADERS = {"user-agent", "accept", "accept-encoding"}


@dataclass(frozen=True)
class CacheCandidate:
    resource_id: int
    response_id: int
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class CacheSelection:
    candidate: CacheCandidate | None = None
    reason: str | None = None

    @property
    def requires_full_fetch(self) -> bool:
        return self.candidate is None


def request_uri(value: str) -> str:
    try:
        url = _URI.validate_python(value)
    except ValidationError:
        raise IngestError("invalid_request_uri", "cache") from None
    if url.fragment is not None or "#" in value or "\\" in value:
        raise IngestError("invalid_request_uri", "cache")
    return str(url)  # HttpUrl preserves query order; never parse/sort article parameters.


def _vary(value: str | None) -> frozenset[str] | None:
    if value is None:
        return frozenset()
    fields = [field.strip().lower() for field in value.split(",")]
    if any(field not in _VARY_HEADERS for field in fields):
        return None
    return frozenset(fields)


def _no_store(value: str | None) -> bool:
    return any(
        part.strip().split("=", 1)[0].lower() == "no-store" for part in (value or "").split(",")
    )


def _valid_date(value: str | None) -> bool:
    if value is None:
        return True
    try:
        date = parsedate_to_datetime(value)
        return date.tzinfo is not None and date.utcoffset().total_seconds() == 0
    except (ValueError, TypeError, OverflowError):
        return False


def reuse_reason(response, resource) -> str | None:
    """Metadata eligibility; file content is checked outside transactions at use time."""
    if (
        response["status_code"] != 200
        or response["body_state"] != "complete"
        or not response["body_path"]
    ):
        return "baseline_not_complete_200"
    if (
        response["resource_id"] != resource["id"]
        or response["source_id"] != resource["source_id"]
        or response["requested_url"] != resource["request_uri"]
    ):
        return "baseline_key_mismatch"
    if response["final_url"] != response["requested_url"]:
        return "redirect_requires_full_fetch"
    if response["body_path"] != f"raw/{response['body_sha256']}.bin":
        return "baseline_reference_invalid"
    if _vary(response["vary"]) is None:
        return "vary_unsupported"
    if _no_store(response["cache_control"]):
        return "cache_no_store"
    if response["content_encoding"] not in {None, "", "identity"}:
        return "encoding_unsupported"
    if (response["etag"] is not None and not _ETAG.fullmatch(response["etag"])) or not _valid_date(
        response["last_modified"]
    ):
        return "validator_invalid"
    if response["etag"] is None and response["last_modified"] is None:
        return "validator_missing"
    return None


def validation_reason(observed, body) -> str | None:
    if observed["vary"] is not None and _vary(observed["vary"]) != _vary(body["vary"]):
        return "vary_changed"
    if _no_store(observed["cache_control"]):
        return "cache_no_store"
    if observed["etag"] is not None:
        if not _ETAG.fullmatch(observed["etag"]):
            return "validator_invalid"
        if body["etag"] is not None and observed["etag"].removeprefix("W/") != body[
            "etag"
        ].removeprefix("W/"):
            return "validator_changed"
    if observed["last_modified"] is not None and (
        not _valid_date(observed["last_modified"])
        or observed["last_modified"] != body["last_modified"]
    ):
        return "validator_changed"
    return None


def validate_binding(connection: Connection, candidate: CacheCandidate, observed):
    resource = (
        connection.execute(
            sa.select(http_resources).where(http_resources.c.id == candidate.resource_id)
        )
        .mappings()
        .one_or_none()
    )
    body = (
        connection.execute(
            sa.select(raw_responses).where(raw_responses.c.id == candidate.response_id)
        )
        .mappings()
        .one_or_none()
    )
    if (
        resource is None
        or body is None
        or observed["status_code"] != 304
        or observed["resource_id"] != resource["id"]
        or observed["source_id"] != body["source_id"]
        or observed["requested_url"] != body["requested_url"]
        or observed["final_url"] != body["final_url"]
        or observed["page_type"] != body["page_type"]
        or observed["document_id"] != body["document_id"]
        or observed["fetched_at"] < body["fetched_at"]
        or resource["latest_response_id"] != body["id"]
        or resource["blocked_by_response_id"] is not None
        or candidate.etag != body["etag"]
        or candidate.last_modified != body["last_modified"]
        or reuse_reason(body, resource) is not None
    ):
        raise IngestError("cache_binding_invalid", "cache")
    return body


def select_cache_candidate(
    engine: Engine, raw_store: RawStore, source_id: str, uri: str, profile: RequestProfile
) -> CacheSelection:
    uri = request_uri(uri)
    try:
        with engine.connect() as connection:
            resource = (
                connection.execute(
                    sa.select(http_resources).where(
                        http_resources.c.source_id == source_id,
                        http_resources.c.request_uri == uri,
                        http_resources.c.profile_sha256 == profile.sha256(),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if resource is None or resource["latest_response_id"] is None:
                return CacheSelection(reason="baseline_missing")
            if resource["blocked_by_response_id"] is not None:
                return CacheSelection(reason="cache_validation_blocked")
            body = (
                connection.execute(
                    sa.select(raw_responses).where(
                        raw_responses.c.id == resource["latest_response_id"]
                    )
                )
                .mappings()
                .one()
            )
    except SQLAlchemyError as exc:
        raise IngestError("database_read_failed", "cache") from exc
    if resource["request_profile"] != profile.model_dump():
        return CacheSelection(reason="profile_mismatch")
    reason = reuse_reason(body, resource)
    if reason is not None:
        return CacheSelection(reason=reason)
    try:
        content = raw_store.read(body["body_path"], body["body_sha256"])
    except RawStoreError as exc:
        return CacheSelection(reason=exc.code)
    if not content:
        return CacheSelection(reason="baseline_empty")
    return CacheSelection(
        CacheCandidate(resource["id"], body["id"], body["etag"], body["last_modified"])
    )


def pending_resources(engine: Engine, source_id: str, parser_version: str):
    """Latest-body/rule mismatch, including prior parse failures; no raw-hash shortcut."""
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                sa.select(http_resources)
                .where(
                    http_resources.c.source_id == source_id,
                    http_resources.c.latest_response_id.is_not(None),
                    sa.or_(
                        http_resources.c.last_processed_response_id.is_(None),
                        http_resources.c.last_processed_response_id
                        != http_resources.c.latest_response_id,
                        http_resources.c.last_processed_parser_version != parser_version,
                    ),
                )
                .order_by(http_resources.c.id)
            ).mappings()
        )
