"""Durable matching jobs: ownership, stale input and atomic result regressions."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import select, update
from test_store_regressions import store as store  # noqa: PLC0414

from entitybridge import schema as s
from entitybridge.jobs import JobCancelled, JobConflict, JobQueue, LeaseLost, StaleJob, cancel_restored_jobs


def prepare(queue, lease):
    return queue.store.prepare_revision([], basis_guard=lambda con: queue.validate_basis(con, lease),
        commit_hook=lambda con, revision: queue.bind_prepared(con, lease, revision))


def test_submit_is_idempotent_and_freezes_server_basis(store):
    queue = JobQueue(store)
    first = queue.enqueue({"threshold": 0.9}, idempotency_key="same")
    assert queue.enqueue({"threshold": 0.9}, idempotency_key="same")["job_id"] == first["job_id"]
    with pytest.raises(JobConflict):
        queue.enqueue({"threshold": 0.8}, idempotency_key="same")
    assert first["status"] == "queued" and first["attempt"] == 0
    assert "lease_token" not in first
    assert first["parent_revision"] is None and first["event_cutoff"] == 0


def test_two_workers_cannot_claim_the_same_live_job(store):
    queue = JobQueue(store)
    queue.enqueue({}, idempotency_key="claim")
    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(queue.claim, ["worker-a", "worker-b"]))
    assert sum(lease is not None for lease in leases) == 1


def test_expired_lease_is_reclaimed_and_old_owner_cannot_commit(store):
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="recover")
    old = queue.claim("dead-worker")
    with store.engine.begin() as con:
        con.execute(update(s.jobs).where(s.jobs.c.job_id == job["job_id"]).values(lease_until=0))
    new = queue.claim("replacement")
    assert new.attempt == 2 and new.token != old.token
    with pytest.raises(LeaseLost):
        queue.heartbeat(old)
    with pytest.raises(LeaseLost):
        prepare(queue, old)
    result = prepare(queue, new)
    assert queue.get(job["job_id"])["result_revision"] == result["revision_id"]
    assert store.current_revision() is None
    assert queue.claim("another") is None
    assert queue.heartbeat(new) is False


def test_prepare_and_success_rollback_together(store):
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="rollback")
    lease = queue.claim("worker")

    def failing_hook(con, revision):
        queue.bind_prepared(con, lease, revision)
        raise RuntimeError("injected transaction failure")

    with pytest.raises(RuntimeError, match="injected"):
        store.prepare_revision([], basis_guard=lambda con: queue.validate_basis(con, lease),
            commit_hook=failing_hook)
    assert queue.get(job["job_id"])["status"] == "running"
    with store.engine.connect() as con:
        assert con.execute(select(s.revisions)).all() == []
    prepare(queue, lease)
    assert queue.get(job["job_id"])["status"] == "succeeded"


def test_cancel_queued_and_running_jobs_prevents_prepared_result(store):
    queue = JobQueue(store)
    queued = queue.enqueue({}, idempotency_key="queued")
    assert queue.cancel(queued["job_id"])["status"] == "cancelled"
    running = queue.enqueue({}, idempotency_key="running")
    lease = queue.claim("worker")
    assert queue.cancel(running["job_id"])["cancel_requested"] is True
    with pytest.raises(JobCancelled):
        prepare(queue, lease)
    with pytest.raises(JobCancelled):
        queue.heartbeat(lease)
    assert queue.get(running["job_id"])["status"] == "cancelled"


def test_source_change_rejects_queued_and_running_basis(store):
    queue = JobQueue(store)
    queued = queue.enqueue({}, idempotency_key="queued")
    running = queue.enqueue({}, idempotency_key="running")
    lease = queue.claim("worker")
    store.import_records("registry", [{"source_key": "A", "name": "ALPHA"}])
    with pytest.raises(StaleJob):
        prepare(queue, lease)
    assert queue.claim("replacement") is None
    other_id = ({queued["job_id"], running["job_id"]} - {lease.job_id}).pop()
    assert queue.get(other_id)["last_error"]["code"] == "stale_basis"


def test_run_once_requires_atomic_binding_and_bounded_explicit_retry(store):
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="retry", max_attempts=2)
    assert queue.run_once("worker", lambda lease: None)["status"] == "failed"
    assert queue.get(job["job_id"])["last_error"]["code"] == "missing_atomic_binding"
    queue.retry(job["job_id"])
    result = queue.run_once("worker", lambda lease: prepare(queue, lease))
    assert result["status"] == "succeeded" and result["attempt"] == 2
    with pytest.raises(JobConflict):
        queue.retry(job["job_id"])


def test_concurrent_idempotent_submissions_create_one_job_and_event(store):
    queue = JobQueue(store)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: queue.enqueue({"x": 1}, idempotency_key="same"), range(2)))
    assert results[0]["job_id"] == results[1]["job_id"]
    assert [event["action"] for event in queue.get(results[0]["job_id"], include_events=True)["events"]] == ["SUBMIT"]


def test_last_expired_attempt_is_failed_without_exceeding_budget(store):
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="budget", max_attempts=1)
    lease = queue.claim("worker")
    with store.engine.begin() as con:
        con.execute(update(s.jobs).where(s.jobs.c.job_id == job["job_id"]).values(lease_until=0))
    assert queue.claim("other") is None
    result = queue.get(job["job_id"])
    assert result["status"] == "failed" and result["attempt"] == 1
    assert result["last_error"]["code"] == "attempts_exhausted"
    with pytest.raises(JobConflict):
        queue.retry(job["job_id"])
    with pytest.raises(LeaseLost):
        queue.fail(lease, "late failure")


@pytest.mark.parametrize("change", ["parent", "event"])
def test_parent_or_decision_event_change_invalidates_job(store, change):
    records = store.import_records("registry", [
        {"source_key": "A", "name": "ALPHA"}, {"source_key": "B", "name": "BETA"}])["records"]
    parent = store.prepare_revision([])["revision_id"]
    store.publish(parent, expected_parent=None)
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="basis")
    lease = queue.claim("worker")
    if change == "parent":
        candidate = store.prepare_revision([])["revision_id"]
        store.publish(candidate, expected_parent=parent)
    else:
        from test_store_regressions import decide
        decide(store, *records, base=parent)
    with pytest.raises(StaleJob):
        prepare(queue, lease)
    queue.fail(lease, "stale", code="stale_basis")
    with pytest.raises(StaleJob):
        queue.retry(job["job_id"])
    assert queue.get(job["job_id"])["result_revision"] is None


def test_expiry_during_preparation_rolls_back_candidate_and_success(store, monkeypatch):
    from entitybridge import query_projection
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="expire-during-commit")
    lease = queue.claim("worker")
    original = query_projection.write

    def expire(con, *args, **kwargs):
        original(con, *args, **kwargs)
        con.execute(update(s.jobs).where(s.jobs.c.job_id == job["job_id"]).values(lease_until=0))

    monkeypatch.setattr(query_projection, "write", expire)
    with pytest.raises(LeaseLost):
        prepare(queue, lease)
    with store.engine.connect() as con:
        assert con.execute(select(s.revisions)).all() == []
    assert queue.get(job["job_id"])["status"] == "running"


def test_running_cancellation_is_recorded_by_worker_and_never_creates_revision(store):
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="cancel")

    def execute(lease):
        queue.cancel(lease.job_id)
        prepare(queue, lease)

    result = queue.run_once("worker", execute)
    assert result["status"] == "cancelled" and result["result_revision"] is None
    assert queue.get(job["job_id"])["lease_owner"] is None


def test_worker_errors_do_not_store_exception_text_or_credentials(store):
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="secrets")

    def execute(lease):
        raise RuntimeError("postgresql://private:password@host SQL SELECT secret_data")

    result = queue.run_once("worker", execute)
    assert result["status"] == "failed"
    state = json.dumps(queue.get(job["job_id"], include_events=True))
    assert "password" not in state and "secret_data" not in state and "postgresql://" not in state


def test_background_heartbeat_keeps_long_callback_alive(store):
    queue = JobQueue(store)
    queue.enqueue({}, idempotency_key="long-callback")

    def execute(lease):
        time.sleep(0.8)
        assert queue.claim("competitor") is None
        prepare(queue, lease)

    result = queue.run_once("worker", execute, lease_seconds=0.4, heartbeat_interval=0.05)
    assert result["status"] == "succeeded" and result["attempt"] == 1


def test_killed_process_lease_is_reclaimed_without_duplicate_result(store, tmp_path):
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="process-kill")
    marker = tmp_path / "claimed.json"
    child = """
