"""Finite development, immutable freeze, exploratory replay and external transfer."""
from __future__ import annotations

import hashlib
import json
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .benchmark_metrics import evaluate_labeled_pairs, review_budget_curve
from .benchmarks import file_hash, verify_benchmark
from .candidates import FrozenNameRetriever, generate_candidates
from .company_matching import FrozenCompanyMatcher, calibrate_scores, fit_platt, load_study_model
from .country import country_candidate_view
from .ditto_training import select_policies
from .feiii_benchmark import read_archive
from .madi_benchmark import SOURCE_PAIRS
from .supervised import FrozenPairClassifier

MODELS = ("legacy_lr", "iso_lr", "company_lr", "company_gb")
TRACKS = ("fixed_legacy", "hybrid_legacy", "hybrid_iso", "hybrid_soft", "supplied_pairs_only")


def save(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                                    allow_nan=False) + "\n", encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def now():
    return datetime.now(UTC).isoformat()


def pair(row):
    return tuple(sorted((row.get("left", row.get("left_id")), row.get("right", row.get("right_id")))))


def partition(dataset, split):
    if split not in {"train", "validation", "test"}:
        raise ValueError("Unknown partition")
    return (pq.read_table(dataset / "matcher" / f"{split}.parquet").to_pylist(),
            pq.read_table(dataset / "evaluator/labelled_pairs.parquet", filters=[("split", "=", split)]).to_pylist())


def development_split(labels):
    groups = {r["group_id"]: int(hashlib.sha256(("calibration-v1:" + r["group_id"]).encode()).hexdigest(), 16) % 2
              for r in labels}
    halves = [[r for r in labels if groups[r["group_id"]] == half] for half in (0, 1)]
    if any({r["label"] for r in rows} != {0, 1} for rows in halves):
        raise ValueError("Fixed development halves require both classes; no seed search allowed")
    if {r[k] for r in halves[0] for k in ("left_id", "right_id")} & {
            r[k] for r in halves[1] for k in ("left_id", "right_id")}:
        raise ValueError("Calibration and policy halves share records")
    return halves


def make_tracks(records, labels, retriever):
    tracks = {name: [] for name in TRACKS}
    for a, b in SOURCE_PAIRS:
        subset = [r for r in records if r["source"] in (a, b)]
        tracks["fixed_legacy"].extend(generate_candidates(subset))
        tracks["hybrid_legacy"].extend(generate_candidates(subset, retriever=retriever))
        tracks["hybrid_iso"].extend(generate_candidates(country_candidate_view(subset), retriever=retriever))
        tracks["hybrid_soft"].extend(generate_candidates([r | {"country": None} for r in subset], retriever=retriever))
    tracks["supplied_pairs_only"] = [{"left": a, "right": b, "rules": ["supplied_diagnostic"]}
                                      for a, b in sorted({pair(r) for r in labels})]
    return tracks


def union(tracks):
    by_pair = {pair(r): r for rows in tracks.values() for r in rows}
    return [by_pair[key] for key in sorted(by_pair)]


def score(model, records, candidates):
    edges = (model.score(records, candidates, calibrated=False) if isinstance(model, FrozenCompanyMatcher)
             else model.score(records, candidates))
    return {pair(r): r["score"] for r in edges}


def packed(scores):
    return {name: [[*key, value] for key, value in sorted(data.items())] for name, data in scores.items()}


def hashes(directory):
    return {p.relative_to(directory).as_posix(): file_hash(p) for p in sorted(directory.rglob("*")) if p.is_file()}


def verify_hashes(directory, expected):
    for relative, sha in expected.items():
        p = (directory / relative).resolve()
        if not p.is_relative_to(directory.resolve()) or file_hash(p) != sha:
            raise ValueError(f"Frozen study input changed: {relative}")


def policies(labels, tracks, scores):
    return {track: {name: select_policies(labels, {pair(r): values[pair(r)] for r in candidates})
                   for name, values in scores.items()} for track, candidates in tracks.items()}


def summarize(labels, tracks, scores, selections, raw=None):
    report = {}
    for track, candidates in tracks.items():
        report[track] = {}
        for name, values in scores.items():
            subset = {pair(r): values[pair(r)] for r in candidates}
            policy_report = {}
            for policy in ("f1", "cost_10_1"):
                result = evaluate_labeled_pairs(labels, subset, subset,
                    threshold=selections[track][name][policy]["threshold"], score_kind="probability")
                result["cost_10FP_FN"] = 10 * result["end_to_end"]["fp"] + result["end_to_end"]["fn"]
                policy_report[policy] = result
            data = {"policies": policy_report, "review_budgets": review_budget_curve(labels, subset, (25, 50, 100))}
            if raw is not None:
                raw_subset = {key: raw[name][key] for key in subset}
                data["uncalibrated_diagnostic"] = evaluate_labeled_pairs(labels, raw_subset, raw_subset,
                    threshold=None, score_kind="probability")["calibration"]
            report[track][name] = data
    return report


