"""Add exact-URI cache evidence and small source/run facts without rebuilding old tables.

SQLite ADD COLUMN preserves the existing version/current-version foreign-key graph.
Legacy response completeness/profile and discovery origin stay explicitly unknown.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_ingestion_state"
down_revision = "0002_response_target"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_ingestion_state",
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
    op.create_table(
        "ingestion_runs",
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
        sa.CheckConstraint(
            "origin IN ('bootstrap', 'regular', 'historical')", name="ck_run_origin"
        ),
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
    op.create_table(
        "http_resources",
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
            sa.ForeignKey(
                "raw_responses.id", name="fk_resource_processed_response", use_alter=True
            ),
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
    op.execute(
        "ALTER TABLE documents ADD COLUMN discovery_origin TEXT NOT NULL DEFAULT 'unknown' "
        "CONSTRAINT ck_document_origin CHECK "
        "(discovery_origin IN ('unknown','bootstrap','regular','historical'))"
    )
    op.execute(
        "ALTER TABLE documents ADD COLUMN first_discovery_run_id TEXT REFERENCES ingestion_runs(id)"
    )
    op.execute(
        "ALTER TABLE raw_responses ADD COLUMN body_state TEXT NOT NULL DEFAULT 'unknown' "
        "CONSTRAINT ck_response_body_state CHECK "
        "(body_state IN ('unknown','complete','unavailable') AND "
        "(body_state != 'complete' OR (body_path IS NOT NULL AND status_code != 304)) AND "
        "(body_state != 'unavailable' OR body_path IS NULL))"
    )
    op.execute(
        "ALTER TABLE raw_responses ADD COLUMN resource_id INTEGER REFERENCES http_resources(id)"
    )
    op.execute(
        "ALTER TABLE raw_responses ADD COLUMN validated_response_id INTEGER "
        "REFERENCES raw_responses(id) CONSTRAINT ck_response_validation_binding CHECK "
        "(validated_response_id IS NULL OR "
        "(status_code = 304 AND resource_id IS NOT NULL AND validated_response_id != id))"
    )
    for name in ("vary", "cache_control", "content_encoding"):
        op.execute(f"ALTER TABLE raw_responses ADD COLUMN {name} TEXT")


def downgrade():
    # No application downgrade entry point. Reject data loss instead of dropping evidence.
    raise RuntimeError("0003 ingestion evidence cannot be downgraded automatically")
