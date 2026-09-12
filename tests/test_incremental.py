from entitybridge.incremental import Snapshot, recompute
from entitybridge.resolution import Edge, resolve


def test_removing_cannot_link_reconsiders_previously_blocked_support_edge():
    old = Snapshot(frozenset("abcd"), (Edge("a", "b", .99), Edge("b", "c", .98)),
                   cannot_link=(("a", "c"),))
    previous = resolve(old.records, old.edges, cannot_link=old.cannot_link)
    new = Snapshot(old.records, old.edges)
    incremental = recompute(old, new, previous)
    assert incremental.resolution.partitions == resolve(new.records, new.edges).partitions
    assert incremental.affected_records == ("a", "b", "c")
    assert incremental.mode == "incremental"


from hypothesis import given, settings
from hypothesis import strategies as st


@given(st.lists(st.tuples(st.integers(0, 5), st.integers(0, 5), st.integers(0, 100)), max_size=15),
       st.integers(0, 5), st.booleans(), st.integers(0, 100))
@settings(max_examples=150, deadline=None)
def test_add_change_delete_and_constraint_changes_equal_full(raw_edges, changed, delete, score):
    nodes = frozenset(str(i) for i in range(6))
    unique = {tuple(sorted((str(a), str(b)))): value / 100 for a, b, value in raw_edges if a != b}
    old = Snapshot(nodes, tuple(Edge(a, b, value) for (a, b), value in unique.items()), cannot_link=(("0", "5"),))
    original = old.solve()
    node = str(changed)
    new_nodes = nodes - {node} if delete else nodes | {"new"}
    new_edges = [edge for edge in old.edges if edge.left in new_nodes and edge.right in new_nodes]
    if not delete:
        new_edges.append(Edge(node, "new", score / 100))
    new = Snapshot(new_nodes, tuple(new_edges))
    result = recompute(old, new, original, changed_records=(node,), max_records=100)
    assert result.resolution.partitions == new.solve().partitions
    assert recompute(old, new, original, max_records=0).resolution.partitions == new.solve().partitions


def test_policy_change_always_uses_full_rebuild():
    old = Snapshot(frozenset("ab"), (Edge("a", "b", .8),))
    new = Snapshot(old.records, old.edges, threshold=.7)
    result = recompute(old, new, old.solve())
    assert result.mode == "full"
    assert result.resolution.partitions == (("a", "b"),)
