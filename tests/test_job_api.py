import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from entitybridge.api import create_app
from entitybridge.cli import seed_demo
from entitybridge.jobs import JobQueue
from entitybridge.store import Store
from entitybridge.telemetry import RequestMetrics
from entitybridge.worker import execute_matching_job


def setup(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'jobs-api.db'}", tmp_path / "revisions")
    store.initialize()
    store.import_records("left", [{"source_key": "a", "name": "SYNTHETIC ALPHA"}])
    store.import_records("right", [{"source_key": "b", "name": "SYNTHETIC ALPHA"}])
    client = TestClient(create_app(store, tokens={"admin-key": ("operator", "admin"),
                                                 "viewer-key": ("reader", "viewer"),
                                                 "reviewer-key": ("human", "reviewer")}))
    return store, client


def test_job_submission_is_durable_idempotent_and_completion_does_not_publish(tmp_path):
    store, client = setup(tmp_path)
    request = {"settings": {"method": "exact"}, "idempotency_key": "one-click"}
    assert client.post("/jobs", json=request).status_code == 401
    client.headers["Authorization"] = "Bearer viewer-key"
    assert client.post("/jobs", json=request).status_code == 403
    client.headers["Authorization"] = "Bearer admin-key"
    accepted = client.post("/jobs", json=request)
    assert accepted.status_code == 202 and accepted.json()["status"] == "queued"
    job_id = accepted.json()["job_id"]
    assert client.post("/jobs", json=request).json()["job_id"] == job_id
    changed = {**request, "settings": {"method": "fuzzy"}}
    assert client.post("/jobs", json=changed).status_code == 409
    assert store.history() == []
    queue = JobQueue(store)
    result = queue.run_once("test-worker", lambda lease: execute_matching_job(store, queue, lease))
    assert result["status"] == "succeeded" and result["result_revision"]
    assert client.get("/entities").json() == [] and store.current_revision() is None
    detail = client.get(f"/jobs/{job_id}").json()
    assert detail["events"][-1]["action"] == "SUCCEED"
    assert "lease_token" not in json.dumps(detail)
    revision = result["result_revision"]
    assert client.post(f"/revisions/{revision}/publish", json={"expected_parent": None}).status_code == 200
    assert len(client.get("/entities").json()) == 1


def test_worker_refuses_input_changes_and_changed_pipeline(tmp_path, monkeypatch):
    store, client = setup(tmp_path)
    client.headers["Authorization"] = "Bearer admin-key"
    submitted = client.post("/jobs", json={"settings": {"method": "exact"}, "idempotency_key": "old"}).json()
    store.import_records("left", [{"source_key": "a", "name": "UPDATED SYNTHETIC"}])
    queue = JobQueue(store)
    queue.run_once("worker", lambda lease: execute_matching_job(store, queue, lease))
    assert queue.get(submitted["job_id"])["status"] == "failed"
    assert store.history() == []
    newer = client.post("/jobs", json={"settings": {"method": "exact"}, "idempotency_key": "new"}).json()
    monkeypatch.setattr("entitybridge.worker.model_fingerprints", lambda *a, **k: {"pipeline_sha256": "changed"})
    queue.run_once("worker", lambda lease: execute_matching_job(store, queue, lease))
    assert queue.get(newer["job_id"])["status"] == "failed"
    assert store.history() == []


def test_task_page_cancel_and_exact_zero_threshold_boundary(tmp_path):
    store, client = setup(tmp_path)
    client.headers["Authorization"] = "Bearer admin-key"
    page = client.get("/tasks")
    assert page.status_code == 200 and 'action="/ui/jobs"' in page.text
    submitted = client.post("/ui/jobs", data={"idempotency_key": "form-key", "method": "exact",
                                             "threshold": .9, "review_threshold": .5})
    assert submitted.status_code == 200
    job_id = JobQueue(store).list_jobs()[0]["job_id"]
    assert client.post(f"/ui/jobs/{job_id}/cancel").status_code == 200
    assert JobQueue(store).get(job_id)["status"] == "cancelled"
    for path, request in [("/match-runs", {"method": "exact", "threshold": 0, "review_threshold": 0}),
                          ("/jobs", {"settings": {"method": "exact", "threshold": 0, "review_threshold": 0},
                                     "idempotency_key": "zero"})]:
        assert client.post(path, json=request).status_code == 422