import json, os, time
from pathlib import Path
from entitybridge.store import Store
from entitybridge.jobs import JobQueue
store = Store(os.environ['JOB_TEST_URL'], os.environ['JOB_TEST_ARTIFACTS'])
lease = JobQueue(store).claim('doomed-process', lease_seconds=0.3)
Path(os.environ['JOB_TEST_MARKER']).write_text(json.dumps({'job_id': lease.job_id}), encoding='utf-8')
time.sleep(30)
"""
    env = dict(os.environ, JOB_TEST_URL=store.engine.url.render_as_string(hide_password=False),
               JOB_TEST_ARTIFACTS=str(store.artifact_root), JOB_TEST_MARKER=str(marker),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.Popen([sys.executable, "-c", child], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        assert marker.exists(), "child worker did not acquire its lease"
        process.kill()
        process.wait(timeout=5)
        time.sleep(0.35)
        result = queue.run_once("replacement", lambda lease: prepare(queue, lease))
        assert result["job_id"] == job["job_id"]
        assert result["status"] == "succeeded" and result["attempt"] == 2
        with store.engine.connect() as con:
            assert len(con.execute(select(s.revisions)).all()) == 1
        assert store.current_revision() is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_killed_process_after_atomic_commit_is_not_reclaimed(store, tmp_path):
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="kill-after-commit")
    marker = tmp_path / "committed"
    child = """
