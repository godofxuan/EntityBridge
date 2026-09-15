"""Persist human review receipts alongside their events."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("review_operation",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False), sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("reviewer", sa.String(100), nullable=False),
        sa.Column("decision_id", sa.String(36), sa.ForeignKey("review_decision.decision_id"), nullable=False),
        sa.Column("event_seq", sa.Integer(), sa.ForeignKey("decision_event.seq"), nullable=False, unique=True),
        sa.Column("base_revision", sa.String(36), sa.ForeignKey("identity_revision.revision_id"), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False), sa.Column("policy_version", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False), sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(36)), sa.Column("lease_until", sa.Float()),
        sa.Column("candidate_revision_id", sa.String(36), sa.ForeignKey("identity_revision.revision_id"), unique=True),
        sa.Column("safe_error_code", sa.String(40)), sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("kind IN ('decision','revoke')", name="ck_review_operation_kind"),
        sa.CheckConstraint("status IN ('accepted','building','prepared','build_failed','stale_basis')",
                           name="ck_review_operation_status"),
        sa.CheckConstraint("attempt >= 0", name="ck_review_operation_attempt"))
    op.create_index("ix_review_operation_created", "review_operation", ["created_at", "operation_id"])


def downgrade():
    op.drop_table("review_operation")
