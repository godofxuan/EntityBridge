"""Human labels, immutable learning evidence and held-out boundaries."""
import json

import pytest

from entitybridge.learning import (
    export_review_dataset,
    rank_review_candidates,
    snapshot_review_labels,
    split_learning_snapshot,
    train_candidate_model,
    verify_learning_dataset,
)
from entitybridge.store import Store


def store_with_pairs(tmp_path, count=4):
    store = Store(f"sqlite:///{tmp_path / 'learning.db'}", tmp_path / "store-artifacts")
    store.initialize()
    imported = {}
    for source in ("left", "right"):
        imported[source] = store.import_records(source, [
            {"source_key": str(i), "name": f"SYNTHETIC COMPANY {i}", "registration_number": f"SECRET-{i}"}
            for i in range(count)])["records"]
    revision = store.prepare_revision([], policy_version="manual-learning-fixture")["revision_id"]
    store.publish(revision, expected_parent=None)
    return store, imported


def decide(store, imported, number, action):
    a, b = imported["left"][number], imported["right"][number]
    parent = store.current_revision()
    result = store.decide(a["record_id"], b["record_id"], action=action, reason="Synthetic explicit human judgment",
        reviewer="fixture-reviewer", base_revision=parent, left_version=a["record_version_id"],
        right_version=b["record_version_id"], policy_version="manual-learning-fixture")
    store.publish(result["revision_id"], expected_parent=parent)
    return result


def test_snapshot_exports_only_active_explicit_binary_labels_with_version_provenance(tmp_path):
    store, rows = store_with_pairs(tmp_path)
    keep = decide(store, rows, 0, "accept")
    revoked = decide(store, rows, 1, "reject")
    decide(store, rows, 2, "abstain")
    expired = decide(store, rows, 3, "accept")
    parent = store.current_revision()
    preview = store.revoke_preview(revoked["decision_id"], base_revision=parent)
    result = store.revoke(revoked["decision_id"], base_revision=parent, reviewer="fixture-reviewer",
        reason="Withdraw label", preview_cutoff=preview["event_cutoff"])
    store.publish(result["revision_id"], expected_parent=parent)
    store.import_records("left", [{"source_key": "3", "name": "Changed legal record"}])
    with pytest.raises(ValueError, match="unpublished"):
        snapshot_review_labels(store)
    parent = store.current_revision()
    refreshed = store.prepare_revision([], policy_version="manual-learning-fixture")
    store.publish(refreshed["revision_id"], expected_parent=parent)
    snapshot = snapshot_review_labels(store)
    assert snapshot["summary"]["valid_labels"] == 1
    label = snapshot["labels"][0]
    assert label["label"] == 1
    assert label["provenance"][0]["decision_id"] == keep["decision_id"]
    assert label["provenance"][0]["reviewer"] == "fixture-reviewer"
    assert label["left_version"] and label["right_version"]
    assert revoked["decision_id"] not in str(snapshot["labels"])
    assert expired["decision_id"] not in str(snapshot["labels"])
    assert "registration_number" not in str(snapshot["records"])
    assert snapshot["summary"]["excluded_counts"] == {"abstain": 1, "expired": 1, "revoked": 1}
    assert store.current_revision() == snapshot["revision_id"]


def fixture_snapshot(groups=80):
    records, labels = [], []
    for i in range(groups):
        for source in ("left", "right"):
            records.append({"record_id": f"{source}-{i}", "record_version_id": f"{source}-{i}-v1",
                "source": source, "name": f"COMPANY {i}" if source == "left" or i % 2 else f"OTHER BUSINESS {i}",
                "address": None, "city": None, "postcode": None, "country": "GB"})
        labels.append({"left_id": f"left-{i}", "right_id": f"right-{i}", "left_version": f"left-{i}-v1",
            "right_version": f"right-{i}-v1", "label": i % 2, "provenance": [{"decision_id": f"decision-{i}",
            "reviewer": "synthetic", "policy_version": "fixture", "event_seq": i + 1}]})
    return {"records": records, "labels": labels, "revision_id": "synthetic", "artifact_sha256": "a" * 64,
            "event_cutoff": groups, "input_hash": "b" * 64, "summary": {"valid_labels": groups}}


