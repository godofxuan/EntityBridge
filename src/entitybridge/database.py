"""Verified startup migrations, packaged with the application wheel.

Version 0001 was released without Alembic stamps. Version 0002 permits repeated
source observations; 0003 adds query projections; 0004 adds durable jobs.
Version 0005 binds a database to its administrator-selected workspace name.
Version 0006 adds durable human review receipts and fenced recovery.
Do not stamp arbitrary pre-existing schemas: compare their structure first.
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

CURRENT_SCHEMA = "0006"
PROJECTION_TABLES = {"query_projection", "query_entity", "query_search_value"}
JOB_TABLES = {"durable_job", "durable_job_event"}
version_table = Table("alembic_version", MetaData(), Column("version_num", String(32), primary_key=True))


def _matches(connection, expected):
    return compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), expected) == []


def _v5_metadata():
    legacy = MetaData()
    for table in metadata.sorted_tables:
        if table.name != "review_operation":
            table.to_metadata(legacy)
    return legacy


def _v4_metadata():
    legacy = MetaData()
    for table in _v5_metadata().sorted_tables:
        if table.name != "workspace_binding":
            table.to_metadata(legacy)
    return legacy


def _v3_metadata():
    legacy = MetaData()
    for table in _v4_metadata().sorted_tables:
        if table.name not in JOB_TABLES:
            table.to_metadata(legacy)
    return legacy


def _v2_metadata():
    legacy = MetaData()
    for table in _v3_metadata().sorted_tables:
        if table.name not in PROJECTION_TABLES:
            table.to_metadata(legacy)
    memberships = legacy.tables["entity_membership"]
    memberships.indexes.remove(next(index for index in memberships.indexes
                                    if index.name == "ix_membership_revision_entity_record"))
    return legacy


def _v1_metadata():
    # Transform only the declared version differences. Unrelated schema drift
    # must fail comparison instead of being silently stamped as a known schema.
    legacy = _v2_metadata()
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


def _upgrade_v2(connection):
    for table in metadata.sorted_tables:
        if table.name in PROJECTION_TABLES:
            table.create(connection)
    next(index for index in metadata.tables["entity_membership"].indexes
         if index.name == "ix_membership_revision_entity_record").create(connection)


def _upgrade_v3(connection):
    for table in metadata.sorted_tables:
        if table.name in JOB_TABLES:
            table.create(connection)


def _upgrade_v4(connection):
    metadata.tables["workspace_binding"].create(connection)


def _upgrade_v5(connection):
    metadata.tables["review_operation"].create(connection)


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
        if stamped not in {None, "0001", "0002", "0003", "0004", "0005", CURRENT_SCHEMA}:
            raise RuntimeError("Unsupported database schema version; upgrade the application first")
        if not tables - {"alembic_version"}:
            if stamped:
                raise RuntimeError("Schema stamp exists without application tables; refusing to recreate history")
            metadata.create_all(connection)
        elif _matches(connection, metadata):
            if stamped in {"0001", "0002", "0003", "0004", "0005"}:
                raise RuntimeError("Schema and version stamp disagree; inspect the database before migration")
        elif stamped in {None, "0005"} and _matches(connection, _v5_metadata()):
            _upgrade_v5(connection)
        elif stamped in {None, "0004"} and _matches(connection, _v4_metadata()):
            _upgrade_v4(connection)
            _upgrade_v5(connection)
        elif stamped in {None, "0003"} and _matches(connection, _v3_metadata()):
            _upgrade_v3(connection)
            _upgrade_v4(connection)
            _upgrade_v5(connection)
        elif stamped in {None, "0002"} and _matches(connection, _v2_metadata()):
            _upgrade_v2(connection)
            _upgrade_v3(connection)
            _upgrade_v4(connection)
            _upgrade_v5(connection)
        elif stamped in {None, "0001"} and _matches(connection, _v1_metadata()):
            _upgrade_v1(connection)
            _upgrade_v2(connection)
            _upgrade_v3(connection)
            _upgrade_v4(connection)
            _upgrade_v5(connection)
        else:
            raise RuntimeError("Unrecognized database structure; refusing to stamp or migrate it automatically")
        if not _matches(connection, metadata):
            raise RuntimeError("Migration did not produce the expected schema")
        version_table.create(connection, checkfirst=True)
        if stamped:
            connection.execute(update(version_table).values(version_num=CURRENT_SCHEMA))
        else:
            connection.execute(insert(version_table).values(version_num=CURRENT_SCHEMA))
