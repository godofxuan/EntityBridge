"""Freeze train/validation choices before a single final test evaluation.

All model inputs come from matcher/; only entitybridge.evaluation reads truth.
Results are kept in a fresh directory. Do not reuse a test result for tuning.
"""
import argparse
import hashlib
import json
import logging
import platform
import time
from collections import Counter
from pathlib import Path

import psutil
import pyarrow.parquet as pq

from entitybridge.candidates import BLOCKING_VERSION, FrozenNameRetriever, generate_candidates
from entitybridge.evaluation import evaluate_clusters, evaluate_pairs, known_pairs, load_truth, verify_dataset
from entitybridge.matching import SplinkMatcher, score_baselines
from entitybridge.normalization import FEATURE_VIEW_VERSION, NORMALIZATION_VERSION
from entitybridge.resolution import Edge, resolve


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frozen-model", type=Path)
    parser.add_argument("--candidate-mode", choices=("fixed", "hybrid-name"), default="fixed")
    parser.add_argument("--candidate-top-k", type=int, default=10)
    parser.add_argument("--candidate-min-similarity", type=float, default=0.2)
    parser.add_argument("--candidate-validation-grid", action="store_true",
                        help="Compare a bounded retrieval grid using train/validation only, then exit")
    parser.add_argument("--test-status", choices=("held-out", "exploratory-reused"),
                        help="Must mark already inspected test data as exploratory-reused")
    args = parser.parse_args()
    if args.candidate_mode == "hybrid-name" and not args.test_status and not args.candidate_validation_grid:
        parser.error("--test-status must be explicit for a new candidate strategy")
    args.output.mkdir(parents=True, exist_ok=False)
    logging.getLogger("splink").setLevel(logging.WARNING)
    started = time.perf_counter()
    times = {}
    def timed(name, fn):
        begin = time.perf_counter()
        result = fn()
        times[name] = time.perf_counter() - begin
        print(f"{name}: {times[name]:.3f}s", flush=True)
        return result
    try:
        preflight = timed("input_preflight", lambda: verify_dataset(args.dataset))
        save(args.output / "input_verification.json", preflight)
        train = timed("read_train", lambda: pq.read_table(args.dataset / "matcher/train.parquet").to_pylist())
        if args.candidate_validation_grid:
            val = pq.read_table(args.dataset / "matcher/validation.parquet").to_pylist()
            truth = load_truth(args.dataset / "evaluator/truth_map.parquet", "validation")
            grid = []
            for top_k, minimum in ((3, 0.2), (10, 0.2), (20, 0.2), (10, 0.35)):
                begin = time.perf_counter()
                retrieval = FrozenNameRetriever.fit(train, top_k=top_k, min_similarity=minimum)
                candidates = generate_candidates(val, retriever=retrieval)
                metric = evaluate_pairs([], [(c["left"], c["right"]) for c in candidates], truth)
                grid.append({"top_k": top_k, "min_similarity": minimum,
                    "candidate_recall": metric["candidate_recall"], "candidates": len(candidates),
                    "seconds": time.perf_counter() - begin})
            target = 0.95 * max(row["candidate_recall"] for row in grid)
            selected = min((row for row in grid if row["candidate_recall"] >= target),
                           key=lambda row: (row["candidates"], row["top_k"], -row["min_similarity"]))
            save(args.output / "candidate_validation_grid.json", {"grid": grid,
                "selection_rule": "Fewest candidates retaining at least 95% of best grid validation recall",
                "selected": selected, "test_effect_evaluated": False})
            return
        retriever = None
        if args.candidate_mode == "hybrid-name":
            retriever = timed("fit_candidate_retriever", lambda: FrozenNameRetriever.fit(
                train, top_k=args.candidate_top_k, min_similarity=args.candidate_min_similarity))
            retriever.save(args.output / "candidate_model")
        def candidate_builder(records):
            return generate_candidates(records, retriever=retriever)
        model = timed("fit_splink", lambda: SplinkMatcher.load(args.frozen_model) if args.frozen_model else SplinkMatcher.fit(train))
        train_hash = hashlib.sha256(json.dumps(sorted(train, key=lambda row: row["record_id"]),
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if model.training_metadata.get("training_feature_sha256") != train_hash:
            raise ValueError("Frozen model training provenance does not match the permitted train split")
        model.save(args.output / "model")
        val = pq.read_table(args.dataset / "matcher/validation.parquet").to_pylist()
        candidates = timed("validation_candidates", lambda: candidate_builder(val))
        scores = score_baselines(val, candidates)
        scores["splink"] = timed("validation_splink", lambda: model.score(val, candidates))
        truth = load_truth(args.dataset / "evaluator/truth_map.parquet", "validation")
        candidate_pairs = [(item["left"], item["right"]) for item in candidates]
        thresholds, curves = {}, {}
        for method, edges in scores.items():
            grid = [1.0] if method == "exact" else [0.5, 0.7, 0.8, 0.85, 0.9, 0.95, 0.98, 0.99, 0.995, 0.999, 0.9999, 1.0]
            curves[method] = []
            for threshold in grid:
                predictions = [(e["left"], e["right"]) for e in edges if e["score"] >= threshold]
                measurement = evaluate_pairs(predictions, candidate_pairs, truth)
                curves[method].append({"threshold": threshold, **measurement})
            best = max(curves[method], key=lambda item: (item["end_to_end"]["f1"], item["end_to_end"]["precision"], item["threshold"]))
            thresholds[method] = best["threshold"]
        frozen = {"threshold_selection": "maximum validation F1, then precision, then larger threshold",
            "thresholds": thresholds, "model_fingerprint": model.fingerprint,
            "dataset_manifest_sha256": hashlib.sha256((args.dataset / "manifest.json").read_bytes()).hexdigest(),
            "lock_sha256": hashlib.sha256(Path("requirements.lock").read_bytes()).hexdigest(),
            "code_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in
                sorted([*Path("src/entitybridge").glob("*.py"), Path(__file__)]))).hexdigest(),
            "normalization_version": NORMALIZATION_VERSION, "feature_view_version": FEATURE_VIEW_VERSION,
            "blocking_version": BLOCKING_VERSION, "resolver_version": "greedy-v1",
            "candidate_mode": args.candidate_mode,
            "candidate_retriever": {**retriever.metadata, "fingerprint": retriever.fingerprint} if retriever else None,
            "candidate_full_recompute_required": retriever is not None,
            "test_status": args.test_status or "legacy-test-status-not-declared",
            "test_effect_evaluation_after_config_write": True,
            "preflight_reads_test_ids_and_hashes_only_for_integrity": True}
        save(args.output / "frozen_config.json", frozen)
        save(args.output / "validation_curves.json", curves)
        test = pq.read_table(args.dataset / "matcher/test.parquet").to_pylist()
        test_truth = load_truth(args.dataset / "evaluator/truth_map.parquet", "test")
        candidates = timed("test_candidates", lambda: candidate_builder(test))
        scores = timed("test_baselines", lambda: score_baselines(test, candidates))
        scores["splink"] = timed("test_splink", lambda: model.score(test, candidates))
        pairs = [(item["left"], item["right"]) for item in candidates]
        results, errors = {}, {}
        records_by_id = {r["record_id"]: r for r in test}
        for method, edges in scores.items():
            threshold = thresholds[method]
            predictions = [(e["left"], e["right"]) for e in edges if e["score"] >= threshold]
            clusters = timed(f"test_resolve_{method}", lambda edges=edges, threshold=threshold: resolve(test_truth, [Edge(e["left"], e["right"], e["score"]) for e in edges], threshold=threshold))
            results[method] = {"threshold": threshold, **evaluate_pairs(predictions, pairs, test_truth),
                "clusters": evaluate_clusters(clusters.partitions, test_truth),
                "review_pairs": sum(0.5 <= e["score"] < threshold for e in edges)}
            if method == "splink":
                # No hidden registry-derived constraints: B3 equals B2 in this
                # benchmark until independent review labels have been collected.
                results["splink_constrained_no_human_labels"] = dict(results[method])
            positives = known_pairs(test_truth)
            missed_candidates = positives - set(pairs)
            missed_scores = (positives & set(pairs)) - set(predictions)
            wrong = set(predictions) - positives
            errors[method] = {"candidate_misses": len(missed_candidates), "score_misses": len(missed_scores),
                "false_positives": len(wrong), "examples": {kind: [
                    {"left": records_by_id[a], "right": records_by_id[b]} for a, b in sorted(items)[:10]]
                    for kind, items in [("candidate_miss", missed_candidates), ("score_miss", missed_scores), ("false_positive", wrong)]}}
        source_counts = Counter(r["source"] for r in test)
        possible = 1
        for count in source_counts.values():
            possible *= count
        # Execute the full declared scale too. Its labels are not used to tune.
        all_records = train + val + test
        full_candidates = timed("full_scale_candidates", lambda: candidate_builder(all_records))
        full_scores = timed("full_scale_splink", lambda: model.score(all_records, full_candidates))
        full_resolved = timed("full_scale_resolve", lambda: resolve((r["record_id"] for r in all_records),
            [Edge(e["left"], e["right"], e["score"]) for e in full_scores], threshold=thresholds["splink"]))
        import pyarrow as pa
        pq.write_table(pa.Table.from_pylist(full_scores), args.output / "full_scored_edges.parquet")
        report = {"kind": json.loads((args.dataset / "manifest.json").read_text())["kind"],
            "test_status": frozen["test_status"], "candidate_mode": args.candidate_mode,
            "candidate_retriever": frozen["candidate_retriever"],
            "full_scale_records": len(all_records), "full_scale_candidates": len(full_candidates),
            "full_scale_entities": len(full_resolved.partitions), "test_records": len(test),
            "test_true_entities": len(set(test_truth.values())), "test_source_counts": dict(source_counts),
            "test_candidate_reduction": 1 - len(candidates) / possible,
            "results": results, "seconds": times, "total_seconds": time.perf_counter() - started,
            "process_peak_rss_bytes": getattr(psutil.Process().memory_info(), "peak_wset", psutil.Process().memory_info().rss),
            "platform": platform.platform(), "python": platform.python_version(), "logical_cpus": psutil.cpu_count(),
            "ram_bytes": psutil.virtual_memory().total, "model_training": model.training_metadata,
            "limitations": ["Closed-world clean registry overlap; not all unnumbered companies", "GLEIF and CH are not independent business noise",
                "Uncalibrated model probabilities", "No human labels in A; B3 has no advantage from hidden identifiers",
                "Wilson intervals describe pair counts, not independent entity-level confidence"]}
        save(args.output / "report.json", report)
        save(args.output / "errors.json", errors)
        print(json.dumps({"full_scale_records": len(all_records), "candidate_pairs": len(full_candidates),
            "test_results": results, "total_seconds": report["total_seconds"]}, indent=2), flush=True)
    except Exception as exc:
        save(args.output / "failure.json", {"type": type(exc).__name__, "detail": str(exc), "seconds": times})
        raise


if __name__ == "__main__":
    main()
