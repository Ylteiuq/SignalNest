"""Explicit notification activation and atomic event/decision registration.

Only new tables; existing evidence and unknown origins remain untouched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_notification_state"
down_revision = "0003_ingestion_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_policy_revisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("policy_sha256", sa.Text, nullable=False, unique=True),
        sa.Column("manifest", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.CheckConstraint("length(policy_sha256) = 64", name="ck_notification_policy_digest"),
    )
    op.create_table(
        "notification_channel_state",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("installation_id", sa.Text, nullable=False, unique=True),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("activation_id", sa.Text, nullable=False),
        sa.Column("activation_at", sa.Integer, nullable=False),
        sa.Column("parameters_sha256", sa.Text, nullable=False),
        sa.Column(
            "policy_revision_id",
            sa.Integer,
            sa.ForeignKey("notification_policy_revisions.id"),
            nullable=False,
        ),
        sa.Column("notification_mode", sa.Text, nullable=False),
        sa.Column("initial_recent_review", sa.Boolean, nullable=False),
        sa.Column("digest_hour", sa.Integer, nullable=False),
        sa.Column("digest_minute", sa.Integer, nullable=False),
        sa.Column("sender", sa.Text, nullable=False),
        sa.Column("recipient", sa.Text, nullable=False),
        sa.Column("paused", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint("id = 'primary'", name="ck_notification_single_channel"),
        sa.CheckConstraint(
            "notification_mode IN ('hybrid','digest_only')", name="ck_notification_mode"
        ),
        sa.CheckConstraint(
            "digest_hour BETWEEN 0 AND 23 AND digest_minute BETWEEN 0 AND 59",
            name="ck_notification_calendar",
        ),
    )
    op.create_table(
        "notification_listing_evidence",
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), primary_key=True),
        sa.Column("published_date", sa.Date, nullable=False),
        sa.Column("body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id")),
        sa.Column("observed_response_id", sa.Integer, sa.ForeignKey("raw_responses.id")),
        sa.Column("parser_version", sa.Text, nullable=False),
        sa.Column("registered_at", sa.Integer, nullable=False),
        sa.Column("processing_origin", sa.Text, nullable=False),
        sa.Column("live_discovered_run_id", sa.Text, sa.ForeignKey("ingestion_runs.id")),
        sa.Column("live_discovered_at", sa.Integer),
        sa.CheckConstraint(
            "processing_origin IN ('live','offline','maintenance')",
            name="ck_listing_processing_origin",
        ),
        sa.CheckConstraint(
            "(live_discovered_run_id IS NULL) = (live_discovered_at IS NULL)",
            name="ck_listing_live_discovery",
        ),
    )
    op.create_table(
        "notification_activation_members",
        sa.Column(
            "installation_id",
            sa.Text,
            sa.ForeignKey("notification_channel_state.installation_id"),
            primary_key=True,
        ),
        sa.Column("source_id", sa.Text, primary_key=True),
        sa.Column("source_document_id", sa.Text, primary_key=True),
        sa.Column("candidate_state", sa.Text, nullable=False),
        sa.Column("list_evidence", sa.JSON),
        sa.Column("notice_evidence", sa.JSON),
        sa.Column("selection_evidence", sa.JSON),
        sa.Column("conflict_evidence", sa.JSON),
        sa.Column("date_conflict", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "generated_event_id",
            sa.Integer,
            sa.ForeignKey("notification_events.id", use_alter=True),
        ),
        sa.CheckConstraint(
            "candidate_state IN ('unknown','selected','not_recent','disabled','generated')",
            name="ck_activation_candidate",
        ),
        sa.CheckConstraint(
            "(candidate_state = 'generated') = (generated_event_id IS NOT NULL)",
            name="ck_activation_generated",
        ),
    )
    op.create_table(
        "notification_observations",
        sa.Column(
            "installation_id",
            sa.Text,
            sa.ForeignKey("notification_channel_state.installation_id"),
            primary_key=True,
        ),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), primary_key=True),
        sa.Column("version_id", sa.Integer, nullable=False),
        sa.Column(
            "body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
        ),
        sa.Column(
            "observed_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
        ),
        sa.Column("observed_at", sa.Integer, nullable=False),
        sa.Column("event_seq", sa.Integer, nullable=False),
        sa.Column("comparison_error_code", sa.Text),
        sa.Column("comparison_error_at", sa.Integer),
        sa.Column("comparison_evidence", sa.JSON),
        sa.ForeignKeyConstraint(
            ["document_id", "version_id"],
            ["notice_versions.document_id", "notice_versions.id"],
            name="fk_observation_version",
        ),
        sa.CheckConstraint("event_seq >= 0", name="ck_observation_sequence"),
        sa.CheckConstraint(
            "(comparison_error_code IS NULL) = (comparison_error_at IS NULL)",
            name="ck_observation_comparison",
        ),
    )
    op.create_table(
        "notification_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "installation_id",
            sa.Text,
            sa.ForeignKey("notification_channel_state.installation_id"),
            nullable=False,
        ),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("event_seq", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("previous_version_id", sa.Integer),
        sa.Column("version_id", sa.Integer, nullable=False),
        sa.Column("previous_body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id")),
        sa.Column(
            "body_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
        ),
        sa.Column(
            "observed_response_id", sa.Integer, sa.ForeignKey("raw_responses.id"), nullable=False
        ),
        sa.Column("occurred_at", sa.Integer, nullable=False),
        sa.Column("selected_decision_id", sa.Integer),
        sa.Column("effective_route", sa.Text, nullable=False, server_default="none"),
        sa.Column("delivery_intent_registered_at", sa.Integer),
        sa.Column("outbox_id", sa.Integer),
        sa.UniqueConstraint(
            "installation_id", "document_id", "event_seq", name="uq_notification_event_sequence"
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "version_id"],
            ["notice_versions.document_id", "notice_versions.id"],
            name="fk_event_version",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "previous_version_id"],
            ["notice_versions.document_id", "notice_versions.id"],
            name="fk_event_previous_version",
        ),
        sa.ForeignKeyConstraint(
            ["id", "selected_decision_id"],
            ["notification_decisions.event_id", "notification_decisions.id"],
            name="fk_event_selected_decision",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["id", "outbox_id"],
            ["email_outbox.event_id", "email_outbox.id"],
            name="fk_event_outbox",
            use_alter=True,
        ),
        sa.CheckConstraint("event_seq > 0", name="ck_event_sequence"),
        sa.CheckConstraint("kind IN ('new','update','activation_recent')", name="ck_event_kind"),
        sa.CheckConstraint(
            "effective_route IN ('none','immediate','digest')", name="ck_event_route"
        ),
        sa.CheckConstraint(
            "(effective_route = 'none') = (delivery_intent_registered_at IS NULL)",
            name="ck_event_eligibility",
        ),
        sa.CheckConstraint(
            "outbox_id IS NULL OR effective_route = 'immediate'", name="ck_event_outbox_route"
        ),
    )
    op.create_table(
        "notification_decisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("event_id", sa.Integer, sa.ForeignKey("notification_events.id"), nullable=False),
        sa.Column(
            "policy_revision_id",
            sa.Integer,
            sa.ForeignKey("notification_policy_revisions.id"),
            nullable=False,
        ),
        sa.Column("evaluation_key", sa.Text, nullable=False),
        sa.Column("evaluated_at", sa.Integer, nullable=False),
        sa.Column("decision", sa.JSON, nullable=False),
        sa.Column("facts", sa.JSON, nullable=False),
        sa.Column("context", sa.JSON, nullable=False),
        sa.UniqueConstraint(
            "event_id", "policy_revision_id", "evaluation_key", name="uq_event_evaluation"
        ),
        sa.UniqueConstraint("event_id", "id", name="uq_decision_event_id"),
    )
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("notification_events.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision_id", sa.Integer, nullable=False),
        sa.Column("delivery_key", sa.Text, nullable=False, unique=True),
        sa.Column("recipient_key", sa.Text, nullable=False, server_default="primary"),
        sa.Column("kind", sa.Text, nullable=False, server_default="immediate"),
        sa.Column("state", sa.Text, nullable=False, server_default="planned"),
        sa.Column("sender", sa.Text, nullable=False),
        sa.Column("recipient", sa.Text, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.UniqueConstraint("event_id", "id", name="uq_outbox_event_id"),
        sa.ForeignKeyConstraint(
            ["event_id", "decision_id"],
            ["notification_decisions.event_id", "notification_decisions.id"],
            name="fk_outbox_decision",
        ),
        sa.CheckConstraint(
            "state = 'planned' AND kind = 'immediate' AND recipient_key = 'primary'",
            name="ck_outbox_planned_only",
        ),
    )


def downgrade():
    raise RuntimeError("0004 notification evidence cannot be downgraded automatically")
