"""Decision-policy contracts independent of the frozen real benchmark results."""
import importlib.util
from pathlib import Path

import pytest

from entitybridge.benchmark_metrics import evaluate_labeled_pairs, select_cost_threshold
from entitybridge.matching import score_baselines

SPEC = importlib.util.spec_from_file_location(
    "benchmark_contract_runner", Path(__file__).parents[1] / "scripts/run_public_benchmark.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _label(left, right, value):
    return {"left_id": left, "right_id": right, "label": value, "split": "validation"}


def test_exact_baseline_cannot_accept_nonexact_names_when_candidate_negatives_are_unlabelled():
    records = [
        {"record_id": "a", "source": "left", "name": "ALPHA LTD"},
        {"record_id": "b", "source": "right", "name": "ALPHA LIMITED"},
        {"record_id": "c", "source": "left", "name": "BETA LTD"},
        {"record_id": "d", "source": "right", "name": "BETA LTD"},
        {"record_id": "u", "source": "left", "name": "SIGMA LIMITED"},
        {"record_id": "v", "source": "right", "name": "SIGMA LTD"},
        {"record_id": "x", "source": "left", "name": None},
        {"record_id": "y", "source": "right", "name": None},
    ]
    candidates = [{"left": left, "right": right, "rules": ["synthetic"]}
                  for left, right in [("a", "b"), ("c", "d"), ("u", "v"), ("x", "y")]]
    scores = {(edge["left"], edge["right"]): edge["score"]
              for edge in score_baselines(records, candidates)["exact"]}
    # The explicit negative is outside blocking; unknown candidates still exist.
    labels = [_label("a", "b", 1), _label("c", "d", 1), _label("a", "d", 0)]
    costs = {"false_positive_cost": 10, "false_negative_cost": 1}

    # Reproduce why unconstrained cost optimisation is wrong for a named exact baseline.
    generic = select_cost_threshold(labels, scores, scores, split="validation", **costs)
    assert generic["selected_threshold"] == 0
    assert generic["selected"]["unknown_predicted_pairs"] == 2

    selected = runner.select_validation_policy("exact", labels, set(scores), scores, **costs)
    assert selected["selected_threshold"] == 1
    assert {row["threshold"] for row in selected["curve"]} == {None, 1}
    result = evaluate_labeled_pairs(labels, scores, scores, threshold=selected["selected_threshold"])
    assert result["end_to_end"]["tp"] == 1
    assert result["end_to_end"]["fn"] == 1
    assert result["unknown_predicted_pairs"] == 0
    accepted = {pair for pair, score in scores.items() if score >= selected["selected_threshold"]}
    assert accepted == {("c", "d")}


def test_exact_policy_can_reject_all_when_equal_names_produce_costly_false_matches():
    labels = [_label("a", "b", 1), _label("c", "d", 0)]
    scores = {("a", "b"): 1.0, ("c", "d"): 1.0}
    selected = runner.select_validation_policy("exact", labels, set(scores), scores,
        false_positive_cost=10, false_negative_cost=1)
    assert selected["status"] == "selected"
    assert selected["selected_threshold"] is None
    assert selected["selected"]["decision_rule"] == "reject_all"
    assert selected["selected"]["cost"] == 1
    assert selected["selected"]["predicted_pairs"] == 0


@pytest.mark.parametrize("method", ["fuzzy", "splink", "supervised_logistic"])
def test_scored_policies_keep_validation_cost_selection_and_disclose_unknown_acceptances(method):
    labels = [_label("a", "b", 1), _label("c", "d", 0)]
    scores = {("a", "b"): 0.8, ("c", "d"): 0.2, ("u", "v"): 0.9}
    selected = runner.select_validation_policy(method, labels, set(scores), scores,
        false_positive_cost=10, false_negative_cost=1)
    assert selected["selected_threshold"] == 0.8
    assert selected["selected"]["cost"] == 0
    assert selected["selected"]["unknown_predicted_pairs"] == 1
    assert selected["unknown_pairs_excluded_from_cost"] is True
    assert {row["threshold"] for row in selected["curve"]} == {None, 0.8, 0.2}
    labels[0]["split"] = "test"
    with pytest.raises(ValueError, match="validation"):
        runner.select_validation_policy(method, labels, set(scores), scores,
            false_positive_cost=10, false_negative_cost=1)
