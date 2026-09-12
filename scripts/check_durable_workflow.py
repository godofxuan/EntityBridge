"""Real 10k API submission -> separate CLI worker -> explicit publication.

API calls use in-process ASGI TestClient; matching runs in an actual independent
CLI process against PostgreSQL. This is integration evidence, not accuracy or SLA.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import secrets
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psutil
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from entitybridge import schema as s
from entitybridge.api import SourceRow, create_app
from entitybridge.database import CURRENT_SCHEMA
from entitybridge.normalization import MATCHER_COLUMNS, matcher_view
from entitybridge.store import Store
from entitybridge.worker import pipeline_fingerprint

SUBMITTED_FIELDS = frozenset({"source_key", "name", "address", "city", "postcode", "country"})


def file_hash(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def project_sources(path):
    sources, discarded = defaultdict(list), Counter()
    assert SUBMITTED_FIELDS <= set(SourceRow.model_fields)
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            raw = json.loads(line)
            source_name = raw["source"]
            if source_name not in {"gleif", "companies_house"}:
                raise ValueError("Unexpected real input source")
            selected = {key: raw[key] for key in SUBMITTED_FIELDS if key in raw and raw[key] is not None}
            SourceRow.model_validate(selected)
            # Dropping business identifiers must not undo text-redaction policy.
            expected = matcher_view(raw, "projection_record", "projection_version")
            actual = matcher_view({**selected, "source": source_name}, "projection_record", "projection_version")
            if actual != expected:
                raise ValueError("Explicit source projection would change the masked matcher fields")
            sources[source_name].append(selected)
            discarded.update(set(raw) - SUBMITTED_FIELDS - {"source"})
    if {name: len(rows) for name, rows in sources.items()} != {"gleif": 5000, "companies_house": 5000}:
        raise ValueError("Expected exactly 5,000 records per real source")
    if any(len({row["source_key"] for row in rows}) != len(rows) for rows in sources.values()):
        raise ValueError("Source keys must be unique within each source")
    return dict(sources), dict(discarded)


def require(response, code=200):
    if response.status_code != code:
        raise AssertionError(f"Expected HTTP {code}, received {response.status_code}; response text suppressed")
    return response


def stop_worker(process):
    if process is None or process.poll() is not None:
        return
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=5)
    for child in alive:
        child.kill()
    psutil.wait_procs(alive, timeout=5)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_workflow(input_path, model_path, report_path, *, local_postgres=False):
    from check_backup_restore import postgres_test_base

    input_path, model_path, report_path = Path(input_path).resolve(), Path(model_path).resolve(), Path(report_path).resolve()
    if report_path.exists():
        raise ValueError("Use a fresh durable-workflow report filename")
    source_url = postgres_test_base(local=local_postgres)
    run_id = uuid4().hex
    workdir = ROOT / "artifacts/durable_workflow_runs" / run_id
    workdir.mkdir(parents=True, exist_ok=False)
    schema = "durable_workflow_" + run_id
    admin = create_engine(source_url)
    store, process, schema_created = None, None, False
    started = time.perf_counter()
    stage = "initialization"
    report = {"format": "entitybridge-durable-real-workflow-v1", "status": "running",
        "started_at": datetime.now(UTC).isoformat(), "schema_version": CURRENT_SCHEMA,
        "transport": "in-process ASGI API plus independent CLI worker and actual PostgreSQL",
        "scope": "real-source service integration; no truth labels, accuracy estimate or SLA",
        "timings_seconds": {}, "checks": {}, "schema_cleaned": False}
    timings, checks = report["timings_seconds"], report["checks"]
    drains = []
    output_sizes = {"stdout": 0, "stderr": 0}
    try:
        stage = "input_projection"
        begin = time.perf_counter()
        sources, discarded = project_sources(input_path)
        timings[stage] = time.perf_counter() - begin
        input_hash = file_hash(input_path)
        model_hashes = {path.name: file_hash(path) for path in model_path.iterdir() if path.is_file()}
        pipeline_hash = pipeline_fingerprint()
        report["input"] = {"sha256": input_hash, "bytes": input_path.stat().st_size, "records": 10000,
                           "source_counts": {name: len(rows) for name, rows in sources.items()},
                           "submitted_fields": sorted(SUBMITTED_FIELDS), "discarded_field_counts": discarded,
                           "matcher_columns": list(MATCHER_COLUMNS)}
        report["model_file_sha256"] = model_hashes
        report["pipeline_sha256"] = pipeline_hash
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
            report["postgres_version"] = connection.execute(text("SHOW server_version")).scalar_one()
        scoped = source_url.update_query_dict({"options": "-csearch_path=" + schema})
        store = Store(scoped, workdir / "revisions")
        store.initialize()
        token = secrets.token_urlsafe(32)
        app = create_app(store, tokens={token: ["durable_integration_probe", "admin"]}, model_path=model_path)
        with TestClient(app) as client:
            require(client.get("/jobs"), 401)
            client.headers["Authorization"] = "Bearer " + token
            stage = "import"
            begin = time.perf_counter()
            imported_rows = 0
            for source in ("gleif", "companies_house"):
                for offset in range(0, len(sources[source]), 1000):
                    batch = sources[source][offset:offset + 1000]
                    response = require(client.post("/imports", json={"source": source, "rows": batch})).json()
                    assert len(response["records"]) == len(batch)
                    imported_rows += len(batch)
            timings[stage] = time.perf_counter() - begin
            raw = store.active_records()
            assert imported_rows == len(raw) == 10000
            assert all(set(matcher_view(row, key, row["record_version_id"])) == set(MATCHER_COLUMNS)
                       for key, row in raw.items())
            checks["source_counts_and_matcher_boundary"] = True
            checks["source_projection_preserves_masked_feature_values"] = True
            stage = "enqueue"
            begin = time.perf_counter()
            submitted = require(client.post("/jobs", json={"idempotency_key": "durable-probe-" + run_id,
                "max_attempts": 3, "settings": {"method": "splink", "threshold": 0.9,
                "review_threshold": 0.5, "incremental": True, "verify_full": True}}), 202).json()
            timings[stage] = time.perf_counter() - begin
            job_id = submitted["job_id"]
            assert submitted["status"] == "queued" and submitted["result_revision"] is None
            assert submitted["payload"]["models"]["pipeline_sha256"] == pipeline_hash
            assert store.current_revision() is None and require(client.get("/entities")).json() == []
            checks["api_returns_202_queued_without_publication"] = True
            report["model_fingerprint"] = submitted["payload"]["models"]["model"]
            report["job_payload_sha256"] = submitted["payload_hash"]
            report["source_basis_sha256"] = submitted["source_hash"]
            stage = "cli_worker"
            begin = time.perf_counter()
            environment = os.environ.copy()
            environment["DATABASE_URL"] = scoped.render_as_string(hide_password=False)
            environment["PYTHONPATH"] = str(ROOT / "src")
            environment["PYTHONUTF8"] = "1"
            process = subprocess.Popen([sys.executable, "-m", "entitybridge.cli", "worker", "--once",
                "--lease-seconds", "120", "--artifact-root", str(workdir / "revisions"), "--model", str(model_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            def discard_output(stream, name):
                while chunk := stream.read(8192):
                    output_sizes[name] += len(chunk)
                stream.close()
            for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
                thread = threading.Thread(target=discard_output, args=(stream, name), daemon=True)
                thread.start()
                drains.append(thread)
            observed_states = {"queued"}
            deadline = time.monotonic() + 240
            while True:
                job = require(client.get(f"/jobs/{job_id}")).json()
                observed_states.add(job["status"])
                if job["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("CLI worker stopped or timed out before terminal task status")
                time.sleep(0.1)
            process.wait(timeout=15)
            for thread in drains:
                thread.join(timeout=5)
            assert process.returncode == 0 and job["status"] == "succeeded"
            assert job["attempt"] == 1 and job["lease_owner"] is None and job["lease_until"] is None
            assert {"SUBMIT", "CLAIM", "SUCCEED"} <= {event["action"] for event in job["events"]}
            timings[stage] = time.perf_counter() - begin
            revision = job["result_revision"]
            assert revision is not None and store.current_revision() is None
            require(client.get("/entities", params={"revision": revision}), 404)
            require(client.get("/exports", params={"revision": revision}), 404)
            assert require(client.get("/entities")).json() == []
            with store.engine.connect() as connection:
                row = connection.execute(select(s.revisions).where(s.revisions.c.revision_id == revision)).mappings().one()
                run = connection.execute(select(s.runs).where(s.runs.c.run_id == revision)).mappings().one()
                assert row["status"] == "prepared" and row["input_hash"] == submitted["source_hash"]
            payload = store._payload(revision, published_only=False)
            report["matching"] = {"records": len(payload["records"]), "candidate_pairs": len(payload["edges"]),
                "result_entity_count_not_truth": len(payload["entities"]), "policy_sha256": row["policy_version"],
                "artifact_sha256": row["artifact_sha256"], "artifact_bytes": (store.artifact_root / row["artifact_path"]).stat().st_size,
                "manifest_candidate_pairs": run["manifest"]["edges"]}
            assert report["matching"]["records"] == 10000
            assert report["matching"]["candidate_pairs"] == report["matching"]["manifest_candidate_pairs"]
            report["worker"] = {"lease_seconds": 120, "attempts": job["attempt"], "exit_code": process.returncode,
                                "observed_states": sorted(observed_states), "event_actions": [event["action"] for event in job["events"]],
                                "output_bytes_discarded": dict(output_sizes)}
            checks["independent_cli_worker_succeeded_once"] = True
            checks["succeeded_prepared_result_hidden_until_publish"] = True
            stage = "publish"
            begin = time.perf_counter()
            require(client.post(f"/revisions/{revision}/publish", json={"expected_parent": None}))
            assert store.current_revision() == revision
            timings[stage] = time.perf_counter() - begin
            checks["explicit_publication_moves_pointer"] = True
            stage = "fixed_revision_queries"
            begin = time.perf_counter()
            entities = []
            while True:
                response = require(client.get("/entities", params={"revision": revision, "offset": len(entities), "limit": 1000}))
                page = response.json()
                assert response.headers["X-Revision-Id"] == revision
                entities.extend(page)
                if len(entities) >= int(response.headers["X-Total-Count"]):
                    break
                assert page
            members = [member for entity in entities for member in entity["members"]]
            assert len(members) == len(set(members)) == 10000 and set(members) == set(raw)
            by_version = {row["record_version_id"]: row for row in raw.values()}
            canonical_fields = 0
            for entity in entities:
                for field, evidence in entity["canonical"].items():
                    original = by_version[evidence["record_version_id"]]
                    assert original["record_id"] in entity["members"]
                    assert original[field] == evidence["value"] and original["source"] == evidence["source"]
                    canonical_fields += 1
            detail = require(client.get(f"/entities/{entities[0]['entity_id']}", params={"revision": revision})).json()
            assert detail["revision_id"] == revision and detail["status"] == "historical"
            assert {row["record_id"] for row in detail["records"]} == set(entities[0]["members"])
            assert store.projection_status(revision, verify=True)["ready"]
            timings[stage] = time.perf_counter() - begin
            checks["fixed_revision_directory_detail_and_projection"] = True
            checks["canonical_source_versions_verified"] = True
            report["canonical_fields_checked"] = canonical_fields
            stage = "export"
            begin = time.perf_counter()
            exported = require(client.get("/exports", params={"revision": revision}))
            rows = list(csv.DictReader(io.StringIO(exported.text)))
            assert len(rows) == 10000 and {row["record_id"] for row in rows} == set(raw)
            assert {row["revision_id"] for row in rows} == {revision}
            assert all(row["record_version_id"] == raw[row["record_id"]]["record_version_id"] for row in rows)
            report["export"] = {"rows": len(rows), "bytes": len(exported.content), "sha256": hashlib.sha256(exported.content).hexdigest()}
            timings[stage] = time.perf_counter() - begin
            checks["export_contains_each_source_record_version_once"] = True
            assert store.decision_history() == []
            assert file_hash(input_path) == input_hash
            assert {path.name: file_hash(path) for path in model_path.iterdir() if path.is_file()} == model_hashes
            assert pipeline_fingerprint() == pipeline_hash
            checks["input_model_pipeline_unchanged_no_invented_review_labels"] = True
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = {"stage": stage, "type": type(error).__name__, "details_suppressed": True}
        raise
    finally:
        stop_worker(process)
        for thread in drains:
            thread.join(timeout=5)
        if store:
            store.engine.dispose()
        if schema_created:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                report["schema_cleaned"] = not connection.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema)"), {"schema": schema}).scalar_one()
        admin.dispose()
        report["timings_seconds"]["total_including_cleanup"] = time.perf_counter() - started
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["script_sha256"] = file_hash(__file__)
        report["hardware"] = {"platform": platform.platform(), "logical_cpus": psutil.cpu_count(),
                              "memory_bytes": psutil.virtual_memory().total}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "artifacts/datasets/v3/real_10000/evaluator/rich_records.jsonl")
    parser.add_argument("--model", type=Path, default=ROOT / "artifacts/reports/baseline_100k_v2/model")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/reports/durable_workflow_v04.json")
    parser.add_argument("--local-postgres", action="store_true")
    args = parser.parse_args()
    try:
        report = run_workflow(args.input, args.model, args.output, local_postgres=args.local_postgres)
    except Exception as error:  # noqa: BLE001 - CLI boundary suppresses source and credential details
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "details_suppressed": True}))
        raise SystemExit(1) from None
    print(json.dumps({"status": report["status"], "records": report["input"]["records"],
                      "candidates": report["matching"]["candidate_pairs"], "schema_cleaned": report["schema_cleaned"],
                      "seconds": report["timings_seconds"]["total_including_cleanup"]}))


if __name__ == "__main__":
    main()
