"""Scored evidence must reflect existing explicit decisions in review queues."""

import pytest

from entitybridge.resolution import Edge, resolve


@pytest.mark.parametrize("auto_merge", [False, True])
@pytest.mark.parametrize("decision,status,reason", [
    ("must_link", "accepted", "explicit_must_link"),
    ("cannot_link", "conflict", "explicit_cannot_link"),
])
def test_scored_status_reflects_explicit_pair_decision_even_below_threshold(auto_merge, decision, status, reason):
    result = resolve("ab", [Edge("b", "a", .1, auto_merge=auto_merge)], **{decision: [("a", "b")]})
    scored = [item for item in result.decisions if item.score is not None]
    assert len(scored) == 1
    assert (scored[0].score, scored[0].status, scored[0].reason) == (.1, status, reason)
    assert scored[0].conflicts == ((("a", "b"),) if decision == "cannot_link" else ())


def test_manual_transitive_confirmation_outweighs_old_automatic_edge_suppression():
    result = resolve("abc", [Edge("a", "c", .1, auto_merge=False)],
                     must_link=[("a", "b"), ("b", "c")], suppressed=[("a", "c")])
    scored = [item for item in result.decisions if item.score is not None]
    assert result.partitions == (("a", "b", "c"),)
    assert (scored[0].score, scored[0].status, scored[0].reason) == (.1, "accepted", "transitive_must_link")


def test_review_only_candidate_reports_cluster_cannot_without_merging():
    result = resolve("abc", [Edge("b", "c", 1.0, auto_merge=False)],
                     must_link=[("a", "b")], cannot_link=[("a", "c")])
    scored = [item for item in result.decisions if item.score is not None]
    assert result.partitions == (("a", "b"), ("c",))
    assert (scored[0].status, scored[0].reason, scored[0].conflicts) == (
        "conflict", "cluster_cannot_link", (("a", "c"),))


def test_automatic_connectivity_never_masquerades_as_manual_confirmation_of_review_only_pair():
    result = resolve("abcd", [Edge("a", "b", 1.0), Edge("b", "c", .99),
                              Edge("a", "c", .9, auto_merge=False), Edge("c", "d", .95, auto_merge=False)])
    assert result.partitions == (("a", "b", "c"), ("d",))
    pending = {(item.left, item.right): item for item in result.decisions if item.status == "review"}
    assert set(pending) == {("a", "c"), ("c", "d")}
    assert all(item.reason == "candidate_requires_review" for item in pending.values())
