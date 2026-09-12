"""Permit repeated source content after a change without losing observation history."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    constraint = next(item for item in sa.inspect(connection).get_unique_constraints("source_snapshot")
                      if set(item["column_names"]) == {"source_id", "content_sha256"})
    # SQLite's initial unnamed constraint receives this name during reflection;
    # PostgreSQL preserves the server-assigned name returned by its inspector.
    name = constraint["name"] or "uq_source_snapshot_source_id_content_sha256"
    with op.batch_alter_table("source_snapshot", naming_convention={
        "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s"
    }) as batch:
        batch.drop_constraint(name, type_="unique")
        batch.create_index("ix_source_snapshot_source_content", ["source_id", "content_sha256"])


def downgrade():
    connection = op.get_bind()
    duplicates = connection.execute(sa.text("SELECT 1 FROM source_snapshot GROUP BY source_id, "
                                           "content_sha256 HAVING COUNT(*) > 1 LIMIT 1")).first()
    if duplicates:
        raise RuntimeError("Cannot downgrade: repeated source observations exist; preserve their history")
    with op.batch_alter_table("source_snapshot") as batch:
        batch.drop_index("ix_source_snapshot_source_content")
        batch.create_unique_constraint("uq_source_snapshot_source_id_content_sha256", ["source_id", "content_sha256"])
