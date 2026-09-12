"""Fixed-policy candidate refresh and conservative old/new support closure."""
from collections import defaultdict, deque
from dataclasses import dataclass

from .candidates import bucket_keys, generate_candidates
from .resolution import Edge, Resolution, resolve


@dataclass(frozen=True)
class Snapshot:
    records: frozenset[str]
    edges: tuple[Edge, ...]
    must_link: tuple[tuple[str, str], ...] = ()
    cannot_link: tuple[tuple[str, str], ...] = ()
    suppressed: tuple[tuple[str, str], ...] = ()
    threshold: float = 0.9
    review_threshold: float = 0.5
    policy_version: str = "default-v1"

    def solve(self):
        return resolve(self.records, self.edges, must_link=self.must_link, cannot_link=self.cannot_link,
                       suppressed=self.suppressed, threshold=self.threshold, review_threshold=self.review_threshold)


@dataclass(frozen=True)
class IncrementalResult:
    resolution: Resolution
    affected_records: tuple[str, ...]
    mode: str
    reason: str


def recompute(old: Snapshot, new: Snapshot, previous: Resolution, *, changed_records=(), max_records=10000):
    signatures = lambda state: (state.threshold, state.review_threshold, state.policy_version)
    if signatures(old) != signatures(new):
        return IncrementalResult(new.solve(), tuple(sorted(new.records)), "full", "policy_or_model_changed")
    changed = set(changed_records) | (old.records ^ new.records)
    old_edges = {(e.left, e.right, e.score, e.auto_merge) for e in old.edges}
    new_edges = {(e.left, e.right, e.score, e.auto_merge) for e in new.edges}
    for a, b, _score, _auto_merge in old_edges ^ new_edges:
        changed.update((a, b))
    for kind in ("must_link", "cannot_link", "suppressed"):
        for pair in set(getattr(old, kind)) ^ set(getattr(new, kind)):
            changed.update(pair)
    adjacency = defaultdict(set)
    def join(a, b):
        adjacency[a].add(b)
        adjacency[b].add(a)
    for state in (old, new):
        # Including all scored candidate edges is a deliberate conservative
        # closure, including suppressed and formerly conflict-blocked edges.
        for edge in state.edges:
            join(edge.left, edge.right)
        for kind in ("must_link", "cannot_link", "suppressed"):
            for a, b in getattr(state, kind):
                join(a, b)
    for cluster in previous.partitions:
        for member in cluster[1:]:
            join(cluster[0], member)
    affected = set(changed)
    queue = deque(changed)
    while queue:
        for neighbor in adjacency[queue.popleft()]:
            if neighbor not in affected:
                affected.add(neighbor)
                queue.append(neighbor)
    if len(affected & new.records) > max_records:
        return IncrementalResult(new.solve(), tuple(sorted(new.records)), "full", "closure_exceeds_budget_same_policy")
    local = resolve(new.records & affected, [e for e in new.edges if e.left in affected and e.right in affected],
        must_link=[p for p in new.must_link if set(p) <= affected],
        cannot_link=[p for p in new.cannot_link if set(p) <= affected],
        suppressed=[p for p in new.suppressed if set(p) <= affected],
        threshold=new.threshold, review_threshold=new.review_threshold)
    stable_partitions = [p for p in previous.partitions if not set(p) & affected]
    stable_decisions = [d for d in previous.decisions if d.left not in affected and d.right not in affected]
    decisions = tuple(sorted(stable_decisions + list(local.decisions), key=lambda d: (d.left, d.right, d.reason)))
    result = Resolution(tuple(sorted(stable_partitions + list(local.partitions))), decisions)
    return IncrementalResult(result, tuple(sorted(affected)), "incremental", "old_new_candidate_closure")


def refresh_scores(old_records, new_records, old_edges, score):
    """Re-score only pairs incident to changed records, with frozen statistics.

    Index maintenance scans record bucket keys; it does not re-score unchanged
    pairs. score(records, candidates) must use an immutable model/TF snapshot.
    """
    old = {r["record_id"]: r for r in old_records}
    new = {r["record_id"]: r for r in new_records}
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    keys = {key for record_id in changed for record in (old.get(record_id), new.get(record_id)) if record
            for key in bucket_keys(record)}
    local = [record for record in new.values() if keys.intersection(bucket_keys(record))]
    candidates = [edge for edge in generate_candidates(local) if edge["left"] in changed or edge["right"] in changed]
    refreshed = score(local, candidates)
    unchanged = [edge for edge in old_edges if edge["left"] not in changed and edge["right"] not in changed]
    return unchanged + refreshed, {"changed_records": sorted(changed), "bucket_records": len(local),
                                   "rescored_pairs": len(candidates), "unchanged_pairs": len(unchanged)}
