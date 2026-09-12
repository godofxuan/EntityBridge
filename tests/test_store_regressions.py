"""Business regressions exercising import, review and publication boundaries."""

import os
from uuid import uuid4

import pytest

from entitybridge.database import CURRENT_SCHEMA
from entitybridge.store import Store, VersionConflict


@pytest.fixture
def store(tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    url = os.environ.get("ENTITYBRIDGE_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'state.db'}")
    namespace = None
    if str(url).startswith("postgresql"):
        parsed = make_url(url)
        if not (parsed.database or "").endswith("_test"):
            raise ValueError("Regression tests require a database name ending in _test")
        namespace = "store_regression_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{namespace}"'))
        url = parsed.update_query_dict({"options": f"-csearch_path={namespace}"})
    result = Store(url, tmp_path / "artifacts")
    result.initialize()
    yield result
    result.engine.dispose()
    if namespace:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
        admin.dispose()


def publish(store, edges=()):
    parent = store.current_revision()
    candidate = store.prepare_revision(list(edges))
    store.publish(candidate["revision_id"], expected_parent=parent)
    return candidate["revision_id"]


def decide(store, left, right, *, action="accept", base=None):
    return store.decide(left["record_id"], right["record_id"], action=action,
        reviewer="regression", reason="Synthetic regression evidence", base_revision=base or store.current_revision(),
        left_version=left["record_version_id"], right_version=right["record_version_id"], policy_version="default-v1")


def test_reimport_old_content_restores_tombstoned_record_without_reusing_its_old_version(store):
    original_rows = [{"source_key": "00000001", "name": "ALPHA LIMITED"}]
    original = store.import_records("registry", original_rows)
    record = original["records"][0]
    initial = publish(store)
    old_entity = store.entities()[0]["entity_id"]
    deleted = store.import_records("registry", [{"source_key": "00000001", "tombstone": True}])
    publish(store)
    assert store.active_records() == {}
    restored = store.import_records("registry", original_rows)
    active = store.active_records()
    assert record["record_id"] in active
    assert active[record["record_id"]]["name"] == "ALPHA LIMITED"
    assert restored["snapshot_id"] != original["snapshot_id"]
    assert restored["records"][0]["record_id"] == record["record_id"]
    assert restored["records"][0]["record_version_id"] not in {
        record["record_version_id"], deleted["records"][0]["record_version_id"]}
    assert store.import_records("registry", original_rows) == restored
    publish(store)
    assert store.entity(old_entity, revision=initial)["records"][0]["record_version_id"] == record["record_version_id"]


def test_preview_uses_the_event_cutoff_that_actually_produced_its_partitions(store, monkeypatch):
    a, b, c, d = store.import_records("demo", [{"source_key": key, "name": key} for key in "abcd"])["records"]
    initial = publish(store)
    accepted = decide(store, a, b)
    base = accepted["revision_id"]
    store.publish(base, expected_parent=initial)
    expected_cutoff = store.decision_history()[0]["events"][-1]["seq"]
    original = store._constraints
    injected = False

    def commit_between_constraints_and_response(*args, **kwargs):
        nonlocal injected
        constraints = original(*args, **kwargs)
        if kwargs.get("revoked") and not injected:
            injected = True
            # A second actual transaction commits after the preview has read
            # its constraints but before the preview returns its response.
            decide(store, c, d, base=base)
        return constraints

    monkeypatch.setattr(store, "_constraints", commit_between_constraints_and_response)
    preview = store.revoke_preview(accepted["decision_id"], base_revision=base)
    assert preview["entity_count_after"] == 4
    assert preview["event_cutoff"] == expected_cutoff
    with pytest.raises(VersionConflict, match="stale"):
        store.revoke(accepted["decision_id"], base_revision=base,
            reviewer="regression", reason="Correct the synthetic confirmation", preview_cutoff=preview["event_cutoff"])
    assert store.current_revision() == base


def test_initialize_upgrades_verified_unversioned_v1_database_and_preserves_its_data(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, insert, select

    from entitybridge import schema

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
        connection.exec_driver_sql("DROP TABLE alembic_version")
        connection.execute(insert(schema.sources).values(source_id="legacy", schema_version="1"))
    engine.dispose()
    upgraded = Store(url, tmp_path / "artifacts")
    upgraded.initialize()
    with upgraded.engine.connect() as connection:
        assert connection.execute(select(schema.sources.c.source_id)).scalar_one() == "legacy"
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == CURRENT_SCHEMA
    rows = [{"source_key": "a", "name": "ALPHA"}]
    upgraded.import_records("legacy", rows)
    upgraded.import_records("legacy", [{"source_key": "a", "name": "BETA"}])
    upgraded.import_records("legacy", rows)
    assert next(iter(upgraded.active_records().values()))["name"] == "ALPHA"
    upgraded.initialize()  # The migrated state also supports a second startup.
    upgraded.engine.dispose()


def test_explicit_full_rebuild_preserves_policy_and_records_why(store):
    store.import_records("demo", [{"source_key": "a", "name": "ALPHA"}])
    initial = publish(store)
    candidate = store.prepare_revision([], force_full=True, force_full_reason="global_top_k_candidates")
    assert candidate["computation"]["mode"] == "full"
    assert candidate["computation"]["reason"] == "global_top_k_candidates"
    store.publish(candidate["revision_id"], expected_parent=initial)
    assert store._payload(candidate["revision_id"])["policy_version"] == "default-v1"


def test_review_only_score_never_auto_merges_and_manual_accept_then_revoke_is_preserved(store):
    a, b = store.import_records("demo", [{"source_key": key, "name": key} for key in "ab"])["records"]
    edge = {"left": a["record_id"], "right": b["record_id"], "left_version": a["record_version_id"],
            "right_version": b["record_version_id"], "score": 1.0, "auto_merge": False}
    initial = publish(store, [edge])
    assert len(store.entities()) == 2
    assert store._payload(initial)["edge_decisions"][0]["reason"] == "candidate_requires_review"
    accepted = decide(store, a, b)
    store.publish(accepted["revision_id"], expected_parent=initial)
    assert len(store.entities()) == 1
    base = store.current_revision()
    preview = store.revoke_preview(accepted["decision_id"], base_revision=base)
    assert preview["entity_count_after"] == 2
    revoked = store.revoke(accepted["decision_id"], base_revision=base, reviewer="regression",
        reason="Synthetic correction", preview_cutoff=preview["event_cutoff"])
    store.publish(revoked["revision_id"], expected_parent=base)
    assert len(store.entities()) == 2
    assert store._payload(store.current_revision())["edges"][0]["score"] == 1.0


@pytest.mark.parametrize("revoke_latest_first", [False, True])
def test_independent_same_pair_confirmations_are_revoked_individually_and_never_revive(store, revoke_latest_first):
    a, b = store.import_records("demo", [{"source_key": key, "name": key} for key in "ab"])["records"]
    publish(store)
    choices = []
    for _ in range(2):
        base = store.current_revision()
        choice = decide(store, a, b)
        store.publish(choice["revision_id"], expected_parent=base)
        choices.append(choice)
    if revoke_latest_first:
        choices.reverse()
    for index, choice in enumerate(choices):
        base = store.current_revision()
        preview = store.revoke_preview(choice["decision_id"], base_revision=base)
        assert preview["entity_count_after"] == index + 1
        revoked = store.revoke(choice["decision_id"], base_revision=base, reviewer="regression",
            reason="Withdraw this evidence independently", preview_cutoff=preview["event_cutoff"])
        store.publish(revoked["revision_id"], expected_parent=base)
    assert len(store.entities()) == 2
    # A new explicit confirmation can override old automatic-edge suppression.
    base = store.current_revision()
    accepted = decide(store, a, b)
    store.publish(accepted["revision_id"], expected_parent=base)
    assert len(store.entities()) == 1
    assert [item["events"][-1]["action"] for item in store.decision_history()] == ["REVOKE", "REVOKE", "CREATE"]


def test_conflicting_judgment_is_rejected_without_an_event_or_candidate(store):
    from entitybridge.resolution import ConstraintConflict
    a, b = store.import_records("demo", [{"source_key": key, "name": key} for key in "ab"])["records"]
    initial = publish(store)
    accepted = decide(store, a, b)
    store.publish(accepted["revision_id"], expected_parent=initial)
    before = store.history(), store.decision_history()
    with pytest.raises(ConstraintConflict):
        decide(store, b, a, action="reject")
    assert (store.history(), store.decision_history()) == before
    assert len(store.entities()) == 1


def test_prepared_revision_cannot_be_read_as_a_published_identity(store):
    store.import_records("demo", [{"source_key": "a", "name": "ALPHA"}])
    candidate = store.prepare_revision([])
    entity_id = next(iter(store._payload(candidate["revision_id"], published_only=False)["entities"]))
    for query in (lambda: store.entities(revision=candidate["revision_id"]),
                  lambda: store.entity(entity_id, revision=candidate["revision_id"]),
                  lambda: store.entity(entity_id)):
        with pytest.raises(KeyError):
            query()


def test_initialize_refuses_unknown_database_structure_without_stamping_or_editing_it(tmp_path):
    from sqlalchemy import create_engine, inspect
    url = f"sqlite:///{tmp_path / 'unrelated.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE customer (name TEXT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO customer VALUES ('KEEP ME')")
    unknown = Store(url, tmp_path / "artifacts")
    with pytest.raises(RuntimeError, match="Unrecognized"):
        unknown.initialize()
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == {"customer"}
        assert connection.exec_driver_sql("SELECT name FROM customer").scalar_one() == "KEEP ME"
    unknown.engine.dispose()
    engine.dispose()


def test_same_policy_auto_merge_change_is_detected_by_incremental_recomputation():
    from entitybridge.incremental import Snapshot, recompute
    from entitybridge.resolution import Edge
    old = Snapshot(frozenset("ab"), (Edge("a", "b", 1.0),))
    new = Snapshot(frozenset("ab"), (Edge("a", "b", 1.0, auto_merge=False),))
    changed = recompute(old, new, old.solve())
    assert changed.resolution == new.solve()
    assert changed.resolution.partitions == (("a",), ("b",))
