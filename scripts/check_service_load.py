"""Measure authenticated loopback HTTP against a real Uvicorn child process.

Fixed concurrency, a closed request loop and synthetic data are a reproducible
local capacity probe; they do not establish production SLA or internet latency.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx
import psutil
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from entitybridge.store import Store


def _worker():
    import uvicorn

    from entitybridge.api import create_app
    settings = json.loads(sys.stdin.readline())
    store = Store(settings["database_url"], settings["artifact_root"])
    app = create_app(store, tokens={settings["token"]: ["synthetic_load_viewer", "viewer"]}, local_demo=False)
    identity = Path(settings["worker_identity"])
    partial = identity.with_suffix(".partial")
    partial.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    partial.replace(identity)
    try:
        uvicorn.run(app, host="127.0.0.1", port=settings["port"], workers=1,
                    access_log=False, log_level="error")
    finally:
        store.engine.dispose()


def _percentile(values, quantile):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _source_hashes():
    # Only the measured route, its guards/telemetry, persistence and seed path.
    # Unrelated model-training edits do not change this read-only experiment.
    names = ("api.py", "store.py", "schema.py", "database.py", "query_projection.py", "jobs.py",
             "telemetry.py", "identity.py", "resolution.py", "incremental.py")
    paths = [ROOT / "src/entitybridge" / name for name in names]
    paths += [Path(__file__).resolve(), ROOT / "scripts/check_backup_restore.py"]
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def exercise(database_url, workdir, *, records=10000, concurrency=8, duration=15.0, warmup=40):
    from check_backup_restore import seed_store

    if concurrency < 1 or concurrency > 64 or duration < 1 or duration > 300 or warmup < 0:
        raise ValueError("Use concurrency 1..64, duration 1..300 seconds and nonnegative warmup")
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=False)
    store = Store(database_url, workdir / "artifacts")
    store.initialize()
    begin = time.perf_counter()
    try:
        seed = seed_store(store, records, history=False)
        revision = store.current_revision()
    finally:
        store.engine.dispose()
    seed_seconds = time.perf_counter() - begin
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    token = secrets.token_urlsafe(32)
    settings = {"database_url": make_url(database_url).render_as_string(hide_password=False),
                "artifact_root": str(workdir / "artifacts"), "port": port, "token": token,
                "worker_identity": str(workdir / "worker_identity.json")}
    source_hashes = _source_hashes()
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", cwd=ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    process.stdin.write(json.dumps(settings) + "\n")
    process.stdin.flush()
    process.stdin.close()
    base_url = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer " + token}
    entity_count = records // 2
    patterns = [
        ("first_page", {"limit": 100, "offset": 0, "revision": revision}, entity_count, min(100, entity_count)),
        ("deep_page", {"limit": 100, "offset": max(entity_count - 100, 0), "revision": revision},
         entity_count, min(100, entity_count)),
        ("alias_contains", {"limit": 100, "query": "SYNTHETIC FORMER 000001", "revision": revision}, 1, 1),
        ("no_match", {"limit": 100, "query": "ABSENT SYNTHETIC MARKER", "revision": revision}, 0, 0),
    ]
    def request(client, pattern):
        name, params, expected_total, expected_rows = pattern
        started = time.perf_counter()
        try:
            response = client.get("/entities", params=params)
            elapsed = (time.perf_counter() - started) * 1000
            valid = (response.status_code == 200 and response.headers.get("X-Revision-Id") == revision
                     and response.headers.get("X-Total-Count") == str(expected_total)
                     and isinstance(response.json(), list) and len(response.json()) == expected_rows)
            return name, elapsed, None if valid else str(response.status_code) + "_or_response_mismatch"
        except (httpx.HTTPError, ValueError):
            return name, (time.perf_counter() - started) * 1000, "transport_or_decode_error"
    child = None
    try:
        started = time.perf_counter()
        with httpx.Client(base_url=base_url, timeout=5.0, trust_env=False) as probe:
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Uvicorn child exited before readiness; diagnostics suppressed")
                try:
                    if probe.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.perf_counter() - started > 30:
                    raise TimeoutError("Uvicorn readiness timeout")
                time.sleep(0.05)
            startup_seconds = time.perf_counter() - started
            assert probe.get("/entities").status_code == 401, "Load server must require authorization"
        with httpx.Client(base_url=base_url, headers=headers, timeout=10.0, trust_env=False) as client:
            cold = request(client, patterns[0])
            if cold[2]:
                raise RuntimeError("Authenticated first response did not match the seeded revision")
            for index in range(warmup):
                if request(client, patterns[index % len(patterns)])[2]:
                    raise RuntimeError("Warmup response validation failed")
        identity = json.loads((workdir / "worker_identity.json").read_text(encoding="utf-8"))
        child = psutil.Process(identity["pid"])
        if child.pid != process.pid and process.pid not in {parent.pid for parent in child.parents()}:
            raise RuntimeError("Reported HTTP worker is not owned by this load probe")
        before_cpu = child.cpu_times()
        peak_rss = [child.memory_info().rss]
        stop_monitor = threading.Event()
        def monitor():
            while not stop_monitor.wait(0.05):
                try:
                    peak_rss[0] = max(peak_rss[0], child.memory_info().rss)
                except psutil.NoSuchProcess:
                    return
        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        clock = {}
        def start_clock():
            clock["started"] = time.perf_counter()
            clock["deadline"] = clock["started"] + duration
        gate = threading.Barrier(concurrency + 1, action=start_clock, timeout=30)
        def run_client(worker):
            observations = []
            with httpx.Client(base_url=base_url, headers=headers, timeout=10.0, trust_env=False,
                              limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
                gate.wait()
                index = worker
                while time.perf_counter() < clock["deadline"]:
                    observations.append(request(client, patterns[index % len(patterns)]))
                    index += 1
            return observations
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run_client, worker) for worker in range(concurrency)]
            gate.wait()
            observed = [item for future in futures for item in future.result()]
        elapsed = time.perf_counter() - clock["started"]
        stop_monitor.set()
        monitor_thread.join(timeout=2)
        after_cpu = child.cpu_times()
        cpu_seconds = after_cpu.user + after_cpu.system - before_cpu.user - before_cpu.system
        by_route = defaultdict(list)
        for item in observed:
            by_route[item[0]].append(item)
        def describe(items):
            values = [item[1] for item in items]
            errors = Counter(item[2] for item in items if item[2])
            return {"requests": len(items), "errors": sum(errors.values()), "error_codes": dict(errors),
                    "error_rate": sum(errors.values()) / len(items) if items else None,
                    "p50_ms": statistics.median(values) if values else None,
                    "p95_ms": _percentile(values, 0.95), "p99_ms": _percentile(values, 0.99)}
        report = {"format": "entitybridge-service-load-v1", "backend": make_url(database_url).get_backend_name(),
            "synthetic": True, "seed": seed, "seed_seconds": seed_seconds, "uvicorn_startup_seconds": startup_seconds,
            "configuration": {"concurrency": concurrency, "requested_seconds": duration, "elapsed_seconds": elapsed,
                "uvicorn_workers": 1, "warmup_requests": warmup, "request_timeout_seconds": 10,
                "transport": "real_loopback_http_1_1", "authorization": "viewer_bearer_token",
                "pattern_mix": "equal_round_robin_four_entity_queries", "generator": "closed_loop_one_request_per_client",
                "cache_state": "first_application_query_then_warmup; OS cache was warmed by synthetic seeding"},
            "first_authenticated_query_ms": cold[1], "aggregate": describe(observed),
            "routes": {name: describe(items) for name, items in sorted(by_route.items())},
            "completed_requests_per_second": len(observed) / elapsed,
            "server_cpu_seconds": cpu_seconds, "server_peak_sampled_rss_bytes": peak_rss[0],
            "resource_probe": {"identity": "worker_reported_pid_with_descendant_ownership_check",
                               "worker_differs_from_launcher": child.pid != process.pid,
                               "rss_sample_interval_seconds": 0.05},
            "hardware": {"platform": platform.platform(), "logical_cpus": psutil.cpu_count(),
                "memory_bytes": psutil.virtual_memory().total, "python": platform.python_version()},
            "versions": {name: importlib.metadata.version(name) for name in ("uvicorn", "fastapi", "httpx", "sqlalchemy")},
            "source_sha256": source_hashes, "source_unchanged_during_run": source_hashes == _source_hashes(),
            "checks": {"unauthorized_rejected": True, "authorized_seeded_revision_verified": True,
                       "all_measured_responses_valid": not any(item[2] for item in observed)},
            "limitations": ["Client and server share one machine; closed-loop load hides unconstrained queue growth",
                "Warm-cache synthetic reads only; no concurrent writes, matching jobs, TLS, proxy, SSO or WAN",
                "One fixed load point, not maximum capacity, production SLA or a cold-disk benchmark"]}
        if not report["source_unchanged_during_run"]:
            raise RuntimeError("Source changed during the measurement; rerun using a frozen working tree")
        return report
    finally:
        # Windows virtualenv redirectors may have a different PID from Python.
        # Stop the actual owned server first, then any launcher descendants.
        descendants = []
        try:
            descendants = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            pass
        if child and child.is_running() and all(item.pid != child.pid for item in descendants):
            descendants.append(child)
        for descendant in reversed(descendants):
            try:
                descendant.terminate()
            except psutil.NoSuchProcess:
                pass
        _, remaining = psutil.wait_procs(descendants, timeout=5)
        for descendant in remaining:
            descendant.kill()
        psutil.wait_procs(remaining, timeout=5)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main():
    from check_backup_restore import postgres_test_base
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--local-postgres", action="store_true")
    parser.add_argument("--records", type=int, default=10000)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--warmup", type=int, default=40)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        _worker()
        return
    if args.output is None or args.output.exists():
        raise ValueError("Pass a new --output filename")
    workdir = ROOT / "artifacts/service_load" / uuid4().hex
    admin, schema = None, None
    if args.postgres or args.local_postgres:
        base = postgres_test_base(local=args.local_postgres)
        admin = create_engine(base)
        schema = "load_source_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = base.update_query_dict({"options": "-csearch_path=" + schema})
    else:
        url = URL.create("sqlite", database=str(workdir / "source.db"))
    try:
        report = exercise(url, workdir, records=args.records, concurrency=args.concurrency,
                          duration=args.seconds, warmup=args.warmup)
    finally:
        if admin and schema:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
    report["disposable_postgres_schema_cleaned"] = bool(admin)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"backend": report["backend"], "requests": report["aggregate"]["requests"],
                      "p95_ms": report["aggregate"]["p95_ms"], "error_rate": report["aggregate"]["error_rate"]}))


if __name__ == "__main__":
    main()
