"""Bounded official-source downloader. Run from repository root; output is local only."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import httpx

from entitybridge.ingestion import CH_TERMS, GLEIF_TERMS, PARSER_VERSION, file_sha256


def download(client, url, path, *, max_bytes, terms_url, published_at=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    if path.exists():
        if not manifest_path.exists():
            raise ValueError(f"Existing file has no manifest: {path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_url"] != url or file_sha256(path) != manifest["sha256"]:
            raise ValueError(f"Existing snapshot mismatch: {path}")
        return manifest
    partial = path.with_suffix(path.suffix + ".partial")
    for attempt in range(4):
        try:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                length = int(response.headers.get("content-length", 0))
                if length > max_bytes:
                    raise ValueError("Download exceeds byte budget")
                count = 0
                with partial.open("wb") as target:
                    for chunk in response.iter_bytes(1024 * 1024):
                        count += len(chunk)
                        if count > max_bytes:
                            raise ValueError("Download exceeds byte budget")
                        target.write(chunk)
                if length and not response.headers.get("content-encoding") and count != length:
                    raise ValueError("Incomplete response")
            break
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    manifest = {
        "source_url": url, "terms_url": terms_url,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "source_published_at": published_at, "bytes": partial.stat().st_size,
        "sha256": file_sha256(partial), "parser_version": PARSER_VERSION,
        "schema_version": "official-raw-v1", "last_modified": response.headers.get("last-modified"),
    }
    partial.replace(path)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=Path("artifacts/raw"))
    parser.add_argument("--ch-date", default="2026-09-01")
    parser.add_argument("--ch-parts", type=int, default=1)
    parser.add_argument("--gleif-pages", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260912)
    parser.add_argument("--golden-copy", action="store_true", help="Fetch official complete CSV ZIP, then locally filter GB")
    args = parser.parse_args()
    if not 0 <= args.ch_parts <= 7 or not 0 <= args.gleif_pages <= 1200 or not 1 <= args.page_size <= 200:
        parser.error("Bounds: CH 0..7 parts; GLEIF 0..1200 pages; size 1..200")
    if shutil.disk_usage(Path.cwd()).free < 4 * 1024**3:
        raise RuntimeError("At least 4 GiB free disk required")
    with httpx.Client(timeout=httpx.Timeout(90, connect=30), follow_redirects=True, headers={"User-Agent": "EntityBridge-public-research/0.1"}) as client:
        if args.golden_copy:
            metadata_url = "https://leidata-preview.gleif.org/api/v2/golden-copies/publishes?page=1&per_page=1"
            metadata_path = args.raw_dir / "gleif_golden" / "catalog.json"
            download(client, metadata_url, metadata_path, max_bytes=1024**2, terms_url=GLEIF_TERMS)
            publication = json.loads(metadata_path.read_text(encoding="utf-8"))["data"][0]
            info = publication["lei2"]["full_file"]["csv"]
            path = metadata_path.parent / info["url"].rsplit("/", 1)[1]
            manifest = download(client, info["url"], path, max_bytes=700 * 1024**2, terms_url=GLEIF_TERMS, published_at=publication["publish_date"])
            if manifest["bytes"] != info["size"]:
                raise ValueError("Golden Copy catalog size mismatch")
            print(json.dumps({"gleif_golden": path.name, "bytes": manifest["bytes"], "sha256": manifest["sha256"]}), flush=True)
        for part in range(1, args.ch_parts + 1):
            filename = f"BasicCompanyData-{args.ch_date}-part{part}_7.zip"
            manifest = download(client, f"https://download.companieshouse.gov.uk/{filename}", args.raw_dir / "companies_house" / filename, max_bytes=100 * 1024**2, terms_url=CH_TERMS, published_at=args.ch_date)
            print(json.dumps({"file": filename, "bytes": manifest["bytes"], "sha256": manifest["sha256"]}), flush=True)
        if args.gleif_pages:
            params = {"page[size]": args.page_size, "filter[entity.legalAddress.country]": "GB", "sort": "lei", "page[cursor]": "*"}
            prefix = "https://api.gleif.org/api/v1/lei-records?"
            first_path = args.raw_dir / "gleif_cursor" / "page-000001.json"
            download(client, prefix + urlencode(params), first_path, max_bytes=10 * 1024**2, terms_url=GLEIF_TERMS)
            first = json.loads(first_path.read_text(encoding="utf-8"))
            url = prefix + urlencode(params)
            pages = []
            for page in range(1, args.gleif_pages + 1):
                path = args.raw_dir / "gleif_cursor" / f"page-{page:06d}.json"
                download(client, url, path, max_bytes=10 * 1024**2, terms_url=GLEIF_TERMS, published_at=first["meta"]["goldenCopy"]["publishDate"])
                document = json.loads(path.read_text(encoding="utf-8"))
                if document["meta"]["goldenCopy"]["publishDate"] != first["meta"]["goldenCopy"]["publishDate"]:
                    raise RuntimeError("GLEIF golden-copy date changed during collection")
                print(json.dumps({"gleif_page": page, "rows": len(document["data"])}), flush=True)
                pages.append(page)
                url = document.get("links", {}).get("next")
                if not url:
                    break
            selection = {"pages": pages, "page_size": args.page_size, "published_at": first["meta"]["goldenCopy"]["publishDate"], "complete": not bool(url), "sampling": "LEI-sorted cursor prefix for diagnostics only; use Golden Copy for fixed population sampling"}
            (args.raw_dir / "gleif_cursor" / "selection.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