def choose_model(validation):
    scores = validation["supplied_pairs_only"]
    return min(MODELS, key=lambda name: (-scores[name]["review_budgets"][-1]["known_positive_pairs"],
        scores[name]["policies"]["cost_10_1"]["cost_10FP_FN"],
        -(scores[name]["policies"]["f1"]["ranking"]["average_precision"] or 0), MODELS.index(name)))


def paired_comparison(labels, scores, selections, selected, *, n_resamples=1000):
    if selected == "legacy_lr":
        return {"status": "baseline_selected", "difference": "legacy_lr_minus_legacy_lr"}
    names = ["legacy_lr", selected]
    keys = [pair(r) for r in labels]
    group_names = sorted({r["group_id"] for r in labels})
    group_map = {name: i for i, name in enumerate(group_names)}
    groups = np.array([group_map[r["group_id"]] for r in labels])
    sizes = np.bincount(groups)
    info = {"groups": len(group_names), "largest_group_fraction": int(max(sizes)) / len(labels),
            "size_balance_effective_groups": len(labels)**2 / int(sum(sizes**2)), "replicates": n_resamples,
            "difference": selected + "_minus_legacy_lr", "seed": 20260915,
            "scope": "conditional fixed-model paired dependency-group bootstrap; no training uncertainty"}
    if len(group_names) < 2 or info["largest_group_fraction"] > .5:
        return info | {"status": "insufficient_independent_groups"}
    # Reject bad group declarations instead of reporting spuriously narrow CIs.
    seen = {}
    for row in labels:
        for endpoint in pair(row):
            if seen.setdefault(endpoint, row["group_id"]) != row["group_id"]:
                raise ValueError("Shared endpoint crosses bootstrap groups")
    y = np.array([r["label"] for r in labels])
    orders = [np.array(sorted(range(len(keys)), key=lambda i: (-scores[name][keys[i]], keys[i]))) for name in names]
    preds = {policy: np.array([[selections[name][policy]["threshold"] is not None and
        scores[name][key] >= selections[name][policy]["threshold"] for key in keys] for name in names])
        for policy in ("f1", "cost_10_1")}

    def metrics(weights):
        captures = []
        for order in orders:
            ordered = weights[order]
            used = np.minimum(ordered, np.maximum(100 - (np.cumsum(ordered) - ordered), 0))
            captures.append(float(used @ y[order]))
        result = {"known_positives_at100": captures[1] - captures[0]}
        for policy, predicted in preds.items():
            tp, fp, fn = (predicted * y) @ weights, (predicted * (1 - y)) @ weights, ((1 - predicted) * y) @ weights
            if policy == "f1":
                denom = 2 * tp + fp + fn
                values = np.divide(2 * tp, denom, out=np.zeros(2, dtype=float), where=denom != 0)
                result["f1"] = float(values[1] - values[0])
            else:
                costs = 10 * fp + fn
                result["cost_10FP_FN"] = float(costs[1] - costs[0])
        return result
    rng = np.random.default_rng(20260915)
    samples = [metrics(np.bincount(rng.integers(0, len(group_names), len(group_names)),
                        minlength=len(group_names))[groups]) for _ in range(n_resamples)]
    point = metrics(np.ones(len(labels), dtype=int))
    return info | {"status": "estimated", "intervals": {name: {"estimate": value,
        "interval": np.quantile([s[name] for s in samples], [.025, .975]).tolist()} for name, value in point.items()}}


