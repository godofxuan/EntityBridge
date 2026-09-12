"""Fetch two small author-hosted benchmarks; retain raw files locally only."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from entitybridge.benchmarks import (
    CATALOG,
    DATASET_DOCUMENTATION,
    FILES,
    convert_benchmark,
    file_hash,
    source_url,
)


def fetch_file(client, dataset, filename, directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    url, expected = source_url(dataset, filename), CATALOG[dataset]["source_sha256"][filename]
    if path.exists():
        if file_hash(path) != expected:
            raise ValueError(f"Existing benchmark source differs from pinned SHA-256: {path.name}")
    else:
        partial = path.with_suffix(".csv.partial")
        try:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                if int(response.headers.get("content-length", 0)) > 5 * 1024**2:
                    raise ValueError("Benchmark download exceeds 5 MiB file budget")
                count = 0
                with partial.open("wb") as target:
                    for chunk in response.iter_bytes(64 * 1024):
                        count += len(chunk)
                        if count > 5 * 1024**2:
                            raise ValueError("Benchmark download exceeds 5 MiB file budget")
                        target.write(chunk)
            if file_hash(partial) != expected:
                raise ValueError("Author-hosted benchmark changed: review and pin a new version explicitly")
            partial.replace(path)
        finally:
            if partial.exists():
                partial.unlink()
    receipt = path.with_suffix(".csv.download.json")
    if not receipt.exists():
        receipt.write_text(json.dumps({"url": url, "sha256": expected, "bytes": path.stat().st_size,
            "verified_at": datetime.now(UTC).isoformat(), "documentation_url": DATASET_DOCUMENTATION,
            "dataset_license": "not specified on official dataset page; not covered by code-license assumption",
            "redistributed": False}, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("all", *CATALOG), default="all")
    parser.add_argument("--raw-root", type=Path, default=Path("artifacts/raw/benchmarks"))
    parser.add_argument("--output-root", type=Path, default=Path("artifacts/benchmarks/v1"))
    parser.add_argument("--seed", type=int, default=20260912)
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    datasets = tuple(CATALOG) if args.dataset == "all" else (args.dataset,)
    with httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=True,
                      headers={"User-Agent": "EntityBridge-public-benchmark-research/0.3"}) as client:
        for dataset in datasets:
            for filename in FILES:
                fetch_file(client, dataset, filename, args.raw_root / dataset)
            if not args.download_only:
                result = convert_benchmark(args.raw_root / dataset, args.output_root / dataset, dataset, seed=args.seed)
                print(json.dumps({"dataset": dataset, "output": str(args.output_root / dataset),
                                  "statistics": result["statistics"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
