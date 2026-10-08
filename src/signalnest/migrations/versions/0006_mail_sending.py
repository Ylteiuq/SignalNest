"""Add delivery attempts and bounded maintenance, retaining frozen N2 records."""

import sqlalchemy as sa
from alembic import op

revision = "0006_mail_sending"
down_revision = "0005_mail_planning"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("notification_channel_state", sa.Column("pause_reason", sa.Text))
    op.add_column("notification_channel_state", sa.Column("paused_at", sa.Integer))
    op.create_table(
        "mail_delivery",
        sa.Column("mail_id", sa.Integer, sa.ForeignKey("mail_messages.id"), primary_key=True),
        sa.Column("state", sa.Text, nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("manual_retry_pending", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("next_attempt_at", sa.Integer),
        sa.Column("accepted_at", sa.Integer),
        sa.Column("updated_at", sa.Integer, nullable=False),
        sa.Column("blocked_reason", sa.Text),
        sa.CheckConstraint(
            "state IN ('pending','sending','retry','uncertain','accepted','blocked')",
            name="ck_delivery_state",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_delivery_attempt_count"),
        sa.CheckConstraint(
            "(state IN ('pending','retry','uncertain') AND next_attempt_at IS NOT NULL) "
            "OR (state IN ('sending','accepted','blocked') AND next_attempt_at IS NULL)",
            name="ck_delivery_due",
        ),
        sa.CheckConstraint(
            "(state = 'accepted') = (accepted_at IS NOT NULL)", name="ck_delivery_accepted"
        ),
        sa.CheckConstraint(
            "(state = 'blocked' AND blocked_reason IS NOT NULL AND blocked_reason IN "
            "('permanent','retry_exhausted','manual_attempt_failed','frozen_corrupt')) "
            "OR (state != 'blocked' AND blocked_reason IS NULL)",
            name="ck_delivery_blocked",
        ),
    )

    op.create_table(
        "mail_attempts",
        sa.Column("mail_id", sa.Integer, sa.ForeignKey("mail_delivery.mail_id"), primary_key=True),
        sa.Column("attempt_no", sa.Integer, primary_key=True),
        sa.Column("started_at", sa.Integer, nullable=False),
        sa.Column("finished_at", sa.Integer),
        sa.Column("outcome", sa.Text),
        sa.Column("stage", sa.Text),
        sa.Column("error_code", sa.Text),
        sa.Column("smtp_code", sa.Integer),
        sa.Column("scope", sa.Text),
        sa.Column("uncertain_until", sa.Integer),
        sa.Column("cleanup_failed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("manual", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("recovered", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint("attempt_no > 0", name="ck_mail_attempt_number"),
        sa.CheckConstraint(
            "(finished_at IS NULL AND outcome IS NULL AND stage IS NULL "
            "AND error_code IS NULL AND smtp_code IS NULL AND scope IS NULL) OR "
            "(finished_at IS NOT NULL AND outcome IS NOT NULL AND stage IS NOT NULL "
            "AND scope IS NOT NULL AND finished_at >= started_at AND outcome IN "
            "('accepted','retryable','uncertain','permanent') AND stage IN "
            "('config','connect','hello','tls','auth','mail','rcpt',"
            "'body_or_final','accepted','unknown') "
            "AND scope IN ('message','channel'))",
            name="ck_mail_attempt_result",
        ),
        sa.CheckConstraint(
            "smtp_code IS NULL OR smtp_code BETWEEN 100 AND 599", name="ck_mail_smtp_code"
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR (outcome = 'accepted' AND stage = 'accepted' "
            "AND smtp_code IS NOT NULL AND smtp_code = 250 AND error_code IS NULL) OR "
            "(outcome != 'accepted' AND stage != 'accepted' AND error_code IS NOT NULL)",
            name="ck_mail_acceptance_evidence",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome != 'uncertain' OR "
            "(stage IN ('body_or_final','unknown') AND uncertain_until IS NOT NULL "
            "AND uncertain_until >= finished_at + 1800)",
            name="ck_mail_uncertain_stage",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN "
            "('invalid_frozen_mail','credentials_missing','credentials_invalid',"
            "'tls_verification_failed','tls_not_supported','tls_failed','auth_not_supported',"
            "'auth_mechanism_not_supported','authentication_rejected','server_rejected',"
            "'connection_failed','timeout','disconnected','protocol_error','process_interrupted')",
            name="ck_mail_attempt_error",
        ),
    )

    op.create_table(
        "notification_operations",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "installation_id",
            sa.Text,
            sa.ForeignKey("notification_channel_state.installation_id"),
            nullable=False,
        ),
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("parameters_sha256", sa.Text, nullable=False),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.Column("results", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("finished_at", sa.Integer),
        sa.CheckConstraint(
            "kind IN ('policy_update','reevaluate')", name="ck_notification_operation_kind"
        ),
        sa.CheckConstraint(
            "length(parameters_sha256) = 64", name="ck_notification_operation_digest"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at",
            name="ck_notification_operation_finished",
        ),
    )

    # Frozen mail already has a durable eligibility time. No mail is created or sent.
    op.execute(
        "INSERT INTO mail_delivery (mail_id,state,attempt_count,manual_retry_pending,"
        "next_attempt_at,updated_at) SELECT id,'pending',0,0,frozen_at,frozen_at "
        "FROM mail_messages"
    )


def downgrade():
    raise RuntimeError("N4 downgrade is unsupported: retain delivery and acceptance evidence")
