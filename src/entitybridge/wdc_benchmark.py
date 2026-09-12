"""Strict WDC Products input boundary and fixed domain-specific pair baseline.

This diagnostic never creates fake shops, clusters entities or deploys a model.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
import zipfile
from collections import Counter
from pathlib import Path

from rapidfuzz.fuzz import ratio, token_set_ratio

from .benchmarks import _Components
from .normalization import MATCHER_COLUMNS, normalize_text
from .store import digest

VERSION = "wdc-products-80cc-small-corrected-validation-v1"
SOURCE_PAGE = "https://webdatacommons.org/largescaleproductcorpus/wdc-products/"
SOURCE_BASE = "https://data.dws.informatik.uni-mannheim.de/largescaleproductcorpus/data/wdc-products/"
ARCHIVES = {"80pair.zip": "b2044939cee5ea6f12148a2f3551508de3cb77660dfc91767c44daaf9d8a9c4a",
            "val_pair.zip": "15bb323e9aff4771be1baaeb048c9b6ef7459446ccbadca61cc9cb1272cb4143"}
MEMBERS = {"train": ("80pair.zip", None, "wdcproducts80cc20rnd000un_train_small.json.gz"),
           "validation": ("val_pair.zip", "80pair_addvalid.zip", "wdcproducts80cc20rnd100un_valid_small.json.gz"),
           "test": ("80pair.zip", None, "wdcproducts80cc20rnd100un_gs.json.gz")}
PRODUCT_FIELDS = ("title", "brand", "description", "price", "priceCurrency")
PRODUCT_FEATURES = ("title_ratio", "title_token_set", "title_exact", "title_both_present",
                    "brand_ratio", "brand_exact", "brand_both_present", "description_token_set", "description_both_present",
                    "price_ratio_same_currency", "price_comparable")


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_archives(directory):
    directory = Path(directory)
    hashes = {name: file_sha256(directory / name) for name in ARCHIVES}
    if hashes != ARCHIVES:
        raise ValueError("WDC source archive hash mismatch")
    return {name: {"sha256": value, "url": SOURCE_BASE + name, "bytes": (directory / name).stat().st_size}
            for name, value in hashes.items()}


def read_split(directory, split):
    """Read exactly the declared member; call for test only after model freeze."""
    if split not in MEMBERS:
        raise ValueError("Unsupported WDC partition")
    archive, nested, member = MEMBERS[split]
    with zipfile.ZipFile(Path(directory) / archive) as outer:
        if nested:
            with zipfile.ZipFile(io.BytesIO(outer.read(nested))) as inner:
                compressed = inner.read(member)
        else:
            compressed = outer.read(member)
    # Fixed small benchmark resource limits; never extract arbitrary zip paths.
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        content = stream.read(80_000_001)
    if len(content) > 80_000_000:
        raise ValueError("WDC member exceeds diagnostic size bound")
    lines = content.splitlines()
    if len(lines) > 25_000:
        raise ValueError("WDC partition exceeds diagnostic pair bound")
    result = adapt_rows([json.loads(line) for line in lines if line.strip()], split=split)
    result["source"] = {"archive": archive, "nested_archive": nested, "member": member,
                        "compressed_member_sha256": hashlib.sha256(compressed).hexdigest(),
                        "decompressed_sha256": hashlib.sha256(content).hexdigest()}
    return result


def adapt_rows(rows, *, split):
    """Separate official evaluator fields from two explicit feature views."""
    if split not in MEMBERS:
        raise ValueError("Unsupported WDC partition")
    features, truths, labels, seen = {}, {}, [], set()
    for original in rows:
        if type(original["label"]) is not int or original["label"] not in (0, 1) or type(original["is_hard_negative"]) is not bool:
            raise ValueError("WDC requires explicit binary labels and boolean negative type")
        endpoints = []
        for side in ("left", "right"):
            offer, cluster = original[f"id_{side}"], original[f"cluster_id_{side}"]
            if type(offer) is not int or type(cluster) is not int:
                raise ValueError("WDC offer and evaluator cluster identifiers must be integers")
            record_id = f"wdc-offer:{offer}"
            record = {"record_id": record_id, **{field: original[f"{field}_{side}"] for field in PRODUCT_FIELDS}}
            if any(value is not None and not isinstance(value, str) for field, value in record.items() if field != "record_id"):
                raise ValueError("WDC product fields must be strings or null")
            if record_id in features and features[record_id] != record:
                raise ValueError("WDC repeated offer has inconsistent descriptive values")
            if record_id in truths and truths[record_id] != str(cluster):
                raise ValueError("WDC repeated offer has inconsistent product cluster")
            features[record_id], truths[record_id] = record, str(cluster)
            endpoints.append(record_id)
        a, b = sorted(endpoints)
        if a == b or (a, b) in seen:
            raise ValueError("WDC duplicate unordered pair or self pair")
        expected_pair_id = f"{original['id_left']}#{original['id_right']}"
        if original["pair_id"] != expected_pair_id:
            raise ValueError("WDC pair identifier does not match endpoints")
        if original["label"] != int(truths[a] == truths[b]) or (original["label"] and original["is_hard_negative"]):
            raise ValueError("WDC explicit label contradicts supplied cluster or negative metadata")
        seen.add((a, b))
        labels.append({"left_id": a, "right_id": b, "label": original["label"], "split": split,
            "left_cluster_id": truths[a], "right_cluster_id": truths[b], "pair_id": original["pair_id"],
            "is_hard_negative": original["is_hard_negative"]})
    components = _Components(features)
    # A bootstrap draw must preserve both shared offer and known product effects.
    by_cluster = {}
    for key, cluster in truths.items():
        components.union(key, by_cluster.setdefault(cluster, key))
    for label in labels:
        components.union(label["left_id"], label["right_id"])
    for label in labels:
        label["group_id"] = digest(["wdc-shared-label-and-product-component", components.find(label["left_id"])])
    matcher = []
    for key, record in sorted(features.items()):
        item = dict.fromkeys(MATCHER_COLUMNS)
        item.update(record_id=key, record_version_id=digest(record), source="wdc-products-offer-pool", name=normalize_text(record["title"]))
        matcher.append(item)
    group_counts = Counter(label["group_id"] for label in labels)
    stats = {"records": len(features), "products": len(set(truths.values())), "pairs": len(labels),
        "positive": sum(item["label"] for item in labels), "negative": sum(1 - item["label"] for item in labels),
        "hard_negative": sum(not item["label"] and item["is_hard_negative"] for item in labels),
        "random_negative": sum(not item["label"] and not item["is_hard_negative"] for item in labels),
        "dependency_groups": len(group_counts), "largest_group_pairs": max(group_counts.values(), default=0),
        "largest_group_pair_fraction": max(group_counts.values(), default=0) / len(labels) if labels else None}
    return {"records": matcher, "product_records": [features[key] for key in sorted(features)],
            "labels": sorted(labels, key=lambda item: (item["left_id"], item["right_id"])), "truth": truths, "statistics": stats}


def split_overlap(partitions):
    result = {}
    names = list(partitions)
    for i, name in enumerate(names):
        a = partitions[name]["truth"]
        for other in names[i + 1:]:
            b = partitions[other]["truth"]
            offers, products = set(a) & set(b), set(a.values()) & set(b.values())
            result[f"{name}:{other}"] = {"shared_offers": len(offers), "shared_products": len(products),
                "shared_offer_ids_sha256": digest(sorted(offers)), "shared_product_ids_sha256": digest(sorted(products))}
    return result


def require_disjoint(partitions):
    audit = split_overlap(partitions)
    if any(item["shared_offers"] or item["shared_products"] for item in audit.values()):
        raise ValueError(f"WDC record/product split overlap: {json.dumps(audit, sort_keys=True)}")
    return audit


def corrected_validation(train, validation):
    """Declared correction: remove validation pairs touching training products.

    Never accepts or inspects test. Preserve the raw overlap audit separately;
    this protocol is intentionally not represented as the original paper setup.
    """
    train_products = set(train["truth"].values())
    product_records = {record["record_id"]: record for record in validation["product_records"]}
    retained, removed = [], []
    for label in validation["labels"]:
        target = removed if train_products.intersection((label["left_cluster_id"], label["right_cluster_id"])) else retained
        target.append(label)
    originals = []
    for label in retained:
        left, right = map(int, label["pair_id"].split("#"))
        original = {"pair_id": label["pair_id"], "label": label["label"], "is_hard_negative": label["is_hard_negative"]}
        for side, number in (("left", left), ("right", right)):
            key = f"wdc-offer:{number}"
            original[f"id_{side}"], original[f"cluster_id_{side}"] = number, int(validation["truth"][key])
            original.update({f"{field}_{side}": product_records[key][field] for field in PRODUCT_FIELDS})
        originals.append(original)
    result = adapt_rows(originals, split="validation")
    result["source"] = validation.get("source", {})
    before, after = validation["statistics"], result["statistics"]
    audit = {"protocol": "remove_validation_pairs_touching_training_product_ids_before_test",
        "original_statistics": before, "retained_statistics": after,
        "removed_positive": sum(item["label"] for item in removed), "removed_negative": sum(1 - item["label"] for item in removed),
        "removed_pairs_sha256": digest(sorted((item["left_id"], item["right_id"]) for item in removed)),
        "retained_pairs_sha256": digest(sorted((item["left_id"], item["right_id"]) for item in retained)),
        "original_positive_fraction": before["positive"] / before["pairs"] if before["pairs"] else None,
        "retained_positive_fraction": after["positive"] / after["pairs"] if after["pairs"] else None,
        "test_used_for_correction": False, "official_original_protocol": False}
    require_disjoint({"train": train, "validation": result})
    return result, audit


def product_features(left, right):
    """Fixed legitimate attributes only; no offer/cluster/negative flags."""
    def norm(record, key):
        return normalize_text(record.get(key)) or ""
    a, b = norm(left, "title"), norm(right, "title")
    c, d = norm(left, "brand"), norm(right, "brand")
    e, f = norm(left, "description"), norm(right, "description")
    def price(record):
        # No locale guessing, symbol stripping or currency conversion.
        raw = record.get("price")
        if not isinstance(raw, str) or not re.fullmatch(r"\s*\+?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*", raw):
            return None
        value = float(raw)
        return value if math.isfinite(value) and value > 0 else None
    p, q = price(left), price(right)
    currency_a, currency_b = norm(left, "priceCurrency"), norm(right, "priceCurrency")
    comparable = p is not None and q is not None and bool(currency_a) and currency_a == currency_b
    return [ratio(a, b) / 100 if a and b else 0, token_set_ratio(a, b) / 100 if a and b else 0,
        float(bool(a and a == b)), float(bool(a and b)), ratio(c, d) / 100 if c and d else 0,
        float(bool(c and c == d)), float(bool(c and d)), token_set_ratio(e, f) / 100 if e and f else 0,
        float(bool(e and f)), min(p, q) / max(p, q) if comparable else 0, float(comparable)]


class FrozenProductClassifier:
    """Fixed C=1 logistic for this domain diagnostic; no production integration."""

    def __init__(self, state):
        self.state = state

    @property
    def fingerprint(self):
        return digest(self.state)

    @classmethod
    def fit(cls, records, labels):
        from sklearn.linear_model import LogisticRegression
        indexed = _validate_product_records(records)
        rows, seen = [], set()
        for item in labels:
            pair = tuple(sorted((item["left_id"], item["right_id"])))
            if (item["split"] != "train" or type(item["label"]) is not int or item["label"] not in (0, 1)
                    or pair[0] == pair[1] or pair in seen or any(key not in indexed for key in pair)):
                raise ValueError("Product model fit requires unique explicit train pairs with known endpoints")
            seen.add(pair)
            rows.append((pair, item["label"]))
        rows.sort()
        if {label for _, label in rows} != {0, 1}:
            raise ValueError("Product model needs both train classes")
        values = [product_features(indexed[a], indexed[b]) for (a, b), _ in rows]
        model = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000, random_state=20260912)
        model.fit(values, [label for _, label in rows])
        return cls({"version": VERSION, "features": list(PRODUCT_FEATURES), "C": 1.0,
            "coefficients": model.coef_[0].tolist(), "intercept": float(model.intercept_[0]),
            "training_pairs": len(rows), "training_features_sha256": digest(sorted(records, key=lambda item: item["record_id"])),
            "training_labels_sha256": digest(rows), "scope": "WDC supplied-pair domain diagnostic only",
            "automatic_deployment": False, "calibrated": False})

    def score(self, records, candidates):
        indexed = _validate_product_records(records)
        result = {}
        for pair in candidates:
            if pair[0] == pair[1] or any(key not in indexed for key in pair):
                raise ValueError("Product score requires known distinct endpoints")
            values = product_features(indexed[pair[0]], indexed[pair[1]])
            z = self.state["intercept"] + sum(a * b for a, b in zip(self.state["coefficients"], values, strict=True))
            result[tuple(sorted(pair))] = 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))
        return result

    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "classifier.json").write_text(json.dumps(self.state, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (directory / "manifest.json").write_text(json.dumps({"fingerprint": self.fingerprint}) + "\n", encoding="utf-8")


def _validate_product_records(records):
    indexed = {}
    for record in records:
        if set(record) != {"record_id", *PRODUCT_FIELDS} or not isinstance(record["record_id"], str) or not record["record_id"]:
            raise ValueError("Product feature view must contain only record_id and five legitimate attributes")
        if record["record_id"] in indexed or any(record[key] is not None and not isinstance(record[key], str) for key in PRODUCT_FIELDS):
            raise ValueError("Product feature view contains duplicate IDs or non-string values")
        indexed[record["record_id"]] = record
    return indexed
