"""Initial document identity, raw response references, and parsed versions.

This revision is frozen: future model changes require a new migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("source_document_id", sa.Text(), nullable=False),
        sa.Column("detail_url", sa.Text(), nullable=False),
        sa.Column("discovered_title", sa.Text(), nullable=False),
        sa.Column("discovered_at", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="discovered", nullable=False),
        sa.Column("current_version_id", sa.Integer(), nullable=True),
        sa.Column("last_attempt_at", sa.Integer(), nullable=True),
        sa.Column("last_success_at", sa.Integer(), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("next_due_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "(status = 'discovered' AND last_attempt_at IS NULL AND last_success_at IS "
            "NULL AND last_error_code IS NULL) OR (status = 'processed' AND "
            "last_attempt_at IS NOT NULL AND last_success_at IS NOT NULL AND "
            "last_error_code IS NULL) OR (status = 'failed' AND last_attempt_at IS NOT "
            "NULL AND last_error_code IS NOT NULL)",
            name="ck_document_processing_state",
        ),
        sa.CheckConstraint(
            "status IN ('discovered', 'processed', 'failed')", name="ck_document_status"
        ),
        sa.CheckConstraint(
            "(current_version_id IS NULL AND last_success_at IS NULL) OR "
            "(current_version_id IS NOT NULL AND last_success_at IS NOT NULL)",
            name="ck_document_success_reference",
        ),
        sa.CheckConstraint(
            "last_success_at IS NULL OR last_success_at <= last_attempt_at",
            name="ck_document_success_time",
        ),
        sa.ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["notice_versions.document_id", "notice_versions.id"],
            name="fk_document_current_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "source_document_id", name="uq_document_identity"),
    )
    op.create_index("ix_documents_next_due_at", "documents", ["next_due_at"], unique=False)
    op.create_table(
        "notice_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("raw_response_id", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("parsed_at", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("published_date", sa.Date(), nullable=False),
        sa.Column("normalized_content", sa.JSON(none_as_null=True), nullable=False),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_version_digest"),
        sa.CheckConstraint("length(parser_version) > 0", name="ck_version_parser"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
        ),
        sa.ForeignKeyConstraint(
            ["raw_response_id"],
            ["raw_responses.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "content_sha256", "parser_version", name="uq_notice_version"
        ),
        sa.UniqueConstraint("document_id", "id", name="uq_version_document_id"),
    )
    op.create_table(
        "raw_responses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.Integer(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("body_path", sa.Text(), nullable=True),
        sa.Column("body_sha256", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(body_path IS NULL AND body_sha256 IS NULL) OR (body_path IS NOT NULL AND "
            "body_sha256 IS NOT NULL AND length(body_sha256) = 64)",
            name="ck_response_body_reference",
        ),
        sa.CheckConstraint(
            "status_code != 304 OR body_path IS NULL", name="ck_response_304_has_no_body"
        ),
        sa.CheckConstraint("status_code BETWEEN 100 AND 599", name="ck_response_status"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    # Break the current-version reference before dropping its target table.
    op.execute(
        "UPDATE documents SET current_version_id = NULL, last_success_at = NULL, "
        "last_attempt_at = NULL, last_error_code = NULL, status = 'discovered'"
    )
    op.drop_table("notice_versions")
    op.drop_table("raw_responses")
    op.drop_index("ix_documents_next_due_at", table_name="documents")
    op.drop_table("documents")
