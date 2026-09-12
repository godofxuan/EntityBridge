import os
from uuid import uuid4

import pytest

from entitybridge.store import Store, VersionConflict


@pytest.fixture
def store(tmp_path):
    url = os.environ.get("ENTITYBRIDGE_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'state.db'}")
    schema = None
    if url.startswith("postgresql"):
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        if not parsed.database.endswith("_test"):
            raise ValueError("Integration tests require a database name ending in _test")
        schema = "test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as con:
            con.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = parsed.update_query_dict({"options": f"-csearch_path={schema}"})
    result = Store(url, tmp_path / "artifacts")
    result.initialize()
    yield result
    result.engine.dispose()
    if schema:
        with admin.begin() as con:
            con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_import_is_idempotent_and_only_complete_revision_is_visible(store):
    rows = [{"source_key": "00000001", "name": "EXAMPLE LIMITED", "postcode": "AB1 2CD"}]
    first = store.import_records("demo", rows)
    second = store.import_records("demo", rows)
    assert second == first
    assert store.entities() == []
    prepared = store.prepare_revision([])
    assert store.entities() == []
    store.publish(prepared["revision_id"], expected_parent=None)
    entity = store.entities()[0]
    assert entity["canonical"]["name"]["value"] == "EXAMPLE LIMITED"
    assert entity["canonical"]["name"]["record_version_id"]
    assert len(entity["members"]) == 1


def test_revoke_splits_bridge_preserves_history_and_suppresses_same_automatic_evidence(store):
    imported = store.import_records("demo", [
        {"source_key": "a", "name": "ALPHA LIMITED"},
        {"source_key": "b", "name": "ALPHA LTD"},
    ])
    a, b = imported["records"]
    edges = [{"left": a["record_id"], "right": b["record_id"], "left_version": a["record_version_id"],
              "right_version": b["record_version_id"], "score": 0.99, "evidence": {"kind": "synthetic"}}]
    initial = store.prepare_revision(edges)["revision_id"]
    store.publish(initial, expected_parent=None)
    old_entity = store.entities()[0]["entity_id"]
    decision = store.decide(a["record_id"], b["record_id"], action="accept", reason="Synthetic test confirmation",
        reviewer="tester", base_revision=initial, left_version=a["record_version_id"],
        right_version=b["record_version_id"], policy_version="default-v1")
    store.publish(decision["revision_id"], expected_parent=initial)
    base = store.current_revision()
    preview = store.revoke_preview(decision["decision_id"], base_revision=base)
    assert len(preview["partitions"]) == 2
    assert store.current_revision() == base
    revoked = store.revoke(decision["decision_id"], base_revision=base, preview_cutoff=preview["event_cutoff"],
        reviewer="tester", reason="Wrong confirmation")
    store.publish(revoked["revision_id"], expected_parent=base)
    assert len(store.entities()) == 2
    assert len(store.entity(old_entity)["destinations"]) == 2
    assert len(store.entity(old_entity, revision=initial)["members"]) == 2
    repeated = store.prepare_revision(edges)
    store.publish(repeated["revision_id"], expected_parent=revoked["revision_id"])
    assert len(store.entities()) == 2


def test_scores_cannot_publish_against_changed_endpoint_versions(store):
    a, b = store.import_records("demo", [{"source_key": "a", "name": "ALPHA"},
        {"source_key": "b", "name": "ALPHA"}])["records"]
    edge = {"left": a["record_id"], "right": b["record_id"], "left_version": a["record_version_id"],
        "right_version": b["record_version_id"], "score": 1.0}
    store.import_records("demo", [{"source_key": "b", "name": "OMEGA"}])
    with pytest.raises(VersionConflict):
        store.prepare_revision([edge])


def test_human_not_same_survives_model_change_and_expires_explicitly_on_source_change(store):
    a, b = store.import_records("demo", [{"source_key": "a", "name": "ALPHA"},
        {"source_key": "b", "name": "ALPHA"}])["records"]
    edge = {"left": a["record_id"], "right": b["record_id"], "left_version": a["record_version_id"],
        "right_version": b["record_version_id"], "score": 1.0}
    initial = store.prepare_revision([edge], policy_version="p1")["revision_id"]
    store.publish(initial, expected_parent=None)
    choice = store.decide(a["record_id"], b["record_id"], action="reject", reason="Distinct corporations",
        reviewer="tester", base_revision=initial, left_version=a["record_version_id"],
        right_version=b["record_version_id"], policy_version="p1")
    store.publish(choice["revision_id"], expected_parent=initial)
    new_model = store.prepare_revision([edge], policy_version="p2")["revision_id"]
    store.publish(new_model, expected_parent=choice["revision_id"])
    assert len(store.entities()) == 2
    store.import_records("demo", [{"source_key": "b", "name": "BETA"}])
    assert store.decision_history()[0]["events"][-1]["action"] == "EXPIRE"


def test_competing_publishers_cannot_replace_each_others_revision(store):
    from concurrent.futures import ThreadPoolExecutor
    store.import_records("demo", [{"source_key": "a", "name": "ALPHA"}])
    a, b = store.prepare_revision([]), store.prepare_revision([])
    def publish(candidate):
        try:
            store.publish(candidate["revision_id"], expected_parent=None)
            return "published"
        except VersionConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, [a, b]))
    assert sorted(outcomes) == ["conflict", "published"]
    assert len(store.entities()) == 1


def test_incomplete_or_corrupt_artifact_never_changes_published_pointer(store):
    store.import_records("demo", [{"source_key": "a", "name": "ALPHA"}])
    first = store.prepare_revision([])["revision_id"]
    store.publish(first, expected_parent=None)
    second = store.prepare_revision([])["revision_id"]
    (store.artifact_root / f"{second}.json").write_text("partial")
    with pytest.raises(ValueError, match="checksum"):
        store.publish(second, expected_parent=first)
    assert store.current_revision() == first
    assert store.entities()[0]["canonical"]["name"]["value"] == "ALPHA"


def test_revocation_refuses_preview_stale_after_another_unpublished_decision(store):
    a, b, c = store.import_records("demo", [{"source_key": key, "name": key} for key in "abc"])["records"]
    first = store.prepare_revision([])["revision_id"]
    store.publish(first, expected_parent=None)
    def accept(left, right, base):
        return store.decide(left["record_id"], right["record_id"], action="accept", reason="Synthetic confirmation",
            reviewer="tester", base_revision=base, left_version=left["record_version_id"],
            right_version=right["record_version_id"], policy_version="default-v1")
    decision = accept(a, b, first)
    base = decision["revision_id"]
    store.publish(base, expected_parent=first)
    preview = store.revoke_preview(decision["decision_id"], base_revision=base)
    accept(b, c, base)
    with pytest.raises(VersionConflict, match="stale"):
        store.revoke(decision["decision_id"], base_revision=base, reviewer="tester", reason="Correct old judgment",
                     preview_cutoff=preview["event_cutoff"])
    assert store.current_revision() == base
    assert store.decision_history()[0]["events"][-1]["action"] == "CREATE"
