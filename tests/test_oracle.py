"""Independent small-graph specification using Boolean transitive closure.

The oracle never calls production resolve/solve or shares component helpers.
It deliberately recomputes all-pairs reachability after each proposed edge.
"""

from itertools import combinations

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from entitybridge.incremental import Snapshot, recompute
from entitybridge.resolution import ConstraintConflict, Edge, resolve


class ReferenceContradiction(Exception):
    """Independent reference rejects an unsatisfiable must/cannot specification."""


def _reachability(records, accepted):
    """Boolean Floyd-Warshall, deliberately unlike production group merging."""
    reach = {(a, b): a == b for a in records for b in records}
    for a, b in accepted:
        reach[a, b] = reach[b, a] = True
    for via in records:
        for a in records:
            for b in records:
                reach[a, b] = reach[a, b] or (reach[a, via] and reach[via, b])
    return reach


def reference_resolve(records, edges, *, must_link=(), cannot_link=(), suppressed=(),
                      threshold=.9, review_threshold=.5, review_only=()):
    records = tuple(sorted(records))
    accepted = sorted({tuple(sorted(pair)) for pair in must_link})
    decisions = [(a, b, None, "accepted", "explicit_must_link", ()) for a, b in accepted]
    cannot = sorted({tuple(sorted(pair)) for pair in cannot_link})
    suppressions = {frozenset(pair) for pair in suppressed}
    requires_review = {frozenset(pair) for pair in review_only}
    reach = _reachability(records, accepted)
    if any(reach[pair] for pair in cannot):
        raise ReferenceContradiction("Cannot-link connected by mandatory paths")
    mandatory_reach = dict(reach)
    mandatory_pairs = set(accepted)
    ordered = sorted({(min(a, b), max(a, b), score) for a, b, score in edges})
    ordered.sort(key=lambda edge: -edge[2])
    for a, b, score in ordered:
        conflicts = ()
        if (a, b) in mandatory_pairs:
            status, reason = "accepted", "explicit_must_link"
        elif (a, b) in cannot:
            status, reason = "conflict", "explicit_cannot_link"
            conflicts = ((a, b),)
        elif mandatory_reach[a, b]:
            status, reason = "accepted", "transitive_must_link"
        elif frozenset((a, b)) in suppressions:
            status, reason = "suppressed", "revoked_basis_requires_review"
        elif frozenset((a, b)) in requires_review:
            trial = _reachability(records, accepted + [(a, b)])
            conflicts = tuple(pair for pair in cannot if trial[pair])
            if conflicts:
                status, reason = "conflict", "cluster_cannot_link"
            else:
                status, reason = "review", "candidate_requires_review"
        elif score < review_threshold:
            status, reason = "below_threshold", "insufficient_score_not_a_cannot_link"
        elif score < threshold:
            status, reason = "review", "score_in_review_band"
        else:
            trial = _reachability(records, accepted + [(a, b)])
            conflicts = tuple(pair for pair in cannot if trial[pair])
            if conflicts:
                status, reason = "conflict", "cluster_cannot_link"
            else:
                accepted.append((a, b))
                status, reason = "accepted", "score_at_or_above_threshold"
        decisions.append((a, b, score, status, reason, conflicts))
    reach = _reachability(records, accepted)
    partitions = tuple(sorted({tuple(b for b in records if reach[a, b]) for a in records}))
    return partitions, tuple(decisions)


def test_oracle_manual_bridge_conflict_and_review_bands():
    partitions, decisions = reference_resolve(
        "abcde", (("a", "b", .99), ("b", "c", .98), ("c", "d", .7), ("d", "e", .2)),
        cannot_link=(("a", "c"),))
    assert partitions == (("a", "b"), ("c",), ("d",), ("e",))
    assert decisions == (
        ("a", "b", .99, "accepted", "score_at_or_above_threshold", ()),
        ("b", "c", .98, "conflict", "cluster_cannot_link", (("a", "c"),)),
        ("c", "d", .7, "review", "score_in_review_band", ()),
        ("d", "e", .2, "below_threshold", "insufficient_score_not_a_cannot_link", ()),
    )


