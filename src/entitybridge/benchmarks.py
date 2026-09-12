"""Explicitly partially labelled public benchmarks, separate from registry truth."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pyarrow as pa
import pyarrow.parquet as pq

from .normalization import MATCHER_COLUMNS, NORMALIZATION_VERSION, normalize_text

ADAPTER_VERSION = "public-pair-positive-component-split-v1"
DATASET_DOCUMENTATION = "https://github.com/anhaidgroup/deepmatcher/blob/master/Datasets.md"
SOFTWARE_LICENSE_URL = "https://github.com/anhaidgroup/deepmatcher/blob/master/LICENSE"
FILES = ("tableA.csv", "tableB.csv", "train.csv", "valid.csv", "test.csv")
SPLITS = ("train", "validation", "test")
LABEL_COLUMNS = ("left_id", "right_id", "label", "split", "original_split", "left_group_id", "right_group_id", "group_id")
GROUP_COLUMNS = ("record_id", "entity_group_id", "split")
CATALOG = {
    "fodors_zagats": {
        "folder": "Fodors-Zagats", "domain": "restaurant", "sources": ("fodors", "zagats"),
        "projection": {"name": "name", "address": "addr", "city": "city"},
        "excluded_columns": ["id", "phone", "type", "class"],
        "source_sha256": dict(zip(FILES, (
            "dae8867efd8da4cc0ce729d08b506f4c37a9d106b977fa75929618c2a311a356",
            "d31e7dfa7fe363c594bf8ebe4eaaf5018e11362125edd49042271d2aae7a1d37",
            "fda195b062d7becc9abb941daef30c97aa22c4914330c3dac15e675353c52d2c",
            "7d0b35d5e5e90defb7c88a6bd5d359057fd8919a7d36321f6dc8087ab66939e3",
            "20b066790d1a5982a360c99c88af6e58ebf13f8d3150b7ff8172720c72f07a24"))),
    },
    "dblp_acm": {
        "folder": "DBLP-ACM", "domain": "bibliographic", "sources": ("dblp", "acm"),
        "projection": {"name": "title"},
        "excluded_columns": ["id", "authors", "venue", "year"],
        "source_sha256": dict(zip(FILES, (
            "a83dfac196a4e263f3adac7aaf095c7198254a98fcaed0ec68d59130c74c43a7",
            "bd103ffdccdff4d8b9d04c18d90d04110b70b6fc87dec83c4e52e9616c58431a",
            "ad94b36b178bbf76023d3cee689565fbda1fe01b19d9a3926a51db382f45f0a5",
            "862f848ed3f3f005ae6c8997ecf571984bd575f6bbd319fc1a2170830a91132b",
            "e49adc4590d24c18b1a9bbd96011d9c745e10432e10e93e050d856a206fac394"))),
    },
}


def source_url(dataset, filename):
    if filename not in FILES:
        raise ValueError("Unknown benchmark source file")
    return ("https://pages.cs.wisc.edu/~anhai/data1/deepmatcher_data/Structured/"
            f"{CATALOG[dataset]['folder']}/exp_data/{filename}")


def file_hash(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _opaque(dataset, kind, key):
    # Stable source-record IDs are independent of labels and assigned split.
    return str(uuid5(NAMESPACE_URL, json.dumps(["entitybridge-public-v1", dataset, kind, key])))


class _Components:
    def __init__(self, keys):
        self.parent = {key: key for key in keys}

    def find(self, key):
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a, b):
        a, b = sorted((self.find(a), self.find(b)))
        self.parent[b] = a


def _split(group, seed):
    draw = int(hashlib.sha256(f"{seed}:{group}".encode()).hexdigest()[:16], 16) % 100
    return "train" if draw < 60 else "validation" if draw < 80 else "test"


def adapt_tables(dataset, tables, labelled_pairs, *, seed=20260912):
    """Project features and split by known-positive components, never pair rows.

    A negative label inside the positive transitive closure is contradictory and
    fails before producing any usable output. Missing Cartesian pairs remain
    unknown. 'entity groups' mean known-positive components, not a claim of
    exhaustive real-world entity annotation.
    """
    config = CATALOG[dataset]
    indexed, raw_to_id = {}, {}
    for filename, source in zip(FILES[:2], config["sources"]):
        for original in tables[filename]:
            if not isinstance(original.get("id"), str) or not original["id"]:
                raise ValueError("Source IDs must be non-empty strings")
            key = (filename, original["id"])
            if key in raw_to_id:
                raise ValueError("Duplicate source record ID")
            record_id = _opaque(dataset, "record", key)
            raw_to_id[key] = record_id
            values = {field: normalize_text(original.get(config["projection"][field]))
                      if field in config["projection"] else None
                      for field in ("name", "address", "city", "postcode", "country")}
            if not values["name"]:
                raise ValueError("Projected benchmark name is empty")
            version = _opaque(dataset, "version", [key, values])
            indexed[record_id] = {"record_id": record_id, "record_version_id": version,
                                  "source": source, **values}
    if len(indexed) > 20_000:
        raise ValueError("Benchmark adapter supports at most 20000 records")
    components = _Components(indexed)
    original_record_splits = defaultdict(set)
    original_label_counts = {split: Counter() for split in SPLITS}
    unique, input_count = {}, 0
    for original in labelled_pairs:
        input_count += 1
        if input_count > 200_000:
            raise ValueError("Benchmark adapter supports at most 200000 labelled pairs")
        left = raw_to_id.get(("tableA.csv", original.get("ltable_id")))
        right = raw_to_id.get(("tableB.csv", original.get("rtable_id")))
        if left is None or right is None:
            raise ValueError("Unknown labelled endpoint")
        if original.get("label") not in (0, 1, "0", "1") or isinstance(original["label"], bool):
            raise ValueError("Labels must be explicitly 0 or 1")
        label, original_split = int(original["label"]), original["original_split"]
        if original_split not in SPLITS:
            raise ValueError("Unknown original pair split")
        original_label_counts[original_split][str(label)] += 1
        pair = (left, right)
        if pair in unique and unique[pair]["label"] != label:
            raise ValueError("Contradictory labels for the same pair")
        item = unique.setdefault(pair, {"label": label, "original_splits": set()})
        item["original_splits"].add(original_split)
        original_record_splits[left].add(original_split)
        original_record_splits[right].add(original_split)
        if label:
            components.union(left, right)
    for (left, right), item in unique.items():
        if item["label"] == 0 and components.find(left) == components.find(right):
            raise ValueError("Negative label contradicts positive-label closure")
    entity_group = {record: _opaque(dataset, "positive-component", components.find(record)) for record in indexed}
    record_split = {record: _split(group, seed) for record, group in entity_group.items()}
    labels, dropped = [], Counter()
    original_group_splits = defaultdict(set)
    for record, splits in original_record_splits.items():
        original_group_splits[entity_group[record]].update(splits)
    for (left, right), item in sorted(unique.items()):
        if record_split[left] != record_split[right]:
            dropped[str(item["label"])] += 1
            continue
        labels.append({"left_id": left, "right_id": right, "label": item["label"],
                       "split": record_split[left], "original_split": "|".join(sorted(item["original_splits"])),
                       "left_group_id": entity_group[left], "right_group_id": entity_group[right]})
    # Dependence blocks for the evaluator include every retained labelled edge,
    # including negatives. This can yield giant blocks; the report must say so.
    bootstrap = _Components(indexed)
    for pair in labels:
        bootstrap.union(pair["left_id"], pair["right_id"])
    for pair in labels:
        pair["group_id"] = _opaque(dataset, "label-dependence-component", bootstrap.find(pair["left_id"]))
    matcher = {split: [indexed[key] for key in sorted(indexed) if record_split[key] == split] for split in SPLITS}
    record_groups = [{"record_id": key, "entity_group_id": entity_group[key], "split": record_split[key]}
                     for key in sorted(indexed)]
    statistics = {"records": len(indexed), "source_records": dict(Counter(r["source"] for r in indexed.values())),
        "input_label_rows": input_count, "unique_label_pairs": len(unique), "duplicate_label_rows": input_count - len(unique),
        "original_positive_pairs": sum(item["label"] for item in unique.values()),
        "original_split_label_counts": {split: dict(sorted(counts.items()))
                                        for split, counts in original_label_counts.items()},
        "original_records_in_multiple_pair_splits": sum(len(splits) > 1 for splits in original_record_splits.values()),
        "original_known_entities_in_multiple_pair_splits": sum(len(splits) > 1 for splits in original_group_splits.values()),
        "known_positive_components": len(set(entity_group.values())), "cross_split_pairs_dropped": dict(sorted(dropped.items())),
        "retained_label_pairs": len(labels), "unlabelled_pairs_policy": "unknown-excluded-from-confusion-matrix",
        "complete_cluster_truth": False, "contradictory_labels": 0,
        "splits": {split: {"records": len(matcher[split]),
            "label_pairs": sum(p["split"] == split for p in labels),
            "positive_pairs": sum(p["split"] == split and p["label"] == 1 for p in labels),
            "negative_pairs": sum(p["split"] == split and p["label"] == 0 for p in labels),
            "bootstrap_groups": len({p["group_id"] for p in labels if p["split"] == split})} for split in SPLITS}}
    return {"matcher": matcher, "labels": labels, "record_groups": record_groups, "statistics": statistics}


def convert_benchmark(raw_directory, output_directory, dataset, *, seed=20260912):
    raw_directory, output_directory = Path(raw_directory), Path(output_directory)
    config, sources, tables, labels = CATALOG[dataset], {}, {}, []
    for filename in FILES:
        path = raw_directory / filename
        actual = file_hash(path)
        if actual != config["source_sha256"][filename]:
            raise ValueError(f"Public source hash changed: {dataset}/{filename}")
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            rows = list(reader)
            if any(None in row or any(value is None for value in row.values()) for row in rows):
                raise ValueError(f"Malformed source CSV: {filename}")
        if filename.startswith("table"):
            tables[filename] = rows
        else:
            split = {"train.csv": "train", "valid.csv": "validation", "test.csv": "test"}[filename]
            labels.extend(row | {"original_split": split} for row in rows)
        sources[filename] = {"url": source_url(dataset, filename), "sha256": actual,
                             "bytes": path.stat().st_size, "rows": len(rows)}
    result = adapt_tables(dataset, tables, labels, seed=seed)
    output_directory.mkdir(parents=True, exist_ok=False)
    (output_directory / "matcher").mkdir()
    (output_directory / "evaluator").mkdir()
    feature_schema = pa.schema([(field, pa.string()) for field in MATCHER_COLUMNS])
    for split in SPLITS:
        pq.write_table(pa.Table.from_pylist(result["matcher"][split], schema=feature_schema),
                       output_directory / "matcher" / f"{split}.parquet")
    label_schema = pa.schema([(field, pa.int8() if field == "label" else pa.string()) for field in LABEL_COLUMNS])
    pq.write_table(pa.Table.from_pylist(result["labels"], schema=label_schema), output_directory / "evaluator/labelled_pairs.parquet")
    pq.write_table(pa.Table.from_pylist(result["record_groups"], schema=pa.schema([(field, pa.string()) for field in GROUP_COLUMNS])),
        output_directory / "evaluator/record_groups.parquet")
    manifest = {"kind": "public-partially-labelled-pairs", "dataset": dataset, "domain": config["domain"],
        "adapter_version": ADAPTER_VERSION, "normalization_version": NORMALIZATION_VERSION,
        "records": result["statistics"]["records"], "partial_labels_only": True,
        "documentation_url": DATASET_DOCUMENTATION, "source_files": sources,
        "license": {"dataset_license": "not specified on official dataset page",
            "software_license": "BSD-3-Clause; not assumed to license third-party dataset contents",
            "software_license_url": SOFTWARE_LICENSE_URL, "raw_data_redistribution": "not included in this repository"},
        "field_projection": config["projection"], "excluded_original_columns": config["excluded_columns"],
        "split_protocol": {"version": ADAPTER_VERSION, "seed": seed, "fractions": [0.6, 0.2, 0.2],
            "grouping": "known-positive-label connected components; no original pair-split reuse",
            "cross_split_labelled_pairs": "discarded with counts; never relabelled",
            "bootstrap_grouping": "connected components of all retained positive AND negative labelled edges"},
        "statistics": result["statistics"],
        "files": {path.relative_to(output_directory).as_posix(): file_hash(path)
                  for path in sorted(output_directory.rglob("*.parquet"))}}
    (output_directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    verify_benchmark(output_directory)
    return manifest


def verify_benchmark(directory):
    """Verify hashes, exact feature allowlist, partial labels and disjoint groups."""
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    required = {*(f"matcher/{split}.parquet" for split in SPLITS),
                "evaluator/labelled_pairs.parquet", "evaluator/record_groups.parquet"}
    if set(manifest["files"]) != required or manifest.get("partial_labels_only") is not True:
        raise ValueError("Expected a partial-labelled benchmark manifest")
    for relative, expected in manifest["files"].items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory) or file_hash(path) != expected:
            raise ValueError("Benchmark file path or hash mismatch")
    records = {}
    for split in SPLITS:
        path = directory / "matcher" / f"{split}.parquet"
        if pq.read_schema(path) != pa.schema([(field, pa.string()) for field in MATCHER_COLUMNS]):
            raise ValueError("Benchmark matcher schema is not the eight-column allowlist")
        # Preflight reads only identifiers and source; held-out name/address
        # features are not materialised before the runner freezes its choices.
        table = pq.read_table(path, columns=["record_id", "record_version_id", "source"])
        for row in table.to_pylist():
            if any(not isinstance(row[field], str) or not row[field] for field in ("record_id", "record_version_id", "source")):
                raise ValueError("Benchmark record identifiers must be non-empty strings")
            if row["record_id"] in records:
                raise ValueError("Record occurs in multiple benchmark splits")
            records[row["record_id"]] = row | {"split": split}
    group_path = directory / "evaluator/record_groups.parquet"
    if pq.read_schema(group_path) != pa.schema([(field, pa.string()) for field in GROUP_COLUMNS]):
        raise ValueError("Benchmark record group schema mismatch")
    record_groups = pq.read_table(group_path).to_pylist()
    groups, record_group = {}, {}
    for row in record_groups:
        if any(not isinstance(value, str) or not value for value in row.values()):
            raise ValueError("Benchmark group values must be non-empty strings")
        key = row["record_id"]
        if key in record_group or key not in records or row["split"] != records[key]["split"]:
            raise ValueError("Benchmark record-group coverage mismatch")
        if groups.setdefault(row["entity_group_id"], row["split"]) != row["split"]:
            raise ValueError("Known entity group leaks across splits")
        record_group[key] = row["entity_group_id"]
    if record_group.keys() != records.keys() or len(records) != manifest["records"]:
        raise ValueError("Benchmark row count mismatch")
    seen, dependence_group = set(), {}
    label_path = directory / "evaluator/labelled_pairs.parquet"
    if pq.read_schema(label_path) != pa.schema([(field, pa.int8() if field == "label" else pa.string()) for field in LABEL_COLUMNS]):
        raise ValueError("Benchmark label schema mismatch")
    labels = pq.read_table(label_path).to_pylist()
    for pair in labels:
        a, b = pair["left_id"], pair["right_id"]
        if any(not isinstance(pair[field], str) or not pair[field] for field in LABEL_COLUMNS if field != "label"):
            raise ValueError("Benchmark label metadata must be non-empty strings")
        key = tuple(sorted((a, b)))
        if a not in records or b not in records or a == b or key in seen or pair["label"] not in (0, 1):
            raise ValueError("Invalid or duplicated explicit pair label")
        seen.add(key)
        if records[a]["source"] == records[b]["source"] or any(records[k]["split"] != pair["split"] for k in (a, b)):
            raise ValueError("Label endpoints cross source/split boundary")
        if pair["left_group_id"] != record_group[a] or pair["right_group_id"] != record_group[b]:
            raise ValueError("Pair entity group mismatch")
        if (record_group[a] == record_group[b]) != bool(pair["label"]):
            raise ValueError("Pair labels contradict known-positive components")
        for endpoint in (a, b):
            if dependence_group.setdefault(endpoint, pair["group_id"]) != pair["group_id"]:
                raise ValueError("Dependent labelled pairs split across bootstrap groups")
    return {"records": len(records), "label_pairs": len(labels), "known_entity_groups": len(groups),
            "record_and_known_entity_splits_disjoint": True, "unknown_pairs_are_not_negatives": True}
