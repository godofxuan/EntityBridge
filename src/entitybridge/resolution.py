"""Deterministic, score-ordered entity resolution; no source or truth access."""

from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite

Pair = tuple[str, str]


@dataclass(frozen=True)
class Edge:
    left: str
    right: str
    score: float
    auto_merge: bool = True


@dataclass(frozen=True)
class EdgeDecision:
    left: str
    right: str
    score: float | None
    status: str
    reason: str
    conflicts: tuple[Pair, ...] = ()


@dataclass(frozen=True)
class Resolution:
    partitions: tuple[tuple[str, ...], ...]
    decisions: tuple[EdgeDecision, ...]


class ConstraintConflict(ValueError):
    """An explicit cannot-link contradicts the transitive must-link closure."""


def resolve(
    records: Iterable[str],
    edges: Iterable[Edge],
    *,
    must_link: Iterable[Pair] = (),
    cannot_link: Iterable[Pair] = (),
    suppressed: Iterable[Pair] = (),
    threshold: float = 0.9,
    review_threshold: float = 0.5,
) -> Resolution:
    groups = {record: {record} for record in records}
    if not (isfinite(threshold) and isfinite(review_threshold) and 0 <= review_threshold <= threshold <= 1):
        raise ValueError("require 0 <= review_threshold <= threshold <= 1")
    def pairs(values):
        result = set()
        for pair in values:
            if len(pair) != 2 or any(endpoint not in groups for endpoint in pair):
                raise ValueError(f"invalid pair endpoints: {pair!r}")
            result.add(tuple(sorted(pair)))
        return result
    must = pairs(must_link)
    cannot = pairs(cannot_link)
    suppressions = pairs(suppressed)
    scored = {}
    for edge in edges:
        pair = next(iter(pairs([(edge.left, edge.right)])))
        if (edge.left == edge.right or not isfinite(edge.score) or not 0 <= edge.score <= 1
                or not isinstance(edge.auto_merge, bool)):
            raise ValueError(f"invalid scored edge: {edge!r}")
        if pair in scored and (scored[pair].score, scored[pair].auto_merge) != (edge.score, edge.auto_merge):
            raise ValueError(f"conflicting scores for pair: {pair!r}")
        scored[pair] = Edge(*pair, edge.score, edge.auto_merge)
    decisions = []
    for left, right in sorted(must):
        merged = groups[left] | groups[right]
        for member in merged:
            groups[member] = merged
        decisions.append(EdgeDecision(left, right, None, "accepted", "explicit_must_link"))
    contradictory = tuple(sorted(pair for pair in cannot if groups[pair[0]] is groups[pair[1]]))
    if contradictory:
        raise ConstraintConflict(f"must-link closure violates cannot-link: {contradictory!r}")
    # Keep the manual-only closure separate from later automatic components.
    # Already sharing an automatically built cluster is not human confirmation.
    mandatory_groups = dict(groups)
    for edge in sorted(scored.values(), key=lambda e: (-e.score, e.left, e.right)):
        left, right = sorted((edge.left, edge.right))
        conflicts = ()
        if (left, right) in must:
            status, reason = "accepted", "explicit_must_link"
        elif (left, right) in cannot:
            status, reason = "conflict", "explicit_cannot_link"
            conflicts = ((left, right),)
        elif mandatory_groups[left] is mandatory_groups[right]:
            status, reason = "accepted", "transitive_must_link"
        elif (left, right) in suppressions:
            status, reason = "suppressed", "revoked_basis_requires_review"
        elif not edge.auto_merge:
            merged = groups[left] | groups[right]
            conflicts = tuple(sorted(pair for pair in cannot if set(pair) <= merged))
            if conflicts:
                status, reason = "conflict", "cluster_cannot_link"
            else:
                status, reason = "review", "candidate_requires_review"
        elif edge.score >= threshold:
            merged = groups[left] | groups[right]
            conflicts = tuple(sorted(pair for pair in cannot if set(pair) <= merged))
            if conflicts:
                status, reason = "conflict", "cluster_cannot_link"
            else:
                for member in merged:
                    groups[member] = merged
                status, reason = "accepted", "score_at_or_above_threshold"
        elif edge.score >= review_threshold:
            status, reason = "review", "score_in_review_band"
        else:
            status, reason = "below_threshold", "insufficient_score_not_a_cannot_link"
        decisions.append(EdgeDecision(left, right, edge.score, status, reason, conflicts))
    partitions = tuple(sorted({tuple(sorted(group)) for group in groups.values()}))
    return Resolution(partitions, tuple(decisions))
