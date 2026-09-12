"""Pure metrics for explicit, partially labelled record pairs.

Missing labels are unknown, never implicit negatives. Labels belong only in the
evaluator. These sample-conditional metrics do not estimate population accuracy.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping
from numbers import Real

Pair = tuple[str, str]


def _pair(value) -> Pair:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("Pairs must contain exactly two record IDs")
    if any(not isinstance(item, str) or not item for item in value) or value[0] == value[1]:
        raise ValueError("Pairs require distinct nonempty string record IDs")
    return tuple(sorted(value))


def _labels(rows: Iterable[Mapping]) -> dict[Pair, dict]:
    result = {}
    for row in rows:
        pair = _pair((row["left_id"], row["right_id"]))
        if type(row["label"]) is not int or row["label"] not in (0, 1):
            raise ValueError("Labels must be explicit integers 0 or 1; omit unknown pairs")
        if pair in result:
            raise ValueError("Duplicate or conflicting labelled pair")
        result[pair] = dict(row)
    return result


def _scores(values: Mapping[Pair, float]) -> dict[Pair, float]:
    result = {}
    for key, score in values.items():
        pair = _pair(key)
        if not isinstance(score, Real) or not math.isfinite(score):
            raise ValueError("Scores must be finite real numbers")
        if pair in result:
            raise ValueError("Duplicate score for an unordered pair")
        result[pair] = float(score)
    return result


def _inputs(labels, candidates, scores):
    labeled, scored = _labels(labels), _scores(scores)
    candidate_set = {_pair(pair) for pair in candidates}
    if scored.keys() != candidate_set:
        raise ValueError("Scores must exactly cover the declared candidate set")
    return labeled, candidate_set, scored


def _threshold(value):
    if value is not None and (not isinstance(value, Real) or not math.isfinite(value)):
        raise ValueError("Threshold must be finite, or None for reject_all")


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _confusion(tp, fp, fn, tn):
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn),
            "f1": _ratio(2 * tp, 2 * tp + fp + fn)}


def _ranking(labeled, scored):
    pairs = sorted(labeled.keys() & scored.keys())
    labels = [labeled[pair]["label"] for pair in pairs]
    scores = [scored[pair] for pair in pairs]
    positives = sum(labels)
    result = {"scope": "scored_labeled_candidates_only", "pairs": len(pairs),
              "positive_fraction": _ratio(positives, len(pairs)), "average_precision": None,
              "pr_auc_trapezoid": None, "roc_auc": None,
              "status": "no_labeled_candidates" if not pairs else "no_positive_labels"}
    if positives:
        from sklearn.metrics import auc, average_precision_score, precision_recall_curve, roc_auc_score
        result["average_precision"] = float(average_precision_score(labels, scores))
        precision, recall, _ = precision_recall_curve(labels, scores)
        result["pr_auc_trapezoid"] = float(auc(recall, precision))
        result["status"] = "single_class_all_positive"
        if positives != len(pairs):
            result["roc_auc"] = float(roc_auc_score(labels, scores))
            result["status"] = "both_classes"
    return result


def _calibration(labeled, scored, score_kind, number_of_bins):
    if type(number_of_bins) is not int or not 1 <= number_of_bins <= 100:
        raise ValueError("calibration_bins must be an integer in [1, 100]")
    result = {"scope": "scored_labeled_candidates_only", "status": "not_probability_scores",
              "brier": None, "ece": None, "binning": "equal_width_positive_class_probability",
              "number_of_bins": number_of_bins, "bins": []}
    if score_kind != "probability":
        return result
    pairs = labeled.keys() & scored.keys()
    if not pairs:
        result["status"] = "no_labeled_candidates"
        return result
    bins = [[] for _ in range(number_of_bins)]
    squared_errors = []
    for pair in sorted(pairs):
        probability, label = scored[pair], labeled[pair]["label"]
        bins[min(int(probability * number_of_bins), number_of_bins - 1)].append((probability, label))
        squared_errors.append((probability - label) ** 2)
    ece = 0.0
    for index, items in enumerate(bins):
        mean_probability = math.fsum(item[0] for item in items) / len(items) if items else None
        positive_fraction = math.fsum(item[1] for item in items) / len(items) if items else None
        if items:
            ece += len(items) / len(pairs) * abs(mean_probability - positive_fraction)
        result["bins"].append({"lower": index / number_of_bins, "upper": (index + 1) / number_of_bins,
                               "count": len(items), "mean_probability": mean_probability,
                               "positive_fraction": positive_fraction})
    result.update(status="sample_conditional_probability_diagnostic",
                  brier=math.fsum(squared_errors) / len(pairs), ece=ece)
    return result


def evaluate_labeled_pairs(labels, candidates, scores, *, threshold, universe_pair_count=None,
                           score_kind="similarity", calibration_bins=10) -> dict:
    """Evaluate known labels, keeping unknown predictions outside confusion counts.

    All candidates must have scores. A missing positive candidate remains an
    end-to-end false negative. None threshold means reject every candidate.
    """
    labeled, candidate_set, scored = _inputs(labels, candidates, scores)
    _threshold(threshold)
    if score_kind not in {"similarity", "probability"}:
        raise ValueError("score_kind must be similarity or probability")
    if score_kind == "probability" and any(not 0 <= score <= 1 for score in scored.values()):
        raise ValueError("Probabilities must lie in [0, 1]")
    positive = {pair for pair, row in labeled.items() if row["label"] == 1}
    negative = labeled.keys() - positive
    predicted = {pair for pair, score in scored.items() if threshold is not None and score >= threshold}
    tp, fp = len(predicted & positive), len(predicted & negative)
    candidate_positive, candidate_negative = positive & candidate_set, negative & candidate_set
    if universe_pair_count is not None and (
        type(universe_pair_count) is not int or universe_pair_count < len(candidate_set | labeled.keys())
    ):
        raise ValueError("Pair universe must cover candidates and all explicitly labelled pairs")
    return {
        "scope": "explicit_labeled_pairs_only", "threshold": threshold,
        "decision_rule": "reject_all" if threshold is None else "score_gte_threshold",
        "labeled_pairs": len(labeled), "known_positive_pairs": len(positive),
        "known_negative_pairs": len(negative), "candidate_pairs": len(candidate_set),
        "labeled_candidate_pairs": len(candidate_set & labeled.keys()),
        "unknown_candidate_pairs": len(candidate_set - labeled.keys()),
        "candidate_known_positive_recall": _ratio(len(candidate_positive), len(positive)),
        "candidate_reduction_ratio": (1 - len(candidate_set) / universe_pair_count
                                       if universe_pair_count else None),
        "pair_universe": universe_pair_count,
        "predicted_pairs": len(predicted), "unknown_predicted_pairs": len(predicted - labeled.keys()),
        "prediction_label_coverage": _ratio(tp + fp, len(predicted)),
        "end_to_end": _confusion(tp, fp, len(positive) - tp, len(negative) - fp),
        "conditional": _confusion(tp, fp, len(candidate_positive) - tp, len(candidate_negative) - fp),
        "ranking": _ranking(labeled, scored),
        "calibration": _calibration(labeled, scored, score_kind, calibration_bins),
    }


def _ranked_counts(labeled, scored):
    ordered = sorted(scored, key=lambda pair: (-scored[pair], pair))
    positive, negative = [0], [0]
    for pair in ordered:
        label = labeled[pair]["label"] if pair in labeled else None
        positive.append(positive[-1] + (label == 1))
        negative.append(negative[-1] + (label == 0))
    return ordered, positive, negative


def review_budget_curve(labels, scores, budgets) -> list[dict]:
    """Rank all supplied candidates by score; unknown pairs consume real budget.

    Reports known-positive capture only, not estimated human time saved. Equal
    scores use pair IDs for reproducibility, never their evaluator label.
    """
    labeled, scored = _labels(labels), _scores(scores)
    ordered, positive, negative = _ranked_counts(labeled, scored)
    positives = sum(row["label"] for row in labeled.values())
    ties = Counter(scored.values())
    result = []
    for budget in budgets:
        if type(budget) is not int or budget < 0:
            raise ValueError("Review budgets must be nonnegative integers")
        count = min(budget, len(ordered))
        result.append({"budget": budget, "reviewed_pairs": count,
            "known_positive_pairs": positive[count], "known_negative_pairs": negative[count],
            "unknown_reviewed_pairs": count - positive[count] - negative[count],
            "known_positive_recall": _ratio(positive[count], positives),
            "known_positive_per_review": _ratio(positive[count], count),
            "reviewed_label_coverage": _ratio(positive[count] + negative[count], count),
            "boundary_tie_size": ties[scored[ordered[count - 1]]] if count else 0,
            "ordering": "score_desc_then_pair_id"})
    return result


def select_cost_threshold(labels, candidates, scores, *, split, false_positive_cost,
                          false_negative_cost, thresholds=None, return_curve=False) -> dict:
    """Minimize declared FP/FN cost on validation labels, including blocking misses.

    None selected_threshold means reject_all. Tied costs favor reject_all, then
    the larger threshold. Costs are conditional on this labelled sample, not
    estimated business losses. The caller must isolate validation file access.
    """
    labeled, _candidate_set, scored = _inputs(labels, candidates, scores)
    if split != "validation" or any(row.get("split", "validation") != "validation" for row in labeled.values()):
        raise ValueError("Threshold selection may use validation labels only")
    costs = [false_positive_cost, false_negative_cost]
    if any(not isinstance(cost, Real) or not math.isfinite(cost) or cost < 0 for cost in costs) or not any(costs):
        raise ValueError("Costs must be finite nonnegative numbers with at least one positive cost")
    total_positive = sum(row["label"] for row in labeled.values())
    result = {"scope": "validation_explicit_labeled_pairs_only", "status": "insufficient_class_support",
        "labeled_pairs": len(labeled), "known_positive_pairs": total_positive,
        "known_negative_pairs": len(labeled) - total_positive,
        "false_positive_cost": float(false_positive_cost), "false_negative_cost": float(false_negative_cost),
        "selected_threshold": None, "selected": None,
        "tie_break": "reject_all_then_larger_threshold", "unknown_pairs_excluded_from_cost": True}
    if not 0 < total_positive < len(labeled):
        return result
    values = set(scored.values()) if thresholds is None else set(thresholds)
    for value in values:
        _threshold(value)
    values.discard(None)
    ordered, positive, negative = _ranked_counts(labeled, scored)
    negative_scores = [-scored[pair] for pair in ordered]
    curve = []
    for threshold in [None, *sorted(values, reverse=True)]:
        count = bisect_right(negative_scores, -threshold) if threshold is not None else 0
        tp, fp = positive[count], negative[count]
        fn, tn = total_positive - tp, len(labeled) - total_positive - fp
        curve.append({"threshold": threshold, "decision_rule": "reject_all" if threshold is None else "score_gte_threshold",
            "cost": float(false_positive_cost * fp + false_negative_cost * fn),
            "predicted_pairs": count, "unknown_predicted_pairs": count - tp - fp,
            **_confusion(tp, fp, fn, tn)})
    selected = min(curve, key=lambda row: row["cost"])
    result.update(status="selected", selected_threshold=selected["threshold"], selected=selected,
                  thresholds_evaluated=len(curve))
    if return_curve:
        result["curve"] = curve
    return result


def _bootstrap_metrics(counts):
    tp, fp, fn, _tn, candidate_positive, positives = counts
    return {"candidate_known_positive_recall": _ratio(candidate_positive, positives),
            "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, positives),
            "f1": _ratio(2 * tp, 2 * tp + fp + fn)}


def _quantile(values, probability):
    values = sorted(values)
    location = (len(values) - 1) * probability
    low, high = math.floor(location), math.ceil(location)
    return values[low] + (values[high] - values[low]) * (location - low)


def group_bootstrap_intervals(labels, candidates, scores, *, threshold, n_resamples=1000,
                              confidence_level=0.95, seed=20260912, max_group_fraction=0.5) -> dict:
    """Percentile intervals from whole independent groups with a fixed pipeline.

    Every row needs group_id; an endpoint may not occur in two groups. Entire
    groups, not individual pairs, are sampled with replacement. A dominant
    component or too few groups returns no interval. This cannot remove label
    selection bias or account for model/threshold training uncertainty.
    """
    labeled, candidate_set, scored = _inputs(labels, candidates, scores)
    _threshold(threshold)
    if type(n_resamples) is not int or not 2 <= n_resamples <= 100000:
        raise ValueError("n_resamples must be an integer in [2, 100000]")
    if type(seed) is not int:
        raise ValueError("Bootstrap seed must be an integer for reproducibility")
    if not 0 < confidence_level < 1 or not 0 < max_group_fraction <= 1:
        raise ValueError("Confidence and maximum group fraction must lie in their probability ranges")
    groups, endpoint_groups, sizes = {}, {}, Counter()
    for pair, row in labeled.items():
        group = row.get("group_id")
        if not isinstance(group, str) or not group:
            raise ValueError("Every label needs a nonempty independent group_id")
        for endpoint in pair:
            if endpoint_groups.setdefault(endpoint, group) != group:
                raise ValueError("A shared endpoint cannot belong to different bootstrap groups")
        counts = groups.setdefault(group, [0] * 6)
        sizes[group] += 1
        predicted = threshold is not None and pair in scored and scored[pair] >= threshold
        positive = row["label"] == 1
        counts[0 if positive and predicted else 1 if predicted else 2 if positive else 3] += 1
        counts[4] += positive and pair in candidate_set
        counts[5] += positive
    totals = [sum(counts[index] for counts in groups.values()) for index in range(6)]
    point = _bootstrap_metrics(totals)
    maximum_share = _ratio(max(sizes.values(), default=0), len(labeled))
    result = {
        "method": "whole_group_percentile_bootstrap_fixed_pipeline", "groups": len(groups),
        "size_balance_effective_groups": _ratio(len(labeled) ** 2, sum(size ** 2 for size in sizes.values())),
        "largest_group_pair_fraction": maximum_share, "max_group_fraction": max_group_fraction,
        "confidence_level": confidence_level, "requested_resamples": n_resamples, "seed": seed,
        "status": "insufficient_independent_groups",
        "metrics": {name: {"estimate": value, "interval": None, "defined_resamples": 0,
                            "reason": "insufficient_independent_groups"} for name, value in point.items()},
    }
    if len(groups) < 2:
        return result
    if maximum_share > max_group_fraction:
        result["status"] = "dominant_dependency_group"
        for metric in result["metrics"].values():
            metric["reason"] = "dominant_dependency_group"
        return result
    group_counts = [groups[group] for group in sorted(groups)]
    generator = random.Random(seed)
    samples = {name: [] for name in point}
    for _ in range(n_resamples):
        counts = [0] * 6
        for _ in group_counts:
            sampled = generator.choice(group_counts)
            for index, count in enumerate(sampled):
                counts[index] += count
        for name, value in _bootstrap_metrics(counts).items():
            if value is not None:
                samples[name].append(value)
    tail = (1 - confidence_level) / 2
    for name, values in samples.items():
        metric = result["metrics"][name]
        metric["defined_resamples"] = len(values)
        if point[name] is None or len(values) < math.ceil(0.95 * n_resamples):
            metric["reason"] = "undefined_point_or_insufficient_defined_resamples"
        else:
            metric["interval"] = [_quantile(values, tail), _quantile(values, 1 - tail)]
            metric["reason"] = None
    result["status"] = ("descriptive_group_intervals" if any(
        metric["interval"] is not None for metric in result["metrics"].values()) else "no_defined_intervals")
    result["warning"] = "Few groups; percentile intervals are unstable" if len(groups) < 20 else None
    return result
