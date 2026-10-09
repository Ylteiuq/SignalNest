"""Literal lexical search over current successful notices, with a derived FTS index.

No import-time I/O, network, HTML parsing or implicit migration. Readers never
repair the index. Writers hold the instance lock and use an explicit transaction.
"""

import re
import unicodedata
from datetime import date
from types import MappingProxyType

import sqlalchemy as sa
from pydantic import Field, ValidationError, model_validator
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from signalnest.contracts import Contract, NonemptyText, NoticeContent
from signalnest.schema import documents, notice_versions, raw_responses, search_documents

INDEX_VERSION = "literal-trigram-v1"
QUERY_VERSION = "literal-alias-v1"
# Explicit, finite lexical alternatives witnessed in the evaluation corpus. Bare
# 助教 stays literal and does not acquire a recruitment or eligibility meaning.
QUERY_ALIASES = MappingProxyType(
    {
        "助教招聘": ("助教招聘", "助教选聘", "招聘助教", "选聘助教", "助教招募"),
        "竞赛": ("竞赛", "大赛"),
    }
)


class SearchError(RuntimeError):
    """Finite diagnostics; database exceptions and notice bodies stay private."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SearchQuery(Contract):
    """Whitespace separates AND terms; finite visible aliases are OR alternatives."""

    query: NonemptyText = Field(max_length=200)
    date_from: date | None = None
    date_to: date | None = None
    source_id: NonemptyText | None = None
    limit: int = Field(default=20, ge=1, le=100, strict=True)
    offset: int = Field(default=0, ge=0, le=10000, strict=True)

    @model_validator(mode="after")
    def supported(self):
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from > self.date_to
        ):
            raise ValueError("date_from must not be after date_to")
        if any(ord(character) < 32 and not character.isspace() for character in self.query):
            raise ValueError("query must not contain control characters")
        if len(_terms(self.query)) > 8:
            raise ValueError("query supports at most eight literal terms")
        return self


class SearchHit(Contract):
    document_id: int
    source_id: str
    source_document_id: str
    version_id: int
    title: str
    published_date: date
    original_url: str
    snippet: str


class SearchPage(Contract):
    query: str
    items: tuple[SearchHit, ...]
    total: int
    offset: int
    limit: int
    index_version: str = INDEX_VERSION
    query_version: str = QUERY_VERSION
    expanded_terms: tuple[tuple[str, ...], ...] = ()


class NoticeDetail(Contract):
    document_id: int
    source_id: str
    source_document_id: str
    version_id: int
    original_url: str
    parser_version: str
    content_sha256: str
    status: str
    last_error_code: str | None = None
    content: NoticeContent


class IndexUpdate(Contract):
    indexed_count: int
    deleted_count: int
    index_version: str = INDEX_VERSION


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _terms(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_normalized(query).split()))


def expanded_terms(query: str) -> tuple[tuple[str, ...], ...]:
    """Inspectable query plan; no arbitrary synonyms, stemming or intent inference."""
    return tuple(QUERY_ALIASES.get(term, (term,)) for term in _terms(query))


def _require_index(connection: Connection) -> None:
    tables = set(
        connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name IN ('search_documents','search_fts')"
        ).scalars()
    )
    if tables != {"search_documents", "search_fts"}:
        raise SearchError("search_index_missing")


def _content(row) -> NoticeContent:
    try:
        content = NoticeContent.model_validate(row["normalized_content"])
    except ValidationError as exc:
        raise SearchError("notice_content_invalid") from exc
    if (
        content.title != row["title"]
        or content.published_date != row["published_date"]
        or content.content_sha256() != row["content_sha256"]
    ):
        raise SearchError("notice_content_invalid")
    return content


def _current():
    return documents.join(
        notice_versions,
        sa.and_(
            documents.c.id == notice_versions.c.document_id,
            documents.c.current_version_id == notice_versions.c.id,
        ),
    ).join(raw_responses, raw_responses.c.id == notice_versions.c.raw_response_id)


def _current_select():
    return sa.select(
        documents.c.id.label("document_id"),
        documents.c.source_id,
        documents.c.source_document_id,
        documents.c.status,
        documents.c.last_error_code,
        notice_versions.c.id.label("version_id"),
        notice_versions.c.title,
        notice_versions.c.published_date,
        notice_versions.c.normalized_content,
        notice_versions.c.content_sha256,
        notice_versions.c.parser_version,
        raw_responses.c.final_url.label("original_url"),
    ).select_from(_current())


def sync_document_in_transaction(connection: Connection, document_id: int) -> None:
    """Update this current-version index in the caller's success transaction.

    The caller holds writer_lock. No files or Parser are read. FTS trigger failure
    propagates, so the business success and derived row roll back together.
    """
    if not connection.in_transaction():
        raise SearchError("transaction_required")
    _require_index(connection)
    row = (
        connection.execute(_current_select().where(documents.c.id == document_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        target = (
            connection.execute(
                sa.select(documents.c.id, documents.c.current_version_id).where(
                    documents.c.id == document_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if target is None:
            raise SearchError("document_not_found")
        if target["current_version_id"] is not None:
            raise SearchError("notice_content_invalid")
        connection.execute(
            search_documents.delete().where(search_documents.c.document_id == document_id)
        )
        return
    content = _content(row)
    values = {
        "document_id": document_id,
        "version_id": row["version_id"],
        "title_text": _normalized(content.title),
        "body_text": _normalized(content.body_text),
        "index_version": INDEX_VERSION,
    }
    connection.execute(
        insert(search_documents)
        .values(**values)
        .on_conflict_do_update(index_elements=[search_documents.c.document_id], set_=values)
    )


def rebuild_index(engine: Engine) -> IndexUpdate:
    """Explicit all-or-nothing rebuild from normalized JSON, under caller's lock.

    Small single-user corpora use one local database transaction. No body file,
    network or HTML reparsing occurs. Any malformed success row aborts the rebuild.
    """
    try:
        with engine.begin() as connection:
            _require_index(connection)
            deleted = connection.execute(
                sa.select(sa.func.count()).select_from(search_documents)
            ).scalar_one()
            # External-content FTS can be repaired even after its postings were lost.
            connection.exec_driver_sql("INSERT INTO search_fts(search_fts) VALUES('rebuild')")
            connection.execute(search_documents.delete())
            ids = (
                connection.execute(
                    sa.select(documents.c.id)
                    .where(documents.c.current_version_id.is_not(None))
                    .order_by(documents.c.id)
                )
                .scalars()
                .all()
            )
            for document_id in ids:
                sync_document_in_transaction(connection, document_id)
            connection.exec_driver_sql(
                "INSERT INTO search_fts(search_fts, rank) VALUES('integrity-check', 1)"
            )
            return IndexUpdate(indexed_count=len(ids), deleted_count=deleted)
    except SQLAlchemyError as exc:
        raise SearchError("search_index_write_failed") from exc


def _fresh_index(connection: Connection) -> None:
    _require_index(connection)
    stale = connection.execute(
        sa.select(documents.c.id)
        .select_from(
            documents.outerjoin(search_documents, documents.c.id == search_documents.c.document_id)
        )
        .where(
            documents.c.current_version_id.is_not(None),
            sa.or_(
                search_documents.c.document_id.is_(None),
                search_documents.c.version_id != documents.c.current_version_id,
                search_documents.c.index_version != INDEX_VERSION,
            ),
        )
        .limit(1)
    ).first()
    if stale is not None:
        raise SearchError("search_index_stale")


def _snippet(content: NoticeContent, terms: tuple[str, ...]) -> str:
    text = re.sub(r"\s+", " ", content.body_text).strip()
    positions = [text.casefold().find(term) for term in terms]
    position = min((value for value in positions if value >= 0), default=0)
    start = max(0, position - 60)
    end = min(len(text), start + 240)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def search_notices(engine: Engine, query: SearchQuery) -> SearchPage:
    """Read one SQLite snapshot; never initialize, migrate, index or acquire a lock.

    Query groups are ANDed; fixed aliases within a group are ORed and returned in
    expanded_terms. Each alternative is literal in either title or body. Groups
    with only long alternatives use FTS5 trigram candidates; short groups use
    instr. Every group is finally checked literally. FTS/SQL operators are data.
    """
    groups = expanded_terms(query.query)
    terms = tuple(dict.fromkeys(term for group in groups for term in group))
    long_groups = [group for group in groups if all(len(term) >= 3 for term in group)]
    filters = []
    params = {}
    title_groups = []
    for group_number, group in enumerate(groups):
        matches = []
        title_matches = []
        for alternative_number, alternative in enumerate(group):
            parameter = f"term_{group_number}_{alternative_number}"
            params[parameter] = alternative
            title_matches.append(f"instr(i.title_text,:{parameter})>0")
            matches.append(
                f"(instr(i.title_text,:{parameter})>0 OR instr(i.body_text,:{parameter})>0)"
            )
        filters.append("(" + " OR ".join(matches) + ")")
        title_groups.append("(" + " OR ".join(title_matches) + ")")
    if query.source_id is not None:
        filters.append("d.source_id=:source_id")
        params["source_id"] = query.source_id
    if query.date_from is not None:
        filters.append("v.published_date>=:date_from")
        params["date_from"] = query.date_from.isoformat()
    if query.date_to is not None:
        filters.append("v.published_date<=:date_to")
        params["date_to"] = query.date_to.isoformat()
    if long_groups:
        params["match"] = " AND ".join(
            "(" + " OR ".join('"' + term.replace('"', '""') + '"' for term in group) + ")"
            for group in long_groups
        )
        filters.append("search_fts MATCH :match")
    joins = (
        " FROM search_documents i JOIN documents d ON d.id=i.document_id "
        "JOIN notice_versions v ON v.id=i.version_id AND v.document_id=d.id "
        "JOIN raw_responses r ON r.id=v.raw_response_id "
        + ("JOIN search_fts ON search_fts.rowid=i.document_id " if long_groups else "")
        + "WHERE d.current_version_id=i.version_id AND i.index_version=:index_version AND "
        + " AND ".join(filters)
    )
    params["index_version"] = INDEX_VERSION
    if long_groups:
        order = "bm25(search_fts,5.0,1.0),v.published_date DESC,d.id"
    else:
        title_hits = "+".join(f"CASE WHEN {group} THEN 1 ELSE 0 END" for group in title_groups)
        order = f"({title_hits}) DESC,v.published_date DESC,d.id"
    try:
        with engine.connect() as connection:
            _fresh_index(connection)
            total = connection.execute(sa.text("SELECT count(*)" + joins), params).scalar_one()
            result = (
                connection.execute(
                    sa.text(
                        "SELECT d.id AS document_id,d.source_id,d.source_document_id,"
                        "v.id AS version_id,v.title,v.published_date,r.final_url AS original_url"
                        + joins
                        + " ORDER BY "
                        + order
                        + " LIMIT :limit OFFSET :offset"
                    ),
                    params | {"limit": query.limit, "offset": query.offset},
                )
                .mappings()
                .all()
            )
            hits = []
            for row in result:
                version = (
                    connection.execute(
                        _current_select().where(documents.c.id == row["document_id"])
                    )
                    .mappings()
                    .one()
                )
                content = _content(version)
                hits.append(SearchHit(**row, snippet=_snippet(content, terms)))
            return SearchPage(
                query=query.query,
                items=tuple(hits),
                total=total,
                offset=query.offset,
                limit=query.limit,
                expanded_terms=groups,
            )
    except SQLAlchemyError as exc:
        raise SearchError("search_query_failed") from exc


def get_notice(engine: Engine, document_id: int) -> NoticeDetail:
    """Return the current successful version, including after a failed recheck.

    HTML is retained evidence, not safe display markup. Callers must not render it
    as trusted HTML. No index is required to inspect an individual notice.
    """
    if type(document_id) is not int or document_id < 1:
        raise SearchError("invalid_document_id")
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(_current_select().where(documents.c.id == document_id))
                .mappings()
                .one_or_none()
            )
            if row is None:
                target = (
                    connection.execute(
                        sa.select(documents.c.id, documents.c.current_version_id).where(
                            documents.c.id == document_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if target is None:
                    raise SearchError("document_not_found")
                if target["current_version_id"] is not None:
                    raise SearchError("notice_content_invalid")
                raise SearchError("notice_not_processed")
            content = _content(row)
            return NoticeDetail(
                **{key: row[key] for key in NoticeDetail.model_fields if key != "content"},
                content=content,
            )
    except SQLAlchemyError as exc:
        raise SearchError("search_query_failed") from exc
