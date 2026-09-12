import pytest

from entitybridge.benchmark_metrics import evaluate_labeled_pairs


def test_partial_labels_keep_unknown_predictions_unknown_and_blocking_misses_in_recall():
    labels = [
        {"left_id": "a", "right_id": "b", "label": 1},
        {"left_id": "c", "right_id": "d", "label": 1},
        {"left_id": "a", "right_id": "d", "label": 0},
    ]
    candidates = {("a", "b"), ("a", "d"), ("x", "y")}
    scores = {("b", "a"): 0.9, ("a", "d"): 0.1, ("x", "y"): 0.99}
    result = evaluate_labeled_pairs(labels, candidates, scores, threshold=0.5, universe_pair_count=10)
    assert result["candidate_known_positive_recall"] == 0.5
    assert result["candidate_reduction_ratio"] == 0.7
    assert result["end_to_end"]["precision"] == 1.0
    assert result["end_to_end"]["recall"] == 0.5
    assert result["conditional"]["recall"] == 1.0
    assert result["unknown_predicted_pairs"] == 1
    assert result["predicted_pairs"] == 2
    assert result["scope"] == "explicit_labeled_pairs_only"
    with pytest.raises(ValueError, match="exactly cover"):
        evaluate_labeled_pairs(labels, candidates, {("a", "b"): 0.9}, threshold=0.5)


def test_ranking_ties_empty_and_single_class_are_explicit_not_fake_zero_quality():
    labels = [{"left_id": "a", "right_id": "b", "label": 1},
              {"left_id": "c", "right_id": "d", "label": 0}]
    scores = {("a", "b"): 0.5, ("c", "d"): 0.5}
    result = evaluate_labeled_pairs(labels, scores, scores, threshold=None)
    assert result["ranking"]["average_precision"] == 0.5
    assert result["ranking"]["roc_auc"] == 0.5
    assert result["end_to_end"]["precision"] is None
    assert result["end_to_end"]["f1"] == 0
    negative_only = evaluate_labeled_pairs(labels[1:], scores, scores, threshold=0.5)
    assert negative_only["ranking"]["average_precision"] is None
    assert negative_only["ranking"]["roc_auc"] is None
    assert negative_only["ranking"]["status"] == "no_positive_labels"
    positive_only = evaluate_labeled_pairs(labels[:1], scores, scores, threshold=0.5)
    assert positive_only["ranking"]["average_precision"] == 1
    assert positive_only["ranking"]["roc_auc"] is None
    empty = evaluate_labeled_pairs([], [], {}, threshold=0.5)
    assert empty["ranking"]["status"] == "no_labeled_candidates"
    assert empty["end_to_end"]["f1"] is None


def test_calibration_requires_probability_semantics_and_accounts_for_bin_boundaries():
    labels = [{"left_id": "a", "right_id": "b", "label": 1},
              {"left_id": "c", "right_id": "d", "label": 0}]
    scores = {("a", "b"): 0.75, ("c", "d"): 0.25, ("x", "y"): 0.0}
    result = evaluate_labeled_pairs(labels, scores, scores, threshold=0.5, score_kind="probability")
    assert result["calibration"]["brier"] == 0.0625
    assert result["calibration"]["ece"] == 0.25
    assert sum(row["count"] for row in result["calibration"]["bins"]) == 2
    similarity = evaluate_labeled_pairs(labels, scores, scores, threshold=0.5)
    assert similarity["calibration"]["status"] == "not_probability_scores"
    scores[("a", "b")], scores[("c", "d")] = 1.0, 0.0
    calibrated = evaluate_labeled_pairs(labels, scores, scores, threshold=0.5, score_kind="probability")
    assert calibrated["calibration"]["brier"] == calibrated["calibration"]["ece"] == 0
    scores[("x", "y")] = 1.01
    with pytest.raises(ValueError, match="Probabilities"):
        evaluate_labeled_pairs(labels, scores, scores, threshold=0.5, score_kind="probability")


def test_review_budget_spends_on_unknown_candidates_and_is_stable_for_ties():
    from entitybridge.benchmark_metrics import review_budget_curve
    labels = [{"left_id": "a", "right_id": "b", "label": 1},
              {"left_id": "c", "right_id": "d", "label": 1},
              {"left_id": "e", "right_id": "f", "label": 0}]
    scores = {("x", "y"): 0.99, ("a", "b"): 0.8, ("e", "f"): 0.8}
    curve = review_budget_curve(labels, scores, [0, 1, 2, 99])
    assert curve[1]["reviewed_pairs"] == curve[1]["unknown_reviewed_pairs"] == 1
    assert curve[1]["known_positive_recall"] == 0
    assert curve[2]["known_positive_recall"] == 0.5
    assert curve[2]["boundary_tie_size"] == 2
    assert curve[3]["reviewed_pairs"] == 3
    assert curve == review_budget_curve(labels, dict(reversed(list(scores.items()))), [0, 1, 2, 99])


