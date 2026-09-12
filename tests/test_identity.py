from uuid import UUID

import pytest

from entitybridge.identity import assign_identities


def test_assigns_independent_uuid_and_preserves_unchanged_members():
    initial = assign_identities([("a", "b"), ("c",)])
    assert len(initial.entities) == 2
    assert all(UUID(entity_id) for entity_id in initial.entities)
    updated = assign_identities([("c",), ("b", "a")], previous=initial.entities)
    assert updated.entities == initial.entities
    assert updated.destinations == {eid: (eid,) for eid in initial.entities}


def test_split_allocates_new_ids_and_all_old_identity_destinations():
    updated = assign_identities([("a",), ("b", "c")], previous={"old": ("a", "b", "c")})
    assert "old" not in updated.entities
    assert set(updated.destinations["old"]) == set(updated.entities)
    assert {(line.from_entity, line.to_entity, line.kind) for line in updated.lineage} == {
        ("old", eid, "SPLIT") for eid in updated.entities
    }


def test_merge_records_all_parents_and_deletion_leaves_no_false_redirect():
    updated = assign_identities([("a", "b")], previous={"old-a": ("a",), "old-b": ("b",), "gone": ("c",)})
    merged = next(iter(updated.entities))
    assert updated.destinations == {"old-a": (merged,), "old-b": (merged,), "gone": ()}
    assert {(line.from_entity, line.kind) for line in updated.lineage} == {
        ("old-a", "MERGE"), ("old-b", "MERGE")
    }


@pytest.mark.parametrize("partitions,previous,id_factory", [
    ([("a",), ("a", "b")], {}, lambda: "id"),
    ([()], {}, lambda: "id"),
    ([("a", "a")], {}, lambda: "id"),
    ([("a",)], {"x": ("a",), "y": ("a",)}, lambda: "id"),
    ([("a",), ("b",)], {}, lambda: "id"),
    ([("a", "b")], {"old": ("a",)}, lambda: "old"),
])
def test_invalid_partition_or_reused_entity_id_cannot_silently_drop_members(partitions, previous, id_factory):
    with pytest.raises(ValueError):
        assign_identities(partitions, previous, id_factory=id_factory)
