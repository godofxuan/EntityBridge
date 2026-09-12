"""Train upstream-derived Ditto and compare both validation threshold objectives."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import time
from dataclasses import asdict
from pathlib import Path

from run_wdc_benchmark import _pairs, _scores

from entitybridge.benchmark_metrics import evaluate_labeled_pairs, group_bootstrap_intervals
from entitybridge.ditto import DittoMatcher, file_hash
from entitybridge.ditto_training import TrainingConfig, fit_ditto, select_policies, write_json
from entitybridge.supervised import FrozenPairClassifier
from entitybridge.wdc_benchmark import (
    FrozenProductClassifier,
    corrected_validation,
    read_split,
    require_disjoint,
    split_overlap,
    verify_archives,
)

ROBERTA_SHA = "5bde1d28afb363d0103324efeb5afc8b2b397fe5e04beabb9b1ef355255ade81"
ROBERTA_REVISION = "e2da8e2f811d1448a5b465c236feacd80ffbac7b"


def run(raw_directory, base_model, output, *, device="cuda", baseline_directory=None, config=None):
    output, base_model = Path(output), Path(base_model)
    config = config or TrainingConfig()
    config.validate()
    if output.exists():
        raise ValueError("Use a fresh diagnostic directory; previous runs are never overwritten")
    archives = verify_archives(raw_directory)
    if file_hash(base_model / "model.safetensors") != ROBERTA_SHA:
        raise ValueError("RoBERTa weights differ from the official pinned SHA256")
    output.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    sources = ["scripts/run_ditto_benchmark.py", "scripts/run_wdc_benchmark.py",
        "src/entitybridge/ditto.py", "src/entitybridge/ditto_training.py",
        "src/entitybridge/vendor/ditto_model.py", "src/entitybridge/vendor/ditto_augment.py",
        "src/entitybridge/wdc_benchmark.py", "src/entitybridge/supervised.py",
        "src/entitybridge/normalization.py", "src/entitybridge/benchmark_metrics.py",
        "src/entitybridge/matching.py", "src/entitybridge/candidates.py", "src/entitybridge/store.py", "pyproject.toml"]
    hashes = {}
    for relative in sources:
        destination = output / "source_archive" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((root / relative).read_bytes())
        hashes[relative] = file_hash(destination)
    plan = {"test_status": "exploratory-replayed", "test_exposure": "WDC test previously scored in v0.3/v0.4",
        "dataset": "WDC Products 80% corner cases / small train / 100% unseen test",
        "validation": "Remove original validation pairs touching any training product; test unchanged",
        "training_config": asdict(config), "source_archives": archives, "source_code_sha256": hashes,
        "base_model": "FacebookAI/roberta-base", "base_revision": ROBERTA_REVISION, "base_weight_sha256": ROBERTA_SHA,
        "weight_hash_authority": "https://huggingface.co/FacebookAI/roberta-base/blob/main/model.safetensors",
        "checkpoint_objective": "validation F1", "threshold_objectives": ["validation F1", "validation 10FP + FN"],
        "baselines": "Reuse historical frozen weights" if baseline_directory else "Refit fixed C=1 on train only",
        "test_gate": "Model and thresholds written before test features are read",
        "device": device, "runtime": {p: importlib.metadata.version(p) for p in
            ("torch", "transformers", "safetensors", "numpy", "scikit-learn")},
        "limitations": ["Single seed with bounded local compute; not an official-paper score reproduction",
            "Product-domain supplied-pair classification, not company quality or candidate recall",
            "Both thresholds share a checkpoint chosen by validation F1; cost is a secondary diagnostic",
            "Validation prevalence changes after overlap removal; no deployment calibration",
            "No raw-data or trained-weight redistribution; dataset redistribution terms unestablished"]}
    write_json(output / "plan.json", plan)
    train, original = read_split(raw_directory, "train"), read_split(raw_directory, "validation")
    validation, correction = corrected_validation(train, original)
    require_disjoint({"train": train, "validation": validation})
    write_json(output / "development_audit.json", {"correction": correction,
        "statistics": {"train": train["statistics"], "validation": validation["statistics"]},
        "original_overlap": split_overlap({"train": train, "validation": original})})
    if baseline_directory:
        previous = Path(baseline_directory)
        title = FrozenPairClassifier.load(previous / "title_model")
        product = FrozenProductClassifier(json.loads((previous / "product_model/classifier.json").read_text(encoding="utf-8")))
        declared = json.loads((previous / "product_model/manifest.json").read_text(encoding="utf-8"))
        if product.fingerprint != declared["fingerprint"]:
            raise ValueError("Historical product model fingerprint differs")
    else:
        title = FrozenPairClassifier.fit(train["records"], train["labels"], allow_within_source=True)
        product = FrozenProductClassifier.fit(train["product_records"], train["labels"])
    title.save(output / "title_model")
    product.save(output / "product_model")
    policies = {method: select_policies(validation["labels"], scores, exact=method == "exact")
                for method, scores in _scores(validation, title, product).items()}
    training = fit_ditto(train["product_records"], train["labels"], validation["product_records"], validation["labels"],
        domain="product", base_model=base_model, output=output / "training", config=config, device=device,
        provenance={"benchmark_plan_sha256": file_hash(output / "plan.json"), "base_revision": ROBERTA_REVISION})
    policies["ditto"] = training["validation_policies"]
    frozen = {"plan_sha256": file_hash(output / "plan.json"), "training": training,
        "validation_policies": policies, "title_model_fingerprint": title.fingerprint,
        "product_model_fingerprint": product.fingerprint, "test_read": False}
    write_json(output / "frozen_config.json", frozen)
    model = DittoMatcher.load(output / "training/model", device=device, batch_size=config.batch_size)
    if model.fingerprint != training["model_fingerprint"]:
        raise ValueError("Frozen model changed before test access")
    test = read_split(raw_directory, "test")
    partitions = {"train": train, "validation": validation, "test": test}
    require_disjoint(partitions)
    started = time.perf_counter()
    scores = _scores(test, title, product)
    baseline_seconds = time.perf_counter() - started
    started = time.perf_counter()
    scores["ditto"] = model.score_pairs(test["product_records"], sorted(_pairs(test)))
    inference_seconds = time.perf_counter() - started
    reports = {}
    for method, values in scores.items():
        reports[method] = {}
        write_json(output / (method + "_test_scores.json"),
                   [{"left_id": a, "right_id": b, "score": s} for (a, b), s in sorted(values.items())])
        for objective, selection in policies[method].items():
            threshold = selection["threshold"]
            metrics = evaluate_labeled_pairs(test["labels"], _pairs(test), values, threshold=threshold,
                score_kind="similarity" if method in {"exact", "fuzzy"} else "probability")
            for key in ("candidate_known_positive_recall", "candidate_reduction_ratio", "pair_universe"):
                metrics.pop(key)
            confusion = metrics["end_to_end"]
            intervals = group_bootstrap_intervals(test["labels"], _pairs(test), values, threshold=threshold)
            intervals["metrics"].pop("candidate_known_positive_recall")
            breakdown = {}
            for name, flag in (("hard_negative", True), ("random_negative", False)):
                negatives = [r for r in test["labels"] if not r["label"] and r["is_hard_negative"] is flag]
                fp = sum(threshold is not None and values[tuple(sorted((r["left_id"], r["right_id"])))] >= threshold
                         for r in negatives)
                breakdown[name] = {"pairs": len(negatives), "false_positive": fp}
            reports[method][objective] = {"threshold": threshold, "metrics": metrics, "intervals": intervals,
                "test_10FP_plus_FN": 10 * confusion["fp"] + confusion["fn"], "negative_breakdown": breakdown}
    report = {"test_status": "exploratory-replayed", "frozen_config_sha256": file_hash(output / "frozen_config.json"),
        "methods": reports, "training": training, "statistics": {k: p["statistics"] for k, p in partitions.items()},
        "overlap": split_overlap(partitions), "ditto_test_seconds": inference_seconds,
        "four_baselines_test_seconds": baseline_seconds, "candidate_recall_measured": False,
        "automatic_deployment": False, "limitations": plan["limitations"]}
    write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-directory", type=Path, default=Path("artifacts/raw/wdc_products"))
    parser.add_argument("--base-model", type=Path, default=Path("artifacts/models/roberta-base"))
    parser.add_argument("--baseline-directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()
    report = run(args.raw_directory, args.base_model, args.output, device=args.device,
                 baseline_directory=args.baseline_directory)
    print(json.dumps({method: {objective: row["metrics"]["end_to_end"] for objective, row in methods.items()}
                      for method, methods in report["methods"].items()}, indent=2), flush=True)


if __name__ == "__main__":
    main()
