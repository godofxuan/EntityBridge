"""Frozen company features inspired by ING EMM, using cleanco's legal forms.

Models rank human-review candidates. Sample probabilities are not calibrated for
deployment. Safe JSON inference avoids executing pickle from model bundles.
"""
from __future__ import annotations

import copy
import importlib.metadata
import json
import math
from collections import Counter
from functools import cached_property
from pathlib import Path

import numpy as np
from cleanco import basename
from rapidfuzz.distance import JaroWinkler
from rapidfuzz.fuzz import ratio, token_set_ratio

from .candidates import validate_records
from .country import country_fingerprint, country_key
from .matching import _edge
from .normalization import normalize_text
from .store import digest
from .supervised import FEATURE_NAMES as BASIC_FEATURES
from .supervised import FrozenPairClassifier, pair_features

VERSION = "company-features-json-v1"
EXTRA_FEATURES = ("base_ratio", "base_token_set", "base_exact", "base_jaro_winkler",
                  "base_token_jaccard", "base_idf_jaccard", "base_abbreviation",
                  "base_length_ratio", "legal_form_removed_either", "legal_form_removed_both",
                  "numeric_tokens_conflict")
VARIANTS = ("iso_lr", "company_lr", "company_gb")


def dependency_versions():
    return {name: importlib.metadata.version(name) for name in ("cleanco", "pycountry", "rapidfuzz")}


def _sigmoid(value):
    return 1 / (1 + math.exp(-value)) if value >= 0 else math.exp(value) / (1 + math.exp(value))


def _base(name):
    # Two bounded suffix-removal passes handle nested legal endings. Original
    # names remain in basic features; stripping a legal form is never a rule.
    return normalize_text(basename(basename(name or ""))) or ""


def _abbreviation(a, b):
    def direction(short, long):
        initials = "".join(word[0] for word in long.split())
        return len(long.split()) >= 2 and any(2 <= len(word) <= 5 and word == initials for word in short.split())
    return float(direction(a, b) or direction(b, a))


def company_features(left, right, *, idf=None, default_idf=1.0, extended=True, bases=None):
    result = pair_features(left, right)
    a, b = country_key(left.get("country")), country_key(right.get("country"))
    result[-1] = float(bool(a and b and a != b))
    if not extended:
        return result
    a, b = bases if bases is not None else (_base(left.get("name")), _base(right.get("name")))
    sa, sb = set(a.split()), set(b.split())
    union, common = sa | sb, sa & sb
    weights = {token: (idf or {}).get(token, default_idf) for token in union}
    digits_a = {word for word in sa if any(c.isdigit() for c in word)}
    digits_b = {word for word in sb if any(c.isdigit() for c in word)}
    removed_a = bool(left.get("name") and a != left["name"])
    removed_b = bool(right.get("name") and b != right["name"])
    return result + [ratio(a, b) / 100 if a and b else 0,
        token_set_ratio(a, b) / 100 if a and b else 0, float(bool(a and a == b)),
        JaroWinkler.normalized_similarity(a, b) if a and b else 0,
        len(common) / len(union) if union else 0,
        sum(weights[w] for w in common) / sum(weights.values()) if union else 0,
        _abbreviation(a, b), min(len(a), len(b)) / max(len(a), len(b)) if a and b else 0,
        float(removed_a or removed_b), float(removed_a and removed_b),
        float(bool(digits_a and digits_b and digits_a != digits_b))]


