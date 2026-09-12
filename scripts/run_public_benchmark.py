"""Compare frozen scorers on entity-disjoint, partially labelled public benchmarks.

No unseen pair is invented as a negative. Retrieval and supplied-pair classification
are separate tracks. All thresholds are selected using validation labels only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psutil
import pyarrow.parquet as pq

from entitybridge.benchmark_metrics import (
    evaluate_labeled_pairs,
    group_bootstrap_intervals,
    review_budget_curve,
    select_cost_threshold,
)
from entitybridge.candidates import FrozenNameRetriever, generate_candidates
from entitybridge.matching import SplinkMatcher, score_baselines
from entitybridge.supervised import FrozenPairClassifier


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pairs(items):
    return {tuple(sorted((item["left"], item["right"]))) for item in items}


def label_pairs(labels):
    return {tuple(sorted((item["left_id"], item["right_id"]))) for item in labels}


def build_candidate_tracks(records, labels, retriever):
    fixed = generate_candidates(records)
    hybrid = generate_candidates(records, retriever=retriever)
    supplied = [{"left": left, "right": right, "rules": ["supplied_labelled_pair_diagnostic"]}
                for left, right in sorted(label_pairs(labels))]
    return {"fixed": fixed, "hybrid": hybrid, "supplied_pairs_only": supplied}


def score_tracks(records, tracks, trained):
    union = {}
    for candidates in tracks.values():
        for candidate in candidates:
            union.setdefault((candidate["left"], candidate["right"]), candidate)
    candidates = [union[pair] for pair in sorted(union)]
    output = score_baselines(records, candidates)
    for method, model in trained.items():
        output[method] = model.score(records, candidates)
    return {method: {tuple(sorted((item["left"], item["right"]))): item["score"] for item in edges}
            for method, edges in output.items()}


def select_validation_policy(method, labels, candidate_set, values, *, false_positive_cost, false_negative_cost):
    # A binary exact-name baseline must never become "accept non-exact" merely
    # because a blocked validation sample contains no labelled candidate negatives.
    thresholds = [1.0] if method == "exact" else sorted({values[pair] for pair in values.keys() & label_pairs(labels)})
    selected = select_cost_threshold(labels, candidate_set, values, split="validation",
        false_positive_cost=false_positive_cost, false_negative_cost=false_negative_cost,
        thresholds=thresholds, return_curve=True)
    if selected["status"] != "selected":
        raise ValueError("Validation needs both label classes before selecting a test decision policy")
    return selected


def run(dataset, output, *, false_positive_cost=10.0, false_negative_cost=1.0, bootstrap=400,
        test_status="exploratory-replayed"):
    from entitybridge.benchmarks import verify_benchmark

    if output.exists():
        raise ValueError("Use a fresh output directory; frozen runs cannot be overwritten")
    verification = verify_benchmark(dataset)
    output.mkdir(parents=True)
    started = time.perf_counter()
    stages, failures = {}, {}

    def timed(name, action):
        begin = time.perf_counter()
        result = action()
        stages[name] = time.perf_counter() - begin
        return result

    def read(split):
        records = pq.read_table(dataset / "matcher" / f"{split}.parquet").to_pylist()
        labels = pq.read_table(dataset / "evaluator/labelled_pairs.parquet", filters=[("split", "=", split)]).to_pylist()
        return records, labels

    save(output / "input_verification.json", verification)
    source_files = [ROOT / "src/entitybridge" / name for name in
                    ("candidates.py", "matching.py", "normalization.py", "supervised.py", "benchmark_metrics.py", "benchmarks.py")]
    source_files.append(Path(__file__).resolve())
    with zipfile.ZipFile(output / "evaluation_source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for file in source_files:
            archive.write(file, file.relative_to(ROOT).as_posix())
    save(output / "protocol.json", {
        "written_at": datetime.now(UTC).isoformat(), "test_evaluated": False,
        "methods": ["exact", "fuzzy", "splink", "supervised_logistic"],
        "tracks": ["fixed", "hybrid", "supplied_pairs_only"],
        "candidate_settings": {"top_k": 10, "min_similarity": 0.2, "fit_split": "train"},
        "selection": "Minimum labelled validation FP/FN cost; reject-all is an explicit option",
        "exact_baseline_thresholds": [1.0], "test_status": test_status,
        "costs": {"false_positive": false_positive_cost, "false_negative": false_negative_cost},
        "bootstrap": {"replicates": bootstrap, "seed": 20260912, "group": "connected explicitly labelled-pair components"},
        "unknown_pairs": "Unknown; never implicit negatives or complete cluster truth",
        "supplied_pairs_track": "Diagnostic pair classification; candidate set supplied by benchmark, not production retrieval",
        "source_files": {file.relative_to(ROOT).as_posix(): sha(file) for file in source_files},
        "source_archive_sha256": sha(output / "evaluation_source.zip"),
        "dataset_manifest_sha256": sha(dataset / "manifest.json"), "requirements_sha256": sha(ROOT / "requirements.lock"),
    })
    try:
        train, train_labels = read("train")
        validation, validation_labels = read("validation")
        retriever = timed("fit_retriever", lambda: FrozenNameRetriever.fit(train, top_k=10, min_similarity=0.2))
        retriever.save(output / "candidate_model")
        trained = {}
        for method, fit in [("splink", lambda: SplinkMatcher.fit(train)),
                            ("supervised_logistic", lambda: FrozenPairClassifier.fit(train, train_labels))]:
            try:
                trained[method] = timed(f"fit_{method}", fit)
                trained[method].save(output / method)
            except ValueError as error:
                failures[method] = {"stage": "train", "type": type(error).__name__, "reason": str(error)}
        val_tracks = timed("validation_candidates", lambda: build_candidate_tracks(validation, validation_labels, retriever))
        val_scores = timed("validation_scores", lambda: score_tracks(validation, val_tracks, trained))
        selections = {}
        for track, candidates in val_tracks.items():
            candidate_set = pairs(candidates)
            selections[track] = {}
            for method, scores in val_scores.items():
                values = {pair: scores[pair] for pair in candidate_set}
                selections[track][method] = select_validation_policy(method, validation_labels, candidate_set, values,
                    false_positive_cost=false_positive_cost, false_negative_cost=false_negative_cost)
        frozen = {"thresholds": selections, "failures": failures,
                  "models": {method: model.fingerprint for method, model in trained.items()},
                  "candidate_model": retriever.fingerprint, "test_features_read_after_this_file": True,
                  "freeze_written_at": datetime.now(UTC).isoformat(),
                  "model_metadata": {method: (model.training_metadata if method == "splink" else model.metadata)
                                     for method, model in trained.items()}}
        save(output / "frozen_config.json", frozen)
        # Test is first scored here, after train/validation choices are on disk.
        test, test_labels = read("test")
        tracks = timed("test_candidates", lambda: build_candidate_tracks(test, test_labels, retriever))
        scores = timed("test_scores", lambda: score_tracks(test, tracks, trained))
        source_counts = Counter(row["source"] for row in test)
        if len(source_counts) != 2:
            raise ValueError("Benchmark pair universe requires exactly two test sources")
        a, b = source_counts.values()
        results = {}
        budgets = sorted({0, 10, 25, 50, 100, 250, 500})
        for track, candidates in tracks.items():
            candidate_set = pairs(candidates)
            results[track] = {}
            for method, all_scores in scores.items():
                values = {pair: all_scores[pair] for pair in candidate_set}
                selected = selections[track][method]
                threshold = selected["selected_threshold"]
                metric = evaluate_labeled_pairs(
                    test_labels, candidate_set, values, threshold=threshold, universe_pair_count=a * b,
                    score_kind="probability" if method in trained else "similarity")
                metric["validation_selection_status"] = selected["status"]
                metric["review_budget"] = review_budget_curve(test_labels, values, budgets)
                metric["group_bootstrap"] = group_bootstrap_intervals(
                    test_labels, candidate_set, values, threshold=threshold, n_resamples=bootstrap, seed=20260912)
                interpretation = []
                if metric["ranking"]["status"] == "single_class_all_positive":
                    interpretation.append("All scored labelled candidates are positive; AP=1 is trivial and is not evidence of discrimination")
                if metric["unknown_predicted_pairs"]:
                    interpretation.append("Unknown predicted pairs are excluded from labelled precision; overall prediction correctness is unmeasured")
                if (metric["group_bootstrap"]["size_balance_effective_groups"] or 0) < 20:
                    interpretation.append("Few balanced independent groups; uncertainty estimates are unstable")
                if any(item["interval"] in ([0.0, 0.0], [1.0, 1.0]) for item in metric["group_bootstrap"]["metrics"].values()):
                    interpretation.append("Boundary-degenerate empirical bootstrap cannot create unobserved errors; it does not establish perfect population performance")
                metric["interpretation_flags"] = interpretation
                metric["test_cost"] = (false_positive_cost * metric["end_to_end"]["fp"]
                                       + false_negative_cost * metric["end_to_end"]["fn"])
                results[track][method] = metric
        save(output / "scores.json", {method: [[*pair, value] for pair, value in sorted(values.items())]
                                       for method, values in scores.items()})
        report = {"benchmark": json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))["dataset"],
                  "protocol": "known-entity-disjoint-partial-labels-v2-exact-contract", "test_status": test_status,
                  "train_records": len(train), "validation_records": len(validation), "test_records": len(test),
                  "train_labelled_pairs": len(train_labels), "validation_labelled_pairs": len(validation_labels),
                  "test_labelled_pairs": len(test_labels), "test_source_counts": dict(source_counts),
                  "results": results, "unavailable_methods": failures, "seconds": stages,
                  "total_seconds": time.perf_counter() - started,
                  "process_peak_rss_bytes": getattr(psutil.Process().memory_info(), "peak_wset", psutil.Process().memory_info().rss),
                  "platform": platform.platform(), "python": platform.python_version(),
                  "frozen_config_sha256": sha(output / "frozen_config.json"),
                  "protocol_sha256": sha(output / "protocol.json"),
                  "limitations": ["Metrics conditional on provided labels; unknown pairs are not negatives",
                                  "Entity-disjoint split differs from the original published paper protocol; scores are not leaderboard-comparable",
                                  "Dropping cross-split negative pairs changes labelled class prevalence; calibration is sample-conditional",
                                  "Restaurant/paper records are cross-domain diagnostics, not legal company identities",
                                  "Supplied-pair classification is not candidate retrieval quality",
                                  "Probability outputs are not calibrated for real deployment prevalence",
                                  "Review-budget curves describe known-label recovery, not measured human time savings",
                                  "Insufficient independent groups prevent a reliable bootstrap interval"]}
        save(output / "report.json", report)
        print(json.dumps({"output": str(output), "test_records": len(test), "labelled_test_pairs": len(test_labels),
                          "methods": list(scores), "seconds": report["total_seconds"]}), flush=True)
        return report
    except Exception as error:
        save(output / "failure.json", {"type": type(error).__name__, "reason": str(error), "seconds": stages,
                                       "frozen_config_exists": (output / "frozen_config.json").exists()})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--false-positive-cost", type=float, default=10.0)
    parser.add_argument("--false-negative-cost", type=float, default=1.0)
    parser.add_argument("--bootstrap", type=int, default=400)
    parser.add_argument("--test-status", choices=["first-predeclared", "exploratory-replayed"], required=True)
    args = parser.parse_args()
    run(args.dataset.resolve(), args.output.resolve(), false_positive_cost=args.false_positive_cost,
        false_negative_cost=args.false_negative_cost, bootstrap=args.bootstrap, test_status=args.test_status)


if __name__ == "__main__":
    main()
