"""Associate replayable response evidence with its page type and detail target.

Use SQLite ADD COLUMN with inline constraints: rebuilding this table would conflict
with existing notice_versions references while foreign keys are enabled.
Legacy rows retain NULL type/target rather than guessing from their URLs.
"""

from alembic import op

revision = "0002_response_target"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE raw_responses ADD COLUMN page_type TEXT "
        "CONSTRAINT ck_response_page_type CHECK "
        "(page_type IS NULL OR page_type IN ('list', 'notice'))"
    )
    op.execute(
        "ALTER TABLE raw_responses ADD COLUMN document_id INTEGER "
        "CONSTRAINT fk_response_document REFERENCES documents(id) "
        "CONSTRAINT ck_response_target CHECK "
        "((page_type IS NULL AND document_id IS NULL) OR "
        "(page_type IS NOT NULL AND ((page_type = 'list' AND document_id IS NULL) OR "
        "(page_type = 'notice' AND document_id IS NOT NULL))))"
    )
    op.execute("ALTER TABLE raw_responses ADD COLUMN last_attempt_at INTEGER")
    op.execute("ALTER TABLE raw_responses ADD COLUMN last_error_code TEXT")


def downgrade():
    # SQLite >= 3.35; no application downgrade command is exposed.
    op.execute("ALTER TABLE raw_responses DROP COLUMN document_id")
    op.execute("ALTER TABLE raw_responses DROP COLUMN page_type")
    op.execute("ALTER TABLE raw_responses DROP COLUMN last_error_code")
    op.execute("ALTER TABLE raw_responses DROP COLUMN last_attempt_at")
