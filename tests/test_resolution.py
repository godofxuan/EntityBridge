import pytest

from entitybridge.resolution import ConstraintConflict, Edge, resolve


def test_links_high_score_and_preserves_unconnected_records_as_unknown():
    result = resolve(["a", "b", "c"], [Edge("b", "a", 0.95)])
    assert result.partitions == (("a", "b"), ("c",))
    assert [(d.left, d.right, d.status) for d in result.decisions] == [("a", "b", "accepted")]


def test_cannot_link_blocks_indirect_bridge_with_specific_conflict_evidence():
    result = resolve(["a", "b", "c"], [Edge("a", "b", 0.99), Edge("b", "c", 0.98)],
                     cannot_link=[("c", "a")])
    assert result.partitions == (("a", "b"), ("c",))
    assert result.decisions[1].status == "conflict"
    assert result.decisions[1].conflicts == (("a", "c"),)


def test_must_link_transitive_contradiction_fails_before_resolving_scores():
    with pytest.raises(ConstraintConflict, match="a.*c"):
        resolve(["a", "b", "c"], [], must_link=[("a", "b"), ("b", "c")],
                cannot_link=[("a", "c")])


def test_explicit_must_has_priority_and_revoke_suppression_is_not_cannot_link():
    direct = resolve(["a", "b"], [Edge("a", "b", 0.99)], suppressed=[("b", "a")])
    assert direct.partitions == (("a",), ("b",))
    assert direct.decisions[0].status == "suppressed"
    alternate = resolve(["a", "b", "c"],
                        [Edge("a", "b", 0.99), Edge("b", "c", 0.98)],
                        must_link=[("a", "c")], suppressed=[("a", "b")])
    assert alternate.partitions == (("a", "b", "c"),)
    assert alternate.decisions[0].reason == "explicit_must_link"


@pytest.mark.parametrize("edges,kwargs", [
    ([Edge("a", "missing", 0.9)], {}),
    ([Edge("a", "a", 0.9)], {}),
    ([Edge("a", "b", float("nan"))], {}),
    ([Edge("a", "b", 1.1)], {}),
    ([Edge("a", "b", 0.9), Edge("b", "a", 0.8)], {}),
    ([], {"cannot_link": [("a", "missing")]}),
    ([], {"threshold": 0.3, "review_threshold": 0.5}),
])
def test_rejects_invalid_or_ambiguous_inputs(edges, kwargs):
    with pytest.raises(ValueError):
        resolve(["a", "b"], edges, **kwargs)
