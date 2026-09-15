import json

import pytest

from entitybridge.company_matching import FrozenCompanyMatcher, company_features, fit_platt
from entitybridge.store import digest


def training():
    rows = []
    for source, suffix in [("a", "CORPORATION"), ("b", "CORP")]:
        for i, name in enumerate(["ALPHA", "BETA", "GAMMA", "DELTA", "OMEGA", "SIGMA"]):
            rows.append({"record_id": f"{source}{i}", "record_version_id": f"{source}{i}v", "source": source,
                "name": f"{name} {suffix}", "country": "USA" if source == "a" else "United States of America"})
    labels = [{"left_id": f"a{i}", "right_id": f"b{j}", "label": int(i == j), "split": "train"}
              for i in range(6) for j in range(6)]
    return rows, labels


@pytest.mark.parametrize("variant", ["iso_lr", "company_lr", "company_gb"])
def test_safe_model_reload_equals_fitted_estimator_and_preserves_evidence(tmp_path, variant):
    rows, labels = training()
    model = FrozenCompanyMatcher.fit(rows, labels, variant=variant)
    model.save(tmp_path / "model")
    restored = FrozenCompanyMatcher.load(tmp_path / "model")
    candidates = [{"left": "a0", "right": f"b{i}", "rules": ["fixture"]} for i in (0, 1)]
    assert restored.score(rows, candidates) == model.score(rows, candidates)
    assert restored.score(rows, candidates)[0]["auto_merge"] is False
    path = tmp_path / "model/company_model.json"
    state = json.loads(path.read_text())
    state["intercept"] += 1
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="hash"):
        FrozenCompanyMatcher.load(tmp_path / "model")


def test_features_are_symmetric_keep_original_and_do_not_use_ids():
    a = {"name": "ALPHA LIMITED", "country": "USA", "record_id": "a"}
    b = {"name": "ALPHA LTD", "country": "United States of America", "record_id": "b"}
    assert company_features(a, b) == company_features(b, a)
    assert company_features(a, b) == company_features(a | {"record_id": "changed"}, b)
    assert company_features(a, b)[-11] == 1  # base ratio
    assert company_features(a, b)[0] < 1  # original evidence retained
    assert company_features(a, b)[10] == 0
    assert company_features(a, b | {"country": "Canada"})[10] == 1


def test_company_fit_rejects_answers_and_wrong_split_and_no_test_vocabulary():
    rows, labels = training()
    with pytest.raises(ValueError, match="allowlist"):
        FrozenCompanyMatcher.fit([r | {"true_entity": "answer"} for r in rows], labels)
    with pytest.raises(ValueError, match="training labels"):
        FrozenCompanyMatcher.fit(rows, [r | {"split": "test"} for r in labels])
    with pytest.raises(ValueError, match="binary"):
        FrozenCompanyMatcher.fit(rows, [labels[0] | {"label": True}])
    model = FrozenCompanyMatcher.fit(rows, labels)
    before = model.fingerprint
    model.score([r | {"name": "UNSEENNAME"} for r in rows], [{"left": "a0", "right": "b0", "rules": []}])
    assert model.fingerprint == before and "UNSEENNAME" not in model._state["idf"]


def test_loader_rejects_cyclic_tree_even_with_recomputed_manifest(tmp_path):
    model = FrozenCompanyMatcher.fit(*training(), variant="company_gb")
    model.save(tmp_path / "model")
    state = model._state
    state["trees"][0]["left"][0] = 0
    (tmp_path / "model/company_model.json").write_text(json.dumps(state))
    (tmp_path / "model/manifest.json").write_text(json.dumps({"fingerprint": digest(state)}))
    with pytest.raises(ValueError, match="cyclic"):
        FrozenCompanyMatcher.load(tmp_path / "model")


def test_calibration_refuses_test_labels_and_declares_insufficient_support():
    _, labels = training()
    with pytest.raises(ValueError, match="validation"):
        fit_platt(labels, {})
    assert fit_platt([r | {"split": "validation"} for r in labels], {})["status"] == "identity_insufficient_support"


def test_company_model_runs_through_durable_worker_and_exposes_form(tmp_path):
    from fastapi.testclient import TestClient

    from entitybridge.api import create_app
    from entitybridge.jobs import JobQueue
    from entitybridge.store import Store
    from entitybridge.worker import execute_matching_job
    model = FrozenCompanyMatcher.fit(*training())
    path = tmp_path / "model"
    model.save(path)
    store = Store(f"sqlite:///{tmp_path / 'service.db'}", tmp_path / "revisions")
    store.initialize()
    for source, country in [("a", "USA"), ("b", "United States of America")]:
        store.import_records(source, [{"source_key": "1", "name": "SYNTHETIC ALPHA", "country": country}])
    client = TestClient(create_app(store, local_demo=True, model_path=path))
    assert 'value="company"' in client.get("/tasks").text
    form = client.post("/ui/jobs", data={"idempotency_key": "company-form", "method": "company",
        "candidate_mode": "fixed_iso", "threshold": .9, "review_threshold": 0.})
    assert form.status_code == 200
    queue = JobQueue(store)
    result = queue.run_once("fixture", lambda lease: execute_matching_job(store, queue, lease, model_path=path))
    assert result["status"] == "succeeded"
    payload = store._payload(result["result_revision"], published_only=False)
    assert len(payload["edges"]) == 1 and payload["edges"][0]["auto_merge"] is False
    assert store.current_revision() is None


def test_company_model_rotation_after_submission_rejects_job(tmp_path):
    from fastapi.testclient import TestClient

    from entitybridge.api import create_app
    from entitybridge.jobs import JobQueue
    from entitybridge.store import Store
    from entitybridge.worker import execute_matching_job
    path, other = tmp_path / "model", tmp_path / "other"
    FrozenCompanyMatcher.fit(*training()).save(path)
    FrozenCompanyMatcher.fit(*training(), variant="iso_lr").save(other)
    store = Store(f"sqlite:///{tmp_path / 'rotated.db'}", tmp_path / "revisions")
    store.initialize()
    client = TestClient(create_app(store, local_demo=True, model_path=path))
    response = client.post("/jobs", json={"idempotency_key": "before-rotation", "settings": {"method": "company"}})
    assert response.status_code == 202
    queue = JobQueue(store)
    result = queue.run_once("fixture", lambda lease: execute_matching_job(store, queue, lease, model_path=other))
    assert result["status"] == "failed" and result["last_error"]["code"] == "stale_basis"
    assert store.history() == []
