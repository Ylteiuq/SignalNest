"""Bounded synchronous HTTP boundary; no Parser, archive or business writes.

One instance owns a run's Client, cooperative deadlines, request budget and gate.
Only server cooldown is written here. Callers hold writer_lock for the whole run
and register every returned response, in order, with the existing evidence service.
"""

import logging
import math
import random
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Literal, Protocol
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from pydantic import Field, model_validator
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.cache import (
    CacheCandidate,
    check_not_modified,
    request_uri,
    select_cache_candidate,
)
from signalnest.config import HttpSettings
from signalnest.contracts import Contract, NonemptyText, RequestProfile
from signalnest.errors import IngestError, validate_time
from signalnest.eventlog import Event, log_event
from signalnest.ingestion import ResponseInput
from signalnest.ingestion_state import read_source_state, set_cooldown_in_transaction
from signalnest.rawstore import RawStore

_SQLITE_MAX = 2**63 - 1
_HEADER_LIMIT = 2048
_REDIRECTS = {301, 302, 303, 307, 308}
_RETRY_STATUSES = {408, 500, 502, 503, 504}


class FetchCode(StrEnum):
    INVALID_TARGET = "invalid_target"
    CONNECT_TIMEOUT = "connect_timeout"
    CONNECT_ERROR = "connect_error"
    READ_TIMEOUT = "read_timeout"
    READ_ERROR = "read_error"
    WRITE_TIMEOUT = "write_timeout"
    WRITE_ERROR = "write_error"
    POOL_TIMEOUT = "pool_timeout"
    TLS_ERROR = "tls_error"
    REMOTE_PROTOCOL_ERROR = "remote_protocol_error"
    LOCAL_PROTOCOL_ERROR = "local_protocol_error"
    TRANSPORT_ERROR = "transport_error"
    DECODING_ERROR = "decoding_error"
    BODY_TOO_LARGE = "body_too_large"
    EMPTY_BODY = "empty_body"
    INCOMPLETE_BODY = "incomplete_body"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"
    INVALID_RESPONSE_HEADERS = "invalid_response_headers"
    REDIRECT_INVALID = "redirect_invalid"
    REDIRECT_LOOP = "redirect_loop"
    REDIRECT_LIMIT = "redirect_limit"
    UNEXPECTED_304 = "unexpected_304"
    CACHE_REPAIR_REQUIRED = "cache_repair_required"
    HTTP_UNAUTHORIZED = "http_unauthorized"
    HTTP_FORBIDDEN = "http_forbidden"
    HTTP_NOT_FOUND = "http_not_found"
    HTTP_STATUS = "http_status"
    HTTP_TRANSIENT = "http_transient"
    HTTP_RATE_LIMITED = "http_rate_limited"
    SERVER_COOLDOWN = "server_cooldown"
    RETRY_AFTER_OUT_OF_RANGE = "retry_after_out_of_range"
    REQUEST_LIMIT = "request_limit"
    RUN_TIME_LIMIT = "run_time_limit"
    RESOURCE_TIME_LIMIT = "resource_time_limit"


_TRANSPORT_CODES = {
    FetchCode.CONNECT_TIMEOUT,
    FetchCode.CONNECT_ERROR,
    FetchCode.READ_TIMEOUT,
    FetchCode.READ_ERROR,
    FetchCode.WRITE_TIMEOUT,
    FetchCode.WRITE_ERROR,
    FetchCode.POOL_TIMEOUT,
    FetchCode.TLS_ERROR,
    FetchCode.REMOTE_PROTOCOL_ERROR,
    FetchCode.LOCAL_PROTOCOL_ERROR,
    FetchCode.TRANSPORT_ERROR,
    FetchCode.DECODING_ERROR,
    FetchCode.INCOMPLETE_BODY,
}


