"""Run 10k real-source API records through an isolated PostgreSQL workflow.

This is a service integration/throughput probe, not a matching accuracy evaluation.
Only SourceRow fields cross the input boundary. The evaluator truth map is never read.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import platform
import secrets
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psutil
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from entitybridge.api import SourceRow, create_app
from entitybridge.matching import SplinkMatcher
from entitybridge.normalization import MATCHER_COLUMNS, matcher_view
from entitybridge.store import Store


def file_hash(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def project_sources(input_path):
    """Project a read-only benchmark extraction into the declared API contract."""
    sources, discarded = defaultdict(list), Counter()
    allowed = set(SourceRow.model_fields)
    if allowed.intersection({"true_entity_id", "split", "record_id", "record_version_id"}):
        raise AssertionError("Service input contract unexpectedly accepts evaluator fields")
    with Path(input_path).open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            raw = json.loads(line)
            source = raw.get("source")
            if source not in {"gleif", "companies_house"}:
                raise ValueError(f"Unexpected source at row {line_number}")
            selected = {key: value for key, value in raw.items() if key in allowed}
            validated = SourceRow.model_validate(selected).model_dump(exclude_none=True)
            assert set(validated) <= allowed
            sources[source].append(validated)
            discarded.update(set(raw) - allowed - {"source"})
    if {source: len(rows) for source, rows in sources.items()} != {"gleif": 5000, "companies_house": 5000}:
        raise ValueError("This probe requires exactly 5,000 rows from each declared real source")
    for source, rows in sources.items():
        if len({row["source_key"] for row in rows}) != len(rows):
            raise ValueError(f"Duplicate source keys in {source}")
    return dict(sources), dict(discarded)


def require_response(response, status=200):
    if response.status_code != status:
        # Keep source values and connection information out of diagnostic output.
        raise AssertionError(f"API {response.request.method} {response.request.url.path}: "
                             f"expected HTTP {status}, got {response.status_code}")
    return response


def run_workflow(input_path, model_path, report_path):
    started = time.perf_counter()
    run_id = uuid4().hex
    run_root = ROOT / "artifacts" / "reports" / "real_workflow_10k_runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    report = {"run_id": run_id, "started_at": datetime.now(UTC).isoformat(), "status": "running",
              "scope": "10k service integration; no truth-derived judgments, accuracy or gain claims",
              "transport": "FastAPI TestClient in-process ASGI; actual PostgreSQL and Splink execution",
              "timings_seconds": {}, "checks": {}, "schema_cleaned": False}
    timings, checks = report["timings_seconds"], report["checks"]
    credentials = json.loads((ROOT / ".tools" / "database.json").read_text(encoding="utf-8"))
    url = URL.create("postgresql+psycopg", username=credentials["user"], password=credentials["password"],
        host=credentials["host"], port=credentials["port"], database="entitybridge_test",
        query={"connect_timeout": "8"})
    schema_name = "real_workflow_" + run_id
    admin = create_engine(url)
    store = None
    schema_created = False
    try:
        step = time.perf_counter()
        sources, discarded = project_sources(input_path)
        timings["load_validate_and_project"] = time.perf_counter() - step
        model_before = {path.name: file_hash(path) for path in sorted(model_path.iterdir()) if path.is_file()}
        model = SplinkMatcher.load(model_path)
        report["input"] = {"path": str(input_path.relative_to(ROOT)), "sha256": file_hash(input_path),
            "bytes": input_path.stat().st_size, "records": sum(map(len, sources.values())),
            "source_counts": {source: len(rows) for source, rows in sources.items()},
            "allowed_source_fields": sorted(SourceRow.model_fields), "discarded_field_counts": discarded}
        report["model"] = {"path": str(model_path.relative_to(ROOT)), "fingerprint": model.fingerprint,
            "file_sha256": model_before, "tf_policy": model.training_metadata["tf_policy"],
            "calibrated": model.training_metadata["calibrated"],
            "unobserved_levels_using_splink_defaults": model.training_metadata.get(
                "unobserved_levels_using_splink_defaults", [])}
        del model
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
            report["postgres_version"] = connection.execute(text("SHOW server_version")).scalar_one()
        scoped = url.update_query_dict({"options": f"-csearch_path={schema_name}"})
        store = Store(scoped, run_root / "revisions")
        with store.engine.begin() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "migrations"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        store.initialize()
        token = secrets.token_urlsafe(24)
        app = create_app(store, tokens={token: ("integration-probe", "admin")}, model_path=model_path)
        report["imports"] = []
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer " + token}
            client.headers.update(headers)
            assert require_response(client.get("/entities")).json() == []
            import_started = time.perf_counter()
            for source in ("gleif", "companies_house"):
                source_started = time.perf_counter()
                for offset in range(0, len(sources[source]), 1000):
                    rows = sources[source][offset:offset + 1000]
                    payload = {"source": source, "rows": rows}
                    payload_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                    batch_started = time.perf_counter()
                    imported = require_response(client.post("/imports", json=payload, headers=headers)).json()
                    report["imports"].append({"source": source, "rows": len(rows),
                        "elapsed_seconds": time.perf_counter() - batch_started, "request_bytes": payload_size,
                        "snapshot_id": imported["snapshot_id"], "content_sha256": imported["content_sha256"]})
                timings["import_" + source] = time.perf_counter() - source_started
                print(json.dumps({"stage": "imported", "source": source, "rows": len(sources[source])}), flush=True)
            timings["import_total"] = time.perf_counter() - import_started
            raw = store.active_records()
            assert len(raw) == 10000
            assert all(set(row) <= set(SourceRow.model_fields) | {"record_id", "record_version_id", "source"}
                       for row in raw.values())
            assert all(set(matcher_view(row, key, row["record_version_id"])) == set(MATCHER_COLUMNS)
                       for key, row in raw.items())
            checks["evaluator_fields_absent_from_service_and_matcher"] = True
            by_version = {row["record_version_id"]: row for row in raw.values()}
            step = time.perf_counter()
            candidate = require_response(client.post("/match-runs", headers=headers,
                json={"method": "splink", "threshold": .9, "review_threshold": .5,
                      "incremental": True, "verify_full": True})).json()
            timings["matching_and_preparation"] = time.perf_counter() - step
            report["matching"] = candidate
            revision = candidate["revision_id"]
            assert candidate["records"] == 10000 and store.current_revision() is None
            assert require_response(client.get("/entities")).json() == []
            require_response(client.get("/entities", params={"revision": revision}), 404)
            require_response(client.get("/exports", params={"revision": revision}), 404)
            checks["prepared_revision_hidden_from_queries_and_export"] = True
            step = time.perf_counter()
            require_response(client.post(f"/revisions/{revision}/publish", headers=headers,
                                         json={"expected_parent": None}))
            timings["publish"] = time.perf_counter() - step
            assert store.current_revision() == revision
            checks["publication_pointer_matches_complete_revision"] = True
            step = time.perf_counter()
            entities, response_bytes = [], 0
            while True:
                response = require_response(client.get("/entities", params={
                    "revision": revision, "offset": len(entities), "limit": 1000}))
                page = response.json()
                entities.extend(page)
                response_bytes += len(response.content)
                if len(entities) >= int(response.headers["X-Total-Count"]):
                    break
                assert page, "Pagination stopped before the declared total"
            timings["query_all_entities"] = time.perf_counter() - step
            report["query_response_bytes"] = response_bytes
            members = [member for entity in entities for member in entity["members"]]
            assert len(members) == len(set(members)) == 10000 and set(members) == set(raw)
            canonical_count = 0
            for entity in entities:
                for field, evidence in entity["canonical"].items():
                    source_row = by_version[evidence["record_version_id"]]
                    assert source_row["record_id"] in entity["members"]
                    assert source_row[field] == evidence["value"] and source_row["source"] == evidence["source"]
                    assert evidence["rule"] == "companies-house-first-then-stable-id-v1"
                    canonical_count += 1
            checks["canonical_values_reference_actual_member_source_versions"] = True
            report["canonical_fields_checked"] = canonical_count
            report["result_entity_count_not_truth_count"] = len(entities)
            chosen = entities[0]
            step = time.perf_counter()
            detail = require_response(client.get(f"/entities/{chosen['entity_id']}",
                                                params={"revision": revision})).json()
            timings["query_one_entity_at_fixed_revision"] = time.perf_counter() - step
            assert detail["revision_id"] == revision and detail["status"] == "historical"
            step = time.perf_counter()
            exported = require_response(client.get("/exports", params={"revision": revision}))
            timings["export_fixed_revision"] = time.perf_counter() - step
            export_rows = list(csv.DictReader(io.StringIO(exported.text)))
            assert len(export_rows) == 10000
            assert {row["revision_id"] for row in export_rows} == {revision}
            assert {row["record_id"] for row in export_rows} == set(raw)
            assert all(row["record_version_id"] == raw[row["record_id"]]["record_version_id"] for row in export_rows)
            assert all(row["canonical_name_source_version"] in by_version for row in export_rows)
            checks["fixed_revision_csv_contains_every_record_once"] = True
            (run_root / "entities.csv").write_bytes(exported.content)
            report["export"] = {"rows": len(export_rows), "bytes": len(exported.content),
                "sha256": hashlib.sha256(exported.content).hexdigest(),
                "path": str((run_root / "entities.csv").relative_to(ROOT))}
            assert store.decision_history() == []
            checks["no_human_or_truth_derived_review_decisions"] = True
        with store.engine.connect() as connection:
            report["postgres_schema_bytes_before_cleanup"] = connection.execute(text(
                "SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :schema AND c.relkind = 'r'"),
                {"schema": schema_name}).scalar_one()
        revision_file = store.artifact_root / f"{revision}.json"
        report["revision_artifact"] = {"bytes": revision_file.stat().st_size,
            "sha256": file_hash(revision_file), "path": str(revision_file.relative_to(ROOT))}
        assert model_before == {path.name: file_hash(path) for path in sorted(model_path.iterdir()) if path.is_file()}
        assert file_hash(input_path) == report["input"]["sha256"]
        checks["input_and_frozen_model_files_unchanged"] = True
        report["status"] = "passed"
    finally:
        if store:
            store.engine.dispose()
        if schema_created:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
                report["schema_cleaned"] = not connection.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :schema)"),
                    {"schema": schema_name}).scalar_one()
        admin.dispose()
        memory = psutil.Process().memory_info()
        report["process_memory_bytes"] = {"rss_at_end": memory.rss,
                                          "peak_working_set_if_available": getattr(memory, "peak_wset", None)}
        report["hardware"] = {"platform": platform.platform(), "logical_cpus": psutil.cpu_count(),
                              "ram_bytes": psutil.virtual_memory().total}
        report["script_sha256"] = file_hash(__file__)
        timings["total_including_setup_validation_and_cleanup"] = time.perf_counter() - started
        report["finished_at"] = datetime.now(UTC).isoformat()
        if report["status"] != "passed":
            report["status"] = "failed"
        encoded = json.dumps(report, ensure_ascii=False, indent=2, default=int) + "\n"
        (run_root / "report.json").write_text(encoded, encoding="utf-8")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(encoded, encoding="utf-8")
    print(json.dumps({"status": report["status"], "records": report["input"]["records"],
        "candidate_pairs": report["matching"]["candidate_pairs"], "timings_seconds": timings,
        "schema_cleaned": report["schema_cleaned"], "report": str(report_path.relative_to(ROOT))}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path,
        default=ROOT / "artifacts/datasets/v3/real_10000/evaluator/rich_records.jsonl")
    parser.add_argument("--model", type=Path, default=ROOT / "artifacts/reports/baseline_100k_v2/model")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/reports/real_workflow_10k.json")
    args = parser.parse_args()
    run_workflow(args.input.resolve(), args.model.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
