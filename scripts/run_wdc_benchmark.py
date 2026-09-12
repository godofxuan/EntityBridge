"""First-scored WDC Products external diagnostic with frozen title/domain models."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
from pathlib import Path

from entitybridge.benchmark_metrics import (
    evaluate_labeled_pairs,
    group_bootstrap_intervals,
    select_cost_threshold,
)
from entitybridge.matching import score_baselines
from entitybridge.supervised import FrozenPairClassifier
from entitybridge.wdc_benchmark import (
    MEMBERS,
    PRODUCT_FEATURES,
    SOURCE_PAGE,
    VERSION,
    FrozenProductClassifier,
    corrected_validation,
    file_sha256,
    read_split,
    require_disjoint,
    split_overlap,
    verify_archives,
)

METHODS = ("exact", "fuzzy", "title_logistic", "product_logistic")


def _write(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _pairs(partition):
    return {tuple(sorted((item["left_id"], item["right_id"]))) for item in partition["labels"]}


def _scores(partition, title_model, product_model):
    pairs = _pairs(partition)
    candidates = [{"left": a, "right": b, "rules": ["official_supplied_pair"]} for a, b in sorted(pairs)]
    baselines = score_baselines(partition["records"], candidates)
    baseline_scores = {name: {tuple(sorted((item["left"], item["right"]))): item["score"] for item in edges}
                       for name, edges in baselines.items()}
    return baseline_scores | {
        "title_logistic": {tuple(sorted((item["left"], item["right"]))): item["score"]
                           for item in title_model.score(partition["records"], candidates)},
        "product_logistic": product_model.score(partition["product_records"], pairs)}


def _freeze_sources(output):
    root = Path(__file__).resolve().parents[1]
    relative_files = ["scripts/run_wdc_benchmark.py", "src/entitybridge/wdc_benchmark.py", "src/entitybridge/supervised.py",
        "src/entitybridge/normalization.py", "src/entitybridge/benchmark_metrics.py", "src/entitybridge/matching.py",
        "src/entitybridge/candidates.py", "src/entitybridge/benchmarks.py", "src/entitybridge/store.py", "pyproject.toml"]
    hashes = {}
    for relative in relative_files:
        destination = output / "source_archive" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((root / relative).read_bytes())
        hashes[relative] = file_sha256(destination)
    return hashes


def run(raw_directory, output, *, test_status):
    output = Path(output)
    if test_status not in {"first-scored-external-diagnostic", "exploratory-replayed"}:
        raise ValueError("Declare first-scored-external-diagnostic or exploratory-replayed")
    if output.exists():
        raise ValueError("Use a fresh WDC result directory")
    source_archives = verify_archives(raw_directory)
    output.mkdir(parents=True, exist_ok=False)
    code_hashes = _freeze_sources(output)
    plan = {"version": VERSION, "test_status": test_status, "source_page": SOURCE_PAGE, "source_archives": source_archives,
        "members": MEMBERS, "data_license": "not established; do not redistribute raw data", "methods": list(METHODS),
        "title_projection": "NFKC uppercase punctuation-to-space; legitimate title only in existing eight-column view",
        "product_features": list(PRODUCT_FEATURES), "product_numeric_policy": "Positive strict decimal/scientific prices, same nonempty normalized currency, min/max ratio; otherwise missing",
        "development_size": "small", "corner_case_ratio": 0.8, "test_unseen_ratio": 1.0,
        "validation_protocol": "corrected-validation external diagnostic; remove validation pairs touching training products",
        "split_changes": "Validation only, declared before full test access; never modify test", "false_positive_cost": 10, "false_negative_cost": 1,
        "exact_allowed_thresholds": [1, None], "logistic_C": 1, "calibration": "none; sample conditional diagnostics only",
        "source_archive_sha256": code_hashes, "automatic_deployment": False, "candidate_recall_measured": False,
        "public_sample_exposure": "Official schema/sample pair was inspected earlier; no claim that no test example was ever visible",
        "test_gate": "Read full test only after training, validation thresholds, protocol and source snapshots are frozen",
        "runtime": {"python": platform.python_version(), **{name: importlib.metadata.version(name)
                    for name in ("scikit-learn", "numpy", "rapidfuzz", "pyarrow")}}}
    _write(output / "plan.json", plan)
    train, original_validation = read_split(raw_directory, "train"), read_split(raw_directory, "validation")
    original_overlap = split_overlap({"train": train, "validation": original_validation})
    validation, correction = corrected_validation(train, original_validation)
    development = {"train": train, "validation": validation}
    development_audit = {"original_overlap": original_overlap, "validation_correction": correction,
                         "statistics": {name: item["statistics"] for name, item in development.items()},
                         "overlap": split_overlap(development), "source_members": {name: item["source"] for name, item in development.items()}}
    _write(output / "development_audit.json", development_audit)
    try:
        require_disjoint(development)
    except ValueError as error:
        _write(output / "blocked_audit.json", {"status": "blocked_split_overlap", "error": str(error),
            "test_read": False, "test_scored": False, "plan_sha256": file_sha256(output / "plan.json"), **development_audit})
        raise
    if validation["statistics"]["pairs"] < 10 or min(validation["statistics"]["positive"], validation["statistics"]["negative"]) < 2:
        _write(output / "blocked_audit.json", {"status": "insufficient_corrected_validation", "test_read": False,
            "test_scored": False, "development_audit_sha256": file_sha256(output / "development_audit.json")})
        raise ValueError("Insufficient corrected validation support; do not select another protocol from test")
    title_model = FrozenPairClassifier.fit(train["records"], train["labels"], allow_within_source=True)
    product_model = FrozenProductClassifier.fit(train["product_records"], train["labels"])
    title_model.save(output / "title_model")
    product_model.save(output / "product_model")
    validation_scores = _scores(validation, title_model, product_model)
    selections = {method: select_cost_threshold(validation["labels"], _pairs(validation), scores, split="validation",
        false_positive_cost=10, false_negative_cost=1, thresholds=[1] if method == "exact" else None)
        for method, scores in validation_scores.items()}
    if any(item["status"] != "selected" for item in selections.values()):
        raise ValueError("WDC validation lacks class support")
    frozen = {"plan_sha256": file_sha256(output / "plan.json"), "development_audit_sha256": file_sha256(output / "development_audit.json"),
        "title_model_fingerprint": title_model.fingerprint, "product_model_fingerprint": product_model.fingerprint,
        "validation_selection": selections, "source_archive_sha256": code_hashes, "test_read": False}
    _write(output / "frozen_config.json", frozen)
    test = read_split(raw_directory, "test")
    overlap = split_overlap(development | {"test": test})
    try:
        require_disjoint(development | {"test": test})
    except ValueError as error:
        _write(output / "blocked_audit.json", {"status": "blocked_split_overlap", "error": str(error),
            "test_read": True, "test_scored": False, "frozen_config_sha256": file_sha256(output / "frozen_config.json"),
            "overlap": overlap, "test_statistics": test["statistics"]})
        raise
    test_scores = _scores(test, title_model, product_model)
    reports = {}
    for method, scores in test_scores.items():
        threshold = selections[method]["selected_threshold"]
        metrics = evaluate_labeled_pairs(test["labels"], _pairs(test), scores, threshold=threshold,
            score_kind="probability" if "logistic" in method else "similarity")
        for key in ("candidate_known_positive_recall", "candidate_reduction_ratio", "pair_universe"):
            metrics.pop(key)
        breakdown = {}
        for name, flag in (("hard_negative", True), ("random_negative", False)):
            negative = [item for item in test["labels"] if not item["label"] and item["is_hard_negative"] is flag]
            fp = sum(threshold is not None and scores[tuple(sorted((item["left_id"], item["right_id"])))] >= threshold for item in negative)
            breakdown[name] = {"pairs": len(negative), "false_positive": fp, "false_positive_rate": fp / len(negative) if negative else None}
        intervals = group_bootstrap_intervals(test["labels"], _pairs(test), scores, threshold=threshold, n_resamples=1000)
        intervals["metrics"].pop("candidate_known_positive_recall")
        reports[method] = {"threshold": threshold, "metrics": metrics, "negative_breakdown": breakdown,
            "intervals": intervals, "scope": "classification_of_official_supplied_pairs_only"}
        _write(output / f"{method}_test_scores.json", [{"left_id": pair[0], "right_id": pair[1], "score": score}
            for pair, score in sorted(scores.items())])
    report = {"version": VERSION, "kind": "corrected-validation external diagnostic", "test_status": test_status,
        "frozen_config_sha256": file_sha256(output / "frozen_config.json"), "validation_correction": correction, "original_overlap": original_overlap,
        "statistics": {name: item["statistics"] for name, item in (development | {"test": test}).items()},
        "overlap": overlap, "test_source_member": test["source"], "methods": reports,
        "automatic_deployment": False, "candidate_recall_measured": False, "winner_selected": False,
        "limitations": ["Product-identifier-derived labels with sampled manual checks, not exhaustive human ground truth",
            "Title-only and fixed product-attribute baselines; no claim of parity with paper models or their tuning protocol",
            "Unknown pairs outside the official supplied sample remain unknown",
            "Score calibration and class prevalence are conditional on this sample",
            "Public schema/sample exposure was disclosed; first scored does not mean no test example was ever visible",
            "Product-domain diagnostic does not establish company matching generalization"]}
    _write(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-directory", type=Path, default=Path("artifacts/raw/wdc_products"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-status", required=True, choices=("first-scored-external-diagnostic", "exploratory-replayed"))
    args = parser.parse_args()
    report = run(args.raw_directory, args.output, test_status=args.test_status)
    print(json.dumps({"output": str(args.output), "methods": {method: item["metrics"]["end_to_end"]
        for method, item in report["methods"].items()}}, indent=2))


if __name__ == "__main__":
    main()
