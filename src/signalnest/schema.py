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
    sa.Column("discovery_origin", sa.Text, nullable=False, server_default="unknown"),
    sa.Column("first_discovery_run_id", sa.Text, sa.ForeignKey("ingestion_runs.id")),
    sa.CheckConstraint(
        "discovery_origin IN ('unknown', 'bootstrap', 'regular', 'historical')",
        name="ck_document_origin",
    ),
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
    # NULL is reserved for responses written before the offline ingestion migration.
    sa.Column("page_type", sa.Text),
    sa.Column("document_id", sa.ForeignKey("documents.id", name="fk_response_document")),
    sa.Column("last_attempt_at", sa.Integer),
    sa.Column("last_error_code", sa.Text),
    sa.Column("body_state", sa.Text, nullable=False, server_default="unknown"),
    sa.Column("resource_id", sa.Integer, sa.ForeignKey("http_resources.id")),
    sa.Column("validated_response_id", sa.Integer, sa.ForeignKey("raw_responses.id")),
    sa.Column("vary", sa.Text),
    sa.Column("cache_control", sa.Text),
    sa.Column("content_encoding", sa.Text),
    sa.CheckConstraint(
        "body_state IN ('unknown', 'complete', 'unavailable') AND "
        "(body_state != 'complete' OR (body_path IS NOT NULL AND status_code != 304)) AND "
        "(body_state != 'unavailable' OR body_path IS NULL)",
        name="ck_response_body_state",
    ),
    sa.CheckConstraint(
        "validated_response_id IS NULL OR "
        "(status_code = 304 AND resource_id IS NOT NULL AND validated_response_id != id)",
        name="ck_response_validation_binding",
    ),
    sa.CheckConstraint(
        "page_type IS NULL OR page_type IN ('list', 'notice')", name="ck_response_page_type"
    ),
    sa.CheckConstraint(
        "(page_type IS NULL AND document_id IS NULL) OR "
        "(page_type IS NOT NULL AND ((page_type = 'list' AND document_id IS NULL) OR "
        "(page_type = 'notice' AND document_id IS NOT NULL)))",
        name="ck_response_target",
    ),
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

source_ingestion_state = sa.Table(
    "source_ingestion_state",
    metadata,
    sa.Column("source_id", sa.Text, primary_key=True),
    sa.Column("last_list_attempt_at", sa.Integer),
    sa.Column("last_list_response_at", sa.Integer),
    sa.Column("last_list_registered_at", sa.Integer),
    sa.Column("last_complete_scan_at", sa.Integer),
    sa.Column(
        "last_complete_scan_run_id",
        sa.Text,
        sa.ForeignKey("ingestion_runs.id", name="fk_source_complete_run", use_alter=True),
    ),
    sa.Column("bootstrap_completed_at", sa.Integer),
    sa.Column("not_before_at", sa.Integer),
    sa.CheckConstraint(
        "(last_complete_scan_at IS NULL) = (last_complete_scan_run_id IS NULL)",
        name="ck_source_scan_reference",
    ),
)

ingestion_runs = sa.Table(
    "ingestion_runs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column(
        "source_id", sa.Text, sa.ForeignKey("source_ingestion_state.source_id"), nullable=False
    ),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("parser_version", sa.Text, nullable=False),
    sa.Column("started_at", sa.Integer, nullable=False),
    sa.Column("finished_at", sa.Integer),
    sa.Column("result", sa.Text, nullable=False, server_default="running"),
    sa.Column("coverage", sa.Text, nullable=False, server_default="pending"),
    sa.Column("coverage_at", sa.Integer),
    sa.Column("coverage_error_code", sa.Text),
    sa.Column("error_code", sa.Text),
    sa.CheckConstraint("origin IN ('bootstrap', 'regular', 'historical')", name="ck_run_origin"),
    sa.CheckConstraint(
        "result IN ('running','succeeded','partial_failure','failed','interrupted')",
        name="ck_run_result",
    ),
    sa.CheckConstraint(
        "coverage IN ('pending','complete','limited','interrupted')", name="ck_run_coverage"
    ),
    sa.CheckConstraint(
        "(result = 'running' AND finished_at IS NULL) OR "
        "(result != 'running' AND finished_at IS NOT NULL AND finished_at >= started_at)",
        name="ck_run_finished",
    ),
    sa.CheckConstraint(
        "(coverage = 'pending' AND coverage_at IS NULL) OR "
        "(coverage != 'pending' AND coverage_at IS NOT NULL AND coverage_at >= started_at)",
        name="ck_run_coverage_time",
    ),
)

http_resources = sa.Table(
    "http_resources",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("request_uri", sa.Text, nullable=False),
    sa.Column("profile_sha256", sa.Text, nullable=False),
    sa.Column("request_profile", sa.JSON(none_as_null=True), nullable=False),
    sa.Column(
        "blocked_by_response_id",
        sa.Integer,
        sa.ForeignKey("raw_responses.id", name="fk_resource_blocked_response", use_alter=True),
    ),
    sa.Column(
        "latest_response_id",
        sa.Integer,
        sa.ForeignKey("raw_responses.id", name="fk_resource_latest_response", use_alter=True),
    ),
    sa.Column(
        "last_processed_response_id",
        sa.Integer,
        sa.ForeignKey("raw_responses.id", name="fk_resource_processed_response", use_alter=True),
    ),
    sa.Column("last_processed_parser_version", sa.Text),
    sa.Column("last_processed_at", sa.Integer),
    sa.UniqueConstraint("source_id", "request_uri", "profile_sha256", name="uq_http_resource"),
    sa.CheckConstraint("length(profile_sha256) = 64", name="ck_resource_profile"),
    sa.CheckConstraint(
        "(last_processed_response_id IS NULL AND last_processed_parser_version IS NULL "
        "AND last_processed_at IS NULL) OR (last_processed_response_id IS NOT NULL "
        "AND last_processed_parser_version IS NOT NULL AND last_processed_at IS NOT NULL)",
        name="ck_resource_processed_reference",
    ),
)
