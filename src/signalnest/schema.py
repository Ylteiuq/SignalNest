"""Current SQLAlchemy Core schema; only Alembic creates or alters tables.

Times are integer UTC Unix seconds; publication dates are site calendar dates.
"""

import sqlalchemy as sa

metadata = sa.MetaData()

# A document's stable identity is independent of its URL, title, and content digest.
documents = sa.Table(
    "documents",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("source_document_id", sa.Text, nullable=False),
    sa.Column("detail_url", sa.Text, nullable=False),
    sa.Column("discovered_title", sa.Text, nullable=False),
    sa.Column("discovered_at", sa.Integer, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="discovered"),
    sa.Column("current_version_id", sa.Integer),
    sa.Column("last_attempt_at", sa.Integer),
    sa.Column("last_success_at", sa.Integer),
    sa.Column("last_error_code", sa.Text),
    sa.Column("next_due_at", sa.Integer),
    sa.UniqueConstraint("source_id", "source_document_id", name="uq_document_identity"),
    sa.ForeignKeyConstraint(
        ["id", "current_version_id"],
        ["notice_versions.document_id", "notice_versions.id"],
        name="fk_document_current_version",
        use_alter=True,
    ),
    sa.CheckConstraint(
        "(current_version_id IS NULL AND last_success_at IS NULL) OR "
        "(current_version_id IS NOT NULL AND last_success_at IS NOT NULL)",
        name="ck_document_success_reference",
    ),
    sa.CheckConstraint(
        "status IN ('discovered', 'processed', 'failed')", name="ck_document_status"
    ),
    sa.CheckConstraint(
        "(status = 'discovered' AND last_attempt_at IS NULL AND last_success_at IS NULL "
        "AND last_error_code IS NULL) OR "
        "(status = 'processed' AND last_attempt_at IS NOT NULL AND last_success_at IS NOT NULL "
        "AND last_error_code IS NULL) OR "
        "(status = 'failed' AND last_attempt_at IS NOT NULL AND last_error_code IS NOT NULL)",
        name="ck_document_processing_state",
    ),
    sa.CheckConstraint(
        "last_success_at IS NULL OR last_success_at <= last_attempt_at",
        name="ck_document_success_time",
    ),
    sa.Index("ix_documents_next_due_at", "next_due_at"),
)

raw_responses = sa.Table(
    "raw_responses",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("requested_url", sa.Text, nullable=False),
    sa.Column("final_url", sa.Text, nullable=False),
    sa.Column("fetched_at", sa.Integer, nullable=False),
    sa.Column("status_code", sa.Integer, nullable=False),
    sa.Column("content_type", sa.Text),
    sa.Column("etag", sa.Text),
    sa.Column("last_modified", sa.Text),
    sa.Column("body_path", sa.Text),
    sa.Column("body_sha256", sa.Text),
    sa.CheckConstraint("status_code BETWEEN 100 AND 599", name="ck_response_status"),
    sa.CheckConstraint(
        "(body_path IS NULL AND body_sha256 IS NULL) OR "
        "(body_path IS NOT NULL AND body_sha256 IS NOT NULL AND length(body_sha256) = 64)",
        name="ck_response_body_reference",
    ),
    sa.CheckConstraint(
        "status_code != 304 OR body_path IS NULL", name="ck_response_304_has_no_body"
    ),
)

notice_versions = sa.Table(
    "notice_versions",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("raw_response_id", sa.ForeignKey("raw_responses.id"), nullable=False),
    sa.Column("content_sha256", sa.Text, nullable=False),
    sa.Column("parser_version", sa.Text, nullable=False),
    sa.Column("parsed_at", sa.Integer, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("published_date", sa.Date, nullable=False),
    sa.Column("normalized_content", sa.JSON(none_as_null=True), nullable=False),
    sa.UniqueConstraint(
        "document_id", "content_sha256", "parser_version", name="uq_notice_version"
    ),
    sa.UniqueConstraint("document_id", "id", name="uq_version_document_id"),
    sa.CheckConstraint("length(content_sha256) = 64", name="ck_version_digest"),
    sa.CheckConstraint("length(parser_version) > 0", name="ck_version_parser"),
)
