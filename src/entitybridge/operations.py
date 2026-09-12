"""Consistent application snapshots and restore into exclusively new targets.

This is a portable logical backup of EntityBridge tables and registered immutable
artifacts. It is not PostgreSQL PITR, a server/role backup, or encrypted storage.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Integer, create_engine, delete, insert, inspect, select, text
from sqlalchemy.engine import make_url

from . import query_projection
from . import schema as s
from .database import CURRENT_SCHEMA, version_table
from .store import Store, encoded

FORMAT = "entitybridge-logical-backup-v1"


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _json(content):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key in backup JSON")
            result[key] = value
        return result
    return json.loads(content, object_pairs_hook=unique,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Nonfinite backup JSON")))


def _write(path, content):
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _revision_name(revision):
    if not isinstance(revision, str) or str(UUID(revision)) != revision:
        raise ValueError("Invalid revision identifier in backup")
    return revision + ".json"


def _rows(connection):
    return {table.name: [dict(row) for row in connection.execute(
        select(table).order_by(*table.primary_key.columns)).mappings()]
        for table in s.metadata.sorted_tables}


def _portable_rows(tables):
    result = copy.deepcopy(tables)
    for row in result[s.revisions.name]:
        row["artifact_path"] = _revision_name(row["revision_id"])
    return result


def _summary(tables, artifacts):
    published = {}
    for revision in tables[s.revisions.name]:
        if revision["status"] == "published":
            payload = _json(artifacts[_revision_name(revision["revision_id"])])
            published[revision["revision_id"]] = _hash(encoded([
                payload["entities"][key] for key in sorted(payload["entities"])]))
    return {"table_counts": {name: len(rows) for name, rows in tables.items()},
            "table_sha256": {name: _hash(encoded(rows)) for name, rows in tables.items()},
            "published_query_sha256": published,
            "current_revision": tables[s.current.name][0]["revision_id"]}


def _validate_state(tables, artifacts):
    if set(tables) != set(s.metadata.tables):
        raise ValueError("Backup table set differs from the installed schema")
    for table in s.metadata.sorted_tables:
        rows = tables[table.name]
        if not isinstance(rows, list) or any(set(row) != set(table.columns.keys()) for row in rows):
            raise ValueError("Backup table columns do not match the installed schema")
        keys = [tuple(row[column.name] for column in table.primary_key.columns) for row in rows]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate primary key in backup")
        for constraint in table.foreign_key_constraints:
            elements = list(constraint.elements)
            target_table = elements[0].column.table
            targets = {tuple(row[element.column.name] for element in elements)
                       for row in tables[target_table.name]}
            for row in rows:
                key = tuple(row[element.parent.name] for element in elements)
                if all(value is not None for value in key) and key not in targets:
                    raise ValueError("Backup foreign-key history is incomplete")
    current = tables[s.current.name]
    if len(current) != 1 or current[0]["workspace"] != "default":
        raise ValueError("Backup requires exactly one default workspace")
    revisions = {row["revision_id"]: row for row in tables[s.revisions.name]}
    pointer = current[0]["revision_id"]
    if pointer is not None and (pointer not in revisions or revisions[pointer]["status"] != "published"):
        raise ValueError("Backup published pointer is invalid")
    expected_artifacts = {_revision_name(key) for key in revisions}
    if set(artifacts) != expected_artifacts:
        raise ValueError("Backup artifacts do not cover every registered revision")
    records = {row["record_id"]: row for row in tables[s.records.name]}
    versions = {row["record_version_id"]: row for row in tables[s.versions.name]}
    snapshots = {row["snapshot_id"] for row in tables[s.snapshots.name]}
    for row in records.values():
        version = versions.get(row["current_version"])
        if not version or version["record_id"] != row["record_id"]:
            raise ValueError("Backup current source version is missing or belongs to another record")
    for version in versions.values():
        if version["record_id"] not in records or version["snapshot_id"] not in snapshots:
            raise ValueError("Backup source provenance is incomplete")
    for revision, row in revisions.items():
        name = _revision_name(revision)
        if row["artifact_path"] != name or _hash(artifacts[name]) != row["artifact_sha256"]:
            raise ValueError("Backup artifact binding or checksum mismatch")
        payload = _json(artifacts[name])
        if (payload["revision_id"] != revision or payload["parent_revision"] != row["parent_revision"]
                or payload["event_cutoff"] != row["event_cutoff"]):
            raise ValueError("Backup revision metadata disagrees with its artifact")
        parent = row["parent_revision"]
        if parent is not None and (parent not in revisions or revisions[parent]["status"] != "published"):
            raise ValueError("Backup revision ancestry is incomplete")
        seen = {revision}
        while parent is not None:
            if parent in seen:
                raise ValueError("Backup revision ancestry contains a cycle")
            seen.add(parent)
            parent = revisions[parent]["parent_revision"]
        for record_id, record in payload["records"].items():
            version = versions.get(record["record_version_id"])
            if not version or version["record_id"] != record_id:
                raise ValueError("Backup historical source version is missing")
            if record != {**version["payload"], "record_id": record_id,
                          "record_version_id": version["record_version_id"], "source": records[record_id]["source_id"]}:
                raise ValueError("Backup historical source content disagrees with its artifact")
        expected = query_projection.expected_rows(payload)
        actual = {
            "entities": sorted([item for item in tables[s.query_entities.name] if item["revision_id"] == revision],
                               key=lambda item: item["entity_id"]),
            "search": sorted([item for item in tables[s.query_values.name] if item["revision_id"] == revision],
                             key=lambda item: (item["entity_id"], item["ordinal"])),
            "members": sorted([item for item in tables[s.memberships.name] if item["revision_id"] == revision],
                              key=lambda item: (item["entity_id"], item["record_id"])),
        }
        states = [item for item in tables[s.query_projections.name] if item["revision_id"] == revision]
        if actual != expected or states != [query_projection.expected_state(payload, row["artifact_sha256"], expected)]:
            raise ValueError("Backup projection is incomplete or corrupt; rebuild it before backup")


def create_backup(store: Store, destination, *, expected_workspace=None) -> dict:
    """Read one DB snapshot and hash immutable files; write manifest last.

    All registered revisions are included, including prepared candidates and
    history. Concurrent normal writes are allowed; schema migrations and artifact
    removal must be stopped. Missing or damaged projections fail closed.
    """
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Backup destination must be a new directory")
    backend = store.engine.dialect.name
    if backend not in {"sqlite", "postgresql"}:
        raise ValueError("Backup supports SQLite and PostgreSQL only")
    if {"dbname", "database", "service"} & store.engine.url.query.keys():
        raise ValueError("Backup refuses database query overrides")
    with ExitStack() as cleanup:
        engine = store.engine
        if backend == "sqlite":
            source_database = store.engine.url.database
            if not source_database or source_database == ":memory:" or store.engine.url.query:
                raise ValueError("Backup requires an existing persistent SQLite database file")
            source_path = Path(source_database).resolve()
            if not source_path.is_file():
                raise ValueError("Backup requires an existing SQLite database file")
            # mode=ro also closes the check/open race: deletion cannot create a new DB.
            engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(
                source_path.as_uri() + "?mode=ro", uri=True))
            cleanup.callback(engine.dispose)
        connection = cleanup.enter_context(engine.connect())
        if backend == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        else:
            connection.exec_driver_sql("BEGIN")
        if set(inspect(connection).get_table_names()) != set(s.metadata.tables) | {"alembic_version"}:
            raise ValueError("Backup refuses unknown or incomplete database tables")
        if expected_workspace is not None:
            from .workspace_binding import WorkspaceBindingConflict
            owners = connection.execute(select(s.workspace_binding.c.workspace_name)).scalars().all()
            if owners != [expected_workspace]:
                raise WorkspaceBindingConflict("Backup requires an existing binding to the selected workspace")
        for table in s.metadata.sorted_tables:
            if {column["name"] for column in inspect(connection).get_columns(table.name)} != set(table.columns.keys()):
                raise ValueError("Backup refuses database columns absent from the installed schema")
        if connection.execute(select(version_table.c.version_num)).scalars().all() != [CURRENT_SCHEMA]:
            raise ValueError("Upgrade and verify the database before backup")
        raw = _rows(connection)
        artifacts = {}
        for row in raw[s.revisions.name]:
            source = (store.artifact_root / row["artifact_path"])
            if source.is_symlink() or not source.resolve().is_relative_to(store.artifact_root):
                raise ValueError("Registered artifact escapes the source artifact root")
            artifacts[_revision_name(row["revision_id"])] = source.read_bytes()
        tables = _portable_rows(raw)
        _validate_state(tables, artifacts)
    database = encoded({"format": FORMAT, "schema_version": CURRENT_SCHEMA, "tables": tables})
    manifest = {"format": FORMAT, "schema_version": CURRENT_SCHEMA, "source_backend": backend,
        "created_at": datetime.now(UTC).isoformat(), "snapshot": "repeatable_read_logical_application_state",
        "database": {"file": "database.json", "bytes": len(database), "sha256": _hash(database)},
        "artifacts": {f"artifacts/{name}": {"bytes": len(content), "sha256": _hash(content)}
                      for name, content in sorted(artifacts.items())},
        "summary": _summary(tables, artifacts)}
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "artifacts").mkdir()
    _write(destination / "database.json", database)
    for name, content in artifacts.items():
        _write(destination / "artifacts" / name, content)
    _write(destination / "manifest.json", encoded(manifest))
    return manifest


def _load_backup(directory):
    directory = Path(directory).resolve()
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("Backup is incomplete: manifest is absent or unsafe")
    manifest = _json(manifest_path.read_bytes())
    if manifest.get("format") != FORMAT or manifest.get("schema_version") != CURRENT_SCHEMA:
        raise ValueError("Unsupported backup format or schema version")
    if manifest.get("database", {}).get("file") != "database.json":
        raise ValueError("Invalid backup database member")
    files = {"database.json": manifest["database"]}
    for name, attributes in manifest["artifacts"].items():
        if not re.fullmatch(r"artifacts/[0-9a-f-]{36}\.json", name):
            raise ValueError("Unsafe artifact member in backup")
        _revision_name(Path(name).stem)
        files[name] = attributes
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError("Backup contains a symlink or escaping member")
        if path.is_dir() and path != directory / "artifacts":
            raise ValueError("Backup contains an unexpected directory")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(files) | {"manifest.json"}:
        raise ValueError("Backup has missing or unexpected files")
    contents = {}
    for name, attributes in files.items():
        content = (directory / name).read_bytes()
        if len(content) != attributes["bytes"] or _hash(content) != attributes["sha256"]:
            raise ValueError("Backup member checksum mismatch")
        contents[name] = content
    database = _json(contents.pop("database.json"))
    if database.get("format") != FORMAT or database.get("schema_version") != CURRENT_SCHEMA:
        raise ValueError("Backup database schema disagrees with its manifest")
    artifacts = {Path(name).name: content for name, content in contents.items()}
    _validate_state(database["tables"], artifacts)
    if _summary(database["tables"], artifacts) != manifest["summary"]:
        raise ValueError("Backup state summary mismatch")
    return manifest, database["tables"], artifacts


def verify_backup(directory) -> dict:
    """Check complete member hashes, source references, history and projections."""
    manifest, _tables, _artifacts = _load_backup(directory)
    return {"verified": True, "manifest_sha256": _hash(encoded(manifest)), **manifest}


def _reset_sequences(connection):
    if connection.dialect.name != "postgresql":
        return
    for table in s.metadata.sorted_tables:
        for column in table.primary_key.columns:
            if isinstance(column.type, Integer):
                sequence = connection.execute(text("SELECT pg_get_serial_sequence(:table, :column)"),
                                              {"table": table.name, "column": column.name}).scalar_one()
                if sequence:
                    maximum = connection.execute(select(column).order_by(column.desc()).limit(1)).scalar_one_or_none()
                    connection.execute(text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                                       {"sequence": sequence, "value": maximum or 1, "called": maximum is not None})


def restore_backup(directory, target_database_url, target_artifact_root) -> dict:
    """Restore only into a new SQLite file or a newly allocated PG _test schema.

    Artifact paths become relative basenames. No existing target database file,
    schema or artifact directory is overwritten. Failed newly owned targets are
    removed. PostgreSQL schema name is returned without connection credentials.
    """
    manifest, tables, artifacts = _load_backup(directory)
    url = make_url(target_database_url)
    target = Path(target_artifact_root).resolve()
    backup_root = Path(directory).resolve()
    if target.is_relative_to(backup_root):
        raise ValueError("Restore target must be outside the backup")
    if target.exists():
        raise ValueError("Restore artifact directory must be new")
    if {"dbname", "database", "service", "options"} & url.query.keys():
        raise ValueError("Restore refuses database or schema query overrides")
    backend = url.get_backend_name()
    schema_name, admin, database_path, store = None, None, None, None
    if backend == "sqlite":
        if not url.database or url.database == ":memory:" or url.query:
            raise ValueError("Restore requires a new persistent SQLite file")
        database_path = Path(url.database).resolve()
        if database_path.exists() or database_path.is_relative_to(target) or database_path.is_relative_to(backup_root):
            raise ValueError("Restore requires a new SQLite file outside the new artifact directory")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        url = url.set(database=str(database_path))
    elif backend == "postgresql":
        if not (url.database or "").endswith("_test"):
            raise ValueError("PostgreSQL restores require a database ending in _test")
    else:
        raise ValueError("Restore supports SQLite and PostgreSQL only")
    created_target = schema_created = database_created = False
    try:
        if backend == "postgresql":
            schema_name = "restore_" + uuid4().hex
            admin = create_engine(url)
            with admin.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
            url = url.update_query_dict({"options": f"-csearch_path={schema_name}"})
        else:
            # Reserve exclusively; a concurrent creator cannot turn this into an overwrite.
            with database_path.open("xb"):
                pass
            database_created = True
        target.mkdir(parents=True, exist_ok=False)
        created_target = True
        for name, content in artifacts.items():
            _write(target / name, content)
        store = Store(url, target)
        store.initialize()
        with store.engine.begin() as connection:
            connection.execute(delete(s.current))  # Only the just-created default workspace.
            for table in s.metadata.sorted_tables:
                rows = tables[table.name]
                for offset in range(0, len(rows), 500):
                    connection.execute(insert(table), rows[offset:offset + 500])
            _reset_sequences(connection)
            if _portable_rows(_rows(connection)) != tables:
                raise ValueError("Restored database rows differ from the snapshot")
            cancelled_jobs = 0
            if "durable_job" in s.metadata.tables:
                from .jobs import cancel_restored_jobs
                cancelled_jobs = cancel_restored_jobs(connection)
            for row in tables[s.revisions.name]:
                query_projection.verify(connection, _json(artifacts[_revision_name(row["revision_id"])]),
                                        row["artifact_sha256"])
        for revision, expected in manifest["summary"]["published_query_sha256"].items():
            if _hash(encoded(store.entities(revision=revision))) != expected:
                raise ValueError("Restored published queries differ from the snapshot")
        if store.current_revision() != manifest["summary"]["current_revision"]:
            raise ValueError("Restored publication pointer differs from the snapshot")
        return {"restored": True, "backend": backend, "postgres_schema": schema_name,
                "backup_manifest_sha256": _hash(encoded(manifest)), "schema_version": CURRENT_SCHEMA,
                "artifacts": len(artifacts), "table_counts": manifest["summary"]["table_counts"],
                "recovery_cancelled_jobs": cancelled_jobs,
                "checks": {name: True for name in ["database_rows", "source_versions", "history", "publication_pointer",
                           "artifact_relocation", "artifact_hashes", "query_projections", "published_queries"]}}
    except BaseException:
        if store:
            store.engine.dispose()
        if created_target and target.is_dir() and target.resolve() == target:
            shutil.rmtree(target)
        if database_created:
            for path in (database_path, Path(str(database_path) + "-wal"), Path(str(database_path) + "-shm")):
                if path.is_file():
                    path.unlink()
        if admin and schema_created:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        raise
    finally:
        if store:
            store.engine.dispose()
        if admin:
            admin.dispose()
