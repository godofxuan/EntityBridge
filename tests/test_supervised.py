import json

import pytest

from entitybridge.supervised import FrozenPairClassifier, pair_features


def rows():
    return [{"record_id": key, "record_version_id": key + "-v", "source": source,
             "name": name, "address": address, "city": None, "postcode": None, "country": "GB"}
            for key, source, name, address in [("a", "left", "ALPHA LAB", "1 ROAD"),
                ("b", "right", "ALPHA LABS", "1 ROAD"), ("c", "left", "BETA WORKS", "2 ROAD"),
                ("d", "right", "BETA WORKS", "2 ROAD")]]


def labels():
    return [{"left_id": a, "right_id": b, "label": value, "split": "train"}
            for a, b, value in [("a", "b", 1), ("c", "d", 1), ("a", "d", 0), ("c", "b", 0)]]


def test_frozen_supervised_scorer_is_order_stable_reloadable_and_ignores_ids_as_features(tmp_path):
    model = FrozenPairClassifier.fit(rows(), labels())
    reversed_model = FrozenPairClassifier.fit(list(reversed(rows())), list(reversed(labels())))
    assert model.fingerprint == reversed_model.fingerprint
    candidates = [{"left": "a", "right": b, "rules": ["fixture"]} for b in ("b", "d")]
    scores = model.score(rows(), candidates)
    assert scores[0]["score"] > scores[1]["score"]
    left, right = rows()[:2]
    assert pair_features(left, right) == pair_features(left | {"record_id": "hidden"}, right)
    model.save(tmp_path / "model")
    restored = FrozenPairClassifier.load(tmp_path / "model")
    assert restored.score(rows(), candidates) == scores
    exposed = model.metadata
    exposed["coefficients"][0] = 999
    assert model.metadata["coefficients"][0] != 999
    path = tmp_path / "model/classifier.json"
    corrupt = json.loads(path.read_text())
    corrupt["intercept"] += 1
    path.write_text(json.dumps(corrupt))
    with pytest.raises(ValueError, match="hash"):
        FrozenPairClassifier.load(tmp_path / "model")


def test_supervised_training_rejects_test_labels_missing_endpoints_and_answer_columns():
    with pytest.raises(ValueError, match="train labels"):
        FrozenPairClassifier.fit(rows(), labels(), split="test")
    with pytest.raises(ValueError, match="Non-training"):
        FrozenPairClassifier.fit(rows(), [labels()[0] | {"split": "test"}])
    with pytest.raises(ValueError, match="endpoints"):
        FrozenPairClassifier.fit(rows(), [labels()[0] | {"right_id": "held-out"}])
    with pytest.raises(ValueError, match="allowlist"):
        FrozenPairClassifier.fit([row | {"true_entity_id": "answer"} for row in rows()], labels())