def test_model_rotation_between_validation_and_actual_load_is_rejected(tmp_path, monkeypatch):
    store, _client = setup(tmp_path)
    class FakeModel:
        def __init__(self, fingerprint):
            self.fingerprint = fingerprint

        def score(self, *args):
            raise AssertionError("A replaced model must not score any records")

    loads = iter([FakeModel("original"), FakeModel("original"), FakeModel("replacement")])
    monkeypatch.setattr("entitybridge.matching.SplinkMatcher.load", lambda _path: next(loads))
    client = TestClient(create_app(store, local_demo=True, model_path=Path("configured-frozen-model")))
    job_id = client.post("/jobs", json={"settings": {"method": "splink"},
                                        "idempotency_key": "model-rotation"}).json()["job_id"]
    queue = JobQueue(store)
    queue.run_once("worker", lambda lease: execute_matching_job(store, queue, lease,
                                                                  model_path=Path("configured-frozen-model")))
    result = queue.get(job_id)
    assert result["status"] == "failed" and result["last_error"]["code"] == "stale_basis"
    assert store.history() == []


def test_cli_worker_restarts_from_durable_database_and_runs_one_task(tmp_path):
    store, client = setup(tmp_path)
    client.headers["Authorization"] = "Bearer admin-key"
    job_id = client.post("/jobs", json={"settings": {"method": "exact"}, "idempotency_key": "cli"}).json()["job_id"]
    package = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run([sys.executable, "-m", "entitybridge.cli", "worker", "--once", "--database-url",
                             str(store.engine.url), "--artifact-root", str(store.artifact_root)], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(package), "PYTHONUTF8": "1"},
                            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "succeeded"
    assert JobQueue(store).get(job_id)["result_revision"] and store.current_revision() is None


def test_metrics_are_authorized_route_bounded_and_do_not_record_payloads(tmp_path):
    _store, client = setup(tmp_path)
    private_text = "PERSONAL-QUERY-DO-NOT-LOG"
    client.get("/missing-" + private_text)
    assert client.get("/ops/metrics").status_code == 401
    client.headers["Authorization"] = "Bearer viewer-key"
    assert client.get("/ops/metrics").status_code == 403
    client.headers["Authorization"] = "Bearer admin-key"
    client.get("/entities", params={"query": private_text})
    metrics = client.get("/ops/metrics")
    assert metrics.status_code == 200 and metrics.headers["X-Request-Id"]
    assert private_text not in metrics.text and "admin-key" not in metrics.text
    assert all("{" in item["route"] or private_text not in item["route"] for item in metrics.json()["series"])
    counter = RequestMetrics(sample_limit=2, route_limit=1)
    for status in (200, 200, 200, 503):
        counter.record("GET", "/test", status, .01)
    summary = counter.snapshot()
    assert summary["requests"] == 4 and summary["errors_5xx"] == 1
    assert all(row["latency_sample_count"] <= 2 for row in summary["series"])


@pytest.mark.parametrize("action,valid_labels", [("reject", 1), ("abstain", 0)])
def test_learning_api_uses_published_human_labels_and_suppresses_reviewed_pairs(tmp_path, action, valid_labels):
    store = Store(f"sqlite:///{tmp_path / 'learning-api.db'}", tmp_path / "revisions")
    store.initialize()
    revision = seed_demo(store)
    client = TestClient(create_app(store, tokens={"review": ["actual-reviewer", "reviewer"],
                                                 "read": ["reader", "viewer"]}))
    assert client.get("/reviews/queue").status_code == 401
    client.headers["Authorization"] = "Bearer read"
    assert client.get("/learning/summary").status_code == 403
    client.headers["Authorization"] = "Bearer review"
    queue = client.get("/reviews/queue").json()
    assert queue["revision_id"] == revision and len(queue["items"]) == 3
    assert client.get("/learning/summary").json()["summary"]["valid_labels"] == 0
    edge = queue["items"][0]
    payload = store._payload(revision)
    result = client.post("/reviews/decision", json={"left": edge["left"], "right": edge["right"],
        "left_version": edge["left_version"], "right_version": edge["right_version"],
        "policy_version": payload["policy_version"], "base_revision": revision, "action": action,
        "reason": "Explicit synthetic human judgment"})
    assert result.status_code == 200
    assert client.get("/learning/summary").status_code == 422
    store.publish(result.json()["revision_id"], expected_parent=revision)
    assert client.get("/learning/summary").json()["summary"]["valid_labels"] == valid_labels
    new_queue = client.get("/reviews/queue").json()["items"]
    assert all({item["left"], item["right"]} != {edge["left"], edge["right"]} for item in new_queue)
