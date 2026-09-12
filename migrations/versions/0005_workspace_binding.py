"""Persist workspace ownership independently of connection URL spelling."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("workspace_binding",
        sa.Column("singleton", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("workspace_name", sa.String(200), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_workspace_binding_singleton"),
        sa.CheckConstraint("length(workspace_name) BETWEEN 1 AND 200", name="ck_workspace_binding_name_length"))


def downgrade():
    op.drop_table("workspace_binding")
