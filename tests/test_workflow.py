from entitybridge.api import RunRequest, run_matching
from entitybridge.store import Store


def test_source_update_replaces_old_edges_and_incremental_workflow_equals_full(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'workflow.db'}", tmp_path / "artifacts")
    store.initialize()
    store.import_records("left", [{"source_key": "a", "name": "ALPHA LIMITED", "country": "GB"},
                                  {"source_key": "c", "name": "BETA LIMITED", "country": "GB"}])
    store.import_records("right", [{"source_key": "b", "name": "ALPHA LIMITED", "country": "GB"}])
    config = RunRequest(method="exact", threshold=1.0, verify_full=True)
    initial = run_matching(store, config)
    store.publish(initial["revision_id"], expected_parent=None)
    store.import_records("right", [{"source_key": "b", "name": "BETA LIMITED", "country": "GB"}])
    updated = run_matching(store, config)
    assert updated["score_refresh"]["rescored_pairs"] == 1
    assert updated["computation"]["full_equivalence_checked"]
    store.publish(updated["revision_id"], expected_parent=initial["revision_id"])
    assert sorted(len(e["members"]) for e in store.entities()) == [1, 2]
    assert len(store.entities(revision=initial["revision_id"])) == 2


def test_feature_version_change_forces_full_rebuild(tmp_path, monkeypatch):
    from entitybridge import matching_service
    store = Store(f"sqlite:///{tmp_path / 'version.db'}", tmp_path / "artifacts")
    store.initialize()
    store.import_records("left", [{"source_key": "a", "name": "ALPHA LIMITED"}])
    store.import_records("right", [{"source_key": "b", "name": "ALPHA LTD"}])
    config = RunRequest(method="exact", threshold=1)
    initial = run_matching(store, config)
    store.publish(initial["revision_id"], expected_parent=None)
    monkeypatch.setattr(matching_service, "FEATURE_VIEW_VERSION", "a-new-view")
    rebuilt = run_matching(store, config)
    assert rebuilt["score_refresh"] is None
    assert rebuilt["computation"]["mode"] == "full"