import os, time
from pathlib import Path
from entitybridge.store import Store
from entitybridge.jobs import JobQueue
store = Store(os.environ['JOB_TEST_URL'], os.environ['JOB_TEST_ARTIFACTS'])
queue = JobQueue(store)
lease = queue.claim('doomed-process', lease_seconds=10)
store.prepare_revision([], basis_guard=lambda con: queue.validate_basis(con, lease),
    commit_hook=lambda con, revision: queue.bind_prepared(con, lease, revision))
Path(os.environ['JOB_TEST_MARKER']).write_text('committed', encoding='utf-8')
time.sleep(30)
"""
    env = dict(os.environ, JOB_TEST_URL=store.engine.url.render_as_string(hide_password=False),
               JOB_TEST_ARTIFACTS=str(store.artifact_root), JOB_TEST_MARKER=str(marker),
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.Popen([sys.executable, "-c", child], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        assert marker.exists(), "child worker did not commit its result"
        process.kill()
        process.wait(timeout=5)
        assert queue.run_once("replacement", lambda lease: pytest.fail("completed job was rerun")) is None
        result = queue.get(job["job_id"])
        assert result["status"] == "succeeded" and result["attempt"] == 1
        assert result["result_revision"] is not None
        assert store.current_revision() is None
        with store.engine.connect() as con:
            assert len(con.execute(select(s.revisions)).all()) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_completion_rejects_revision_with_another_frozen_basis(store):
    wrong = store.prepare_revision([])["revision_id"]
    store.import_records("registry", [{"source_key": "A", "name": "ALPHA"}])
    queue = JobQueue(store)
    job = queue.enqueue({}, idempotency_key="wrong-revision")
    lease = queue.claim("worker")
    with store.engine.begin() as con, pytest.raises(JobConflict, match="does not match"):
        queue.bind_prepared(con, lease, wrong)
    assert queue.get(job["job_id"])["status"] == "running"
    assert queue.get(job["job_id"])["result_revision"] is None
    assert prepare(queue, lease)["entity_count"] == 1


def test_success_survives_late_callback_error(store):
    queue = JobQueue(store)
    queue.enqueue({}, idempotency_key="late-error")

    def execute(lease):
        prepare(queue, lease)
        raise RuntimeError("lost acknowledgement after successful commit")

    result = queue.run_once("worker", execute)
    assert result["status"] == "succeeded" and result["last_error"] is None


def test_restore_cancels_unfinished_jobs_and_preserves_bound_prepared_result(store):
    queue = JobQueue(store)
    completed = queue.enqueue({}, idempotency_key="completed")
    queue.run_once("worker", lambda lease: prepare(queue, lease))
    running = queue.enqueue({}, idempotency_key="running")
    old_lease = queue.claim("old-owner")
    queued = queue.enqueue({}, idempotency_key="queued")
    with store.engine.begin() as con:
        assert cancel_restored_jobs(con) == 2
        assert cancel_restored_jobs(con) == 0
    for job in [running, queued]:
        result = queue.get(job["job_id"], include_events=True)
        assert result["status"] == "cancelled" and result["lease_owner"] is None
        assert result["events"][-1]["action"] == "RESTORE_CANCEL"
    assert queue.get(completed["job_id"])["status"] == "succeeded"
    assert queue.get(completed["job_id"])["result_revision"] is not None
    with pytest.raises(LeaseLost):
        queue.heartbeat(old_lease)


@pytest.mark.parametrize("version", ["0001", "0002", "0003"])
def test_known_release_schema_upgrades_and_retains_source_rows(store, version):
    from alembic import command
    from alembic.config import Config

    from entitybridge.database import CURRENT_SCHEMA

    store.import_records("registry", [{"source_key": "A", "name": "ALPHA"}])
    before = store.active_records()
    with store.engine.begin() as con:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = con
        command.downgrade(config, version)
    store.initialize()
    assert store.active_records() == before
    assert JobQueue(store).enqueue({}, idempotency_key="upgraded")["status"] == "queued"
    with store.engine.connect() as con:
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == CURRENT_SCHEMA
