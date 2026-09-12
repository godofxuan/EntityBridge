"""Frozen initial schema for source provenance, decisions and identity publication.

This revision deliberately does not import application metadata or call create_all.
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("source",
        sa.Column("source_id", sa.String(80), primary_key=True),
        sa.Column("terms_url", sa.Text()),
        sa.Column("schema_version", sa.String(40), nullable=False))
    op.create_table("source_snapshot",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(80), sa.ForeignKey("source.source_id"), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source_id", "content_sha256"))
    op.create_table("source_record",
        sa.Column("record_id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(80), sa.ForeignKey("source.source_id"), nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("current_version", sa.String(36), nullable=False),
        sa.UniqueConstraint("source_id", "source_key"))
    op.create_table("record_version",
        sa.Column("record_version_id", sa.String(36), primary_key=True),
        sa.Column("record_id", sa.String(36), sa.ForeignKey("source_record.record_id"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("source_snapshot.snapshot_id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("tombstone", sa.Boolean(), nullable=False))
    op.create_table("review_decision",
        sa.Column("decision_id", sa.String(36), primary_key=True),
        sa.Column("left_id", sa.String(36), sa.ForeignKey("source_record.record_id"), nullable=False),
        sa.Column("right_id", sa.String(36), sa.ForeignKey("source_record.record_id"), nullable=False),
        sa.Column("left_version", sa.String(36), nullable=False),
        sa.Column("right_version", sa.String(36), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.String(100), nullable=False),
        sa.Column("base_revision", sa.String(36)),
        sa.Column("policy_version", sa.String(200), nullable=False))
    op.create_table("decision_event",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("decision_id", sa.String(36), sa.ForeignKey("review_decision.decision_id"), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("policy_version", sa.String(200)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.String(100), nullable=False))
    op.create_table("match_run",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("pipeline_version", sa.String(200), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False))
    op.create_table("scored_edge",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("match_run.run_id"), primary_key=True),
        sa.Column("left_version", sa.String(36), primary_key=True),
        sa.Column("right_version", sa.String(36), primary_key=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False))
    op.create_table("identity_revision",
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column("parent_revision", sa.String(36)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("event_cutoff", sa.Integer(), nullable=False),
        sa.Column("constraint_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(200), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("published_at", sa.String(40)))
    op.create_table("entity_membership",
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("identity_revision.revision_id"), primary_key=True),
        sa.Column("record_id", sa.String(36), sa.ForeignKey("source_record.record_id"), primary_key=True),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("record_version_id", sa.String(36), sa.ForeignKey("record_version.record_version_id"), nullable=False))
    op.create_index("ix_entity_membership_entity_id", "entity_membership", ["entity_id"])
    op.create_table("entity_lineage",
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("identity_revision.revision_id"), primary_key=True),
        sa.Column("from_entity", sa.String(36), primary_key=True),
        sa.Column("to_entity", sa.String(36), primary_key=True),
        sa.Column("relation", sa.String(20), primary_key=True))
    op.create_table("current_revision",
        sa.Column("workspace", sa.String(40), primary_key=True),
        sa.Column("revision_id", sa.String(36)),
        sa.Column("generation", sa.Integer(), nullable=False))


def downgrade():
    for table in ("current_revision", "entity_lineage", "entity_membership", "identity_revision", "scored_edge",
                  "match_run", "decision_event", "review_decision", "record_version", "source_record",
                  "source_snapshot", "source"):
        op.drop_table(table)
