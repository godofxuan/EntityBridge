"""Restore must preserve immutable history and never overwrite existing state."""
import json
from pathlib import Path

import pytest
from sqlalchemy import select, update

from entitybridge import schema as s
from entitybridge.operations import create_backup, restore_backup, verify_backup
from entitybridge.store import Store


@pytest.fixture
def source(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'source.db'}", tmp_path / "source_artifacts")
    store.initialize()
    records = store.import_records("synthetic", [
        {"source_key": "a", "name": "ALPHA LIMITED", "aliases": ["Former Alpha"]},
        {"source_key": "b", "name": "ALPHA LTD"},
        {"source_key": "c", "name": "OTHER LIMITED"},
    ])["records"]
    initial = store.prepare_revision([])["revision_id"]
    store.publish(initial, expected_parent=None)
    a, b = records[:2]
    decision = store.decide(a["record_id"], b["record_id"], action="accept", reviewer="synthetic",
        reason="Synthetic backup evidence", base_revision=initial, left_version=a["record_version_id"],
        right_version=b["record_version_id"], policy_version="default-v1")
    store.publish(decision["revision_id"], expected_parent=initial)
    yield store
    store.engine.dispose()


def test_round_trip_preserves_versions_decisions_history_and_relocates_legacy_absolute_artifacts(source, tmp_path):
    history = source.history()
    expected = {row["revision_id"]: source.entities(revision=row["revision_id"]) for row in history}
    with source.engine.begin() as connection:
        for row in history:
            connection.execute(update(s.revisions).where(s.revisions.c.revision_id == row["revision_id"])
                .values(artifact_path=str(source.artifact_root / row["artifact_path"])))
    backup = tmp_path / "backup"
    manifest = create_backup(source, backup)
    assert verify_backup(backup)["verified"]
    assert manifest["summary"]["table_counts"]["identity_revision"] == 2
    url, artifacts = f"sqlite:///{tmp_path / 'restored.db'}", tmp_path / "restored_artifacts"
    report = restore_backup(backup, url, artifacts)
    assert all(report["checks"].values())
    restored = Store(url, artifacts)
    try:
        assert restored.active_records() == source.active_records()
        assert restored.decision_history() == source.decision_history()
        assert restored.current_revision() == source.current_revision()
        for row in restored.history():
            assert not Path(row["artifact_path"]).is_absolute()
            assert restored.entities(revision=row["revision_id"]) == expected[row["revision_id"]]
            assert restored.projection_status(row["revision_id"], verify=True)["ready"]
        assert restored.entities("former alpha") == source.entities("former alpha")
    finally:
        restored.engine.dispose()


