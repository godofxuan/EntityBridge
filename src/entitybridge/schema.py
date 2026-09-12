"""Relational state; immutable large run payloads live outside the database."""

from sqlalchemy import (
                        JSON,
                        Boolean,
                        Column,
                        Float,
                        ForeignKey,
                        Index,
                        Integer,
                        MetaData,
                        String,
                        Table,
                        Text,
                        UniqueConstraint,
)

metadata = MetaData()
sources = Table("source", metadata, Column("source_id", String(80), primary_key=True),
                Column("terms_url", Text), Column("schema_version", String(40), nullable=False))
snapshots = Table("source_snapshot", metadata,
                  Column("snapshot_id", String(36), primary_key=True),
                  Column("source_id", ForeignKey("source.source_id"), nullable=False),
                  Column("content_sha256", String(64), nullable=False), Column("manifest", JSON, nullable=False),
                  Column("result", JSON, nullable=False))
Index("ix_source_snapshot_source_content", snapshots.c.source_id, snapshots.c.content_sha256)
records = Table("source_record", metadata,
                Column("record_id", String(36), primary_key=True),
                Column("source_id", ForeignKey("source.source_id"), nullable=False),
                Column("source_key", String(200), nullable=False),
                Column("current_version", String(36), nullable=False),
                UniqueConstraint("source_id", "source_key"))
versions = Table("record_version", metadata,
                 Column("record_version_id", String(36), primary_key=True),
                 Column("record_id", ForeignKey("source_record.record_id"), nullable=False),
                 Column("snapshot_id", ForeignKey("source_snapshot.snapshot_id"), nullable=False),
                 Column("payload", JSON, nullable=False), Column("tombstone", Boolean, nullable=False))
decisions = Table("review_decision", metadata,
                  Column("decision_id", String(36), primary_key=True),
                  Column("left_id", ForeignKey("source_record.record_id"), nullable=False),
                  Column("right_id", ForeignKey("source_record.record_id"), nullable=False),
                  Column("left_version", String(36), nullable=False),
                  Column("right_version", String(36), nullable=False),
                  Column("action", String(20), nullable=False), Column("reason", Text, nullable=False),
                  Column("reviewer", String(100), nullable=False), Column("base_revision", String(36)),
                  Column("policy_version", String(200), nullable=False))
events = Table("decision_event", metadata,
               Column("seq", Integer, primary_key=True, autoincrement=True),
               Column("decision_id", ForeignKey("review_decision.decision_id"), nullable=False),
               Column("action", String(20), nullable=False), Column("created_at", String(40), nullable=False),
               Column("policy_version", String(200)),
               Column("reason", Text, nullable=False), Column("reviewer", String(100), nullable=False))
runs = Table("match_run", metadata, Column("run_id", String(36), primary_key=True),
             Column("pipeline_version", String(200), nullable=False), Column("manifest", JSON, nullable=False),
             Column("status", String(20), nullable=False))
edges = Table("scored_edge", metadata,
              Column("run_id", ForeignKey("match_run.run_id"), primary_key=True),
              Column("left_version", String(36), primary_key=True),
              Column("right_version", String(36), primary_key=True),
              Column("score", Float, nullable=False), Column("evidence", JSON, nullable=False))
revisions = Table("identity_revision", metadata,
                  Column("revision_id", String(36), primary_key=True), Column("parent_revision", String(36)),
                  Column("status", String(20), nullable=False), Column("artifact_path", Text, nullable=False),
                  Column("artifact_sha256", String(64), nullable=False), Column("input_hash", String(64), nullable=False),
                  Column("event_cutoff", Integer, nullable=False), Column("constraint_hash", String(64), nullable=False),
                  Column("policy_version", String(200), nullable=False), Column("created_at", String(40), nullable=False),
                  Column("published_at", String(40)))
memberships = Table("entity_membership", metadata,
                    Column("revision_id", ForeignKey("identity_revision.revision_id"), primary_key=True),
                    Column("record_id", ForeignKey("source_record.record_id"), primary_key=True),
                    Column("entity_id", String(36), nullable=False, index=True),
                    Column("record_version_id", ForeignKey("record_version.record_version_id"), nullable=False))
lineage = Table("entity_lineage", metadata,
                Column("revision_id", ForeignKey("identity_revision.revision_id"), primary_key=True),
                Column("from_entity", String(36), primary_key=True), Column("to_entity", String(36), primary_key=True),
                Column("relation", String(20), primary_key=True))
current = Table("current_revision", metadata, Column("workspace", String(40), primary_key=True),
                Column("revision_id", String(36)), Column("generation", Integer, nullable=False))
