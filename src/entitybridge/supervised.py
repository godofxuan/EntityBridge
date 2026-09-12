"""Training-label-only, interpretable pair baseline for independent benchmarks.

This is a benchmark scorer, not an automatic production merge policy. Probabilities
describe the sampled labelled-pair distribution and are not deployment-calibrated.
Only explicit train labels enter fit; score never accepts labels or entity groups.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path

from rapidfuzz.fuzz import ratio, token_set_ratio

from .candidates import validate_records
from .matching import _edge

FEATURE_NAMES = (
    "name_ratio", "name_token_set", "name_exact", "name_both_present",
    "address_ratio", "address_both_present", "city_exact", "city_both_present",
    "postcode_exact", "postcode_both_present", "country_conflict",
)
VERSION = "labelled-pair-logistic-fixed-c1-v1"


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def pair_features(left, right):
    """No identifiers/source keys/labels are used as model features."""
    a, b = left.get("name") or "", right.get("name") or ""
    c, d = left.get("address") or "", right.get("address") or ""
    city = bool(left.get("city") and right.get("city"))
    postcode = bool(left.get("postcode") and right.get("postcode"))
    country = bool(left.get("country") and right.get("country"))
    return [ratio(a, b) / 100 if a and b else 0,
            token_set_ratio(a, b) / 100 if a and b else 0,
            float(bool(a and a == b)), float(bool(a and b)),
            ratio(c, d) / 100 if c and d else 0, float(bool(c and d)),
            float(city and left["city"] == right["city"]), float(city),
            float(postcode and left["postcode"] == right["postcode"]), float(postcode),
            float(country and left["country"] != right["country"])]


class FrozenPairClassifier:
    def __init__(self, state):
        self._state = copy.deepcopy(state)

    @property
    def fingerprint(self):
        return hashlib.sha256(_json(self._state).encode()).hexdigest()

    @property
    def metadata(self):
        return copy.deepcopy(self._state)

    @classmethod
    def fit(cls, records, labelled_pairs, *, split="train", allow_within_source=False):
        from sklearn.linear_model import LogisticRegression
        if split != "train":
            raise ValueError("The supervised baseline may fit train labels only")
        if type(allow_within_source) is not bool:
            raise ValueError("allow_within_source must be an explicit boolean")
        rows = {row["record_id"]: row for row in validate_records(records)}
        labels = {}
        for item in labelled_pairs:
            if item.get("split", "train") != "train":
                raise ValueError("Non-training labels passed to fit")
            pair = tuple(sorted((item["left_id"], item["right_id"])))
            label = item["label"]
            if label not in (0, 1) or pair[0] == pair[1] or any(key not in rows for key in pair):
                raise ValueError("Expected explicit binary labels with known, distinct train endpoints")
            if not allow_within_source and rows[pair[0]]["source"] == rows[pair[1]]["source"]:
                raise ValueError("This baseline requires cross-source labelled pairs")
            if pair in labels and labels[pair] != label:
                raise ValueError("Contradictory pair labels")
            labels[pair] = int(label)
        if set(labels.values()) != {0, 1}:
            raise ValueError("Both positive and negative training labels are required")
        pairs = sorted(labels)
        features = [pair_features(rows[a], rows[b]) for a, b in pairs]
        model = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000, random_state=20260912)
        model.fit(features, [labels[pair] for pair in pairs])
        state = {"version": VERSION, "features": list(FEATURE_NAMES),
                 "coefficients": model.coef_[0].tolist(), "intercept": float(model.intercept_[0]),
                 "training_pairs": len(pairs), "training_positives": sum(labels.values()),
                 "training_records": len(rows), "C": 1.0, "class_weight": None,
                 "sklearn_version": importlib.metadata.version("scikit-learn"),
                 "training_features_sha256": hashlib.sha256(_json(sorted(rows.values(), key=lambda r: r["record_id"])).encode()).hexdigest(),
                 "training_labels_sha256": hashlib.sha256(_json([[*pair, labels[pair]] for pair in pairs]).encode()).hexdigest(),
                 "calibrated": False, "scope": "Sampled labelled benchmark pairs; not deployment probabilities"}
        if allow_within_source:
            state.update(allow_within_source=True, scope="Explicit supplied pairs from a genuine shared offer pool; not deployment probabilities")
        return cls(state)

    def score(self, records, candidates):
        rows = {row["record_id"]: row for row in validate_records(records)}
        output = []
        for candidate in candidates:
            left, right = rows[candidate["left"]], rows[candidate["right"]]
            values = pair_features(left, right)
            logit = self._state["intercept"] + sum(c * x for c, x in zip(self._state["coefficients"], values, strict=True))
            probability = 1 / (1 + math.exp(-logit)) if logit >= 0 else math.exp(logit) / (1 + math.exp(logit))
            edge = _edge(candidate, rows, probability, VERSION)
            edge["evidence"]["pair_features"] = dict(zip(FEATURE_NAMES, values, strict=True))
            output.append(edge)
        return output

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "classifier.json").write_text(_json(self._state) + "\n", encoding="utf-8")
        (directory / "manifest.json").write_text(_json({"fingerprint": self.fingerprint}) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        state = json.loads((directory / "classifier.json").read_text(encoding="utf-8"))
        model = cls(state)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("fingerprint") != model.fingerprint or state.get("version") != VERSION:
            raise ValueError("Supervised model hash/version mismatch")
        if state.get("features") != list(FEATURE_NAMES) or len(state["coefficients"]) != len(FEATURE_NAMES):
            raise ValueError("Supervised model feature schema mismatch")
        if not all(math.isfinite(value) for value in [*state["coefficients"], state["intercept"]]):
            raise ValueError("Supervised model has non-finite coefficients")
        return model
