"""Paired query benchmark on one frozen synthetic revision; never evaluates matching quality."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest.mock import patch

from entitybridge import schema as s
from entitybridge.store import Store, digest


def measure(function, artifact_root, repeats):
    original = Path.read_bytes
    times, reads, sizes = [], [], []
    for _ in range(repeats):
        count, size = 0, 0
        def counted(path):
            nonlocal count, size
            content = original(path)
            if path.parent == artifact_root and path.suffix == ".json":
                count += 1
                size += len(content)
            return content
        with patch.object(Path, "read_bytes", counted):
            started = time.perf_counter()
            result = function()
            times.append((time.perf_counter() - started) * 1000)
        reads.append(count)
        sizes.append(size)
    return {"p50_ms": statistics.median(times), "p95_ms": sorted(times)[math.ceil(.95 * len(times)) - 1],
            "samples_ms": times, "artifact_reads": sum(reads), "artifact_bytes_read": sum(sizes)}, result


def legacy_page(store, query, revision, offset, limit):
    # Exact v0.2 behavior, retained for a paired reference after implementation.
    payload = store._payload(revision)
    rows = [entity for entity in payload["entities"].values() if not query or any(
        query.casefold() in str(value).casefold() for member in entity["members"]
        for value in (payload["records"][member].get("name", ""), *payload["records"][member].get("aliases", [])))]
    return {"revision_id": revision, "total": len(rows), "offset": offset, "limit": limit,
            "items": rows[offset:offset + limit]}


def paired_measure(before, after, artifact_root, repeats):
    summaries = {"legacy": [], "projection": []}
    expected = actual = None
    for repeat in range(repeats):
        functions = [("legacy", before), ("projection", after)]
        if repeat % 2:
            functions.reverse()
        for name, function in functions:
            measurement, result = measure(function, artifact_root, 1)
            summaries[name].append(measurement)
            if name == "legacy":
                expected = result
            else:
                actual = result
        assert actual == expected
    result = {}
    for name, samples in summaries.items():
        times = [sample["p50_ms"] for sample in samples]
        result[name] = {"p50_ms": statistics.median(times),
            "p95_ms": sorted(times)[math.ceil(.95 * repeats) - 1], "samples_ms": times,
            "artifact_reads": sum(sample["artifact_reads"] for sample in samples),
            "artifact_bytes_read": sum(sample["artifact_bytes_read"] for sample in samples)}
    return result, expected


def prepare(workdir, record_count):
    store = Store(f"sqlite:///{(workdir / 'state.sqlite').as_posix()}", workdir / "revisions")
    store.initialize()
    if store.current_revision():
        return store
    if record_count < 2 or record_count % 2:
        raise ValueError("An even record count >= 2 is required")
    records = []
    for source in ("synthetic_registry", "synthetic_contract"):
        records.append(store.import_records(source, [{
            "source_key": f"{index:08d}", "name": f"SYNTHETIC COMPANY {index:05d} LIMITED",
            "aliases": [f"FORMER SYNTHETIC {index:05d} INC"], "address": f"{index + 1} EXAMPLE ROAD",
            "city": "EXAMPLE CITY", "postcode": f"ZZ{index % 100:02d} {index % 1000:03d}", "country": "GB",
        } for index in range(record_count // 2)])["records"])
    edges = [{"left": left["record_id"], "right": right["record_id"],
              "left_version": left["record_version_id"], "right_version": right["record_version_id"], "score": 1.0,
              "evidence": {"kind": "synthetic_query_performance_fixture_not_matching_quality"}}
             for left, right in zip(*records)]
    candidate = store.prepare_revision(edges, policy_version="synthetic-query-benchmark-v1", force_full=True)
    store.publish(candidate["revision_id"], expected_parent=None)
    return store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--records", type=int, default=10000)
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--stage", choices=("baseline", "compare"), default="baseline")
    args = parser.parse_args()
    if args.repeats < 2:
        raise ValueError("Use at least two repeats for the paired comparison")
    args.workdir.mkdir(parents=True, exist_ok=True)
    output = args.workdir / f"{args.stage}.json"
    if output.exists():
        raise ValueError("Report already exists; preserve prior evidence and choose a new directory")
    store = prepare(args.workdir, args.records)
    if len(store.active_records()) != args.records:
        raise ValueError("Existing fixture record count differs from --records; choose another directory")
    revision = store.current_revision()
    path = store.artifact_root / f"{revision}.json"
    cases = [("first_page", "", 0, 100), ("deep_page", "", args.records // 4, 100),
             ("alias_contains", "former synthetic 012", 0, 100), ("no_match", "DOES NOT EXIST", 0, 100)]
    report = {"stage": args.stage, "engine": "sqlite", "synthetic": True, "revision_id": revision,
        "created_at": datetime.now(UTC).isoformat(),
        "records": args.records, "artifact_bytes": path.stat().st_size,
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "repeats": args.repeats,
        "timing_scope": "warm process/OS cache; logical file bytes counted, not physical disk I/O; p95 is nearest-rank",
        "cases": {}}
    if args.stage == "compare":
        report["projection_before_first_query"] = store.projection_status(revision)
        report["first_query_including_legacy_backfill"], _ = measure(
            lambda: store.entities_page(revision=revision), store.artifact_root, 1)
    for name, query, offset, limit in cases:
        before_query = partial(legacy_page, store, query, revision, offset, limit)
        if args.stage == "compare":
            measurements, expected = paired_measure(before_query,
                partial(store.entities_page, query, revision=revision, offset=offset, limit=limit),
                store.artifact_root, args.repeats)
            statement = store._entity_select(revision, query).order_by(s.query_entities.c.entity_id).offset(offset).limit(limit)
            with store.engine.connect() as connection:
                plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + str(statement.compile(
                    dialect=connection.dialect, compile_kwargs={"literal_binds": True}))).all()
            result = {**measurements, "equivalent": True, "query_plan": [list(row) for row in plan]}
        else:
            before, expected = measure(before_query, store.artifact_root, args.repeats)
            result = {"legacy": before}
        result.update(query=query, offset=offset, limit=limit, total=expected["total"], result_sha256=digest(expected))
        report["cases"][name] = result
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), "artifact_bytes": report["artifact_bytes"],
        "cases": {name: {"legacy_p50_ms": value["legacy"]["p50_ms"], "legacy_p95_ms": value["legacy"]["p95_ms"],
                          **({"projection_p50_ms": value["projection"]["p50_ms"]} if "projection" in value else {})}
                  for name, value in report["cases"].items()}}, ensure_ascii=True))
    store.engine.dispose()


if __name__ == "__main__":
    main()
