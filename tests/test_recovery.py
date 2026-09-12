"""Run against isolated schemas; never migrate or destroy an existing application database."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from entitybridge.schema import metadata

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def isolated_database(request, tmp_path):
    if request.param == "sqlite":
        url = URL.create("sqlite", database=str(tmp_path / "migration.db"))
        engine = create_engine(url)
        yield engine, url
        engine.dispose()
        return
    if os.environ.get("ENTITYBRIDGE_RECOVERY_POSTGRES") != "1":
        pytest.skip("Set ENTITYBRIDGE_RECOVERY_POSTGRES=1 to use local PostgreSQL credentials")
    config = json.loads((ROOT / ".tools" / "database.json").read_text(encoding="utf-8"))
    url = URL.create("postgresql+psycopg", username=config["user"], password=config["password"],
                     host=config["host"], port=config["port"], database="entitybridge_test",
                     query={"connect_timeout": "8"})
    schema_name = "recovery_" + uuid4().hex
    admin = create_engine(url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    except SQLAlchemyError as error:
        admin.dispose()
        pytest.fail(f"Local PostgreSQL unavailable ({type(error).__name__}); connection details suppressed", pytrace=False)
    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema_name}"})
    engine = create_engine(scoped_url)
    try:
        yield engine, scoped_url
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        admin.dispose()


def migration_config(connection):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.attributes["connection"] = connection
    return config


def test_frozen_initial_migration_matches_schema_and_can_round_trip(isolated_database):
    engine, _url = isolated_database
    with engine.begin() as connection:
        config = migration_config(connection)
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) == set(metadata.tables) | {"alembic_version"}
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        command.upgrade(config, "head")
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []


def test_process_killed_after_artifact_replace_keeps_old_revision_and_recovers(isolated_database, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("failure_recovery", ROOT / "scripts" / "check_failure_recovery.py")
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    engine, url = isolated_database
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
    report = recovery.check_recovery(url, tmp_path / "recovery")
    assert report["worker_terminated"]
    assert report["complete_orphan_file"]
    assert report["orphan_not_registered"]
    assert report["old_revision_readable"]
    assert report["rerun_published"]
    assert report["history_preserved"]
