"""Alembic environment: use the application's configured, transactional connection."""

from alembic import context

from signalnest.schema import metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use signalnest storage-init --config <file>")

context.configure(connection=connection, target_metadata=metadata, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
