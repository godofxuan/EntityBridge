import pytest

from entitybridge.evaluation import evaluate_clusters, evaluate_pairs


def test_candidate_miss_stays_in_end_to_end_recall_denominator():
    truth = {"a": "one", "b": "one", "c": "two", "d": "two"}
    result = evaluate_pairs([("a", "b")], [("a", "b")], truth)
    assert result["candidate_recall"] == 0.5
    assert result["conditional"]["recall"] == 1.0
    assert result["end_to_end"]["recall"] == 0.5
    clusters = evaluate_clusters([("a", "b", "c"), ("d",)], truth)
    assert clusters["overmerged_clusters"] == 1
    assert clusters["records_in_overmerged_clusters"] == 3


def test_cluster_metrics_separate_overmerge_fragmentation_and_exact_recovery():
    truth = {"a": 1, "b": 1, "c": 2, "d": 2, "e": 3}
    result = evaluate_clusters([("a", "b", "c"), ("d",), ("e",)], truth)
    assert result["cluster_pairwise"]["tp"] == 1
    assert result["cluster_pairwise"]["fp"] == 2
    assert result["cluster_pairwise"]["fn"] == 1
    assert result["split_true_entities"] == 1
    assert result["exact_entity_recovery"] == pytest.approx(1 / 3)
    assert 0 < result["b_cubed_f1"] < 1
    correct = evaluate_clusters([("a", "b"), ("c", "d"), ("e",)], truth)
    assert correct["b_cubed_f1"] == correct["exact_entity_recovery"] == 1


@pytest.mark.parametrize("partitions", [[("a", "a"), ("b",)], [(), ("a", "b")], [("a", "unknown")]])
def test_cluster_evaluator_refuses_invalid_partition_members(partitions):
    with pytest.raises(ValueError):
        evaluate_clusters(partitions, {"a": "one", "b": "one"})


def test_preflight_refuses_entity_leakage_and_tampered_files(tmp_path):
    import hashlib
    import json

    import pyarrow as pa
    import pyarrow.parquet as pq
    import pytest

    from entitybridge.evaluation import verify_dataset
    from entitybridge.normalization import MATCHER_COLUMNS
    (tmp_path / "matcher").mkdir()
    (tmp_path / "evaluator").mkdir()
    truth = []
    for split in ("train", "validation", "test"):
        row = dict.fromkeys(MATCHER_COLUMNS)
        row.update(record_id=split, record_version_id=split, source="gleif", name="ALPHA")
        pq.write_table(pa.Table.from_pylist([row]), tmp_path / "matcher" / f"{split}.parquet")
        truth.append({"record_id": split, "true_entity_id": "same-entity", "split": split})
    pq.write_table(pa.Table.from_pylist(truth), tmp_path / "evaluator/truth_map.parquet")
    files = {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*.parquet")}
    (tmp_path / "manifest.json").write_text(json.dumps({"files": files, "records": 3}))
    with pytest.raises(ValueError, match="entity.*split"):
        verify_dataset(tmp_path)
    files["matcher/test.parquet"] = "wrong"
    (tmp_path / "manifest.json").write_text(json.dumps({"files": files, "records": 3}))
    with pytest.raises(ValueError, match="hash"):
        verify_dataset(tmp_path)
