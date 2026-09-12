"""Add fixed-revision entity query projections; original artifacts remain unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("query_projection",
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("identity_revision.revision_id"), primary_key=True),
        sa.Column("projection_version", sa.String(40), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("entity_count", sa.Integer(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("search_value_count", sa.Integer(), nullable=False))
    op.create_table("query_entity",
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("identity_revision.revision_id"), primary_key=True),
        sa.Column("entity_id", sa.String(36), primary_key=True),
        sa.Column("canonical", sa.JSON(), nullable=False))
    op.create_table("query_search_value",
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column("entity_id", sa.String(36), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("value_folded", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["revision_id", "entity_id"], ["query_entity.revision_id", "query_entity.entity_id"]))
    op.create_index("ix_membership_revision_entity_record", "entity_membership",
                    ["revision_id", "entity_id", "record_id"])


def downgrade():
    op.drop_index("ix_membership_revision_entity_record", table_name="entity_membership")
    op.drop_table("query_search_value")
    op.drop_table("query_entity")
    op.drop_table("query_projection")
