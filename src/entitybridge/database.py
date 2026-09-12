"""Verified startup migrations, packaged with the application wheel.

Version 0001 was released without Alembic stamps. Its only structural change in
0002 is replacing the source-content uniqueness constraint with an index. Do
not stamp an arbitrary pre-existing schema: compare every table first.
"""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    insert,
    inspect,
    select,
    text,
    update,
)

from .schema import metadata

CURRENT_SCHEMA = "0002"
version_table = Table("alembic_version", MetaData(), Column("version_num", String(32), primary_key=True))


def _matches(connection, expected):
    return compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), expected) == []


def _v1_metadata():
    # This transformation describes the entire 0001 -> 0002 difference. Any
    # unrelated schema drift still fails comparison instead of being stamped.
    legacy = MetaData()
    for table in metadata.sorted_tables:
        table.to_metadata(legacy)
    snapshots = legacy.tables["source_snapshot"]
    snapshots.indexes.remove(next(index for index in snapshots.indexes
                                 if index.name == "ix_source_snapshot_source_content"))
    snapshots.append_constraint(UniqueConstraint("source_id", "content_sha256"))
    return legacy


def _upgrade_v1(connection):
    operations = Operations(MigrationContext.configure(connection))
    unique = next(item for item in inspect(connection).get_unique_constraints("source_snapshot")
                  if set(item["column_names"]) == {"source_id", "content_sha256"})
    with operations.batch_alter_table("source_snapshot", naming_convention={
        "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s"
    }) as batch:
        batch.drop_constraint(unique["name"] or "uq_source_snapshot_source_id_content_sha256", type_="unique")
        batch.create_index("ix_source_snapshot_source_content", ["source_id", "content_sha256"])


def initialize_database(engine):
    """Create an empty DB or upgrade only an exactly verified released schema."""
    with engine.begin() as connection:
        # Serialize first startup across processes before a workspace row exists.
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(74798291502612)"))
        elif connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise ValueError("EntityBridge supports SQLite and PostgreSQL databases")
        tables = set(inspect(connection).get_table_names())
        stamped = None
        if "alembic_version" in tables:
            stamps = connection.execute(select(version_table.c.version_num)).scalars().all()
            if len(stamps) > 1:
                raise RuntimeError("Multiple schema heads found; run an explicit reviewed migration")
            stamped = stamps[0] if stamps else None
        if stamped not in {None, "0001", CURRENT_SCHEMA}:
            raise RuntimeError("Unsupported database schema version; upgrade the application first")
        if not tables - {"alembic_version"}:
            if stamped:
                raise RuntimeError("Schema stamp exists without application tables; refusing to recreate history")
            metadata.create_all(connection)
        elif _matches(connection, metadata):
            if stamped == "0001":
                raise RuntimeError("Schema and version stamp disagree; inspect the database before migration")
        elif stamped != CURRENT_SCHEMA and _matches(connection, _v1_metadata()):
            _upgrade_v1(connection)
            if not _matches(connection, metadata):
                raise RuntimeError("Migration did not produce the expected schema")
        else:
            raise RuntimeError("Unrecognized database structure; refusing to stamp or migrate it automatically")
        version_table.create(connection, checkfirst=True)
        if stamped:
            connection.execute(update(version_table).values(version_num=CURRENT_SCHEMA))
        else:
            connection.execute(insert(version_table).values(version_num=CURRENT_SCHEMA))
