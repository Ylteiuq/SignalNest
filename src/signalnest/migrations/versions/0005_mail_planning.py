"""Add frozen local mail and unique membership without rebuilding N1 tables."""

import sqlalchemy as sa
from alembic import op

revision = "0005_mail_planning"
down_revision = "0004_notification_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mail_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "installation_id",
            sa.Text,
            sa.ForeignKey("notification_channel_state.installation_id"),
            nullable=False,
        ),
        sa.Column("immediate_intent_id", sa.Integer, sa.ForeignKey("email_outbox.id"), unique=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("delivery_key", sa.Text, nullable=False, unique=True),
        sa.Column("digest_slot", sa.Integer),
        sa.Column("part", sa.Integer, nullable=False),
        sa.Column("sender", sa.Text, nullable=False),
        sa.Column("recipient", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("message_id", sa.Text, nullable=False, unique=True),
        sa.Column("date_at", sa.Integer, nullable=False),
        sa.Column("frozen_at", sa.Integer, nullable=False),
        sa.Column("rendering_version", sa.Text, nullable=False),
        sa.Column("payload_bytes", sa.LargeBinary, nullable=False),
        sa.Column("payload_sha256", sa.Text, nullable=False),
        sa.Column("members_sha256", sa.Text, nullable=False),
        sa.Column("max_bytes", sa.Integer, nullable=False),
        sa.Column("max_events", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.UniqueConstraint("installation_id", "digest_slot", "part", name="uq_digest_part"),
        sa.CheckConstraint(
            "(kind = 'immediate' AND immediate_intent_id IS NOT NULL "
            "AND digest_slot IS NULL AND part = 1) OR (kind = 'digest' "
            "AND immediate_intent_id IS NULL AND digest_slot IS NOT NULL AND part > 0)",
            name="ck_mail_kind",
        ),
        sa.CheckConstraint("state = 'pending'", name="ck_mail_pending_only"),
        sa.CheckConstraint(
            "length(payload_sha256) = 64 AND length(members_sha256) = 64", name="ck_mail_digests"
        ),
        sa.CheckConstraint(
            "length(payload_bytes) > 0 AND length(payload_bytes) <= max_bytes AND max_events > 0",
            name="ck_mail_payload_limit",
        ),
    )
    op.create_table(
        "mail_message_members",
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("notification_events.id"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("mail_id", sa.Integer, sa.ForeignKey("mail_messages.id"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("decision_id", sa.Integer, nullable=False),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id", "decision_id"],
            ["notification_decisions.event_id", "notification_decisions.id"],
            name="fk_mail_member_decision",
        ),
        sa.UniqueConstraint("mail_id", "position", name="uq_mail_member_position"),
        sa.CheckConstraint("position >= 0", name="ck_mail_member_position"),
    )
    op.create_table(
        "mail_plan_errors",
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("notification_events.id"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("decision_id", sa.Integer, nullable=False),
        sa.Column("rendering_version", sa.Text, nullable=False),
        sa.Column("max_bytes", sa.Integer, nullable=False),
        sa.Column("error_code", sa.Text, nullable=False),
        sa.Column("attempted_at", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id", "decision_id"],
            ["notification_decisions.event_id", "notification_decisions.id"],
            name="fk_mail_error_decision",
        ),
        sa.CheckConstraint(
            "error_code IN ('mail_item_too_large','render_input_invalid',"
            "'render_content_mismatch')",
            name="ck_mail_render_error",
        ),
    )


def downgrade():
    raise RuntimeError("0005 frozen mail cannot be downgraded automatically")
