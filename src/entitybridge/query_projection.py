"""Rebuildable relational query views of an immutable identity artifact.

These functions never publish identities or alter source facts. A completed
projection is written in the same transaction as its rows, and is checked
against the artifact before an identity revision becomes visible.
"""

import hashlib
import json

from sqlalchemy import delete, insert, select

from . import schema as s

VERSION = "entity-query-v1"


def content_hash(value):
    content = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def expected_rows(payload):
    revision = payload["revision_id"]
    entities, search, members = [], [], []
    for entity_id, entity in sorted(payload["entities"].items()):
        entities.append({"revision_id": revision, "entity_id": entity_id, "canonical": entity["canonical"]})
        values = set()
        for member in sorted(entity["members"]):
            record = payload["records"][member]
            members.append({"revision_id": revision, "entity_id": entity_id,
                            "record_id": member, "record_version_id": record["record_version_id"]})
            for value in (record.get("name", ""), *record.get("aliases", [])):
                values.add(str(value).casefold())
        search.extend({"revision_id": revision, "entity_id": entity_id, "ordinal": ordinal,
                       "value_folded": value} for ordinal, value in enumerate(sorted(values)))
    return {"entities": entities, "search": search, "members": members}


def expected_state(payload, artifact_sha256, rows):
    return {"revision_id": payload["revision_id"], "projection_version": VERSION,
            "artifact_sha256": artifact_sha256, "content_sha256": content_hash(rows),
            "entity_count": len(rows["entities"]), "member_count": len(rows["members"]),
            "search_value_count": len(rows["search"])}


def write(connection, payload, artifact_sha256):
    """Replace only derived query data; the caller owns one database transaction."""
    rows = expected_rows(payload)
    revision = payload["revision_id"]
    for table in (s.query_values, s.query_entities, s.query_projections):
        connection.execute(delete(table).where(table.c.revision_id == revision))
    for table, values in ((s.query_entities, rows["entities"]), (s.query_values, rows["search"])):
        for start in range(0, len(values), 1000):
            connection.execute(insert(table), values[start:start + 1000])
    connection.execute(insert(s.query_projections).values(**expected_state(payload, artifact_sha256, rows)))


def verify(connection, payload, artifact_sha256):
    """Check content and complete member/version coverage, not only row counts."""
    revision = payload["revision_id"]
    expected = expected_rows(payload)
    state = connection.execute(select(s.query_projections).where(
        s.query_projections.c.revision_id == revision)).mappings().one_or_none()
    if not state or dict(state) != expected_state(payload, artifact_sha256, expected):
        raise ValueError("Query projection is missing or its completion metadata does not match the artifact")
    actual = {
        "entities": [dict(row) for row in connection.execute(select(s.query_entities)
            .where(s.query_entities.c.revision_id == revision).order_by(s.query_entities.c.entity_id)).mappings()],
        "search": [dict(row) for row in connection.execute(select(s.query_values)
            .where(s.query_values.c.revision_id == revision)
            .order_by(s.query_values.c.entity_id, s.query_values.c.ordinal)).mappings()],
        "members": [dict(row) for row in connection.execute(select(s.memberships.c.revision_id,
            s.memberships.c.entity_id, s.memberships.c.record_id, s.memberships.c.record_version_id)
            .where(s.memberships.c.revision_id == revision)
            .order_by(s.memberships.c.entity_id, s.memberships.c.record_id)).mappings()],
    }
    if actual != expected or content_hash(actual) != state["content_sha256"]:
        raise ValueError("Query projection content or member coverage does not match the artifact")
    return dict(state)
