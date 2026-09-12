"""A scorer spy proves test access is gated by frozen models and both policies."""
import importlib.util
import json
from pathlib import Path

import pytest
from test_wdc_benchmark import fixture_rows

from entitybridge.ditto import file_hash
from entitybridge.ditto_training import select_policies
from entitybridge.wdc_benchmark import adapt_rows


@pytest.mark.parametrize("overlap", [False, True])
def test_neural_runner_freezes_before_test_and_rejects_overlap(tmp_path, monkeypatch, overlap):
    root = Path(__file__).parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    spec = importlib.util.spec_from_file_location("ditto_runner_contract", root / "scripts/run_ditto_benchmark.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    base = tmp_path / "base"
    base.mkdir()
    (base / "model.safetensors").write_bytes(b"synthetic scorer spy, not trained weights")
    monkeypatch.setattr(runner, "ROBERTA_SHA", file_hash(base / "model.safetensors"))
    # This test checks orchestration without importing Torch or claiming quality.
    monkeypatch.setattr(runner.importlib.metadata, "version", lambda _: "synthetic-contract-test")
    monkeypatch.setattr(runner, "verify_archives", lambda _: {"fixture": "synthetic"})
    output = tmp_path / "result"
    reads, test_scores = [], []
    def read(_directory, split):
        if split == "test":
            frozen = json.loads((output / "frozen_config.json").read_text(encoding="utf-8"))
            assert set(frozen["validation_policies"]) == {"exact", "fuzzy", "title_logistic", "product_logistic", "ditto"}
            assert all(set(value) == {"f1", "cost_10_1"} for value in frozen["validation_policies"].values())
            assert frozen["training"]["model_fingerprint"] == "synthetic-spy"
            assert (output / "training/model/spied-checkpoint").exists()
        reads.append(split)
        offset = {"train": 0, "validation": 1000, "test": 0 if overlap else 2000}[split]
        return adapt_rows(fixture_rows(offset), split=split) | {"source": {"fixture": True}}
    monkeypatch.setattr(runner, "read_split", read)
    def fit(records, labels, validation_records, validation_labels, *, output, **kwargs):
        assert reads == ["train", "validation"]
        assert {r["split"] for r in labels} == {"train"}
        assert {r["split"] for r in validation_labels} == {"validation"}
        (output / "model").mkdir(parents=True)
        (output / "model/spied-checkpoint").write_bytes(b"synthetic")
        scores = {tuple(sorted((r["left_id"], r["right_id"]))): .5 for r in validation_labels}
        return {"model_fingerprint": "synthetic-spy", "validation_policies": select_policies(validation_labels, scores)}
    monkeypatch.setattr(runner, "fit_ditto", fit)
    class Spy:
        fingerprint = "synthetic-spy"
        @classmethod
        def load(cls, *args, **kwargs):
            return cls()
        def score_pairs(self, records, pairs):
            assert reads == ["train", "validation", "test"]
            assert not overlap
            test_scores.append(True)
            return {pair: .5 for pair in pairs}
    monkeypatch.setattr(runner, "DittoMatcher", Spy)
    if overlap:
        with pytest.raises(ValueError, match="split overlap"):
            runner.run(tmp_path, base, output, device="cpu")
        assert not test_scores and not (output / "report.json").exists()
    else:
        result = runner.run(tmp_path, base, output, device="cpu")
        assert test_scores == [True] and result["test_status"] == "exploratory-replayed"
        for methods in result["methods"].values():
            for policy in methods.values():
                assert policy["intervals"]["status"] == "insufficient_independent_groups"
