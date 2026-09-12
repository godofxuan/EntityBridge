"""Evaluator-only truth access and explicit full-truth metric denominators."""
import math
from collections import Counter, defaultdict
from itertools import combinations


def verify_dataset(directory):
    """Fail before fitting if any frozen input differs or an entity crosses splits."""
    import hashlib
    import json
    from pathlib import Path

    import pyarrow.parquet as pq

    from .normalization import MATCHER_COLUMNS
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    files = {key.replace("\\", "/"): value for key, value in manifest["files"].items()}
    required = {"evaluator/truth_map.parquet", *(f"matcher/{split}.parquet" for split in ("train", "validation", "test"))}
    if not required <= files.keys():
        raise ValueError("Dataset manifest is missing required input hashes")
    for relative, expected in files.items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory):
            raise ValueError("Manifest path escapes the dataset")
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected:
                raise ValueError(f"Dataset hash mismatch: {relative}")
    truth = pq.read_table(directory / "evaluator/truth_map.parquet").to_pylist()
    entities, truth_ids = {}, {}
    for row in truth:
        entity, split, record = row["true_entity_id"], row["split"], row["record_id"]
        if entities.setdefault(entity, split) != split:
            raise ValueError("A true entity occurs in more than one split")
        if record in truth_ids:
            raise ValueError("Truth record ID is duplicated")
        truth_ids[record] = split
    seen = set()
    for split in ("train", "validation", "test"):
        path = directory / "matcher" / f"{split}.parquet"
        if set(pq.read_schema(path).names) != set(MATCHER_COLUMNS):
            raise ValueError("Matcher feature schema is not the identifier-missing allowlist")
        ids = pq.read_table(path, columns=["record_id"]).column(0).to_pylist()
        if len(ids) != len(set(ids)) or seen.intersection(ids):
            raise ValueError("Matcher record IDs are duplicated across splits")
        if set(ids) != {record for record, expected_split in truth_ids.items() if expected_split == split}:
            raise ValueError("Matcher IDs do not exactly cover evaluator split IDs")
        seen.update(ids)
    if seen != truth_ids.keys() or len(seen) != manifest["records"]:
        raise ValueError("Dataset row count and truth coverage differ")
    return {"files_verified": len(files), "records": len(seen), "entities": len(entities),
            "entity_splits_disjoint": True, "train_file_sha256": files["matcher/train.parquet"]}


def load_truth(path, split):
    import pyarrow.parquet as pq
    rows = pq.read_table(path, filters=[("split", "=", split)]).to_pylist()
    return {row["record_id"]: row["true_entity_id"] for row in rows}


def known_pairs(truth):
    groups = defaultdict(list)
    for record, entity in truth.items():
        groups[entity].append(record)
    return {tuple(sorted(pair)) for members in groups.values() for pair in combinations(members, 2)}


def _metrics(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def evaluate_pairs(predictions, candidates, truth):
    positives = known_pairs(truth)
    predicted = {tuple(sorted(pair)) for pair in predictions}
    candidate_set = {tuple(sorted(pair)) for pair in candidates}
    if not predicted <= candidate_set:
        raise ValueError("Predictions must come from the declared candidate set")
    if any(endpoint not in truth for pair in candidate_set for endpoint in pair):
        raise ValueError("Full truth required for all evaluated candidate endpoints")
    tp = len(predicted & positives)
    fp = len(predicted - positives)
    result = {"known_positive_pairs": len(positives), "candidates": len(candidate_set),
        "predicted_pairs": len(predicted), "candidate_recall": len(candidate_set & positives) / len(positives) if positives else 0,
        "end_to_end": _metrics(tp, fp, len(positives - predicted)),
        "conditional": _metrics(tp, fp, len((positives & candidate_set) - predicted)),
        "record_coverage": len({item for pair in predicted for item in pair}) / len(truth) if truth else 0}
    n = len(predicted)
    if n:
        p, z = tp / n, 1.96
        center = (p + z * z / (2 * n)) / (1 + z * z / n)
        radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        result["precision_wilson95_descriptive"] = [center - radius, center + radius]
    else:
        result["precision_wilson95_descriptive"] = None
    return result


def evaluate_clusters(partitions, truth):
    entity_sizes = Counter(truth.values())
    seen, over, affected, largest, precision, recall = set(), 0, 0, 0, 0.0, 0.0
    for members in partitions:
        if seen.intersection(members):
            raise ValueError("Partitions overlap")
        seen.update(members)
        counts = Counter(truth[record] for record in members)
        if len(counts) > 1:
            over += 1
            affected += len(members)
            largest = max(largest, len(members))
        for record in members:
            correct = counts[truth[record]]
            precision += correct / len(members)
            recall += correct / entity_sizes[truth[record]]
    if seen != set(truth):
        raise ValueError("Cluster evaluation requires complete truth coverage")
    return {"overmerged_clusters": over, "records_in_overmerged_clusters": affected,
        "largest_wrong_cluster": largest, "b_cubed_precision": precision / len(truth) if truth else 0,
        "b_cubed_recall": recall / len(truth) if truth else 0}