class FetchLimits(Contract):
    max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=64 * 1024 * 1024, strict=True)
    max_redirects: int = Field(default=3, ge=0, le=10, strict=True)
    max_retries: int = Field(default=2, ge=0, le=5, strict=True)
    max_requests: int = Field(default=120, ge=1, le=100000, strict=True)
    resource_seconds: float = Field(default=60.0, gt=0, le=3600, allow_inf_nan=False)
    run_seconds: float = Field(default=600.0, gt=0, le=86400, allow_inf_nan=False)
    max_wait_seconds: float = Field(default=15.0, ge=0, le=60, allow_inf_nan=False)
    backoff_seconds: float = Field(default=1.0, gt=0, le=60, allow_inf_nan=False)


class FetchTarget(Contract):
    uri: NonemptyText
    page_type: Literal["list", "notice"]
    source_document_id: NonemptyText | None = None

    @model_validator(mode="after")
    def target(self):
        if (self.page_type == "notice") != (self.source_document_id is not None):
            raise ValueError("notice requires source_document_id; list must not have one")
        return self


@dataclass(frozen=True)
class FetchAttempt:
    """One physical GET; only a fully read nonempty 200 has content.

    candidate is passed to record_response only for a 304. Headers arrival supplies
    metadata.fetched_at; a connection failure has no metadata or fabricated status.
    """

    started_at: int
    finished_at: int
    metadata: ResponseInput | None = None
    content: bytes | None = field(default=None, repr=False)
    candidate: CacheCandidate | None = None
    error_code: FetchCode | None = None
    not_before_at: int | None = None


@dataclass(frozen=True)
class FetchResult:
    outcome: Literal["complete", "bodyless", "transport_failure", "deferred"]
    attempts: tuple[FetchAttempt, ...] = ()
    error_code: FetchCode | None = None
    not_before_at: int | None = None

    @property
    def response(self) -> FetchAttempt | None:
        return self.attempts[-1] if self.attempts else None


class Clock(Protocol):
    def time(self) -> float: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


def default_profile(settings: HttpSettings) -> RequestProfile:
    return RequestProfile(user_agent=settings.user_agent, accept="text/html")


def make_client(
    settings: HttpSettings,
    *,
    profile: RequestProfile | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """No HTTP at construction. Caller owns closure; production transport never retries."""
    profile = profile or default_profile(settings)
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    return httpx.Client(
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.read_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        ),
        headers={
            "User-Agent": profile.user_agent,
            "Accept": profile.accept,
            "Accept-Encoding": profile.accept_encoding,
        },
        transport=transport
        if transport is not None
        else httpx.HTTPTransport(retries=0, verify=True, trust_env=False, limits=limits),
        limits=limits,
        verify=True,
        follow_redirects=False,
        trust_env=False,
    )


def _target_uri(value: str, target: FetchTarget) -> str:
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or any(c in value for c in "\\#"):
        raise ValueError("invalid_target")
    try:
        uri = request_uri(value)
        parts = urlsplit(uri)
        if (
            parts.scheme != "https"
            or parts.hostname != "uc.whu.edu.cn"
            or parts.port not in {None, 443}
        ):
            raise ValueError("invalid_target")
        if target.page_type == "list":
            if "?" in value or not re.fullmatch(
                r"/tzgg/xstz(?:\.htm|/[1-9][0-9]*\.htm)", parts.path
            ):
                raise ValueError("invalid_target")
        else:
            query = parse_qs(parts.query, keep_blank_values=True)
            match = re.fullmatch(r"/info/(1517)/([1-9][0-9]*)\.htm", parts.path)
            if match:
                category, article = match.groups()
                if any(
                    key in query and query[key] != [expected]
                    for key, expected in (("wbtreeid", category), ("wbnewsid", article))
                ):
                    raise ValueError("invalid_target")
            elif parts.path == "/2022/show.jsp":
                if len(query.get("wbtreeid", [])) != 1 or len(query.get("wbnewsid", [])) != 1:
                    raise ValueError("invalid_target")
                category, article = query["wbtreeid"][0], query["wbnewsid"][0]
                if (
                    category != "1517"
                    or not re.fullmatch(r"[1-9][0-9]*", article)
                    or ("urltype" in query and query["urltype"] != ["news.NewsContentUrl"])
                ):
                    raise ValueError("invalid_target")
            else:
                raise ValueError("invalid_target")
            if f"{category}:{article}" != target.source_document_id:
                raise ValueError("invalid_target")
        # Key the actual HTTPX URI, retaining query order and irrelevant parameters.
        return str(httpx.URL(uri))
    except (IngestError, httpx.InvalidURL) as exc:
        raise ValueError("invalid_target") from exc