def test_cost_selection_is_validation_only_and_can_select_reject_all():
    from entitybridge.benchmark_metrics import select_cost_threshold
    labels = [{"left_id": "a", "right_id": "b", "label": 1, "split": "validation"},
              {"left_id": "c", "right_id": "d", "label": 0, "split": "validation"},
              {"left_id": "e", "right_id": "f", "label": 1, "split": "validation"}]
    scores = {("a", "b"): 0.6, ("c", "d"): 0.9, ("x", "y"): 1.0}
    selected = select_cost_threshold(labels, scores, scores, split="validation",
                                     false_positive_cost=10, false_negative_cost=1)
    assert selected["selected_threshold"] is None
    assert selected["selected"]["cost"] == 2
    scores[("c", "d")] = 0.5
    selected = select_cost_threshold(labels, scores, scores, split="validation",
                                     false_positive_cost=10, false_negative_cost=1)
    assert selected["selected_threshold"] == 0.6
    assert selected["selected"]["cost"] == 1
    assert selected["selected"]["unknown_predicted_pairs"] == 1
    with pytest.raises(ValueError, match="validation"):
        select_cost_threshold(labels, scores, scores, split="test", false_positive_cost=10, false_negative_cost=1)
    labels[0]["split"] = "test"
    with pytest.raises(ValueError, match="validation"):
        select_cost_threshold(labels, scores, scores, split="validation", false_positive_cost=10, false_negative_cost=1)


def test_bootstrap_resamples_whole_dependency_groups_and_refuses_false_independence():
    from entitybridge.benchmark_metrics import group_bootstrap_intervals
    labels = [
        {"left_id": "a", "right_id": "b", "label": 1, "group_id": "one"},
        {"left_id": "a", "right_id": "c", "label": 0, "group_id": "one"},
        {"left_id": "d", "right_id": "e", "label": 1, "group_id": "two"},
        {"left_id": "d", "right_id": "f", "label": 0, "group_id": "two"},
    ]
    scores = {("a", "b"): 0.9, ("a", "c"): 0.1, ("d", "e"): 0.1, ("d", "f"): 0.1}
    result = group_bootstrap_intervals(labels, scores, scores, threshold=0.5, n_resamples=200, seed=42)
    assert result["groups"] == 2
    assert result["metrics"]["recall"]["estimate"] == 0.5
    assert result["metrics"]["recall"]["interval"] == [0, 1]
    assert result["metrics"]["candidate_known_positive_recall"]["interval"] == [1, 1]
    assert result == group_bootstrap_intervals(list(reversed(labels)), scores, scores,
                                              threshold=0.5, n_resamples=200, seed=42)
    for row in labels:
        row["group_id"] = "one"
    one = group_bootstrap_intervals(labels, scores, scores, threshold=0.5)
    assert one["status"] == "insufficient_independent_groups"
    assert all(metric["interval"] is None for metric in one["metrics"].values())
    labels[1]["group_id"] = "not-independent"
    with pytest.raises(ValueError, match="endpoint.*group"):
        group_bootstrap_intervals(labels, scores, scores, threshold=0.5)


def test_ambiguous_labels_nonfinite_scores_and_dominant_groups_do_not_produce_confident_reports():
    from entitybridge.benchmark_metrics import group_bootstrap_intervals, select_cost_threshold
    labels = [{"left_id": "a", "right_id": "b", "label": 1}]
    contradictory = labels + [{"left_id": "b", "right_id": "a", "label": 0}]
    with pytest.raises(ValueError, match="Duplicate or conflicting"):
        evaluate_labeled_pairs(contradictory, [], {}, threshold=0.5)
    with pytest.raises(ValueError, match="finite"):
        evaluate_labeled_pairs(labels, [("a", "b")], {("a", "b"): float("nan")}, threshold=0.5)
    with pytest.raises(ValueError, match="universe"):
        evaluate_labeled_pairs(labels, [], {}, threshold=0.5, universe_pair_count=0)
    one_class = select_cost_threshold(labels, [], {}, split="validation",
                                     false_positive_cost=10, false_negative_cost=1)
    assert one_class["status"] == "insufficient_class_support"
    assert one_class["selected"] is None
    groups = [{"left_id": "a", "right_id": target, "label": 1, "group_id": "large"}
              for target in ("b", "c", "d")]
    groups.append({"left_id": "x", "right_id": "y", "label": 0, "group_id": "small"})
    refused = group_bootstrap_intervals(groups, [], {}, threshold=0.5)
    assert refused["groups"] == 2
    assert refused["largest_group_pair_fraction"] == 0.75
    assert refused["status"] == "dominant_dependency_group"
    assert all(metric["interval"] is None for metric in refused["metrics"].values())
