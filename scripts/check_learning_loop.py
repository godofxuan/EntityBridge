"""Oracle simulation on a public benchmark's ORIGINAL TRAIN partition only.

No public validation/test features are read. The inner held-out labels evaluate
predeclared random and uncertainty/diversity methods, never pick a winner.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import pyarrow.parquet as pq

from entitybridge.benchmark_metrics import evaluate_labeled_pairs, select_cost_threshold
from entitybridge.learning import (
    _file_hash,
    _pairs,
    _read_split,
    _score,
    _write_json,
    _write_learning_dataset,
    rank_review_candidates,
)
from entitybridge.matching import score_baselines
from entitybridge.store import digest
from entitybridge.supervised import FrozenPairClassifier

METHODS = ("random", "uncertainty_diversity")


def _key(item):
    return tuple(sorted((item.get("left", item.get("left_id")), item.get("right", item.get("right_id")))))


def _fit_acquired(records, acquired):
    endpoints = {endpoint for item in acquired for endpoint in (item["left_id"], item["right_id"])}
    return FrozenPairClassifier.fit([row for row in records if row["record_id"] in endpoints], acquired)


def run_simulation(dataset, output, *, budgets=(20, 50, 100, 200), seeds=(11, 29, 47), split_seed=20260912, batch_size=10):
    """Simulate fixed annotation budgets with a shared random initial batch.

    The experiment reuses sampled known labels as an oracle. Unknown pairs never
    enter the pool as negatives. Validation labels are an additional fixed cost.
    """
    dataset, output = Path(dataset), Path(output)
    budgets, seeds = tuple(budgets), tuple(seeds)
    if (not budgets or any(type(value) is not int or value < 4 for value in budgets)
            or tuple(sorted(set(budgets))) != budgets or not seeds or len(set(seeds)) != len(seeds)
            or any(type(value) is not int for value in seeds) or type(batch_size) is not int or batch_size < 1):
        raise ValueError("Simulation needs ascending unique budgets >= 4, distinct integer seeds and a positive batch size")
    if output.exists():
        raise ValueError("Simulation requires a fresh output directory")
    train_file = dataset / "matcher/train.parquet"
    labels_file = dataset / "evaluator/labelled_pairs.parquet"
    manifest_file = dataset / "manifest.json"
    records = pq.read_table(train_file).to_pylist()
    # Predicate is mandatory: do not materialize original validation/test labels.
    original_labels = pq.read_table(labels_file, filters=[("split", "=", "train")]).to_pylist()
    indexed = {row["record_id"]: row for row in records}
    labels = [{"left_id": item["left_id"], "right_id": item["right_id"], "label": item["label"],
        "left_version": indexed[item["left_id"]]["record_version_id"],
        "right_version": indexed[item["right_id"]]["record_version_id"],
        "provenance": [{"kind": "simulated_public_train_oracle", "source_split": "train",
            "original_split": item.get("original_split"), "source_label_pair": [item["left_id"], item["right_id"]]}]}
        for item in original_labels]
    source_hashes = {"manifest_sha256": _file_hash(manifest_file), "train_features_sha256": _file_hash(train_file),
        "label_file_sha256": _file_hash(labels_file), "parsed_original_train_labels_sha256": digest(original_labels)}
    snapshot = {"records": records, "labels": labels, "revision_id": "simulation:original-public-train",
        "artifact_sha256": source_hashes["train_features_sha256"], "event_cutoff": 0,
        "input_hash": digest(records), "summary": {"valid_labels": len(labels)}, "simulation": True,
        "source_hashes": source_hashes, "automatic_deployment": False}
    output.mkdir(parents=True, exist_ok=False)
    inner = output / "inner_dataset"
    manifest = _write_learning_dataset(snapshot, inner, seed=split_seed)
    for name in ("train", "validation", "test"):
        stats = manifest["statistics"][name]
        if min(stats["positive"], stats["negative"]) < 2 or stats["groups"] < 2:
            raise ValueError(f"Insufficient independent {name} classes/components; do not retry seeds to force a result")
    if budgets[-1] > manifest["statistics"]["train"]["pairs"]:
        raise ValueError("Annotation budget exceeds known-label training pool")
    plan = {"kind": "simulation", "version": "review-learning-oracle-v1", "methods": list(METHODS),
        "budgets": list(budgets), "seeds": list(seeds), "split_seed": split_seed, "batch_size": batch_size,
        "initial_random_budget": min(20, budgets[0]), "source_hashes": source_hashes,
        "original_public_partition_read": "train_only", "original_public_test_read": False,
        "inner_dataset_manifest_sha256": _file_hash(inner / "manifest.json"), "statistics": manifest["statistics"],
        "split_protocol": manifest["split_protocol"], "cross_split_pairs_dropped": 0,
        "threshold_cost": {"false_positive": 10, "false_negative": 1},
        "fixed_validation_labels_in_addition_to_acquisition_budget": manifest["statistics"]["validation"]["pairs"],
        "calibration": "reserved_independent_partition_unused", "automatic_deployment": False,
        "test_role": "inner_train_partition_holdout; never used for acquisitions or algorithm selection",
        "scope": "Supplied known labelled pairs only; not candidate retrieval or a human user study"}
    # Freeze the complete protocol before any learned scoring or test evaluation.
    _write_json(output / "plan.json", plan)
    pool_records, pool_labels = _read_split(inner, "train")
    val_records, val_labels = _read_split(inner, "validation")
    oracle = {_key(item): item for item in pool_labels}
    baseline = score_baselines(pool_records, _pairs(pool_labels))["fuzzy"]
    checkpoints = []
    for seed in seeds:
        # Both methods spend exactly the same initial labels. Scores are real,
        # although the random strategy uses only its seeded hash ordering.
        initial = rank_review_candidates(pool_records, baseline, strategy="random", seed=seed, limit=plan["initial_random_budget"])
        for method in METHODS:
            acquired_keys = [_key(item) for item in initial]
            acquired = [oracle[pair] for pair in acquired_keys]
            trace = [{"pair": list(pair), "ordinal": i + 1, "reason": "shared_initial_random"} for i, pair in enumerate(acquired_keys)]
            started = time.perf_counter()
            for budget in budgets:
                while len(acquired) < budget:
                    available = [edge for edge in baseline if _key(edge) not in set(acquired_keys)]
                    strategy = method
                    if method == "uncertainty_diversity" and {item["label"] for item in acquired} == {0, 1}:
                        model = _fit_acquired(pool_records, acquired)
                        available = model.score(pool_records, [{"left": item["left"], "right": item["right"],
                            "rules": ["simulation_known_label_pool"]} for item in available])
                    elif method == "uncertainty_diversity":
                        strategy = "random"  # Count every fallback label; never reveal until both classes appear.
                    chosen = rank_review_candidates(pool_records, available, strategy=strategy, seed=seed,
                        limit=min(batch_size, budget - len(acquired)))
                    for item in chosen:
                        pair = _key(item)
                        acquired_keys.append(pair)
                        # Oracle reveal happens only after score-only selection.
                        acquired.append(oracle[pair])
                        trace.append({"pair": list(pair), "ordinal": len(acquired), "reason": item["sampling_reason"],
                            "score_before_label_reveal": item["score"], "fallback_one_class": method != strategy})
                directory = output / f"seed_{seed}" / method / f"budget_{budget}"
                directory.mkdir(parents=True, exist_ok=False)
                _write_json(directory / "acquisitions.json", trace)
                base = {"seed": seed, "method": method, "budget": budget,
                    "acquired_positive": sum(item["label"] for item in acquired), "acquired_negative": sum(1 - item["label"] for item in acquired),
                    "acquisitions_sha256": _file_hash(directory / "acquisitions.json"),
                    "training_labels_sha256": digest(acquired), "simulation": True, "automatic_deployment": False}
                if {item["label"] for item in acquired} != {0, 1}:
                    result = base | {"status": "insufficient_training_classes", "test": None}
                    _write_json(directory / "frozen_config.json", result)
                else:
                    model = _fit_acquired(pool_records, acquired)
                    model.save(directory / "model")
                    val_scores = _score(model, val_records, val_labels)
                    selection = select_cost_threshold(val_labels, set(val_scores), val_scores, split="validation",
                        false_positive_cost=10, false_negative_cost=1)
                    frozen = base | {"status": "frozen", "model_fingerprint": model.fingerprint,
                        "validation_selection": selection, "plan_sha256": _file_hash(output / "plan.json")}
                    _write_json(directory / "frozen_config.json", frozen)
                    # This feature read is deliberately after each checkpoint's freeze.
                    test_records, test_labels = _read_split(inner, "test")
                    scores = _score(model, test_records, test_labels)
                    result = base | {"status": "evaluated", "threshold": selection["selected_threshold"],
                        "frozen_config_sha256": _file_hash(directory / "frozen_config.json"),
                        "test": evaluate_labeled_pairs(test_labels, set(scores), scores,
                            threshold=selection["selected_threshold"], score_kind="probability")}
                result["cumulative_compute_seconds"] = time.perf_counter() - started
                _write_json(directory / "report.json", result)
                checkpoints.append(result)
    aggregate = []
    for budget in budgets:
        for method in METHODS:
            rows = [item for item in checkpoints if item["budget"] == budget and item["method"] == method]
            evaluated = [item for item in rows if item["test"] is not None]
            metrics = {key: [item["test"]["end_to_end"][key] for item in evaluated
                            if item["test"]["end_to_end"][key] is not None] for key in ("precision", "recall", "f1")}
            aggregate.append({"budget": budget, "method": method, "seeds_requested": len(seeds), "seeds_evaluated": len(evaluated),
                **{f"mean_{key}": statistics.mean(values) if values else None for key, values in metrics.items()},
                "mean_acquired_positive": statistics.mean(item["acquired_positive"] for item in rows)})
    report = {"kind": "simulation", "plan_sha256": _file_hash(output / "plan.json"), "aggregate": aggregate,
        "checkpoints": checkpoints, "winner_selected": False, "original_public_test_read": False,
        "limitations": ["Oracle-known sampled pairs; no human annotation time, disagreement or missingness model",
            "Acquisition budget excludes the disclosed fixed independent validation labels",
            "Entire labelled endpoint components retained; achieved split sizes and prevalence can differ greatly",
            "Repeated seeds describe acquisition variability on one inner holdout, not dataset-level confidence",
            "Active ranking uses uncalibrated score uncertainty and endpoint diversity, not guaranteed information gain",
            "No candidate retrieval measurement; public benchmark test remains unopened by this experiment"]}
    _write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("artifacts/benchmarks/v1/dblp_acm"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budgets", default="20,50,100,200")
    parser.add_argument("--seeds", default="11,29,47")
    parser.add_argument("--split-seed", type=int, default=20260912)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()
    report = run_simulation(args.dataset, args.output, budgets=tuple(map(int, args.budgets.split(","))),
        seeds=tuple(map(int, args.seeds.split(","))), split_seed=args.split_seed, batch_size=args.batch_size)
    print(json.dumps({"output": str(args.output), "kind": "simulation", "aggregate": report["aggregate"]}, indent=2))


if __name__ == "__main__":
    main()
