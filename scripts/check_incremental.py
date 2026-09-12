"""Real records with explicitly synthetic updates; measure frozen-score refresh and full equivalence."""
import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import pyarrow.parquet as pq

from entitybridge.candidates import generate_candidates
from entitybridge.incremental import Snapshot, recompute, refresh_scores
from entitybridge.matching import SplinkMatcher
from entitybridge.resolution import Edge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = SplinkMatcher.load(args.model)
    old_records = [row for split in ("train", "validation", "test")
                   for row in pq.read_table(args.dataset / "matcher" / f"{split}.parquet").to_pylist()]
    initial_scores = model.score(old_records, generate_candidates(old_records))
    # Select a real candidate endpoint so the replay exercises actual rescoring.
    anchor = initial_scores[0]["left"]
    anchor_index = next(i for i, row in enumerate(old_records) if row["record_id"] == anchor)
    def state(records, edges):
        return Snapshot(frozenset(r["record_id"] for r in records),
                        tuple(Edge(e["left"], e["right"], e["score"]) for e in edges), policy_version=model.fingerprint)
    old = state(old_records, initial_scores)
    resolved = old.solve()
    results = []
    for scenario in ("rename", "delete", "insert"):
        updated = [dict(record) for record in old_records]
        if scenario == "rename":
            updated[anchor_index] = updated[anchor_index] | {
                "name": updated[anchor_index]["name"] + " SYNTHETIC REPLAY", "record_version_id": str(uuid4())}
        elif scenario == "delete":
            updated.pop(anchor_index)
        else:
            updated.append(updated[anchor_index] | {"record_id": str(uuid4()), "record_version_id": str(uuid4())})
        started = time.perf_counter()
        refreshed, count = refresh_scores(old_records, updated, initial_scores, model.score)
        actual_predictions = model.last_prediction_count
        assert actual_predictions == count["rescored_pairs"]
        refreshed_state = state(updated, refreshed)
        incremental = recompute(old, refreshed_state, resolved, changed_records=count["changed_records"], max_records=2000)
        elapsed_incremental = time.perf_counter() - started
        started = time.perf_counter()
        full_scores = model.score(updated, generate_candidates(updated))
        full = state(updated, full_scores).solve()
        elapsed_full = time.perf_counter() - started
        projection = lambda edges: {(e["left"], e["right"]): e["score"] for e in edges}
        a, b = projection(refreshed), projection(full_scores)
        assert a.keys() == b.keys() and all(abs(a[key] - b[key]) < 1e-12 for key in a)
        assert incremental.resolution.partitions == full.partitions
        results.append({"scenario": scenario, **count, "records": len(updated), "full_pairs": len(full_scores),
            "affected_records": len(incremental.affected_records), "mode": incremental.mode, "reason": incremental.reason,
            "actual_rescored_predictions": actual_predictions,
            "incremental_seconds": elapsed_incremental, "full_seconds": elapsed_full,
            "scores_equal_atol_1e-12": True, "partitions_equal": True})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"kind": "C_synthetic_changes_to_real_records", "model": model.fingerprint,
        "results": results}, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