def test_oracle_must_priority_suppression_and_contradiction_are_explicit():
    partitions, decisions = reference_resolve("abc", (("a", "b", .99), ("b", "c", .98)),
        must_link=(("a", "c"),), suppressed=(("a", "b"),))
    assert partitions == (("a", "b", "c"),)
    assert decisions[0] == ("a", "c", None, "accepted", "explicit_must_link", ())
    assert decisions[1][3:5] == ("suppressed", "revoked_basis_requires_review")
    with pytest.raises(ReferenceContradiction):
        reference_resolve("abc", (), must_link=(("a", "b"), ("b", "c")), cannot_link=(("a", "c"),))


@st.composite
def graph_specs(draw):
    records = tuple(f"n{i}" for i in range(draw(st.integers(2, 6))))
    pairs = tuple(combinations(records, 2))
    scores = draw(st.dictionaries(st.sampled_from(pairs),
        st.sampled_from((0., .25, .5, .7, .9, .99, 1.)), max_size=len(pairs)))
    threshold = draw(st.sampled_from((.5, .7, .9, 1.)))
    review = draw(st.sampled_from(tuple(value for value in (0., .25, .5) if value <= threshold)))
    spec = {"records": records, "edges": tuple((*pair, score) for pair, score in scores.items()),
            "threshold": threshold, "review_threshold": review}
    for kind in ("must_link", "cannot_link", "suppressed"):
        spec[kind] = tuple(sorted(draw(st.sets(st.sampled_from(pairs), max_size=min(3, len(pairs))))))
    return spec


def _actual_full(spec):
    return resolve(spec["records"], [Edge(*edge) for edge in spec["edges"]],
                   **{key: value for key, value in spec.items() if key not in {"records", "edges"}})


def _snapshot(spec):
    return Snapshot(frozenset(spec["records"]), tuple(Edge(*edge) for edge in spec["edges"]),
                    **{key: value for key, value in spec.items() if key not in {"records", "edges"}})


def _decision_values(result):
    return tuple((d.left, d.right, d.score, d.status, d.reason, d.conflicts) for d in result.decisions)


def _assert_matches_reference(actual, expected, *, ordered=False):
    assert actual.partitions == expected[0]
    actual_decisions = _decision_values(actual)
    if ordered:
        assert actual_decisions == expected[1]
    else:
        # Incremental output uses display order, whereas full output uses merge
        # evaluation order. Compare every field and multiplicity, not just pairs.
        key = lambda item: (item[0], item[1], item[4])
        assert sorted(actual_decisions, key=key) == sorted(expected[1], key=key)


@given(graph_specs())
@settings(max_examples=200, deadline=None, derandomize=True)
def test_full_resolver_matches_independent_reachability_oracle(spec):
    try:
        expected = reference_resolve(**spec)
    except ReferenceContradiction:
        event("must/cannot contradiction")
        with pytest.raises(ConstraintConflict):
            _actual_full(spec)
        return
    event("valid graph")
    _assert_matches_reference(_actual_full(spec), expected, ordered=True)
    reversed_spec = {**spec, "records": tuple(reversed(spec["records"])),
        "edges": tuple((b, a, score) for a, b, score in reversed(spec["edges"]))}
    for kind in ("must_link", "cannot_link", "suppressed"):
        reversed_spec[kind] = tuple((b, a) for a, b in reversed(spec[kind]))
    _assert_matches_reference(_actual_full(reversed_spec), expected, ordered=True)


def test_revoked_support_retains_alternative_path_until_bridge_record_is_deleted():
    old = {"records": tuple("abcd"), "edges": (("a", "b", .99), ("b", "c", .98), ("a", "c", .97)),
           "must_link": (("a", "b"),), "cannot_link": (), "suppressed": ()}
    revoked = {**old, "must_link": (), "suppressed": (("a", "b"),)}
    expected = reference_resolve(**revoked)
    assert expected[0] == (("a", "b", "c"), ("d",))
    previous = _actual_full(old)
    result = recompute(_snapshot(old), _snapshot(revoked), previous)
    _assert_matches_reference(result.resolution, expected)
    deleted = {**revoked, "records": tuple("abd"), "edges": (("a", "b", .99),)}
    expected_deleted = reference_resolve(**deleted)
    assert expected_deleted[0] == (("a",), ("b",), ("d",))
    final = recompute(_snapshot(revoked), _snapshot(deleted), result.resolution)
    _assert_matches_reference(final.resolution, expected_deleted)
    assert final.affected_records == ("a", "b", "c")


