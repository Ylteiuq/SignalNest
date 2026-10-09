"""Add a rebuildable, current-version lexical index; retain all historical facts."""

import sqlalchemy as sa
from alembic import op

revision = "0007_history_search"
down_revision = "0006_mail_sending"
branch_labels = None
depends_on = None


def upgrade():
    # Creating the actual virtual table also checks FTS5/trigram availability.
    # The enclosing storage-init transaction rolls back every DDL on failure.
    op.create_table(
        "search_documents",
        sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), primary_key=True),
        sa.Column("version_id", sa.Integer, nullable=False),
        sa.Column("title_text", sa.Text, nullable=False),
        sa.Column("body_text", sa.Text, nullable=False),
        sa.Column("index_version", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "version_id"],
            ["notice_versions.document_id", "notice_versions.id"],
            name="fk_search_current_version",
        ),
        sa.CheckConstraint("length(index_version) > 0", name="ck_search_index_version"),
    )
    op.execute(
        "CREATE VIRTUAL TABLE search_fts USING fts5("
        "title_text,body_text,content='search_documents',content_rowid='document_id',"
        "tokenize='trigram')"
    )
    op.execute(
        "CREATE TRIGGER search_documents_insert AFTER INSERT ON search_documents BEGIN "
        "INSERT INTO search_fts(rowid,title_text,body_text) "
        "VALUES (new.document_id,new.title_text,new.body_text); END"
    )
    op.execute(
        "CREATE TRIGGER search_documents_delete AFTER DELETE ON search_documents BEGIN "
        "INSERT INTO search_fts(search_fts,rowid,title_text,body_text) "
        "VALUES ('delete',old.document_id,old.title_text,old.body_text); END"
    )
    op.execute(
        "CREATE TRIGGER search_documents_update AFTER UPDATE ON search_documents BEGIN "
        "INSERT INTO search_fts(search_fts,rowid,title_text,body_text) "
        "VALUES ('delete',old.document_id,old.title_text,old.body_text); "
        "INSERT INTO search_fts(rowid,title_text,body_text) "
        "VALUES (new.document_id,new.title_text,new.body_text); END"
    )
    # Existing successful versions remain authoritative. A deliberate search-rebuild
    # materializes their normalized JSON without fetching or reparsing old HTML.


def downgrade():
    op.execute("DROP TRIGGER search_documents_update")
    op.execute("DROP TRIGGER search_documents_delete")
    op.execute("DROP TRIGGER search_documents_insert")
    op.execute("DROP TABLE search_fts")
    op.drop_table("search_documents")