def test_split_groups_shared_endpoints_and_rejects_contradictory_positive_closure():
    snapshot = fixture_snapshot()
    split = split_learning_snapshot(snapshot)
    endpoint_splits = {}
    for name, labels in split["labels"].items():
        for pair in labels:
            for key in (pair["left_id"], pair["right_id"]):
                assert endpoint_splits.setdefault(key, name) == name
    assert set(endpoint_splits) == {row["record_id"] for row in snapshot["records"]}
    bad = fixture_snapshot(2)
    bad["labels"].append(bad["labels"][0] | {"label": 1})
    with pytest.raises(ValueError, match="conflicting"):
        split_learning_snapshot(bad)


def test_sampling_is_deterministic_score_only_and_diverse():
    rows = fixture_snapshot(4)["records"]
    candidates = [{"left": "left-0", "right": "right-0", "score": 0.5},
                  {"left": "left-0", "right": "right-1", "score": 0.501},
                  {"left": "left-2", "right": "right-2", "score": 0.51}]
    active = rank_review_candidates(rows, candidates, limit=3)
    assert (active[0]["left"], active[1]["left"]) == ("left-0", "left-2")
    assert active == rank_review_candidates(list(reversed(rows)), list(reversed(candidates)), limit=3)
    assert rank_review_candidates(rows, candidates, strategy="random", seed=91) == rank_review_candidates(rows, candidates, strategy="random", seed=91)
    with pytest.raises(ValueError, match="label"):
        rank_review_candidates(rows, [candidates[0] | {"label": 1}])


def test_small_export_is_auditable_but_training_refuses_insufficient_splits(tmp_path):
    store, rows = store_with_pairs(tmp_path, 2)
    decide(store, rows, 0, "accept")
    decide(store, rows, 1, "reject")
    output = tmp_path / "dataset"
    manifest = export_review_dataset(store, output)
    assert manifest["automatic_deployment"] is False
    assert verify_learning_dataset(output)["labels"] == 2
    with pytest.raises(ValueError, match="Insufficient"):
        train_candidate_model(output, tmp_path / "candidate")
    manifest_path = output / "manifest.json"
    contents = json.loads(manifest_path.read_text(encoding="utf-8"))
    path = output / next(iter(contents["files"]))
    with path.open("ab") as file:
        file.write(b"changed")
    with pytest.raises(ValueError, match="hash"):
        verify_learning_dataset(output)


