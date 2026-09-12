"""A synthetic full runner test freezes all choices before test-feature access."""
import csv
import importlib.util
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from entitybridge.benchmarks import CATALOG, convert_benchmark, file_hash

SPEC = importlib.util.spec_from_file_location("public_runner", Path(__file__).parents[1] / "scripts/run_public_benchmark.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_runner_freezes_validation_thresholds_before_test_scoring_and_preserves_runs(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    records = [{"id": str(i), "name": f"Company {i}", "addr": f"{i} Example Road", "city": "Town"}
               for i in range(30)]
    labelled = [{"ltable_id": str(a), "rtable_id": str(b), "label": int(a == b)}
                for a in range(30) for b in range(30)]
    for filename, rows, fields in [
        ("tableA.csv", records, list(records[0])), ("tableB.csv", records, list(records[0])),
        ("train.csv", labelled, list(labelled[0])), ("valid.csv", [], list(labelled[0])),
        ("test.csv", [], list(labelled[0])),
    ]:
        with (raw / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    monkeypatch.setitem(CATALOG, "fodors_zagats", {**CATALOG["fodors_zagats"],
                        "source_sha256": {path.name: file_hash(path) for path in raw.glob("*.csv")}})
    dataset, output = tmp_path / "dataset", tmp_path / "run"
    convert_benchmark(raw, dataset, "fodors_zagats")
    original_read = pq.read_table
    feature_reads = []
    def guarded_read(path, *args, **kwargs):
        if Path(path) == dataset / "matcher/test.parquet" and kwargs.get("columns") is None:
            assert (output / "frozen_config.json").exists(), "Test features accessed before validation choices were frozen"
            feature_reads.append(path)
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(pq, "read_table", guarded_read)
    report = runner.run(dataset, output, bootstrap=20)
    assert len(feature_reads) == 1
    assert set(report["results"]) == {"fixed", "hybrid", "supplied_pairs_only"}
    assert set(report["results"]["fixed"]) == {"exact", "fuzzy", "splink", "supervised_logistic"}
    assert report["results"]["supplied_pairs_only"]["exact"]["candidate_known_positive_recall"] == 1
    assert report["results"]["fixed"]["exact"]["calibration"]["status"] != "computed"
    frozen = (output / "frozen_config.json").read_bytes()
    assert json.loads(frozen)["test_features_read_after_this_file"] is True
    with pytest.raises(ValueError, match="fresh output"):
        runner.run(dataset, output)
    assert (output / "frozen_config.json").read_bytes() == frozen
