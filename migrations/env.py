"""Use ENTITYBRIDGE_DATABASE_URL or an injected connection; never log credentials."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from entitybridge.schema import metadata

config = context.config


def run(connection):
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=os.environ.get("ENTITYBRIDGE_DATABASE_URL") or config.get_main_option("sqlalchemy.url"),
                      target_metadata=metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    engine = create_engine(os.environ.get("ENTITYBRIDGE_DATABASE_URL") or config.get_main_option("sqlalchemy.url"),
                           poolclass=pool.NullPool)
    with engine.connect() as connection:
        run(connection)