def retry_after_deadline(values: list[str], received_at: float) -> tuple[int | None, bool]:
    """Full UTC deadline and overflow flag; None means absent/invalid, never wait=0.

    Huge valid seconds fail closed at SQLite's maximum time, never wrap around or
    get truncated to this run's waiting budget.
    """
    if len(values) != 1:
        return None, False
    value = values[0].strip()
    if re.fullmatch(r"[0-9]+", value):
        digits = value.lstrip("0") or "0"
        if len(digits) > 19:
            return _SQLITE_MAX, True
        deadline = math.ceil(received_at) + int(digits)
        return (deadline, False) if deadline <= _SQLITE_MAX else (_SQLITE_MAX, True)
    if len(value) > _HEADER_LIMIT or any(ord(c) < 32 or ord(c) > 126 for c in value):
        return None, False
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            # Obsolete asctime HTTP-date has implicit GMT; arbitrary naive dates don't.
            if not re.fullmatch(
                r"[A-Z][a-z]{2} [A-Z][a-z]{2} [ 0-9][0-9] \d\d:\d\d:\d\d \d{4}", value
            ):
                return None, False
            date = date.replace(tzinfo=UTC)
        if date.utcoffset().total_seconds() != 0:
            return None, False
        return max(math.ceil(received_at), math.ceil(date.timestamp())), False
    except (ValueError, TypeError, OverflowError):
        return None, False


def _transport_code(exc: httpx.RequestError) -> tuple[FetchCode, bool]:
    cause: BaseException | None = exc
    visited = set()
    while cause is not None and id(cause) not in visited:
        visited.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return FetchCode.TLS_ERROR, False
        cause = cause.__cause__ or cause.__context__
    for kind, code, retry in (
        (httpx.ConnectTimeout, FetchCode.CONNECT_TIMEOUT, True),
        (httpx.ReadTimeout, FetchCode.READ_TIMEOUT, True),
        (httpx.WriteTimeout, FetchCode.WRITE_TIMEOUT, True),
        (httpx.PoolTimeout, FetchCode.POOL_TIMEOUT, False),
        (httpx.ConnectError, FetchCode.CONNECT_ERROR, True),
        (httpx.ReadError, FetchCode.READ_ERROR, True),
        (httpx.WriteError, FetchCode.WRITE_ERROR, True),
        (httpx.RemoteProtocolError, FetchCode.REMOTE_PROTOCOL_ERROR, True),
        (httpx.LocalProtocolError, FetchCode.LOCAL_PROTOCOL_ERROR, False),
        (httpx.DecodingError, FetchCode.DECODING_ERROR, False),
    ):
        if isinstance(exc, kind):
            return code, retry
    return FetchCode.TRANSPORT_ERROR, False


class _BodyRejected(Exception):
    def __init__(self, code: FetchCode):
        self.code = code


