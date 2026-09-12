"""Rehearse logical backup/restore with synthetic history in disposable targets."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psutil
from sqlalchemy import create_engine, insert, inspect, select, text
from sqlalchemy.engine import URL, make_url

from entitybridge import schema as s
from entitybridge.operations import create_backup, restore_backup, verify_backup
from entitybridge.store import Store, digest


def seed_store(store, records=1000, *, history=True):
    if records < 4 or records % 2:
        raise ValueError("Synthetic record count must be even and at least four")
    if store.active_records() or store.current_revision():
        raise ValueError("Synthetic rehearsal requires an empty workspace")
    groups = []
    for source in ("synthetic_primary", "synthetic_secondary"):
        groups.append(store.import_records(source, [
            {"source_key": str(number), "name": f"SYNTHETIC ORGANISATION {number:06d}",
             "aliases": [f"SYNTHETIC FORMER {number:06d}"], "city": "SYNTHETIC CITY", "country": "GB"}
            for number in range(records // 2)])["records"])
    def edges():
        active = store.active_records()
        return [{"left": left["record_id"], "right": right["record_id"],
                 "left_version": active[left["record_id"]]["record_version_id"],
                 "right_version": active[right["record_id"]]["record_version_id"], "score": 1.0}
                for left, right in zip(*groups, strict=True)]
    first = store.prepare_revision(edges())["revision_id"]
    store.publish(first, expected_parent=None)
    if history:
        store.import_records("synthetic_primary", [{"source_key": "0", "name": "SYNTHETIC UPDATED NAME",
                                                   "aliases": ["SYNTHETIC FORMER NAME"], "country": "GB"}])
        second = store.prepare_revision(edges())["revision_id"]
        store.publish(second, expected_parent=first)
        active = store.active_records()
        left, right = groups[0][0]["record_id"], groups[1][1]["record_id"]
        decision = store.decide(left, right, action="accept", reviewer="synthetic_rehearsal",
            reason="Synthetic restore history", base_revision=second,
            left_version=active[left]["record_version_id"], right_version=active[right]["record_version_id"],
            policy_version="default-v1")
        store.publish(decision["revision_id"], expected_parent=second)
        prepared = store.prepare_revision(edges())["revision_id"]
        if "durable_job" in s.metadata.tables:
            # Explicit lifecycle snapshots exercise recovery cancellation; no worker runs here.
            with store.engine.begin() as connection:
                for status in ("queued", "running", "succeeded"):
                    identity = str(uuid4())
                    payload = {"method": "exact"}
                    connection.execute(insert(s.jobs).values(job_id=identity, idempotency_key="rehearsal-" + status,
                        payload=payload, payload_hash=digest(payload), source_hash=digest(active),
                        parent_revision=store.current_revision(connection), event_cutoff=store._event_cutoff(connection),
                        status=status, attempt=1 if status != "queued" else 0, max_attempts=3,
                        created_at=time.time(), updated_at=time.time(), lease_until=time.time() + 600 if status == "running" else None,
                        lease_owner="synthetic_expired_worker" if status == "running" else None,
                        lease_token=str(uuid4()) if status == "running" else None, cancel_requested=False,
                        result_revision=prepared if status == "succeeded" else None, last_error=None, progress=None))
    return {"records": records, "initial_entities": records // 2, "registered_revisions": len(store.history())}


def postgres_test_base(*, local=False):
    value = os.environ.get("ENTITYBRIDGE_TEST_DATABASE_URL")
    if value:
        url = make_url(value)
    elif local:
        config = json.loads((ROOT / ".tools/database.json").read_text(encoding="utf-8"))
        url = URL.create("postgresql+psycopg", username=config["user"], password=config["password"],
            host=config["host"], port=config["port"], database="entitybridge_test", query={"connect_timeout": "8"})
    else:
        raise ValueError("Set ENTITYBRIDGE_TEST_DATABASE_URL for PostgreSQL rehearsal")
    if (url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test")
            or {"dbname", "database", "service", "options"} & url.query.keys()):
        raise ValueError("PostgreSQL rehearsal requires an explicit _test database without overrides")
    return url


def exercise(source_url, restore_url, workdir, *, records=1000):
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=False)
    store = Store(source_url, workdir / "source_artifacts")
    restore_report = None
    started = time.perf_counter()
    try:
        with store.engine.connect() as connection:
            if inspect(connection).get_table_names():
                raise ValueError("Backup rehearsal source database/schema must be empty")
        store.initialize()
        seeded = seed_store(store, records)
        expected_active = store.active_records()
        expected_history = [{**row, "artifact_path": Path(row["artifact_path"]).name} for row in store.history()]
        expected_decisions = store.decision_history()
        timings = {"seed": time.perf_counter() - started}
        begin = time.perf_counter()
        manifest = create_backup(store, workdir / "backup")
        timings["backup"] = time.perf_counter() - begin
        begin = time.perf_counter()
        verified = verify_backup(workdir / "backup")
        timings["verify"] = time.perf_counter() - begin
        begin = time.perf_counter()
        restore_report = restore_backup(workdir / "backup", restore_url, workdir / "restored_artifacts")
        timings["restore_and_verify"] = time.perf_counter() - begin
        restored_url = make_url(restore_url)
        if restore_report["postgres_schema"]:
            restored_url = restored_url.update_query_dict({"options": "-csearch_path=" + restore_report["postgres_schema"]})
        restored = Store(restored_url, workdir / "restored_artifacts")
        try:
            assert restored.active_records() == expected_active
            assert restored.history() == expected_history
            assert restored.decision_history() == expected_decisions
            cancellation = {"cancelled_nonterminal": 0, "terminal_binding_preserved": True}
            if "durable_job" in s.metadata.tables:
                with restored.engine.begin() as connection:
                    rows = connection.execute(select(s.jobs)).mappings().all()
                    assert sum(row["status"] == "cancelled" for row in rows) == 2
                    assert all(row["lease_owner"] is None and row["lease_token"] is None and row["lease_until"] is None for row in rows)
                    succeeded = next(row for row in rows if row["status"] == "succeeded")
                    assert succeeded["result_revision"] is not None
                    original_seq = max(connection.execute(select(s.job_events.c.seq)).scalars(), default=0)
                    inserted = connection.execute(insert(s.job_events).values(job_id=succeeded["job_id"], attempt=1,
                        action="REHEARSAL_SEQUENCE_CHECK", created_at=time.time(), detail={})).inserted_primary_key[0]
                    assert inserted > original_seq
                    cancellation["cancelled_nonterminal"] = 2
            return {"format": "entitybridge-backup-rehearsal-v1", "backend": store.engine.dialect.name,
                "synthetic": True, "seed": seeded, "seconds": timings,
                "total_seconds": time.perf_counter() - started, "database_bytes": manifest["database"]["bytes"],
                "artifact_bytes": sum(item["bytes"] for item in manifest["artifacts"].values()),
                "manifest_sha256": verified["manifest_sha256"], "schema_version": manifest["schema_version"],
                "checks": {**restore_report["checks"], "active_source_equal": True,
                           "decision_events_equal": True, "history_equal": True, "future_sequence_insert": True},
                "job_recovery": cancellation,
                "hardware": {"platform": platform.platform(), "logical_cpus": psutil.cpu_count(),
                             "memory_bytes": psutil.virtual_memory().total},
                "limitations": ["Application logical snapshot, not physical backup, PITR or roles",
                                "Synthetic rehearsal, not measured production RPO or RTO"]}
        finally:
            restored.engine.dispose()
    finally:
        store.engine.dispose()
        if restore_report and restore_report["postgres_schema"]:
            engine = create_engine(restore_url)
            try:
                with engine.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA "{restore_report["postgres_schema"]}" CASCADE'))
            finally:
                engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--local-postgres", action="store_true", help="Opt into local private PostgreSQL configuration")
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new report filename")
    workdir = ROOT / "artifacts/operations" / uuid4().hex
    admin = None
    schema = None
    if args.postgres or args.local_postgres:
        base = postgres_test_base(local=args.local_postgres)
        admin = create_engine(base)
        schema = "backup_source_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        source = base.update_query_dict({"options": "-csearch_path=" + schema})
    else:
        source = URL.create("sqlite", database=str(workdir / "source.db"))
        base = URL.create("sqlite", database=str(workdir / "restored.db"))
    try:
        report = exercise(source, base, workdir, records=args.records)
        report["disposable_postgres_schemas_cleaned"] = bool(admin)
    finally:
        if admin and schema:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"passed": all(report["checks"].values()), "backend": report["backend"],
                      "records": args.records, "seconds": report["total_seconds"]}))


if __name__ == "__main__":
    main()
