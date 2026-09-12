"""A database, rather than its connection spelling, owns one workspace name."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from test_store_regressions import store as store  # noqa: PLC0414

from entitybridge import schema as s
from entitybridge.database import CURRENT_SCHEMA
from entitybridge.store import Store
from entitybridge.workspace_binding import WorkspaceBindingConflict, bind_workspace


def test_binding_is_persistent_and_same_name_is_idempotent(store):
    assert bind_workspace(store, "workspace-a") == "workspace-a"
    store.initialize()
    assert bind_workspace(store, "workspace-a") == "workspace-a"
    with store.engine.connect() as con:
        assert [dict(row) for row in con.execute(select(s.workspace_binding)).mappings()] == [
            {"singleton": 1, "workspace_name": "workspace-a"}]
    with pytest.raises(WorkspaceBindingConflict):
        bind_workspace(store, "workspace-b")


def test_different_uri_for_the_same_database_cannot_change_workspace(store):
    bind_workspace(store, "workspace-a")
    original = store.engine.url
    if original.get_backend_name() == "sqlite":
        path = Path(original.database)
        alias_url = original.set(database=str(path.parent) + "/./" + path.name)
    else:
        assert original.host in {"localhost", "127.0.0.1"}, "Alias regression needs a loopback test server"
        alias_url = original.set(host="localhost" if original.host == "127.0.0.1" else "127.0.0.1")
    assert alias_url != original
    alias = Store(alias_url, store.artifact_root)
    try:
        alias.initialize()
        with pytest.raises(WorkspaceBindingConflict):
            bind_workspace(alias, "workspace-b")
        assert bind_workspace(alias, "workspace-a") == "workspace-a"
    finally:
        alias.engine.dispose()


def test_concurrent_first_binding_allows_only_one_workspace_name(store):
    second = Store(store.engine.url, store.artifact_root)
    second.initialize()

    def attempt(item):
        target, name = item
        try:
            return bind_workspace(target, name)
        except WorkspaceBindingConflict:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, [(store, "workspace-a"), (second, "workspace-b")]))
        winners = [value for value in results if value]
        assert len(winners) == 1
        with store.engine.connect() as con:
            assert con.execute(select(s.workspace_binding.c.workspace_name)).scalars().all() == winners
    finally:
        second.engine.dispose()


@pytest.mark.parametrize("name", ["", " ", "a" * 201, "workspace\nother", None, 123])
def test_invalid_workspace_name_does_not_bind_the_database(store, name):
    with pytest.raises(ValueError):
        bind_workspace(store, name)
    with store.engine.connect() as con:
        assert con.execute(select(s.workspace_binding)).all() == []


@pytest.mark.parametrize("singleton", [0, 2])
def test_database_enforces_the_singleton_key(store, singleton):
    with pytest.raises(IntegrityError), store.engine.begin() as con:
        con.execute(insert(s.workspace_binding).values(singleton=singleton, workspace_name="a"))


def test_plain_store_is_not_implicitly_bound(store):
    store.import_records("registry", [{"source_key": "a", "name": "ALPHA"}])
    candidate = store.prepare_revision([])["revision_id"]
    store.publish(candidate, expected_parent=None)
    assert len(store.entities()) == 1
    with store.engine.connect() as con:
        assert con.execute(select(s.workspace_binding)).all() == []


@pytest.mark.parametrize("version", ["0001", "0002", "0003", "0004"])
def test_known_schema_upgrades_preserve_records_and_enable_binding(store, version):
    from alembic import command
    from alembic.config import Config

    store.import_records("registry", [{"source_key": "a", "name": "ALPHA"}])
    before = store.active_records()
    with store.engine.begin() as con:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = con
        command.downgrade(config, version)
    store.initialize()
    assert store.active_records() == before
    assert bind_workspace(store, "upgraded") == "upgraded"
    with store.engine.connect() as con:
        assert con.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == CURRENT_SCHEMA


def test_backup_restore_preserves_database_workspace_ownership(store, tmp_path):
    from entitybridge.operations import create_backup, restore_backup

    bind_workspace(store, "workspace-a")
    store.import_records("registry", [{"source_key": "a", "name": "ALPHA"}])
    revision = store.prepare_revision([])["revision_id"]
    store.publish(revision, expected_parent=None)
    backup = tmp_path / "workspace-backup"
    create_backup(store, backup)
    restored_url = f"sqlite:///{tmp_path / 'restored.db'}"
    restored_root = tmp_path / "restored-artifacts"
    restore_backup(backup, restored_url, restored_root)
    restored = Store(restored_url, restored_root)
    try:
        restored.initialize()
        assert bind_workspace(restored, "workspace-a") == "workspace-a"
        with pytest.raises(WorkspaceBindingConflict):
            bind_workspace(restored, "workspace-b")
        assert restored.current_revision() == revision
        assert len(restored.entities()) == 1
    finally:
        restored.engine.dispose()