def prepare(root, dataset, previous, output):
    if output.exists():
        raise ValueError("Optimization output must be new; never overwrite completed or partial runs")
    protocol_path = root / "docs/evaluation/COMPANY_OPTIMIZATION_PROTOCOL.json"
    protocol = read(protocol_path)
    if protocol["version"] != "company-optimization-20260915-v1":
        raise ValueError("Unknown optimization protocol")
    verification = verify_benchmark(dataset)
    source_paths = list((root / "src/entitybridge").rglob("*.py")) + [protocol_path,
        root / "scripts/run_company_optimization.py", root / "requirements.lock"]
    source_hashes = {p.relative_to(root).as_posix(): file_hash(p) for p in source_paths}
    output.mkdir(parents=True)
    save(output / "pre_fit.json", {"at": now(), "verification": verification,
        "source_files": source_hashes, "dataset_files": hashes(dataset), "external_features_read": False,
        "madi_test_status": "previously_seen_exploratory_replay_only"})
    with zipfile.ZipFile(output / "source.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for p in source_paths:
            archive.write(p, p.relative_to(root).as_posix())
    train, train_labels = partition(dataset, "train")
    val, val_labels = partition(dataset, "validation")
    calibration_labels, policy_labels = development_split(val_labels)
    # Partition the records as well, so top-k candidates cannot cross calibration
    # and policy entity groups. Unlabelled val records are not added to a half.
    policy_ids = {r[k] for r in policy_labels for k in ("left_id", "right_id")}
    policy_records = [r for r in val if r["record_id"] in policy_ids]
    retriever = FrozenNameRetriever.load(previous / "retriever")
    retriever.save(output / "retriever")
    tracks = make_tracks(policy_records, policy_labels, retriever)
    all_candidates = union(tracks | {"calibration": [
        {"left": r["left_id"], "right": r["right_id"], "rules": ["calibration"]} for r in calibration_labels]})
    raw, calibrated, calibrations, timings, metadata = {}, {}, {}, {}, {}
    for name in MODELS:
        started = time.perf_counter()
        model = (FrozenPairClassifier.fit(train, train_labels) if name == "legacy_lr"
                 else FrozenCompanyMatcher.fit(train, train_labels, variant=name))
        timings[name] = {"fit_seconds": time.perf_counter() - started}
        raw[name] = score(model, val, all_candidates)
        calibrations[name] = fit_platt(calibration_labels, raw[name])
        calibrated[name] = calibrate_scores(raw[name], calibrations[name])
        if name != "legacy_lr":
            model = model.with_calibration(calibrations[name])
        model.save(output / name)
        metadata[name] = model.metadata
        timings[name]["model_bytes"] = sum(p.stat().st_size for p in (output / name).iterdir())
    selections = policies(policy_labels, tracks, calibrated)
    results = summarize(policy_labels, tracks, calibrated, selections, raw)
    selected = choose_model(results)
    save(output / "validation_results.json", results)
    save(output / "validation_scores.json", packed(calibrated))
    save(output / "validation_tracks.json", tracks)
    save(output / "development_assignment.json", {"calibration": calibration_labels, "policy": policy_labels})
    save(output / "development_names.json", sorted({r["name"] for r in train + val if r.get("name")}))
    frozen = {"at": now(), "selected_model": selected, "calibration": calibrations,
        "policies": selections, "metadata": metadata, "timings": timings,
        "validation_partition": {"calibration": dict(Counter(str(r["label"]) for r in calibration_labels)),
            "policy": dict(Counter(str(r["label"]) for r in policy_labels)), "policy_records": len(policy_records)},
        "source_files": source_hashes, "dataset_files": hashes(dataset), "frozen_files": hashes(output),
        "external_features_read_after_this_file": True, "protocol_sha256": file_hash(protocol_path)}
    save(output / "frozen_config.json", frozen)
    print(json.dumps({"stage": "frozen", "selected": selected, "validation": frozen["validation_partition"]}), flush=True)


def evaluate(root, dataset, output, feiii_archive):
    frozen = read(output / "frozen_config.json")
    if (output / "evaluation_started.json").exists():
        raise ValueError("Evaluation already started; investigate interruption without overwriting evidence")
    verify_hashes(root, frozen["source_files"])
    verify_hashes(dataset, frozen["dataset_files"])
    verify_hashes(output, frozen["frozen_files"])
    save(output / "evaluation_started.json", {"at": now(), "frozen_sha256": file_hash(output / "frozen_config.json")})
    records, labels = partition(dataset, "test")
    tracks = make_tracks(records, labels, FrozenNameRetriever.load(output / "retriever"))
    raw, calibrated, times = {}, {}, {}
    models = {name: load_study_model(output, name) for name in MODELS}
    for name, model in models.items():
        start = time.perf_counter()
        raw[name] = score(model, records, union(tracks))
        calibrated[name] = calibrate_scores(raw[name], frozen["calibration"][name])
        times[name] = time.perf_counter() - start
    report = {"at": now(), "selected_model": frozen["selected_model"], "protocol_sha256": frozen["protocol_sha256"],
        "madi_replay": summarize(labels, tracks, calibrated, frozen["policies"], raw), "scoring_seconds": times,
        "madi_comparison": paired_comparison(labels, calibrated, frozen["policies"]["supplied_pairs_only"],
                                             frozen["selected_model"]),
        "development": {"timings": frozen["timings"], "partitions": frozen["validation_partition"],
                        "results": read(output / "validation_results.json")}}
    save(output / "madi_test_scores.json", packed(calibrated))
    save(output / "madi_test_tracks.json", tracks)
    save(output / "madi_replay_report.json", report)
    print(json.dumps({"stage": "madi_replay_scored", "tracks": {k: len(v) for k, v in tracks.items()}}), flush=True)
    finish_external(output, feiii_archive, frozen, report, models)


def finish_external(output, feiii_archive, frozen, report, models):
    external = read_archive(feiii_archive)
    save(output / "feiii_adapter_audit.json", external["audit"])
    save(output / "feiii_private_cohort.json", external)
    candidates = [{"left": r["left_id"], "right": r["right_id"], "rules": ["external_supplied"]} for r in external["labels"]]
    external_raw = {name: score(model, external["records"], candidates) for name, model in models.items()}
    external_scores = {name: calibrate_scores(data, frozen["calibration"][name]) for name, data in external_raw.items()}
    development_names = set(read(output / "development_names.json"))
    overlapping = {r["record_id"] for r in external["records"] if r.get("name") in development_names}
    report["external"] = {"audit": external["audit"], "exact_name_overlap_records": len(overlapping), "cohorts": {}}
    for source_pair in ("ffiec_lei", "ffiec_sec"):
        for scope in ("all_adjudicated", "exclude_exact_development_name_overlap"):
            selected_labels = [r for r in external["labels"] if r["source_pair"] == source_pair and
                (scope == "all_adjudicated" or not set(pair(r)) & overlapping)]
            pairs = {pair(r) for r in selected_labels}
            subset = [r for r in candidates if pair(r) in pairs]
            selected_scores = {name: {key: value for key, value in data.items() if key in pairs}
                               for name, data in external_scores.items()}
            result = summarize(selected_labels, {"supplied_pairs_only": subset}, selected_scores,
                               frozen["policies"], external_raw)
            comparison = paired_comparison(selected_labels, selected_scores, frozen["policies"]["supplied_pairs_only"],
                                           frozen["selected_model"])
            report["external"]["cohorts"][source_pair + ":" + scope] = {"results": result, "comparison": comparison}
    save(output / "feiii_scores.json", packed(external_scores))
    save(output / "report.json", report)
    save(output / "evidence_sha256.json", hashes(output))
    print(json.dumps({"stage": "complete", "selected_model": frozen["selected_model"],
                      "external_labels": external["audit"]["retained_labels"]}), flush=True)


def resume_external(root, dataset, output, feiii_archive):
    """Resume only the failed data-reader step; no refit, selection or replay."""
    frozen = read(output / "frozen_config.json")
    if (output / "report.json").exists() or (output / "external_recovery.json").exists():
        raise ValueError("External recovery already attempted; preserve evidence")
    verify_hashes(dataset, frozen["dataset_files"])
    verify_hashes(output, frozen["frozen_files"])
    allowed = {"src/entitybridge/feiii_benchmark.py", "src/entitybridge/company_study.py",
               "scripts/run_company_optimization.py"}
    differences = {name: {"frozen_sha256": sha, "current_sha256": file_hash(root / name)}
                   for name, sha in frozen["source_files"].items() if file_hash(root / name) != sha}
    if not differences or set(differences) - allowed:
        raise ValueError("Recovery only permits documented external-reader changes")
    report = read(output / "madi_replay_report.json")
    receipt = {"at": now(), "reason": "Official LEI CSV has invalid UTF-8 and Windows-1252 bytes; use official Unicode XLSX in same pinned archive.",
               "changes": differences, "frozen_config_sha256": file_hash(output / "frozen_config.json"),
               "preserved_replay_sha256": file_hash(output / "madi_replay_report.json"),
               "refitted_models": 0, "reselected_thresholds": 0, "external_scores_observed_before_fix": False}
    save(output / "external_recovery.json", receipt)
    with zipfile.ZipFile(output / "external_recovery_source.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name in differences:
            archive.write(root / name, name)
    report["reader_recovery"] = receipt
    models = {name: load_study_model(output, name) for name in MODELS}
    finish_external(output, feiii_archive, frozen, report, models)
