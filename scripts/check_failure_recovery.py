"""Real process-kill probe at the immutable artifact / database boundary.

Run from the project root. PostgreSQL tests obtain credentials from the local
private configuration and create a disposable schema in entitybridge_test.
The kill hook exists only in the disposable child process, never production code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from entitybridge.store import Store


def _worker():
    settings = json.loads(sys.stdin.readline())
    artifact_root = Path(settings["artifact_root"]).resolve()
    marker = Path(settings["marker"])
    store = Store(settings["database_url"], artifact_root)
    original_replace = Path.replace

    def replace_then_pause(path, target):
        result = original_replace(path, target)
        target = Path(target).resolve()
        if path.suffix == ".partial" and target.parent == artifact_root:
            content = target.read_bytes()
            marker.write_text(json.dumps({"artifact": target.name, "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest()}), encoding="utf-8")
            # The parent must forcibly terminate this process. A timeout fails
            # rather than continuing into database preparation/publication.
            threading.Event().wait(45)
            raise RuntimeError("Parent did not terminate the failure-injection worker")
        return result

    Path.replace = replace_then_pause
    store.prepare_revision([])
    raise RuntimeError("Failure boundary was not reached")


def check_recovery(database_url, workdir: Path) -> dict:
    """Exercise recovery only in an empty, caller-owned isolated database/schema."""
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    marker = workdir / ("boundary-" + uuid4().hex + ".json")
    artifact_root = workdir / "artifacts"
    store = Store(database_url, artifact_root)
    store.initialize()
    if store.current_revision() is not None or store.active_records():
        raise ValueError("Failure probe requires an empty isolated database/schema")
    store.import_records("synthetic_recovery", [{"source_key": "a", "name": "BEFORE UPDATE LIMITED"}])
    old_revision = store.prepare_revision([])["revision_id"]
    store.publish(old_revision, expected_parent=None)
    old_entity_id = store.entities()[0]["entity_id"]
    store.import_records("synthetic_recovery", [{"source_key": "a", "name": "AFTER UPDATE LIMITED"}])
    settings = {"database_url": make_url(database_url).render_as_string(hide_password=False),
                "artifact_root": str(artifact_root), "marker": str(marker)}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", cwd=ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        process.stdin.write(json.dumps(settings) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 20
        while not marker.exists():
            if process.poll() is not None:
                _stdout, stderr = process.communicate()
                # Errors may contain a URL; avoid printing confidential connection settings.
                raise RuntimeError(f"Worker exited before boundary: code={process.returncode}, "
                                   f"diagnostic_bytes={len(stderr)}")
            if time.monotonic() > deadline:
                raise TimeoutError("Worker did not reach the artifact replacement boundary")
            time.sleep(0.02)
        # Marker may be opened before its complete content becomes visible.
        boundary = None
        while boundary is None and time.monotonic() <= deadline:
            try:
                boundary = json.loads(marker.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, PermissionError):
                time.sleep(0.01)
        if boundary is None:
            raise TimeoutError("Worker boundary marker was incomplete")
        if process.poll() is not None:
            raise RuntimeError("Worker exited instead of waiting for forced termination")
        process.kill()
        process.wait(timeout=10)
        terminated = process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        store.engine.dispose()

    recovered = Store(database_url, artifact_root)
    try:
        orphan_path = artifact_root / boundary["artifact"]
        orphan_content = orphan_path.read_bytes()
        orphan = json.loads(orphan_content)
        complete = (len(orphan_content) == boundary["bytes"] and
                    hashlib.sha256(orphan_content).hexdigest() == boundary["sha256"])
        orphan_not_registered = all(row["revision_id"] != orphan["revision_id"] for row in recovered.history())
        old_readable = (recovered.current_revision() == old_revision and
            recovered.entity(old_entity_id)["canonical"]["name"]["value"] == "BEFORE UPDATE LIMITED")
        assert complete and orphan_not_registered and old_readable, "Crash exposed an incomplete revision"
        rerun = recovered.prepare_revision([], verify_full=True)["revision_id"]
        recovered.publish(rerun, expected_parent=old_revision)
        rerun_published = (recovered.current_revision() == rerun and
            recovered.entity(old_entity_id)["canonical"]["name"]["value"] == "AFTER UPDATE LIMITED")
        historical = recovered.entity(old_entity_id, revision=old_revision)
        history_preserved = historical["canonical"]["name"]["value"] == "BEFORE UPDATE LIMITED"
        return {"backend": recovered.engine.dialect.name, "worker_terminated": terminated,
            "failure_boundary": "after_fsync_and_atomic_replace_before_database_registration",
            "complete_orphan_file": complete, "orphan_bytes": len(orphan_content),
            "orphan_not_registered": orphan_not_registered, "old_revision_readable": old_readable,
            "rerun_published": rerun_published, "history_preserved": history_preserved,
            "old_revision": old_revision, "recovered_revision": rerun,
            "orphan_sha256": boundary["sha256"]}
    finally:
        recovered.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--postgres", action="store_true", help="Use local .tools/database.json and an isolated test schema")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "reports" / "failure_recovery.json")
    args = parser.parse_args()
    if args.worker:
        _worker()
        return
    workdir = ROOT / "artifacts" / "recovery" / uuid4().hex
    admin = None
    if args.postgres:
        credentials = json.loads((ROOT / ".tools" / "database.json").read_text(encoding="utf-8"))
        url = URL.create("postgresql+psycopg", username=credentials["user"], password=credentials["password"],
            host=credentials["host"], port=credentials["port"], database="entitybridge_test",
            query={"connect_timeout": "8"})
        schema_name = "recovery_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        url = url.update_query_dict({"options": f"-csearch_path={schema_name}"})
    else:
        workdir.mkdir(parents=True)
        url = URL.create("sqlite", database=str(workdir / "state.db"))
    try:
        report = check_recovery(url, workdir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report))
    finally:
        if admin:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
            admin.dispose()


if __name__ == "__main__":
    main()