class HttpFetcher:
    """A single serial run. No transaction remains open during sleep or HTTP/file I/O."""

    def __init__(
        self,
        engine: Engine,
        raw_store: RawStore,
        settings: HttpSettings,
        *,
        source_id: str,
        profile: RequestProfile | None = None,
        limits: FetchLimits | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Clock = time,
        jitter: Callable[[], float] = lambda: random.uniform(0.0, 0.5),
        run_id: str | None = None,
    ):
        if not source_id.strip():
            raise ValueError("source_id must not be empty")
        self.engine, self.raw_store, self.source_id = engine, raw_store, source_id
        self.settings = settings.model_copy(deep=True)
        self.profile = profile or default_profile(self.settings)
        self.limits = limits or FetchLimits()
        self.clock, self.jitter, self.run_id = clock, jitter, run_id
        self.client = make_client(self.settings, profile=self.profile, transport=transport)
        self.run_deadline = clock.monotonic() + self.limits.run_seconds
        self.requests_sent = 0
        self._last_started: float | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self.client.close()

    def _utc(self) -> int:
        value = int(self.clock.time())
        validate_time(value)
        return value

    def _budget_code(self, resource_deadline: float) -> FetchCode | None:
        if self.clock.monotonic() >= self.run_deadline:
            return FetchCode.RUN_TIME_LIMIT
        if self.clock.monotonic() >= resource_deadline:
            return FetchCode.RESOURCE_TIME_LIMIT
        return None

    def _gate(
        self, resource_deadline: float, ready_at: float
    ) -> tuple[FetchCode | None, int | None]:
        while True:
            if code := self._budget_code(resource_deadline):
                return code, None
            if self.requests_sent >= self.limits.max_requests:
                return FetchCode.REQUEST_LIMIT, None
            try:
                state = read_source_state(self.engine, self.source_id)
            except SQLAlchemyError as exc:
                raise IngestError("database_read_failed", "fetch_gate") from exc
            not_before = state["not_before_at"] if state is not None else None
            server_wait = (
                max(0.0, not_before - self.clock.time()) if not_before is not None else 0.0
            )
            if server_wait > self.limits.max_wait_seconds:
                return FetchCode.SERVER_COOLDOWN, not_before
            earliest = ready_at
            if self._last_started is not None:
                earliest = max(
                    earliest, self._last_started + self.settings.request_interval_seconds
                )
            wait = max(0.0, earliest - self.clock.monotonic(), server_wait)
            deadline = min(resource_deadline, self.run_deadline)
            if self.clock.monotonic() + wait >= deadline:
                code = (
                    FetchCode.RUN_TIME_LIMIT
                    if self.run_deadline <= resource_deadline
                    else FetchCode.RESOURCE_TIME_LIMIT
                )
                return code, not_before if server_wait else None
            if wait == 0:
                return None, None
            self.clock.sleep(min(wait, 60.0))

    def _cooldown(self, response: httpx.Response, received_at: float) -> tuple[int | None, bool]:
        values = response.headers.get_list("Retry-After")
        if response.status_code != 429 and not (
            values and (response.status_code in _REDIRECTS | _RETRY_STATUSES)
        ):
            return None, False
        deadline, overflow = retry_after_deadline(values, received_at)
        if deadline is None:
            deadline = math.ceil(received_at) + 1800
        try:
            with self.engine.begin() as connection:
                set_cooldown_in_transaction(connection, self.source_id, deadline)
        except SQLAlchemyError as exc:
            raise IngestError("cooldown_state_unavailable", "fetch_gate") from exc
        return deadline, overflow

    def _read_body(self, response: httpx.Response, deadline: float) -> bytes:
        if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            raise _BodyRejected(FetchCode.UNSUPPORTED_CONTENT_ENCODING)
        if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "text/html":
            raise _BodyRejected(FetchCode.UNSUPPORTED_CONTENT_TYPE)
        lengths = response.headers.get_list("Content-Length")
        length = None
        if lengths:
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
                raise _BodyRejected(FetchCode.INVALID_RESPONSE_HEADERS)
            if len(lengths[0]) > _HEADER_LIMIT:
                raise _BodyRejected(FetchCode.INVALID_RESPONSE_HEADERS)
            digits = lengths[0].lstrip("0") or "0"
            if len(digits) > 8:
                raise _BodyRejected(FetchCode.BODY_TOO_LARGE)
            length = int(digits)
            if length > self.limits.max_body_bytes:
                raise _BodyRejected(FetchCode.BODY_TOO_LARGE)
        body = bytearray()
        # No iter_bytes/decompression, nor chunk_size buffering of a slow-drip stream.
        for chunk in response.iter_raw():
            if code := self._budget_code(deadline):
                raise _BodyRejected(code)
            if len(body) + len(chunk) > self.limits.max_body_bytes:
                raise _BodyRejected(FetchCode.BODY_TOO_LARGE)
            body.extend(chunk)
        if code := self._budget_code(deadline):
            raise _BodyRejected(code)
        if length is not None and len(body) != length:
            raise _BodyRejected(FetchCode.INCOMPLETE_BODY)
        if not body:
            raise _BodyRejected(FetchCode.EMPTY_BODY)
        return bytes(body)

    def _exchange(
        self, uri: str, target: FetchTarget, candidate: CacheCandidate | None, deadline: float
    ) -> tuple[FetchAttempt, bool, str | None]:
        remaining = min(deadline, self.run_deadline) - self.clock.monotonic()
        headers = {
            "User-Agent": self.profile.user_agent,
            "Accept": self.profile.accept,
            "Accept-Encoding": self.profile.accept_encoding,
        }
        if candidate is not None:
            if candidate.etag is not None:
                headers["If-None-Match"] = candidate.etag
            if candidate.last_modified is not None:
                headers["If-Modified-Since"] = candidate.last_modified
        timeout = {
            "connect": min(remaining, self.settings.connect_timeout_seconds),
            "read": min(remaining, self.settings.read_timeout_seconds),
            "write": min(remaining, self.settings.read_timeout_seconds),
            "pool": min(remaining, self.settings.connect_timeout_seconds),
        }
        # Direct Request excludes accumulated cookies/client defaults/auth entirely.
        self.client.cookies.clear()
        request = httpx.Request(
            "GET",
            uri,
            headers=[(k.encode("ascii"), v.encode("latin-1")) for k, v in headers.items()],
            extensions={"timeout": timeout},
        )
        started = self._utc()
        self._last_started = self.clock.monotonic()
        self.requests_sent += 1
        log_event(
            logging.getLogger("signalnest"),
            Event.FETCH_STARTED,
            source_id=self.source_id,
            run_id=self.run_id,
            stage="request",
        )
        metadata, content, code, location, not_before = None, None, None, None, None
        retryable = False
        response = None
        received_at = None

        def received(observed: httpx.Response) -> None:
            nonlocal response, received_at
            response, received_at = observed, self.clock.time()

        # HTTPX constructs next_request even with follow_redirects=False. Its public
        # hook lets us retain metadata if that construction rejects a bad Location.
        self.client.event_hooks["response"] = [received]
        try:
            redirect_url_error = False
            try:
                self.client.send(request, stream=True, follow_redirects=False, auth=None)
            except httpx.InvalidURL:
                if response is None or response.status_code not in _REDIRECTS:
                    raise
                redirect_url_error = True
            try:
                fetched_at = int(received_at)
                validate_time(fetched_at)
                fields = {}
                invalid_headers = False
                for name, key in (
                    ("Content-Type", "content_type"),
                    ("ETag", "etag"),
                    ("Last-Modified", "last_modified"),
                    ("Vary", "vary"),
                    ("Cache-Control", "cache_control"),
                    ("Content-Encoding", "content_encoding"),
                ):
                    value = response.headers.get(name)
                    if (
                        name in {"Content-Type", "ETag", "Last-Modified", "Content-Encoding"}
                        and len(response.headers.get_list(name)) > 1
                    ):
                        invalid_headers = True
                    if value is not None and (
                        len(value) > _HEADER_LIMIT
                        or any(ord(c) < 32 or ord(c) == 127 for c in value)
                    ):
                        value, invalid_headers = None, True
                    fields[key] = value
                if fields["content_encoding"] is not None:
                    fields["content_encoding"] = fields["content_encoding"].strip().lower()
                metadata = ResponseInput(
                    page_type=target.page_type,
                    source_id=self.source_id,
                    source_document_id=target.source_document_id,
                    requested_url=uri,
                    final_url=uri,
                    fetched_at=fetched_at,
                    status_code=response.status_code,
                    request_profile=self.profile,
                    body_state="unavailable",
                    **fields,
                )
                not_before, overflow = self._cooldown(response, received_at)
                if overflow:
                    code = FetchCode.RETRY_AFTER_OUT_OF_RANGE
                elif response.status_code == 429:
                    code = FetchCode.HTTP_RATE_LIMITED
                elif invalid_headers:
                    code = FetchCode.INVALID_RESPONSE_HEADERS
                elif redirect_url_error:
                    code = FetchCode.REDIRECT_INVALID
                elif code := self._budget_code(deadline):
                    pass
                elif response.status_code == 200:
                    content = self._read_body(response, deadline)
                    metadata = metadata.model_copy(update={"body_state": "complete"})
                elif response.status_code in _REDIRECTS:
                    locations = response.headers.get_list("Location")
                    if len(locations) == 1 and 0 < len(locations[0]) <= _HEADER_LIMIT:
                        location = locations[0]
                    else:
                        code = FetchCode.REDIRECT_INVALID
                elif response.status_code != 304:
                    code = {
                        401: FetchCode.HTTP_UNAUTHORIZED,
                        403: FetchCode.HTTP_FORBIDDEN,
                        404: FetchCode.HTTP_NOT_FOUND,
                        410: FetchCode.HTTP_NOT_FOUND,
                    }.get(response.status_code, FetchCode.HTTP_STATUS)
                    if response.status_code in _RETRY_STATUSES:
                        code, retryable = FetchCode.HTTP_TRANSIENT, True
            finally:
                response.close()
        except _BodyRejected as exc:
            code = exc.code
            retryable = code == FetchCode.INCOMPLETE_BODY
        except httpx.RequestError as exc:
            code, retryable = _transport_code(exc)
            content = None
            if metadata is not None:
                metadata = metadata.model_copy(update={"body_state": "unavailable"})
        finally:
            self.client.event_hooks["response"] = []
        attempt = FetchAttempt(
            started,
            self._utc(),
            metadata,
            content,
            candidate if metadata is not None and metadata.status_code == 304 else None,
            code,
            not_before,
        )
        return attempt, retryable, location

    def fetch(self, target: FetchTarget, *, unconditional: bool = False) -> FetchResult:
        """No nested retry layer. Redirect/retry/304 repair spends the shared run budget."""
        if self.client.is_closed:
            raise RuntimeError("Fetcher is closed")
        attempts = []
        resource_deadline = self.clock.monotonic() + self.limits.resource_seconds
        try:
            uri = _target_uri(target.uri, target)
        except ValueError:
            return FetchResult("bodyless", error_code=FetchCode.INVALID_TARGET)
        visited, redirects, retries, repaired = {uri}, 0, 0, False
        ready_at = self.clock.monotonic()
        force_full = unconditional

        def finish(outcome, code=None, not_before=None):
            log_event(
                logging.getLogger("signalnest"),
                Event.FETCH_FINISHED,
                source_id=self.source_id,
                run_id=self.run_id,
                stage=outcome,
                error_code=code,
            )
            return FetchResult(outcome, tuple(attempts), code, not_before)

        while True:
            code, not_before = self._gate(resource_deadline, ready_at)
            if code:
                return finish("deferred", code, not_before)
            candidate = None
            if not force_full:
                candidate = select_cache_candidate(
                    self.engine,
                    self.raw_store,
                    self.source_id,
                    uri,
                    self.profile,
                    page_type=target.page_type,
                    source_document_id=target.source_document_id,
                ).candidate
                if candidate is not None and any(
                    len(v) > _HEADER_LIMIT
                    or any(ord(c) < 32 or ord(c) == 127 or ord(c) > 255 for c in v)
                    for v in (candidate.etag, candidate.last_modified)
                    if v is not None
                ):
                    candidate = None
            # File verification also spends the cooperative time budget.
            if code := self._budget_code(resource_deadline):
                return finish("deferred", code)
            attempt, retryable, location = self._exchange(uri, target, candidate, resource_deadline)
            attempts.append(attempt)
            status = attempt.metadata.status_code if attempt.metadata is not None else None
            if attempt.error_code in {
                FetchCode.RUN_TIME_LIMIT,
                FetchCode.RESOURCE_TIME_LIMIT,
                FetchCode.HTTP_RATE_LIMITED,
                FetchCode.RETRY_AFTER_OUT_OF_RANGE,
            }:
                return finish("deferred", attempt.error_code, attempt.not_before_at)
            if attempt.not_before_at is not None:
                server_wait = max(0.0, attempt.not_before_at - self.clock.time())
                remaining = min(resource_deadline, self.run_deadline) - self.clock.monotonic()
                if server_wait > self.limits.max_wait_seconds or server_wait >= remaining:
                    return finish("deferred", FetchCode.SERVER_COOLDOWN, attempt.not_before_at)
            if status == 304:
                reason = (
                    "baseline_missing"
                    if candidate is None
                    else check_not_modified(
                        self.engine,
                        self.raw_store,
                        self.source_id,
                        uri,
                        self.profile,
                        candidate,
                        attempt.metadata.model_dump(mode="json"),
                    )
                )
                if budget_code := self._budget_code(resource_deadline):
                    return finish("deferred", budget_code)
                if attempt.error_code is None and reason is None:
                    return finish("bodyless")
                code = (
                    FetchCode.UNEXPECTED_304
                    if candidate is None
                    else FetchCode.CACHE_REPAIR_REQUIRED
                )
                attempts[-1] = replace(attempt, error_code=code)
                if repaired or retries >= self.limits.max_retries:
                    return finish("bodyless", FetchCode.UNEXPECTED_304)
                repaired, force_full, retryable = True, True, True
            elif attempt.content is not None:
                return finish("complete")
            elif location is not None:
                # Check raw Location before urljoin can erase '?'/'#'/backslashes.
                try:
                    if (
                        any(c in location for c in "\\#")
                        or (target.page_type == "list" and "?" in location)
                        or any(ord(c) <= 32 or ord(c) == 127 for c in location)
                    ):
                        raise ValueError("invalid_target")
                    next_uri = _target_uri(urljoin(uri, location), target)
                except ValueError:
                    attempts[-1] = replace(attempt, error_code=FetchCode.REDIRECT_INVALID)
                    return finish("bodyless", FetchCode.REDIRECT_INVALID)
                if next_uri in visited:
                    attempts[-1] = replace(attempt, error_code=FetchCode.REDIRECT_LOOP)
                    return finish("bodyless", FetchCode.REDIRECT_LOOP)
                if redirects >= self.limits.max_redirects:
                    attempts[-1] = replace(attempt, error_code=FetchCode.REDIRECT_LIMIT)
                    return finish("bodyless", FetchCode.REDIRECT_LIMIT)
                redirects += 1
                visited.add(next_uri)
                uri, force_full = next_uri, unconditional
                continue
            if not retryable or retries >= self.limits.max_retries:
                outcome = (
                    "transport_failure"
                    if status is None or attempt.error_code in _TRANSPORT_CODES
                    else "bodyless"
                )
                return finish(outcome, attempts[-1].error_code, attempt.not_before_at)
            retries += 1
            jitter = self.jitter()
            if not math.isfinite(jitter) or not 0 <= jitter <= 0.5:
                raise ValueError("jitter must be in [0, 0.5]")
            ready_at = (
                self.clock.monotonic() + self.limits.backoff_seconds * 2 ** (retries - 1) + jitter
            )
            log_event(
                logging.getLogger("signalnest"),
                Event.FETCH_RETRIED,
                source_id=self.source_id,
                run_id=self.run_id,
                stage="retry",
                error_code=attempts[-1].error_code,
            )
