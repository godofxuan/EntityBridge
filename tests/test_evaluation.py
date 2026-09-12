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