class FrozenCompanyMatcher:
    def __init__(self, state):
        self._state = copy.deepcopy(state)

    @cached_property
    def fingerprint(self):
        return digest(self._state)

    @property
    def metadata(self):
        return copy.deepcopy({k: v for k, v in self._state.items() if k not in {"idf", "trees"}})

    @classmethod
    def fit(cls, records, labelled_pairs, *, variant="company_lr", split="train"):
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.linear_model import LogisticRegression
        if variant not in VARIANTS or split != "train":
            raise ValueError("A declared variant and train split are required")
        rows = {r["record_id"]: r for r in validate_records(records)}
        if not rows or len(rows) > 20_000 or any(len(r.get("name") or "") > 1024 for r in rows.values()):
            raise ValueError("Company training record/name budget exceeded")
        labels = {}
        for row in labelled_pairs:
            if row.get("split", "train") != "train":
                raise ValueError("Company fitting accepts training labels only")
            pair = tuple(sorted((row["left_id"], row["right_id"])))
            label = row["label"]
            if (type(label) is not int or label not in (0, 1) or pair[0] == pair[1]
                    or any(k not in rows for k in pair) or rows[pair[0]]["source"] == rows[pair[1]]["source"]):
                raise ValueError("Explicit binary cross-source train pairs with known endpoints required")
            if pair in labels and labels[pair] != label:
                raise ValueError("Contradictory labels")
            labels[pair] = label
        if set(labels.values()) != {0, 1}:
            raise ValueError("Both training classes required")
        bases = {key: _base(r.get("name")) for key, r in rows.items()}
        counts = Counter(word for base in bases.values() for word in set(base.split()))
        idf = {word: math.log((len(rows) + 1) / (count + 1)) + 1 for word, count in sorted(counts.items())}
        default_idf = math.log(len(rows) + 1) + 1
        pairs = sorted(labels)
        features = [company_features(rows[a], rows[b], idf=idf, default_idf=default_idf,
            extended=variant != "iso_lr", bases=(bases[a], bases[b])) for a, b in pairs]
        y = [labels[p] for p in pairs]
        state = {"version": VERSION, "variant": variant, "domain": "company", "review_only": True,
            "features": list(BASIC_FEATURES + (() if variant == "iso_lr" else EXTRA_FEATURES)),
            "idf": idf, "default_idf": default_idf, "dependencies": dependency_versions(),
            "country_fingerprint": country_fingerprint(), "training_pairs": len(pairs),
            "training_records": len(rows), "training_positives": sum(y),
            "training_features_sha256": digest(sorted(rows.values(), key=lambda r: r["record_id"])),
            "training_labels_sha256": digest([[*p, labels[p]] for p in pairs]),
            "sklearn_version": importlib.metadata.version("scikit-learn"),
            "calibration": {"slope": 1.0, "intercept": 0.0, "status": "identity_uncalibrated"}}
        if variant == "company_gb":
            model = GradientBoostingClassifier(n_estimators=100, max_depth=2, min_samples_leaf=10,
                learning_rate=.05, random_state=20260915)
            model.fit(features, y)
            prior = float(model.init_.class_prior_[1])
            state.update(intercept=math.log(prior / (1 - prior)), learning_rate=.05, trees=[])
            for estimator in model.estimators_[:, 0]:
                t = estimator.tree_
                state["trees"].append({"left": t.children_left.tolist(), "right": t.children_right.tolist(),
                    "feature": t.feature.tolist(), "threshold": t.threshold.tolist(),
                    "value": t.value[:, 0, 0].tolist()})
        else:
            model = LogisticRegression(C=1., solver="lbfgs", max_iter=1000, random_state=20260915)
            model.fit(features, y)
            state.update(intercept=float(model.intercept_[0]), coefficients=model.coef_[0].tolist())
        result = cls(state)
        reference = model.predict_proba(features)[:, 1]
        restored = np.array([_sigmoid(result._logit(values)) for values in features])
        if not np.allclose(restored, reference, rtol=0, atol=1e-12):
            raise ValueError("JSON inference differs from fitted estimator")
        return result

    def _logit(self, features):
        state = self._state
        value = state["intercept"]
        if "coefficients" in state:
            return value + math.fsum(c * x for c, x in zip(state["coefficients"], features, strict=True))
        features = np.asarray(features, dtype=np.float32)
        for tree in state["trees"]:
            node = 0
            while tree["left"][node] != -1:
                node = (tree["left"][node] if features[tree["feature"][node]] <= tree["threshold"][node]
                        else tree["right"][node])
            value += state["learning_rate"] * tree["value"][node]
        return value

    def score(self, records, candidates, *, calibrated=True):
        rows = {r["record_id"]: r for r in validate_records(records)}
        bases = {key: _base(r.get("name")) for key, r in rows.items()}
        output = []
        for candidate in candidates:
            a, b = candidate["left"], candidate["right"]
            features = company_features(rows[a], rows[b], idf=self._state["idf"],
                default_idf=self._state["default_idf"], extended=self._state["variant"] != "iso_lr",
                bases=(bases[a], bases[b]))
            logit = self._logit(features)
            c = self._state["calibration"]
            score = _sigmoid(c["slope"] * logit + c["intercept"] if calibrated else logit)
            edge = _edge(candidate, rows, score, VERSION)
            edge["auto_merge"] = False
            edge["evidence"].update(pair_features=dict(zip(self._state["features"], features, strict=True)),
                country_comparison={"left": country_key(rows[a].get("country")),
                    "right": country_key(rows[b].get("country"))},
                calibration_status=c["status"], model_fingerprint=self.fingerprint)
            output.append(edge)
        return output

    def with_calibration(self, calibration):
        return type(self)(self._state | {"calibration": calibration})

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "company_model.json").write_text(json.dumps(self._state, sort_keys=True,
            ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        (directory / "manifest.json").write_text(json.dumps({"format": VERSION, "domain": "company",
            "fingerprint": self.fingerprint}), encoding="utf-8")

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        path = directory / "company_model.json"
        if path.stat().st_size > 10_000_000:
            raise ValueError("Company model size budget exceeded")
        state = json.loads(path.read_text(encoding="utf-8"))
        model = cls(state)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if (manifest.get("fingerprint") != model.fingerprint or state.get("version") != VERSION
                or state.get("variant") not in VARIANTS or state.get("review_only") is not True
                or state.get("domain") != "company"):
            raise ValueError("Company model hash/version/domain mismatch")
        expected = list(BASIC_FEATURES + (() if state["variant"] == "iso_lr" else EXTRA_FEATURES))
        if state.get("features") != expected or state.get("dependencies") != dependency_versions() or state.get(
                "country_fingerprint") != country_fingerprint():
            raise ValueError("Company feature/dependency schema mismatch")
        numbers = [state["intercept"], state["default_idf"], *state["idf"].values(),
                   state["calibration"]["slope"], state["calibration"]["intercept"]]
        if "coefficients" in state:
            if state["variant"] == "company_gb" or len(state["coefficients"]) != len(expected):
                raise ValueError("Company coefficient schema mismatch")
            numbers.extend(state["coefficients"])
        else:
            if state["variant"] != "company_gb" or len(state["trees"]) != 100:
                raise ValueError("Company tree count mismatch")
            numbers.append(state["learning_rate"])
            for t in state["trees"]:
                n = len(t["left"])
                if not 1 <= n <= 7 or any(len(t[k]) != n for k in ("right", "feature", "threshold", "value")):
                    raise ValueError("Invalid company tree size")
                for i, (left, right, feature) in enumerate(zip(t["left"], t["right"], t["feature"], strict=True)):
                    if (type(left) is not int or type(right) is not int or type(feature) is not int
                            or not ((left == right == -1) or (i < left < n and i < right < n
                                                                             and 0 <= feature < len(expected)))):
                        raise ValueError("Invalid or cyclic company tree")
                numbers.extend(t["threshold"] + t["value"])
        if not all(type(v) in (int, float) and math.isfinite(v) for v in numbers) or state["calibration"]["slope"] <= 0:
            raise ValueError("Invalid company numeric state")
        return model


def fit_platt(labels, scores):
    """One monotonic mapping on a separate validation-calibration partition."""
    from sklearn.linear_model import LogisticRegression
    if any(r.get("split") != "validation" for r in labels):
        raise ValueError("Calibration requires validation labels")
    counts = Counter(r["label"] for r in labels)
    identity = {"slope": 1., "intercept": 0., "status": "identity_insufficient_support"}
    if min(counts.get(0, 0), counts.get(1, 0)) < 20:
        return identity
    x = []
    for row in labels:
        pair = tuple(sorted((row["left_id"], row["right_id"])))
        p = min(1 - 1e-12, max(1e-12, scores[pair]))
        x.append([math.log(p / (1 - p))])
    model = LogisticRegression(C=1., solver="lbfgs", max_iter=1000, random_state=20260915)
    model.fit(x, [r["label"] for r in labels])
    slope = float(model.coef_[0, 0])
    if slope <= 0:
        return identity | {"status": "identity_nonmonotonic_fit"}
    return {"slope": slope, "intercept": float(model.intercept_[0]), "status": "heldout_platt",
            "calibration_pairs": len(labels), "calibration_labels_sha256": digest(labels)}


def calibrate_scores(scores, calibration):
    result = {}
    for key, value in scores.items():
        p = min(1 - 1e-12, max(1e-12, value))
        result[key] = _sigmoid(calibration["slope"] * math.log(p / (1 - p)) + calibration["intercept"])
    return result


def load_study_model(directory, name):
    return (FrozenPairClassifier.load(directory / name) if name == "legacy_lr"
            else FrozenCompanyMatcher.load(directory / name))
