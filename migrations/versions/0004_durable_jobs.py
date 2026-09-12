"""Add durable matching jobs and append-only transition events."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("durable_job",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("parent_revision", sa.String(36), sa.ForeignKey("identity_revision.revision_id")),
        sa.Column("event_cutoff", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float()), sa.Column("lease_owner", sa.String(100)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("result_revision", sa.String(36), sa.ForeignKey("identity_revision.revision_id"), unique=True),
        sa.Column("last_error", sa.JSON()), sa.Column("progress", sa.JSON()),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')", name="ck_job_status"),
        sa.CheckConstraint("attempt >= 0 AND max_attempts >= 1 AND attempt <= max_attempts", name="ck_job_attempts"))
    op.create_index("ix_job_status_created", "durable_job", ["status", "created_at", "job_id"])
    op.create_index("ix_job_status_lease", "durable_job", ["status", "lease_until"])
    op.create_table("durable_job_event",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("durable_job.job_id"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False))
    op.create_index("ix_job_event_job_seq", "durable_job_event", ["job_id", "seq"])


def downgrade():
    op.drop_table("durable_job_event")
    op.drop_table("durable_job")
