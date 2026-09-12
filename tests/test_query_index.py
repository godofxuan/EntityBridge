"""Fixed revision relational projections preserve artifact query semantics."""

import pytest
from test_store_regressions import store as store  # noqa: PLC0414 - re-export the shared pytest fixture


def published_fixture(store):
    left = store.import_records("registry", [
        {"source_key": "a", "name": "Straße 100%_ ALPHA", "aliases": ["Former Acme", "Beta Γ"], "city": "EXAMPLE"},
        {"source_key": "b", "name": "STRASSE 100xxX ALPHA", "aliases": ["Unrelated"]},
        {"source_key": "c", "name": "OTHER ENTITY", "aliases": ["Standalone"]},
    ])["records"]
    right = store.import_records("contract", [{"source_key": "d", "name": "ACME ALIAS", "aliases": ["Distinct Old Name"]}])["records"][0]
    first = left[0]
    candidate = store.prepare_revision([{"left": first["record_id"], "right": right["record_id"],
        "left_version": first["record_version_id"], "right_version": right["record_version_id"], "score": 1.0}])
    store.publish(candidate["revision_id"], expected_parent=None)
    return candidate["revision_id"]


def reference(payload, query=""):
    return [entity for entity in payload["entities"].values() if not query or any(
        query.casefold() in str(value).casefold() for member in entity["members"]
        for value in (payload["records"][member].get("name", ""), *payload["records"][member].get("aliases", [])))]


@pytest.mark.parametrize("query", ["", "STRASSE", "100%_", "former acme", "βeta", "Distinct Old Name", "absent"])
def test_paged_query_matches_frozen_artifact_without_reading_it_again(store, monkeypatch, query):
    revision = published_fixture(store)
    expected = reference(store._payload(revision), query)
    def forbidden(*args, **kwargs):
        raise AssertionError("A ready projection must not reopen the complete artifact")
    monkeypatch.setattr(store, "_payload", forbidden)
    pages = [store.entities_page(query, revision=revision, offset=offset, limit=1)
             for offset in range(len(expected) + 1)]
    assert all(page["revision_id"] == revision and page["total"] == len(expected) for page in pages)
    assert [item for page in pages for item in page["items"]] == expected
    assert store.entities(query, revision=revision) == expected


def test_detail_uses_frozen_member_versions_and_does_not_read_complete_artifact(store, monkeypatch):
    revision = published_fixture(store)
    payload = store._payload(revision)
    entity = next(item for item in payload["entities"].values() if len(item["members"]) == 2)
    expected = {**entity, "revision_id": revision, "status": "historical",
                "records": [payload["records"][member] for member in entity["members"]]}
    store.import_records("registry", [{"source_key": "a", "name": "CHANGED AFTER PUBLICATION"}])
    def forbidden(*args, **kwargs):
        raise AssertionError("Entity detail must use frozen relational member versions")
    monkeypatch.setattr(store, "_payload", forbidden)
    assert store.entity(entity["entity_id"], revision=revision) == expected


@pytest.mark.parametrize("damage", ["missing_state", "canonical", "search", "missing_member", "member_version"])
def test_incomplete_or_corrupt_projection_refuses_publication_and_keeps_old_revision(store, damage):
    from sqlalchemy import delete, select, update

    from entitybridge import schema as s
    initial = published_fixture(store)
    candidate = store.prepare_revision([])["revision_id"]
    with store.engine.begin() as con:
        if damage == "missing_state":
            con.execute(delete(s.query_projections).where(s.query_projections.c.revision_id == candidate))
        elif damage == "canonical":
            con.execute(update(s.query_entities).where(s.query_entities.c.revision_id == candidate)
                        .values(canonical={"name": {"value": "CORRUPT"}}))
        elif damage == "search":
            con.execute(update(s.query_values).where(s.query_values.c.revision_id == candidate).values(value_folded="CORRUPT"))
        else:
            members = con.execute(select(s.memberships).where(s.memberships.c.revision_id == candidate)).mappings().all()
            clause = (s.memberships.c.revision_id == candidate) & (s.memberships.c.record_id == members[0]["record_id"])
            if damage == "missing_member":
                con.execute(delete(s.memberships).where(clause))
            else:
                con.execute(update(s.memberships).where(clause).values(record_version_id=members[1]["record_version_id"]))
    with pytest.raises(ValueError, match="projection"):
        store.publish(candidate, expected_parent=initial)
    assert store.current_revision() == initial
    assert store.projection_status(initial, verify=True)["ready"]
    with pytest.raises(KeyError):
        store.entities_page(revision=candidate)


def test_upgraded_v2_history_backfills_once_atomically_under_competing_readers(store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Barrier

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import select, update

    from entitybridge import schema as s

    revision = published_fixture(store)
    path = store.artifact_root / f"{revision}.json"
    original_bytes = path.read_bytes()
    expected = reference(store._payload(revision))
    with store.engine.begin() as con:
        manifest = con.execute(select(s.runs.c.manifest).where(s.runs.c.run_id == revision)).scalar_one()
        manifest.pop("query_projection_version")
        con.execute(update(s.runs).where(s.runs.c.run_id == revision).values(manifest=manifest))
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = con
        command.downgrade(config, "0002")
    store.initialize()
    assert not store.projection_status(revision)["ready"]
    gate = Barrier(2)
    original = store._payload
    def simultaneous_read(*args, **kwargs):
        payload = original(*args, **kwargs)
        gate.wait(timeout=5)
        return payload
    monkeypatch.setattr(store, "_payload", simultaneous_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store.entities_page(revision=revision), range(2)))
    assert all(result["items"] == expected for result in results)
    monkeypatch.setattr(store, "_payload", original)
    assert store.projection_status(revision, verify=True)["ready"]
    assert store.current_revision() == revision and path.read_bytes() == original_bytes
    def forbidden(*args, **kwargs):
        raise AssertionError("An existing completed projection must be reused")
    monkeypatch.setattr(store, "_payload", forbidden)
    assert store.entities_page(revision=revision)["items"] == expected


def test_rebuild_failure_rolls_back_partial_projection_and_preserves_ready_queries(store, monkeypatch):
    from entitybridge import query_projection
    revision = published_fixture(store)
    before = store.entities_page(revision=revision)
    original = query_projection.verify
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Synthetic crash before committing the replacement projection")
    monkeypatch.setattr(query_projection, "verify", fail)
    with pytest.raises(RuntimeError, match="Synthetic crash"):
        store.rebuild_query_projection(revision)
    assert store.entities_page(revision=revision) == before
    assert store.current_revision() == revision


def test_projection_write_failure_rolls_back_the_entire_prepared_revision(store, monkeypatch):
    from sqlalchemy import func, select

    from entitybridge import query_projection
    from entitybridge import schema as s
    revision = published_fixture(store)
    history = store.history()
    before = store.entities_page(revision=revision)
    original = query_projection.write
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Synthetic failure after projection rows but before candidate commit")
    monkeypatch.setattr(query_projection, "write", fail)
    with pytest.raises(RuntimeError, match="Synthetic failure"):
        store.prepare_revision([])
    assert store.current_revision() == revision
    assert store.history() == history
    assert store.entities_page(revision=revision) == before
    with store.engine.connect() as con:
        assert con.execute(select(func.count()).select_from(s.query_projections)).scalar_one() == 1
        assert con.execute(select(func.count()).select_from(s.runs)).scalar_one() == 1
