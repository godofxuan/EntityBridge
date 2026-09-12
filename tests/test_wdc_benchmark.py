"""WDC evaluator separation, declared corrections and freeze-before-test checks."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from entitybridge.normalization import MATCHER_COLUMNS
from entitybridge.supervised import FrozenPairClassifier
from entitybridge.wdc_benchmark import (
    PRODUCT_FIELDS,
    FrozenProductClassifier,
    adapt_rows,
    corrected_validation,
    product_features,
    require_disjoint,
    split_overlap,
)


def fixture_rows(offset=0, products=10):
    offers = {}
    for i in range(products):
        for number in (offset + 2 * i, offset + 2 * i + 1):
            offers[number] = {"id": number, "cluster_id": offset + i,
                "title": f"Camera product {i}", "brand": "Synthetic", "description": f"Model {i} camera with a lens",
                "price": str(10 + i), "priceCurrency": "USD"}
    rows = []
    for i in range(products):
        for right, label in ((offset + 2 * i + 1, 1), (offset + 2 * ((i + 1) % products) + 1, 0)):
            left = offset + 2 * i
            rows.append({**{f"{key}_left": value for key, value in offers[left].items()},
                         **{f"{key}_right": value for key, value in offers[right].items()},
                         "pair_id": f"{left}#{right}", "label": label, "is_hard_negative": bool(not label and i % 2)})
    return rows


def test_wdc_projects_two_strict_feature_views_without_inventing_shops():
    raw = fixture_rows()
    data = adapt_rows(raw, split="train")
    assert all(set(row) == set(MATCHER_COLUMNS) for row in data["records"])
    assert {row["source"] for row in data["records"]} == {"wdc-products-offer-pool"}
    assert all(set(row) == {"record_id", *PRODUCT_FIELDS} for row in data["product_records"])
    assert data["statistics"]["dependency_groups"] == 1
    changed = copy.deepcopy(raw)
    for row in changed:
        row["cluster_id_left"] += 10000
        row["cluster_id_right"] += 10000
    alternate = adapt_rows(changed, split="train")
    assert alternate["records"] == data["records"]
    assert alternate["product_records"] == data["product_records"]
    bad = copy.deepcopy(raw)
    bad[0]["label"] = 0
    with pytest.raises(ValueError, match="contradicts"):
        adapt_rows(bad, split="train")
    with pytest.raises(ValueError, match="duplicate unordered"):
        adapt_rows(raw + [raw[0]], split="train")


def test_same_pool_title_classifier_requires_explicit_opt_in_and_old_models_load(tmp_path):
    data = adapt_rows(fixture_rows(), split="train")
    with pytest.raises(ValueError, match="cross-source"):
        FrozenPairClassifier.fit(data["records"], data["labels"])
    model = FrozenPairClassifier.fit(data["records"], data["labels"], allow_within_source=True)
    assert model.metadata["allow_within_source"] is True
    model.save(tmp_path / "new")
    assert FrozenPairClassifier.load(tmp_path / "new").fingerprint == model.fingerprint
    # Each supplied pair connects even left IDs with odd right IDs in this fixture.
    cross = [row | {"source": str(int(row["record_id"].split(":")[1]) % 2)} for row in data["records"]]
    default = FrozenPairClassifier.fit(cross, data["labels"])
    assert "allow_within_source" not in default.metadata
    default.save(tmp_path / "old")
    assert FrozenPairClassifier.load(tmp_path / "old").metadata == default.metadata


def test_product_price_feature_refuses_currency_conversion_or_guessing():
    base = {"title": "Camera", "brand": "Synthetic", "description": None, "price": "10.0", "priceCurrency": "USD"}
    assert product_features(base, base | {"price": "20"})[-2:] == [0.5, 1.0]
    for change in ({"priceCurrency": "EUR"}, {"priceCurrency": None}, {"price": "$10"}, {"price": "10,00"},
                   {"price": "0"}, {"price": "-2"}, {"price": "NaN"}, {"price": "1e1000"}):
        assert product_features(base, base | change)[-2:] == [0, 0.0]
    assert product_features(base, base | {"cluster_id": 42, "label": 0, "is_hard_negative": True}) == product_features(base, base)


def test_product_model_cannot_fit_validation_or_evaluator_columns():
    data = adapt_rows(fixture_rows(), split="train")
    model = FrozenProductClassifier.fit(data["product_records"], data["labels"])
    assert model.state["automatic_deployment"] is False
    assert len(model.state["coefficients"]) == 11
    with pytest.raises(ValueError, match="train pairs"):
        FrozenProductClassifier.fit(data["product_records"], [row | {"split": "validation"} for row in data["labels"]])
    with pytest.raises(ValueError, match="only record_id"):
        FrozenProductClassifier.fit([row | {"label": 1} for row in data["product_records"]], data["labels"])


def test_correction_only_removes_validation_pairs_touching_train_products():
    train = adapt_rows(fixture_rows(), split="train")
    raw_validation = fixture_rows(1000)
    # Deliberately recreate one training product ID with different offer IDs.
    for row in raw_validation:
        for side in ("left", "right"):
            if row[f"cluster_id_{side}"] == 1000:
                row[f"cluster_id_{side}"] = 0
    validation = adapt_rows(raw_validation, split="validation")
    overlap = split_overlap({"train": train, "validation": validation})
    assert overlap["train:validation"]["shared_products"] == 1
    assert overlap["train:validation"]["shared_offers"] == 0
    with pytest.raises(ValueError, match="split overlap"):
        require_disjoint({"train": train, "validation": validation})
    corrected, audit = corrected_validation(train, validation)
    assert audit["removed_positive"] == 1
    assert audit["removed_negative"] == 2
    assert audit["retained_statistics"]["pairs"] == 17
    assert audit["test_used_for_correction"] is False
    assert require_disjoint({"train": train, "validation": corrected})["train:validation"]["shared_products"] == 0


def runner_module():
    spec = importlib.util.spec_from_file_location("run_wdc_benchmark", Path(__file__).parents[1] / "scripts/run_wdc_benchmark.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_archives_code_models_and_thresholds_before_first_test_read(tmp_path, monkeypatch):
    runner = runner_module()
    output = tmp_path / "results"
    reads = []
    def fake_read(_directory, split):
        if split == "test":
            assert not reads.count("test")
            config = json.loads((output / "frozen_config.json").read_text(encoding="utf-8"))
            assert set(config["validation_selection"]) == set(runner.METHODS)
            assert config["validation_selection"]["exact"]["selected_threshold"] in (1, None)
            assert (output / "title_model/classifier.json").exists()
            assert (output / "product_model/classifier.json").exists()
            assert (output / "source_archive/src/entitybridge/wdc_benchmark.py").exists()
        reads.append(split)
        result = adapt_rows(fixture_rows({"train": 0, "validation": 1000, "test": 2000}[split]), split=split)
        return result | {"source": {"fixture": True}}
    monkeypatch.setattr(runner, "read_split", fake_read)
    monkeypatch.setattr(runner, "verify_archives", lambda _directory: {"fixture": {"sha256": "a" * 64}})
    report = runner.run(tmp_path, output, test_status="first-scored-external-diagnostic")
    assert reads == ["train", "validation", "test"]
    assert report["candidate_recall_measured"] is False
    assert report["kind"] == "corrected-validation external diagnostic"
    for method in report["methods"].values():
        assert method["intervals"]["status"] == "insufficient_independent_groups"
        assert method["intervals"]["metrics"]["f1"]["interval"] is None
        assert method["negative_breakdown"]["hard_negative"]["pairs"] == 5


def test_runner_stops_without_scoring_or_trimming_overlapping_test(tmp_path, monkeypatch):
    runner = runner_module()
    output = tmp_path / "blocked"
    def fake_read(_directory, split):
        return adapt_rows(fixture_rows(1000 if split == "validation" else 0), split=split) | {"source": {"fixture": True}}
    monkeypatch.setattr(runner, "read_split", fake_read)
    monkeypatch.setattr(runner, "verify_archives", lambda _directory: {})
    original = runner._scores
    def checked_score(partition, *models):
        assert partition["labels"][0]["split"] != "test"
        return original(partition, *models)
    monkeypatch.setattr(runner, "_scores", checked_score)
    with pytest.raises(ValueError, match="split overlap"):
        runner.run(tmp_path, output, test_status="first-scored-external-diagnostic")
    audit = json.loads((output / "blocked_audit.json").read_text(encoding="utf-8"))
    assert audit["test_read"] is True and audit["test_scored"] is False
    assert audit["overlap"]["train:test"]["shared_products"] == 10