def test_replacing_cannot_link_reverses_which_high_score_bridge_is_accepted():
    old = {"records": tuple("abcd"), "edges": (("a", "b", .99), ("b", "c", .98)),
           "cannot_link": (("a", "c"),)}
    new = {**old, "cannot_link": (("a", "b"),)}
    previous = _actual_full(old)
    assert previous.partitions == (("a", "b"), ("c",), ("d",))
    expected = reference_resolve(**new)
    assert expected[0] == (("a",), ("b", "c"), ("d",))
    _assert_matches_reference(recompute(_snapshot(old), _snapshot(new), previous).resolution, expected)


@st.composite
def graph_transitions(draw):
    old = draw(graph_specs())
    must_closure = _reachability(old["records"], old["must_link"])
    # Establish a valid published predecessor independently of production.
    old["cannot_link"] = tuple(pair for pair in old["cannot_link"] if not must_closure[pair])
    new = dict(old)
    operation = draw(st.sampled_from(("delete_record", "revoke_must", "revoke_cannot", "revoke_suppressed",
                                     "replace_cannot", "add_must", "change_score", "add_record")))
    pair = draw(st.sampled_from(tuple(combinations(old["records"], 2))))
    if operation == "delete_record":
        removed = draw(st.sampled_from(old["records"]))
        new["records"] = tuple(record for record in old["records"] if record != removed)
        new["edges"] = tuple(edge for edge in old["edges"] if removed not in edge[:2])
        for kind in ("must_link", "cannot_link", "suppressed"):
            new[kind] = tuple(pair for pair in old[kind] if removed not in pair)
    elif operation == "revoke_must":
        new["must_link"] = ()
        new["suppressed"] = tuple(sorted(set(old["suppressed"]) | set(old["must_link"])))
    elif operation == "revoke_cannot":
        new["cannot_link"] = ()
    elif operation == "revoke_suppressed":
        new["suppressed"] = ()
    elif operation == "replace_cannot":
        new["cannot_link"] = (pair,)
    elif operation == "add_must":
        new["must_link"] = tuple(sorted(set(old["must_link"]) | {pair}))
    elif operation == "change_score":
        scores = {(a, b): score for a, b, score in old["edges"]}
        scores[pair] = draw(st.sampled_from((0., .5, .9, 1.)))
        new["edges"] = tuple((*pair, score) for pair, score in scores.items())
    else:
        new["records"] = (*old["records"], "new")
        score = draw(st.sampled_from((0., .5, .9, 1.)))
        new["edges"] = (*old["edges"], (pair[0], "new", score))
    return operation, old, new


@given(graph_transitions())
@settings(max_examples=250, deadline=None, derandomize=True)
def test_incremental_and_budget_fallback_match_independent_oracle(transition):
    operation, old, new = transition
    event(operation)
    previous = _actual_full(old)
    _assert_matches_reference(previous, reference_resolve(**old), ordered=True)
    try:
        expected = reference_resolve(**new)
    except ReferenceContradiction:
        event("new constraints contradictory")
        with pytest.raises(ConstraintConflict):
            _actual_full(new)
        with pytest.raises(ConstraintConflict):
            recompute(_snapshot(old), _snapshot(new), previous)
        return
    _assert_matches_reference(_actual_full(new), expected, ordered=True)
    for budget in (100, 0):
        result = recompute(_snapshot(old), _snapshot(new), previous, max_records=budget)
        _assert_matches_reference(result.resolution, expected)


@given(graph_specs(), st.data())
@settings(max_examples=150, deadline=None, derandomize=True)
def test_review_only_status_and_incremental_policy_flags_match_independent_oracle(spec, data):
    pairs = [(a, b) for a, b, _score in spec["edges"]]
    review_only = data.draw(st.sets(st.sampled_from(pairs))) if pairs else set()
    try:
        expected = reference_resolve(**spec, review_only=review_only)
    except ReferenceContradiction:
        with pytest.raises(ConstraintConflict):
            _actual_full(spec)
        return
    old = _snapshot(spec)
    previous = old.solve()
    new = Snapshot(old.records,
        tuple(Edge(a, b, score, auto_merge=(a, b) not in review_only) for a, b, score in spec["edges"]),
        must_link=old.must_link, cannot_link=old.cannot_link, suppressed=old.suppressed,
        threshold=old.threshold, review_threshold=old.review_threshold, policy_version=old.policy_version)
    _assert_matches_reference(new.solve(), expected, ordered=True)
    for budget in (100, 0):
        _assert_matches_reference(recompute(old, new, previous, max_records=budget).resolution, expected)