def test_training_freezes_before_test_features_and_calibrates_on_a_separate_split(tmp_path, monkeypatch):
    from entitybridge import learning
    snapshot = fixture_snapshot(240)
    monkeypatch.setattr(learning, "snapshot_review_labels", lambda _store: snapshot)
    directory, output = tmp_path / "dataset", tmp_path / "trained"
    export_review_dataset(None, directory)
    original = learning.pq.read_table
    def checked_read(path, *args, **kwargs):
        if str(path).endswith("test.parquet") and not kwargs.get("columns"):
            assert (output / "frozen_config.json").exists()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(learning.pq, "read_table", checked_read)
    report = train_candidate_model(directory, output, calibrate=True)
    assert report["automatic_deployment"] is False
    config = json.loads((output / "frozen_config.json").read_text(encoding="utf-8"))
    assert config["calibration"]["split"] == "calibration"
    assert config["calibration"]["deployment_calibrated"] is False
    assert config["validation_selection"]["selected_threshold"] is not None
    model = json.loads((output / "model/classifier.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert model["training_pairs"] == manifest["statistics"]["train"]["pairs"]
    assert report["test"]["end_to_end"]["tp"] > 0


def test_pending_revoke_and_corrupted_published_artifact_cannot_export_labels(tmp_path):
    store, rows = store_with_pairs(tmp_path, 1)
    accepted = decide(store, rows, 0, "accept")
    parent = store.current_revision()
    preview = store.revoke_preview(accepted["decision_id"], base_revision=parent)
    candidate = store.revoke(accepted["decision_id"], base_revision=parent, reviewer="synthetic",
        reason="Pending withdrawal", preview_cutoff=preview["event_cutoff"])
    with pytest.raises(ValueError, match="unpublished"):
        snapshot_review_labels(store)
    store.publish(candidate["revision_id"], expected_parent=parent)
    row = next(row for row in store.history() if row["revision_id"] == store.current_revision())
    with (store.artifact_root / row["artifact_path"]).open("ab") as file:
        file.write(b"corrupt")
    with pytest.raises(ValueError, match="hash"):
        snapshot_review_labels(store)


def test_transitive_positive_labels_cannot_hide_a_negative_in_the_same_component():
    snapshot = fixture_snapshot(2)
    by_id = {item["record_id"]: item for item in snapshot["records"]}
    def pair(a, b, label):
        return {"left_id": a, "right_id": b, "left_version": by_id[a]["record_version_id"],
            "right_version": by_id[b]["record_version_id"], "label": label, "provenance": [{"reviewer": "synthetic"}]}
    snapshot["labels"] = [pair("left-0", "right-0", 1), pair("left-1", "right-0", 1),
                          pair("left-1", "right-1", 1), pair("left-0", "right-1", 0)]
    with pytest.raises(ValueError, match="positive-label closure"):
        split_learning_snapshot(snapshot)


def test_active_queue_requires_real_scores_and_respects_exclusions():
    rows = fixture_snapshot(2)["records"]
    candidates = [{"left": "left-0", "right": "right-0", "score": 0.5},
                  {"left": "left-1", "right": "right-1", "score": 0.9}]
    ranked = rank_review_candidates(rows, candidates, excluded_pairs=[("right-0", "left-0")])
    assert len(ranked) == 1 and ranked[0]["left"] == "left-1"
    for invalid in (float("nan"), -0.1, 1.1, True):
        with pytest.raises(ValueError, match="finite actual scores"):
            rank_review_candidates(rows, [candidates[0] | {"score": invalid}], strategy="random")


def test_simulation_reads_only_public_train_and_freezes_each_inner_test_checkpoint(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    import pyarrow as pa
    import pyarrow.parquet as pq

    from entitybridge.normalization import MATCHER_COLUMNS
    spec = importlib.util.spec_from_file_location("check_learning_loop", Path(__file__).parents[1] / "scripts/check_learning_loop.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    snapshot = fixture_snapshot(240)
    dataset, output = tmp_path / "benchmark", tmp_path / "simulation"
    (dataset / "matcher").mkdir(parents=True)
    (dataset / "evaluator").mkdir()
    (dataset / "manifest.json").write_text("{}", encoding="utf-8")
    pq.write_table(pa.Table.from_pylist(snapshot["records"], schema=pa.schema([(key, pa.string()) for key in MATCHER_COLUMNS])),
        dataset / "matcher/train.parquet")
    pq.write_table(pa.Table.from_pylist([item | {"split": "train"} for item in snapshot["labels"]]),
        dataset / "evaluator/labelled_pairs.parquet")
    original, checkpoint_reads = pq.read_table, []
    def checked_read(path, *args, **kwargs):
        path = Path(path)
        if path == dataset / "evaluator/labelled_pairs.parquet":
            assert kwargs["filters"] == [("split", "=", "train")]
        assert path != dataset / "matcher/test.parquet"
        assert path != dataset / "matcher/validation.parquet"
        if path.name == "test.parquet" and not kwargs.get("columns"):
            frozen = [file for file in output.glob("seed_*/*/budget_*/frozen_config.json")
                      if json.loads(file.read_text(encoding="utf-8"))["status"] == "frozen"]
            assert len(frozen) == len(checkpoint_reads) + 1
            assert (output / "plan.json").exists()
            checkpoint_reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(pq, "read_table", checked_read)
    report = script.run_simulation(dataset, output, budgets=(10, 20), seeds=(11, 29), batch_size=5)
    assert report["original_public_test_read"] is False
    assert report["winner_selected"] is False
    assert len(checkpoint_reads) == sum(item["status"] == "evaluated" for item in report["checkpoints"])
    assert any(item["status"] == "insufficient_training_classes" for item in report["checkpoints"])
    for seed in (11, 29):
        prefixes = []
        for method in script.METHODS:
            traces = []
            for budget in (10, 20):
                trace = json.loads((output / f"seed_{seed}/{method}/budget_{budget}/acquisitions.json").read_text(encoding="utf-8"))
                assert len(trace) == budget
                assert len({tuple(item["pair"]) for item in trace}) == budget
                traces.append(trace)
            assert traces[0] == traces[1][:10]
            prefixes.append(traces[0])
        assert prefixes[0] == prefixes[1]
