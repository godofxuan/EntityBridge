"""Real HTTP governance lifecycle for review-only hybrid candidates."""
from uuid import uuid4

from fastapi.testclient import TestClient

from entitybridge.api import create_app
from entitybridge.candidates import FrozenNameRetriever
from entitybridge.store import Store


def frozen_retriever(path, top_k=1):
    # Deliberately synthetic, separately named training records. No production
    # input or evaluator truth is used to fit the candidate vocabulary/IDF.
    training = [{"record_id": f"training-{source}", "record_version_id": f"training-{source}-v1",
                 "source": source, "name": name, "address": None, "city": None,
                 "postcode": None, "country": "GB"}
                for source, name in (("left", "ALPHA TRAINING COMPANY"), ("right", "BETA TRAINING COMPANY"))]
    FrozenNameRetriever.fit(training, top_k=top_k, min_similarity=0).save(path)
    return path


def post(client, path, body):
    response = client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, candidate, parent):
    revision = candidate["revision_id"]
    post(client, f"/revisions/{revision}/publish", {"expected_parent": parent})
    return revision


def imported_client(tmp_path, candidate_path):
    store = Store(f"sqlite:///{tmp_path / 'hybrid.db'}", tmp_path / "artifacts")
    store.initialize()
    client = TestClient(create_app(store, local_demo=True, candidate_model_path=candidate_path))
    for source in ("left", "right"):
        post(client, "/imports", {"source": source, "rows": [
            {"source_key": source + "-1", "name": "ALPHA BUSINESS LIMITED", "country": "GB"}]})
    return store, client


def accept_pair(store, client, revision):
    records = [member for entity in client.get("/entities").json() for member in entity["members"]]
    left, right = sorted(records)
    left_version = client.get(f"/records/{left}/links").json()["record"]["record_version_id"]
    right_version = client.get(f"/records/{right}/links").json()["record"]["record_version_id"]
    return post(client, "/reviews/decision", {"left": left, "right": right, "action": "accept",
        "reason": "Synthetic HTTP regression: manually verified same legal entity", "base_revision": revision,
        "left_version": left_version, "right_version": right_version,
        "policy_version": store._payload(revision)["policy_version"]})


def test_hybrid_http_high_score_requires_review_accept_revoke_and_unchanged_run_is_full(tmp_path):
    model_path = frozen_retriever(tmp_path / "candidate-model")
    store, client = imported_client(tmp_path, model_path)
    config = {"method": "exact", "candidate_mode": "hybrid", "threshold": 1.0,
              "review_threshold": 0.5, "incremental": True, "verify_full": True}
    initial = post(client, "/match-runs", config)
    assert initial["candidate_pairs"] == 1
    assert initial["automatic_merge"] is False
    assert initial["computation"]["mode"] == "full"
    revision = publish(client, initial, None)
    entities = client.get("/entities").json()
    assert len(entities) == 2
    record = entities[0]["members"][0]
    edge = client.get(f"/records/{record}/links").json()["links"][0]
    assert edge["score"] == 1.0 and edge["auto_merge"] is False
    assert "candidate_requires_review" in {d["reason"] for d in store._payload(revision)["edge_decisions"]}

    accepted = accept_pair(store, client, revision)
    accepted_revision = publish(client, accepted, revision)
    assert len(client.get("/entities").json()) == 1
    assert '<article class="panel review">' not in client.get("/reviews", params={"status": "review"}).text
    accepted_page = client.get("/reviews", params={"status": "accepted"})
    assert accepted_page.status_code == 200
    assert '<article class="panel review">' in accepted_page.text
    preview = post(client, f"/decisions/{accepted['decision_id']}/revoke-preview",
                   {"base_revision": accepted_revision})
    assert store.current_revision() == accepted_revision
    revoked = post(client, f"/decisions/{accepted['decision_id']}/revoke", {
        "base_revision": accepted_revision, "reason": "Synthetic HTTP regression: revoke manual confirmation",
        "preview_cutoff": preview["event_cutoff"]})
    revoked_revision = publish(client, revoked, accepted_revision)
    assert len(client.get("/entities").json()) == 2
    assert store._payload(revoked_revision)["constraints"]["suppressed"]

    unchanged = post(client, "/match-runs", config)
    assert unchanged["score_refresh"] is None
    assert unchanged["computation"]["mode"] == "full"
    assert unchanged["computation"]["reason"] == "global_candidate_index"
    unchanged_revision = publish(client, unchanged, revoked_revision)
    assert len(client.get("/entities").json()) == 2

    # Top-k is part of the candidate fingerprint/policy. A revoked basis is
    # suppressed only for its old policy; a new hybrid policy still cannot merge.
    revised_model = frozen_retriever(tmp_path / "candidate-model-k2", top_k=2)
    changed_client = TestClient(create_app(store, local_demo=True, candidate_model_path=revised_model))
    changed = post(changed_client, "/match-runs", config)
    changed_revision = publish(changed_client, changed, unchanged_revision)
    current = store._payload(changed_revision)
    assert current["policy_version"] != store._payload(revoked_revision)["policy_version"]
    assert current["constraints"]["suppressed"] == []
    assert store._payload(revoked_revision)["constraints"]["suppressed"]
    assert len(changed_client.get("/entities").json()) == 2


def test_hybrid_missing_frozen_candidate_model_returns_422(tmp_path):
    store, client = imported_client(tmp_path, None)
    response = client.post("/match-runs", json={"method": "exact", "candidate_mode": "hybrid"})
    assert response.status_code == 422
    assert "frozen" in response.json()["detail"].lower()
    assert store.current_revision() is None


def test_active_manual_accept_survives_a_top_k_policy_change(tmp_path):
    first_model = frozen_retriever(tmp_path / "first-model", top_k=1)
    store, client = imported_client(tmp_path, first_model)
    config = {"method": "exact", "candidate_mode": "hybrid", "threshold": 1}
    first = publish(client, post(client, "/match-runs", config), None)
    accepted = accept_pair(store, client, first)
    accepted_revision = publish(client, accepted, first)
    next_model = frozen_retriever(tmp_path / "next-model", top_k=2)
    next_client = TestClient(create_app(store, local_demo=True, candidate_model_path=next_model))
    next_run = post(next_client, "/match-runs", config)
    next_revision = publish(next_client, next_run, accepted_revision)
    assert store._payload(next_revision)["policy_version"] != store._payload(first)["policy_version"]
    assert store._payload(next_revision)["constraints"]["must_link"]
    assert len(next_client.get("/entities").json()) == 1


def test_explicit_reject_leaves_hybrid_review_queue_and_shows_conflict(tmp_path):
    model = frozen_retriever(tmp_path / "candidate-model")
    store, client = imported_client(tmp_path, model)
    config = {"method": "exact", "candidate_mode": "hybrid", "threshold": 1}
    first = publish(client, post(client, "/match-runs", config), None)
    records = sorted(store._payload(first)["records"].values(), key=lambda r: r["record_id"])
    left, right = records
    rejected = post(client, "/reviews/decision", {
        "left": left["record_id"], "right": right["record_id"], "action": "reject",
        "reason": "Synthetic HTTP regression: distinct legal entities despite equal names",
        "base_revision": first, "left_version": left["record_version_id"],
        "right_version": right["record_version_id"], "policy_version": store._payload(first)["policy_version"]})
    publish(client, rejected, first)
    assert len(client.get("/entities").json()) == 2
    assert '<article class="panel review">' not in client.get("/reviews", params={"status": "review"}).text
    conflict = client.get("/reviews", params={"status": "conflict"})
    assert conflict.status_code == 200
    assert '<article class="panel review">' in conflict.text
