"""Opaque entity IDs and complete identity transitions between revisions."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class Lineage:
    from_entity: str
    to_entity: str
    kind: str


@dataclass(frozen=True)
class IdentityResult:
    entities: dict[str, tuple[str, ...]]
    lineage: tuple[Lineage, ...]
    destinations: dict[str, tuple[str, ...]]


def assign_identities(
    partitions: Iterable[Iterable[str]],
    previous: Mapping[str, Iterable[str]] | None = None,
    *,
    id_factory: Callable = uuid4,
) -> IdentityResult:
    partitions = [tuple(sorted(partition)) for partition in partitions]
    prior = {eid: tuple(sorted(members)) for eid, members in (previous or {}).items()}
    for collection in (partitions, list(prior.values())):
        seen = set()
        for members in collection:
            if not members or len(members) != len(set(members)) or seen.intersection(members):
                raise ValueError("Partitions must be non-empty, disjoint sets of records")
            seen.update(members)
    by_members = {members: eid for eid, members in prior.items()}
    entities = {}
    for members in sorted(partitions):
        eid = by_members.get(members)
        if eid is None:
            eid = str(id_factory())
            if eid in prior or eid in entities:
                raise ValueError("New identity ID collides with a current or historic identity")
        entities[eid] = members
    membership = {member: eid for eid, members in entities.items() for member in members}
    destinations = {eid: tuple(sorted({membership[member] for member in members if member in membership}))
                    for eid, members in prior.items()}
    lineage = []
    for eid in sorted(prior):
        for new_id in destinations[eid]:
            if eid == new_id:
                kinds = ("CONTINUE",)
            else:
                split = len(destinations[eid]) > 1 or bool(set(prior[eid]) - set(entities[new_id]))
                merge = bool(set(entities[new_id]) - set(prior[eid]))
                kinds = tuple(kind for kind, applies in (("SPLIT", split), ("MERGE", merge)) if applies)
            lineage.extend(Lineage(eid, new_id, kind) for kind in kinds)
    return IdentityResult(entities, tuple(lineage), destinations)
