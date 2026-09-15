"""Relational state; immutable large run payloads live outside the database."""

from sqlalchemy import (
                        JSON,
                        Boolean,
                        CheckConstraint,
                        Column,
                        Float,
                        ForeignKey,
                        ForeignKeyConstraint,
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

query_projections = Table("query_projection", metadata,
    Column("revision_id", ForeignKey("identity_revision.revision_id"), primary_key=True),
    Column("projection_version", String(40), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("entity_count", Integer, nullable=False), Column("member_count", Integer, nullable=False),
    Column("search_value_count", Integer, nullable=False))
query_entities = Table("query_entity", metadata,
    Column("revision_id", ForeignKey("identity_revision.revision_id"), primary_key=True),
    Column("entity_id", String(36), primary_key=True), Column("canonical", JSON, nullable=False))
query_values = Table("query_search_value", metadata,
    Column("revision_id", String(36), primary_key=True), Column("entity_id", String(36), primary_key=True),
    Column("ordinal", Integer, primary_key=True), Column("value_folded", Text, nullable=False),
    ForeignKeyConstraint(["revision_id", "entity_id"], ["query_entity.revision_id", "query_entity.entity_id"]))
Index("ix_membership_revision_entity_record", memberships.c.revision_id, memberships.c.entity_id,
      memberships.c.record_id)

jobs = Table("durable_job", metadata,
    Column("job_id", String(36), primary_key=True),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("payload", JSON, nullable=False), Column("payload_hash", String(64), nullable=False),
    Column("source_hash", String(64), nullable=False),
    Column("parent_revision", ForeignKey("identity_revision.revision_id")),
    Column("event_cutoff", Integer, nullable=False), Column("status", String(20), nullable=False),
    Column("attempt", Integer, nullable=False), Column("max_attempts", Integer, nullable=False),
    Column("created_at", Float, nullable=False), Column("updated_at", Float, nullable=False),
    Column("lease_until", Float), Column("lease_owner", String(100)), Column("lease_token", String(36)),
    Column("cancel_requested", Boolean, nullable=False),
    Column("result_revision", ForeignKey("identity_revision.revision_id"), unique=True),
    Column("last_error", JSON), Column("progress", JSON),
    CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')", name="ck_job_status"),
    CheckConstraint("attempt >= 0 AND max_attempts >= 1 AND attempt <= max_attempts", name="ck_job_attempts"))
Index("ix_job_status_created", jobs.c.status, jobs.c.created_at, jobs.c.job_id)
Index("ix_job_status_lease", jobs.c.status, jobs.c.lease_until)
job_events = Table("durable_job_event", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("job_id", ForeignKey("durable_job.job_id"), nullable=False),
    Column("attempt", Integer, nullable=False), Column("action", String(40), nullable=False),
    Column("created_at", Float, nullable=False), Column("detail", JSON, nullable=False))
Index("ix_job_event_job_seq", job_events.c.job_id, job_events.c.seq)

workspace_binding = Table("workspace_binding", metadata,
    Column("singleton", Integer, primary_key=True, autoincrement=False),
    Column("workspace_name", String(200), nullable=False),
    CheckConstraint("singleton = 1", name="ck_workspace_binding_singleton"),
    CheckConstraint("length(workspace_name) BETWEEN 1 AND 200", name="ck_workspace_binding_name_length"))

review_operations = Table("review_operation", metadata,
    Column("operation_id", String(36), primary_key=True),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False), Column("payload", JSON, nullable=False),
    Column("kind", String(20), nullable=False), Column("reviewer", String(100), nullable=False),
    Column("decision_id", ForeignKey("review_decision.decision_id"), nullable=False),
    Column("event_seq", ForeignKey("decision_event.seq"), nullable=False, unique=True),
    Column("base_revision", ForeignKey("identity_revision.revision_id"), nullable=False),
    Column("source_hash", String(64), nullable=False), Column("policy_version", String(200), nullable=False),
    Column("status", String(20), nullable=False), Column("attempt", Integer, nullable=False),
    Column("lease_token", String(36)), Column("lease_until", Float),
    Column("candidate_revision_id", ForeignKey("identity_revision.revision_id"), unique=True),
    Column("safe_error_code", String(40)), Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    CheckConstraint("kind IN ('decision','revoke')", name="ck_review_operation_kind"),
    CheckConstraint("status IN ('accepted','building','prepared','build_failed','stale_basis')",
                    name="ck_review_operation_status"),
    CheckConstraint("attempt >= 0", name="ck_review_operation_attempt"))
Index("ix_review_operation_created", review_operations.c.created_at, review_operations.c.operation_id)
