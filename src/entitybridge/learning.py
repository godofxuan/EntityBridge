"""Auditable human-review learning snapshots and candidate-only model fitting.

No function deploys a model, publishes an identity, or turns scores into labels.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select

from . import schema
from .benchmark_metrics import evaluate_labeled_pairs, select_cost_threshold
from .benchmarks import _Components
from .candidates import validate_records
from .normalization import FEATURE_VIEW_VERSION, MATCHER_COLUMNS, NORMALIZATION_VERSION, matcher_view
from .store import digest
from .supervised import FrozenPairClassifier

VERSION = "review-learning-explicit-active-v1"
SPLITS = ("train", "validation", "calibration", "test")


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _file_hash(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def snapshot_review_labels(store):
    """Read a consistent published basis; pending source/events require publishing.

    Optimistic before/after checks detect concurrent writers without modifying
    Store state. The result is immutable as-of evidence, not an everlasting claim
    that these judgments can never be revoked in a later revision.
    """
    with store.engine.connect() as connection:
        revision = store.current_revision(connection)
        if not revision:
            raise ValueError("Learning requires a published revision")
        row = connection.execute(select(schema.revisions).where(schema.revisions.c.revision_id == revision)).mappings().one()
        cutoff = store._event_cutoff(connection)
        raw = store.active_records(connection)
        if row["status"] != "published" or cutoff != row["event_cutoff"] or digest(raw) != row["input_hash"]:
            raise ValueError("Learning basis has unpublished source or decision changes; publish a current revision first")
        path = (store.artifact_root / row["artifact_path"]).resolve()
        if not path.is_relative_to(store.artifact_root):
            raise ValueError("Published learning artifact path mismatch")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row["artifact_sha256"]:
            raise ValueError("Published learning artifact hash mismatch")
        payload = json.loads(content)
        if digest(payload["records"]) != row["input_hash"] or payload["event_cutoff"] != cutoff:
            raise ValueError("Learning artifact basis is inconsistent")
        events = {event["decision_id"]: dict(event) for event in connection.execute(select(schema.events)
            .where(schema.events.c.seq <= cutoff).order_by(schema.events.c.seq)).mappings()}
        decisions = [dict(item) for item in connection.execute(select(schema.decisions)).mappings()]
        if (store.current_revision(connection) != revision or store._event_cutoff(connection) != cutoff
                or digest(store.active_records(connection)) != row["input_hash"]):
            raise ValueError("Learning basis changed concurrently; retry after publication")
    excluded, labels = Counter(), {}
    for decision in sorted(decisions, key=lambda item: item["decision_id"]):
        event = events.get(decision["decision_id"])
        if event is None:
            excluded["outside_published_cutoff"] += 1
            continue
        if event["action"] == "REVOKE":
            excluded["revoked"] += 1
            continue
        a, b = raw.get(decision["left_id"]), raw.get(decision["right_id"])
        if (event["action"] == "EXPIRE" or not a or not b or
                (a["record_version_id"], b["record_version_id"]) != (decision["left_version"], decision["right_version"])):
            excluded["expired"] += 1
            continue
        if event["action"] != "CREATE" or decision["action"] not in {"accept", "reject"}:
            excluded["abstain" if decision["action"] == "abstain" else "not_explicit_binary"] += 1
            continue
        if a["source"] == b["source"]:
            excluded["same_source_unsupported_by_pair_classifier"] += 1
            continue
        left, right = sorted((a["record_id"], b["record_id"]))
        label = int(decision["action"] == "accept")
        item = labels.setdefault((left, right), {"left_id": left, "right_id": right,
            "left_version": raw[left]["record_version_id"], "right_version": raw[right]["record_version_id"],
            "label": label, "provenance": []})
        if item["label"] != label:
            raise ValueError("Active human judgments contain conflicting labels")
        item["provenance"].append({"decision_id": decision["decision_id"], "action": decision["action"],
            "reviewer": decision["reviewer"], "reason": decision["reason"], "base_revision": decision["base_revision"],
            "policy_version": decision["policy_version"], "event_seq": event["seq"],
            "event_policy_version": event["policy_version"], "event_reviewer": event["reviewer"],
            "event_created_at": event["created_at"]})
    labels = [labels[pair] for pair in sorted(labels)]
    snapshot = {"version": VERSION, "revision_id": revision, "artifact_sha256": row["artifact_sha256"],
        "input_hash": row["input_hash"], "event_cutoff": cutoff, "published_policy_version": row["policy_version"],
        "normalization_version": NORMALIZATION_VERSION, "feature_view_version": FEATURE_VIEW_VERSION,
        "records": [matcher_view(raw[key], key, raw[key]["record_version_id"]) for key in sorted(raw)], "labels": labels,
        "summary": {"valid_labels": len(labels), "positive_labels": sum(item["label"] for item in labels),
            "negative_labels": sum(1 - item["label"] for item in labels), "excluded_counts": dict(sorted(excluded.items())),
            "judgments_preserved": sum(len(item["provenance"]) for item in labels)}, "automatic_deployment": False}
    snapshot["snapshot_sha256"] = digest(snapshot)
    return snapshot


def split_learning_snapshot(snapshot, *, seed=20260912):
    """Split whole positive AND negative endpoint components; no edge dropping."""
    if type(seed) is not int:
        raise ValueError("Learning split seed must be an integer")
    records = {row["record_id"]: row for row in validate_records(snapshot["records"])}
    labels = {}
    positive, shared = _Components(records), _Components(records)
    for original in snapshot["labels"]:
        a, b = sorted((original["left_id"], original["right_id"]))
        if a == b or a not in records or b not in records or type(original["label"]) is not int or original["label"] not in (0, 1):
            raise ValueError("Learning labels require explicit binary values and known distinct endpoints")
        if records[a]["source"] == records[b]["source"]:
            raise ValueError("Learning classifier requires cross-source labels")
        versions = {original["left_id"]: original["left_version"], original["right_id"]: original["right_version"]}
        if any(records[key]["record_version_id"] != versions[key] for key in (a, b)):
            raise ValueError("Learning label endpoint version is stale")
        item = dict(original) | {"left_id": a, "right_id": b, "left_version": versions[a], "right_version": versions[b]}
        if (a, b) in labels and labels[(a, b)]["label"] != item["label"]:
            raise ValueError("Learning data has conflicting labels")
        if (a, b) in labels:
            raise ValueError("Learning data has duplicate labels; merge provenance before splitting")
        labels[(a, b)] = item
        shared.union(a, b)
        if item["label"]:
            positive.union(a, b)
    for (a, b), item in labels.items():
        if not item["label"] and positive.find(a) == positive.find(b):
            raise ValueError("Negative label contradicts positive-label closure")
    by_split = {name: [] for name in SPLITS}
    for (a, b), item in sorted(labels.items()):
        group = digest(["review-endpoint-component", shared.find(a)])
        draw = int(digest([seed, group])[:16], 16) % 100
        split = "train" if draw < 50 else "validation" if draw < 70 else "calibration" if draw < 85 else "test"
        by_split[split].append(item | {"split": split, "group_id": group})
    matcher = {split: [records[key] for key in sorted({key for item in items for key in (item["left_id"], item["right_id"])})]
               for split, items in by_split.items()}
    counts = {split: {"pairs": len(items), "positive": sum(item["label"] for item in items),
                     "negative": sum(1 - item["label"] for item in items), "records": len(matcher[split]),
                     "groups": len({item["group_id"] for item in items})} for split, items in by_split.items()}
    return {"records": matcher, "labels": by_split, "statistics": counts, "seed": seed,
        "protocol": "all-labelled-endpoint-components-50-20-15-15-v1",
        "unlabelled_records_excluded": len(records) - sum(len(rows) for rows in matcher.values())}


def _write_learning_dataset(snapshot, output, *, seed):
    output = Path(output)
    split = split_learning_snapshot(snapshot, seed=seed)
    output.mkdir(parents=True, exist_ok=False)
    (output / "matcher").mkdir()
    (output / "labels").mkdir()
    source = {key: value for key, value in snapshot.items() if key not in {"records", "labels"}}
    source["all_exported_labels_sha256"] = digest(sorted(
        [{key: value for key, value in item.items() if key not in {"split", "group_id"}}
         for items in split["labels"].values() for item in items], key=lambda item: (item["left_id"], item["right_id"])))
    _write_json(output / "source.json", source)
    feature_schema = pa.schema([(field, pa.string()) for field in MATCHER_COLUMNS])
    for name in SPLITS:
        pq.write_table(pa.Table.from_pylist(split["records"][name], schema=feature_schema), output / "matcher" / f"{name}.parquet")
        _write_json(output / "labels" / f"{name}.json", split["labels"][name])
    manifest = {"version": VERSION, "source_revision": snapshot["revision_id"], "source_artifact_sha256": snapshot["artifact_sha256"],
        "source_event_cutoff": snapshot["event_cutoff"], "split_protocol": split["protocol"], "seed": seed,
        "statistics": split["statistics"], "unlabelled_records_excluded": split["unlabelled_records_excluded"],
        "labels_derived_from_scores": False, "unknown_pairs_are_negatives": False, "automatic_deployment": False,
        "files": {path.relative_to(output).as_posix(): _file_hash(path) for path in sorted(output.rglob("*")) if path.is_file()}}
    _write_json(output / "manifest.json", manifest)
    verify_learning_dataset(output)
    return manifest


def export_review_dataset(store, output, *, seed=20260912):
    return _write_learning_dataset(snapshot_review_labels(store), output, seed=seed)


def verify_learning_dataset(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    expected_files = {"source.json", *(f"matcher/{split}.parquet" for split in SPLITS), *(f"labels/{split}.json" for split in SPLITS)}
    if manifest.get("version") != VERSION or set(manifest["files"]) != expected_files:
        raise ValueError("Invalid learning dataset manifest")
    for relative, expected in manifest["files"].items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or _file_hash(path) != expected:
            raise ValueError("Learning dataset hash mismatch")
    source = json.loads((directory / "source.json").read_text(encoding="utf-8"))
    if (source["revision_id"] != manifest["source_revision"] or source["artifact_sha256"] != manifest["source_artifact_sha256"]
            or source["event_cutoff"] != manifest["source_event_cutoff"]
            or manifest.get("labels_derived_from_scores") is not False or manifest.get("unknown_pairs_are_negatives") is not False):
        raise ValueError("Learning source provenance mismatch")
    seen_records, seen_pairs, group_splits, label_count = set(), set(), {}, 0
    endpoint_groups, all_labels = {}, []
    for split in SPLITS:
        path = directory / "matcher" / f"{split}.parquet"
        if pq.read_schema(path) != pa.schema([(field, pa.string()) for field in MATCHER_COLUMNS]):
            raise ValueError("Learning feature schema is not the eight-column allowlist")
        rows = pq.read_table(path, columns=["record_id", "record_version_id", "source"]).to_pylist()
        records = {row["record_id"]: row for row in rows}
        if len(records) != len(rows) or any(any(not isinstance(value, str) or not value for value in row.values()) for row in rows):
            raise ValueError("Learning records need unique non-empty identifiers and source")
        if seen_records.intersection(records):
            raise ValueError("Learning endpoint leaks across splits")
        seen_records.update(records)
        labels = json.loads((directory / "labels" / f"{split}.json").read_text(encoding="utf-8"))
        used = set()
        for item in labels:
            a, b = item["left_id"], item["right_id"]
            key = tuple(sorted((a, b)))
            if a not in records or b not in records or a == b or key in seen_pairs or item["split"] != split:
                raise ValueError("Learning label split/endpoint mismatch")
            if type(item["label"]) is not int or item["label"] not in (0, 1) or not item["provenance"]:
                raise ValueError("Learning labels must retain explicit binary judgments and provenance")
            if (records[a]["record_version_id"], records[b]["record_version_id"]) != (item["left_version"], item["right_version"]):
                raise ValueError("Learning label version mismatch")
            if records[a]["source"] == records[b]["source"]:
                raise ValueError("Learning labels require cross-source endpoints")
            if group_splits.setdefault(item["group_id"], split) != split:
                raise ValueError("Learning dependency group leaks across splits")
            used.update((a, b))
            for endpoint in (a, b):
                if endpoint_groups.setdefault(endpoint, item["group_id"]) != item["group_id"]:
                    raise ValueError("Learning shared endpoint has inconsistent dependency group")
            seen_pairs.add(key)
        if used != records.keys():
            raise ValueError("Learning feature and labelled endpoint coverage differs")
        label_count += len(labels)
        actual = {"pairs": len(labels), "positive": sum(item["label"] for item in labels),
            "negative": sum(1 - item["label"] for item in labels), "records": len(records),
            "groups": len({item["group_id"] for item in labels})}
        if actual != manifest["statistics"][split]:
            raise ValueError("Learning statistics mismatch")
        all_labels.extend(labels)
    components, positive = _Components(seen_records), _Components(seen_records)
    for item in all_labels:
        components.union(item["left_id"], item["right_id"])
        if item["label"]:
            positive.union(item["left_id"], item["right_id"])
    for item in all_labels:
        if item["group_id"] != digest(["review-endpoint-component", components.find(item["left_id"])]):
            raise ValueError("Learning dependency group is not the full endpoint component")
        if not item["label"] and positive.find(item["left_id"]) == positive.find(item["right_id"]):
            raise ValueError("Learning negative contradicts positive closure")
    exported_labels = sorted([{key: value for key, value in item.items() if key not in {"split", "group_id"}}
                              for item in all_labels], key=lambda item: (item["left_id"], item["right_id"]))
    if digest(exported_labels) != source["all_exported_labels_sha256"]:
        raise ValueError("Learning labels differ from source evidence")
    return {"records": len(seen_records), "labels": label_count, "shared_endpoint_splits_disjoint": True,
            "manifest_sha256": _file_hash(directory / "manifest.json")}


def rank_review_candidates(records, candidates, *, strategy="uncertainty_diversity", seed=20260912, limit=100, excluded_pairs=()):
    """Rank real scores without reading labels; diversify endpoints within rounds."""
    indexed = {row["record_id"]: row for row in validate_records(records)}
    if strategy not in {"uncertainty_diversity", "random"} or type(limit) is not int or not 0 <= limit <= 10000 or type(seed) is not int:
        raise ValueError("Unsupported review strategy or limit")
    excluded = {tuple(sorted(pair)) for pair in excluded_pairs}
    rows, seen = [], set()
    for candidate in candidates:
        if any(key in candidate for key in ("label", "true_entity_id", "group_id", "left_group_id", "right_group_id")):
            raise ValueError("Review ranking must not receive labels or truth groups")
        a, b = sorted((candidate["left"], candidate["right"]))
        score = candidate["score"]
        if (a == b or a not in indexed or b not in indexed or isinstance(score, bool)
                or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("Review candidates need known endpoints and finite actual scores in [0,1]")
        if (a, b) in seen:
            raise ValueError("Duplicate review candidate pair")
        seen.add((a, b))
        if (a, b) not in excluded:
            rows.append(dict(candidate) | {"uncertainty": abs(score - 0.5), "_tie": digest([seed, a, b])})
    rows.sort(key=lambda item: (item["uncertainty"], item["_tie"]) if strategy == "uncertainty_diversity" else (item["_tie"],))
    ranked = []
    while rows and len(ranked) < limit:
        occupied, deferred = set(), []
        for item in rows:
            endpoints = {item["left"], item["right"]}
            if strategy == "uncertainty_diversity" and occupied.intersection(endpoints):
                deferred.append(item)
                continue
            occupied.update(endpoints)
            item.pop("_tie")
            ranked.append(item | {"selection_rank": len(ranked) + 1, "sampling_strategy": strategy,
                "sampling_reason": "closest_to_half_with_endpoint_diversity" if strategy == "uncertainty_diversity" else "seeded_hash_random_order"})
            if len(ranked) >= limit:
                break
        rows = deferred
    return ranked


def _read_split(directory, split):
    records = pq.read_table(Path(directory) / "matcher" / f"{split}.parquet").to_pylist()
    labels = json.loads((Path(directory) / "labels" / f"{split}.json").read_text(encoding="utf-8"))
    return records, labels


def _pairs(labels):
    return [{"left": item["left_id"], "right": item["right_id"], "rules": ["explicit_review_label_evaluation"]} for item in labels]


def _score(model, records, labels, calibrator=None):
    values = {(item["left"], item["right"]): item["score"] for item in model.score(records, _pairs(labels))}
    if calibrator is not None:
        for pair, probability in values.items():
            logit = math.log(max(1e-12, probability) / max(1e-12, 1 - probability))
            z = calibrator["coefficient"] * logit + calibrator["intercept"]
            values[pair] = 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))
    return values


def train_candidate_model(directory, output, *, calibrate=False, false_positive_cost=10.0, false_negative_cost=1.0,
                          min_pairs_per_split=10, min_per_class=2):
    """Fit a review-trained candidate, freeze validation policy, then evaluate test.

    Optional sigmoid fitting uses a fourth, endpoint-disjoint calibration split.
    Its output remains sample-conditional and is never deployed automatically.
    """
    directory, output = Path(directory), Path(output)
    if (type(min_pairs_per_split) is not int or min_pairs_per_split < 4
            or type(min_per_class) is not int or min_per_class < 2):
        raise ValueError("Learning minimums require four pairs and at least two labels per class")
    if output.exists():
        raise ValueError("Use a fresh candidate model output directory")
    verified = verify_learning_dataset(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    required = ("train", "validation", "test", "calibration") if calibrate else ("train", "validation", "test")
    for split in required:
        stats = manifest["statistics"][split]
        if (stats["pairs"] < min_pairs_per_split or min(stats["positive"], stats["negative"]) < min_per_class or stats["groups"] < 2):
            raise ValueError(f"Insufficient independent {split} labels: need >= {min_pairs_per_split} pairs, {min_per_class} per class and two groups")
    train_records, train_labels = _read_split(directory, "train")
    model = FrozenPairClassifier.fit(train_records, train_labels)
    output.mkdir(parents=True, exist_ok=False)
    model.save(output / "model")
    calibrator = None
    if calibrate:
        from sklearn.linear_model import LogisticRegression
        calibration_records, calibration_labels = _read_split(directory, "calibration")
        raw_scores = _score(model, calibration_records, calibration_labels)
        lookup = {(row["left_id"], row["right_id"]): row["label"] for row in calibration_labels}
        ordered = sorted(raw_scores)
        logits = [[math.log(max(1e-12, raw_scores[pair]) / max(1e-12, 1 - raw_scores[pair]))] for pair in ordered]
        fitted = LogisticRegression(C=1.0, max_iter=1000, random_state=20260912).fit(logits, [lookup[pair] for pair in ordered])
        calibrator = {"kind": "independent-split-regularized-sigmoid", "coefficient": float(fitted.coef_[0][0]),
            "intercept": float(fitted.intercept_[0]), "split": "calibration", "model_fingerprint": model.fingerprint,
            "labels_sha256": _file_hash(directory / "labels/calibration.json"), "deployment_calibrated": False}
        _write_json(output / "calibration.json", calibrator)
    val_records, val_labels = _read_split(directory, "validation")
    val_scores = _score(model, val_records, val_labels, calibrator)
    selection = select_cost_threshold(val_labels, set(val_scores), val_scores, split="validation",
        false_positive_cost=false_positive_cost, false_negative_cost=false_negative_cost, return_curve=True)
    if selection["status"] != "selected":
        raise ValueError("Validation cannot select a policy from these labels")
    frozen = {"version": VERSION, "dataset_manifest_sha256": verified["manifest_sha256"],
        "model_fingerprint": model.fingerprint, "calibration": calibrator, "validation_selection": selection,
        "source_revision": manifest["source_revision"], "source_event_cutoff": manifest["source_event_cutoff"],
        "automatic_deployment": False, "test_effect_evaluated_after_freeze": True}
    _write_json(output / "frozen_config.json", frozen)
    test_records, test_labels = _read_split(directory, "test")
    test_scores = _score(model, test_records, test_labels, calibrator)
    report = {"kind": "candidate-review-trained-model", "automatic_deployment": False,
        "frozen_config_sha256": _file_hash(output / "frozen_config.json"),
        "statistics": manifest["statistics"], "test": evaluate_labeled_pairs(test_labels, set(test_scores), test_scores,
            threshold=selection["selected_threshold"], score_kind="probability"),
        "limitations": ["Explicit reviewed pairs only; selection bias remains", "Independent calibration is sample-conditional, not a deployment guarantee",
                        "Historical label snapshots can later become stale; never auto-deploy", "This is supplied-pair evaluation, not candidate retrieval recall"]}
    _write_json(output / "report.json", report)
    return report
