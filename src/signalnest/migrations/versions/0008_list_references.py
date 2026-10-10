"""Register unadapted list targets and retain complete-scan response evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0008_list_references"
down_revision = "0007_history_search"
branch_labels = None
depends_on = None


def upgrade():
    # No existing table is rebuilt and no old response is reclassified. The new
    # nullable evidence column explicitly leaves previous scans without a ledger.
    op.create_table(
        "discovered_references",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("candidate_key", sa.Text, nullable=False),
        sa.Column("normalization_version", sa.Text, nullable=False),
        sa.Column("resolved_url", sa.Text, nullable=False),
        sa.Column("raw_href", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("published_date", sa.Date, nullable=False),
        sa.Column("reference_kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending_adapter"),
        sa.Column("first_seen_at", sa.Integer, nullable=False),
        sa.Column("last_seen_at", sa.Integer, nullable=False),
        sa.Column("discovery_origin", sa.Text, nullable=False),
        sa.Column("first_discovery_run_id", sa.Text, sa.ForeignKey("ingestion_runs.id")),
        sa.Column(
            "first_body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
        ),
        sa.Column(
            "first_observed_response_id",
            sa.Integer,
            sa.ForeignKey("raw_responses.id"),
            nullable=False,
        ),
        sa.Column("first_row_index", sa.Integer, nullable=False),
        sa.Column("first_parser_version", sa.Text, nullable=False),
        sa.Column("last_run_id", sa.Text, sa.ForeignKey("ingestion_runs.id")),
        sa.Column(
            "last_body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
        ),
        sa.Column(
            "last_observed_response_id",
            sa.Integer,
            sa.ForeignKey("raw_responses.id"),
            nullable=False,
        ),
        sa.Column("last_row_index", sa.Integer, nullable=False),
        sa.Column("last_parser_version", sa.Text, nullable=False),
        sa.UniqueConstraint("source_id", "candidate_key", name="uq_discovered_reference"),
        sa.CheckConstraint(
            "length(candidate_key) = 72 AND substr(candidate_key,1,8) = 'link:v1:'",
            name="ck_reference_key",
        ),
        sa.CheckConstraint("status = 'pending_adapter'", name="ck_reference_status"),
        sa.CheckConstraint(
            "reference_kind IN ('external','unsupported_column','unsupported_route')",
            name="ck_reference_kind",
        ),
        sa.CheckConstraint(
            "discovery_origin IN ('unknown','bootstrap','regular','historical')",
            name="ck_reference_origin",
        ),
        sa.CheckConstraint(
            "first_seen_at >= 0 AND last_seen_at >= first_seen_at "
            "AND first_row_index >= 0 AND last_row_index >= 0",
            name="ck_reference_observation",
        ),
    )
    op.add_column("ingestion_runs", sa.Column("coverage_evidence", sa.JSON(none_as_null=True)))


def downgrade():
    op.drop_column("ingestion_runs", "coverage_evidence")
    op.drop_table("discovered_references")