@pytest.mark.parametrize("damage", ["database", "artifact", "missing", "traversal", "extra"])
def test_damaged_or_incomplete_backup_is_rejected_before_creating_targets(source, tmp_path, damage):
    backup = tmp_path / "backup"
    create_backup(source, backup)
    artifact = next((backup / "artifacts").iterdir())
    if damage == "database":
        (backup / "database.json").write_bytes(b"{}")
    elif damage == "artifact":
        artifact.write_bytes(b"{}")
    elif damage == "missing":
        artifact.unlink()
    elif damage == "extra":
        (backup / "unexpected.txt").write_text("extra", encoding="utf-8")
    else:
        manifest = json.loads((backup / "manifest.json").read_bytes())
        manifest["artifacts"]["../outside.json"] = {"sha256": "0" * 64, "bytes": 0}
        (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    database, artifacts = tmp_path / "restored.db", tmp_path / "restored_artifacts"
    with pytest.raises(ValueError):
        restore_backup(backup, f"sqlite:///{database}", artifacts)
    assert not database.exists()
    assert not artifacts.exists()


def test_restore_refuses_existing_database_and_artifact_directory_without_modification(source, tmp_path):
    backup = tmp_path / "backup"
    create_backup(source, backup)
    original = Path(source.engine.url.database).read_bytes()
    with pytest.raises(ValueError, match="new SQLite"):
        restore_backup(backup, source.engine.url, tmp_path / "unused_artifacts")
    assert Path(source.engine.url.database).read_bytes() == original
    with pytest.raises(ValueError, match="directory must be new"):
        restore_backup(backup, f"sqlite:///{tmp_path / 'unused.db'}", source.artifact_root)
    assert not (tmp_path / "unused.db").exists()
    with pytest.raises(ValueError, match="query overrides"):
        restore_backup(backup, "postgresql+psycopg://localhost/safe_test?dbname=production", tmp_path / "unused")


def test_failed_restore_rolls_back_and_cleans_only_its_new_targets(source, tmp_path, monkeypatch):
    from entitybridge import query_projection
    backup = tmp_path / "backup"
    create_backup(source, backup)
    original_pointer = source.current_revision()
    def fail_verification(*args):
        raise RuntimeError("Synthetic restore verification failure")
    monkeypatch.setattr(query_projection, "verify", fail_verification)
    database, artifacts = tmp_path / "restored.db", tmp_path / "restored_artifacts"
    with pytest.raises(RuntimeError, match="Synthetic restore"):
        restore_backup(backup, f"sqlite:///{database}", artifacts)
    assert not database.exists()
    assert not artifacts.exists()
    assert source.current_revision() == original_pointer
    assert verify_backup(backup)["verified"]


def test_backup_snapshot_does_not_mix_source_changes_committed_during_copy(source, tmp_path, monkeypatch):
    from entitybridge import operations
    with source.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    before = source.active_records()
    original = operations._rows
    wrote = False
    def capture_then_write(connection):
        nonlocal wrote
        rows = original(connection)
        if not wrote:
            wrote = True
            source.import_records("synthetic", [{"source_key": "a", "name": "AFTER BACKUP SNAPSHOT"}])
        return rows
    monkeypatch.setattr(operations, "_rows", capture_then_write)
    backup = tmp_path / "backup"
    create_backup(source, backup)
    assert source.active_records() != before
    url, artifacts = f"sqlite:///{tmp_path / 'restored.db'}", tmp_path / "restored_artifacts"
    restore_backup(backup, url, artifacts)
    restored = Store(url, artifacts)
    try:
        assert restored.active_records() == before
    finally:
        restored.engine.dispose()


def test_restoring_jobs_cancels_queued_and_running_work_without_reusing_a_lease(source, tmp_path):
    from entitybridge.jobs import JobQueue
    queue = JobQueue(source)
    running = queue.enqueue({"method": "exact"}, idempotency_key="running")
    lease = queue.claim("old-worker", lease_seconds=600)
    assert lease.job_id == running["job_id"]
    queued = queue.enqueue({"method": "fuzzy"}, idempotency_key="queued")
    backup = tmp_path / "backup"
    create_backup(source, backup)
    url, artifacts = f"sqlite:///{tmp_path / 'restored.db'}", tmp_path / "restored_artifacts"
    report = restore_backup(backup, url, artifacts)
    assert report["recovery_cancelled_jobs"] == 2
    restored = Store(url, artifacts)
    try:
        with restored.engine.connect() as connection:
            jobs = connection.execute(select(s.jobs)).mappings().all()
            assert all(row["status"] == "cancelled" and row["cancel_requested"] for row in jobs)
            assert all(row["lease_owner"] is None and row["lease_token"] is None and row["lease_until"] is None for row in jobs)
            events = connection.execute(select(s.job_events.c.action)).scalars().all()
            assert events.count("RESTORE_CANCEL") == 2
        assert JobQueue(restored).claim("new-worker") is None
        assert queue.get(running["job_id"])["status"] == "running"
        assert queue.get(queued["job_id"])["status"] == "queued"
    finally:
        restored.engine.dispose()


def test_backup_refuses_a_missing_sqlite_source_without_creating_a_database(tmp_path):
    database = tmp_path / "missing.db"
    store = Store(f"sqlite:///{database}", tmp_path / "empty_artifacts")
    try:
        with pytest.raises(ValueError, match="existing SQLite database"):
            create_backup(store, tmp_path / "backup")
        assert not database.exists()
        assert not (tmp_path / "backup").exists()
    finally:
        store.engine.dispose()
